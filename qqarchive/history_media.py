"""Separate resumable attachment queue. Existing files are referenced in place."""
from __future__ import annotations
import hashlib
import ipaddress
import os
from pathlib import Path
import re
import socket
import time
import uuid
from urllib.parse import urlsplit, unquote
from urllib.request import Request, build_opener, ProxyHandler
from urllib.error import HTTPError
from .forwards import NoRedirect

MAX_FILE = 2 * 1024 ** 3
DOMAINS = ('qq.com', 'qpic.cn', 'gtimg.cn', 'gtimg.com')


def local_file(value):
    value = str(value or '')
    # Explicit paths only. Never resolve names/relative paths against our cwd,
    # or connect to UNC shares embedded in received data.
    if value.startswith(('//', '\\\\')) or not re.match(r'^[A-Za-z]:[\\/]', value):
        return None
    p = Path(value)
    return p if p.is_file() else None


def check_url(value):
    p = urlsplit(value)
    host = (p.hostname or '').lower()
    if (p.scheme != 'https' or p.username or p.password or p.port not in (None, 443)
            or not any(host == d or host.endswith('.' + d) for d in DOMAINS)):
        raise ValueError('附件链接不是支持的 QQ HTTPS 地址')
    addresses = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    if not addresses or any(not ipaddress.ip_address(a[4][0]).is_global for a in addresses):
        raise ValueError('附件链接解析到非公网地址')
    return value


def hashes(path, cancel):
    sha, md5 = hashlib.sha256(), hashlib.md5()
    with path.open('rb') as f:
        while block := f.read(256 * 1024):
            if cancel.is_set():
                raise InterruptedError()
            sha.update(block); md5.update(block)
    return sha.hexdigest(), md5.hexdigest()


def download(url, target, cancel, progress, expected_md5=''):
    if cancel.is_set():
        raise InterruptedError()
    opener = build_opener(ProxyHandler({}), NoRedirect())
    url = check_url(url)
    response = None
    for _ in range(4):
        try:
            response = opener.open(Request(url, headers={'User-Agent': 'QQArchive/19'}), timeout=10)
            break
        except HTTPError as exc:
            if exc.code not in (301, 302, 303, 307, 308):
                raise ValueError('附件下载失败，链接可能已过期') from exc
            url = check_url(exc.headers.get('Location', ''))
    if response is None:
        raise ValueError('附件重定向次数过多')
    part = target.with_name(target.name + '.' + uuid.uuid4().hex + '.part')
    created = False
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        with response, part.open('xb') as f:
            created = True
            declared = int(response.headers.get('Content-Length', '0'))
            if declared > MAX_FILE or declared < 0:
                raise ValueError('附件超过单文件 2 GB 限制')
            size = 0; deadline = time.monotonic() + 600
            while True:
                if cancel.is_set():
                    raise InterruptedError()
                if time.monotonic() > deadline:
                    raise ValueError('附件下载超时，可稍后重试')
                block = response.read1(256 * 1024)
                if not block:
                    break
                size += len(block)
                if size > MAX_FILE:
                    raise ValueError('附件超过单文件 2 GB 限制')
                f.write(block); progress(size, declared)
            if not size or (declared and size != declared):
                raise ValueError('附件长度校验失败')
            f.flush(); os.fsync(f.fileno())
        sha, md5 = hashes(part, cancel)
        if expected_md5 and md5.lower() != expected_md5.lower():
            raise ValueError('附件摘要校验失败')
        # Never overwrite a previously saved result, even after interrupted DB commit.
        if target.exists():
            raise ValueError('目标文件已存在，请核对后重试')
        part.rename(target)
        return sha, md5
    finally:
        if response:
            response.close()
        # Only our exact generated partial file, no user-selected paths.
        if created and part.is_file():
            part.unlink()


def process(archive, key, kinds, folder, cancel, status):
    result = {'linked': 0, 'downloaded': 0, 'missing': 0, 'failed': 0}
    def pending():
        last = (-1, -1)
        while True:
            chunk = archive.con.execute("""SELECT * FROM history_media WHERE job_key=? AND state!='linked'
                AND (message_id,elem_index)>(?,?) ORDER BY message_id,elem_index LIMIT 100""",
                (key, *last)).fetchall()
            if not chunk:
                return
            for row in chunk:
                yield row
            last = (chunk[-1]['message_id'], chunk[-1]['elem_index'])
    root = Path(folder).resolve() if folder else archive.data_dir / '历史补全附件'
    for item in pending():
        if cancel.is_set():
            raise InterruptedError()
        entry = dict(item)
        args = (key, entry['message_id'], entry['elem_index'])
        already = archive.con.execute("""SELECT f.local_path FROM message_media m JOIN media_files f ON f.id=m.media_id
                        WHERE m.message_id=? AND m.elem_index=? AND m.status='linked'""", args[1:]).fetchone()
        if already and archive.resolve_media_path(already[0]).is_file():
            with archive.con:
                archive.con.execute("UPDATE history_media SET state='linked',note='' WHERE job_key=? AND message_id=? AND elem_index=?", args)
            continue
        path = local_file(entry['local_path']) or local_file(entry['url']) or local_file(entry['name'])
        was_download = False
        try:
            sha = md5 = ''
            if path:
                if path.stat().st_size > MAX_FILE:
                    raise ValueError('附件超过单文件 2 GB 校验限制')
                sha, md5 = hashes(path, cancel)
            elif entry['kind'] in kinds and entry['url']:
                suffix = Path(entry['name']).suffix.lower() or Path(unquote(urlsplit(entry['url']).path)).suffix.lower()
                if suffix not in ('.jpg','.jpeg','.png','.gif','.webp','.mp4','.mov','.mkv','.mp3','.amr','.silk','.ogg','.pdf','.zip','.txt'):
                    suffix = '.bin'  # retain original bytes; don't claim an unknown encoding
                # Deterministic identity + job scope; user-controlled filenames cannot escape root.
                path = root / key[:16] / f"{entry['message_id']}-{entry['elem_index']}{suffix}"
                if path.exists():
                    # Crash recovery: only reuse if a saved checksum proves this exact file.
                    previous = entry['note']
                    if not previous.startswith('sha256:') or hashes(path, cancel)[0] != previous[7:]:
                        raise ValueError('目标已存在但尚未认证，请选择新的附件目录')
                    sha, md5 = hashes(path, cancel)
                else:
                    def pulse(done, total):
                        status(f"补齐附件 · 已下载 {done / 1024 / 1024:.1f} MB", 0, 0)
                    sha, md5 = download(entry['url'], path, cancel, pulse, entry['md5'])
                    with archive.con:
                        archive.con.execute("UPDATE history_media SET note=? WHERE job_key=? AND message_id=? AND elem_index=?",
                                            ('sha256:' + sha, *args))
                    was_download = True
            else:
                result['missing'] += 1
                continue
            if entry['md5'] and md5.lower() != entry['md5'].lower():
                raise ValueError('附件摘要与消息不一致，未建立关联')
            with archive.con:
                archive.con.execute("""INSERT INTO media_files
                 (origin,device,device_path,local_path,name,kind,size,mtime,sha256,md5,status,verified,pulled_at)
                 VALUES('qq-history','',?,?,?,?,?,?,?,?,'done','sha256',?)
                 ON CONFLICT(origin,device,device_path) DO UPDATE SET local_path=excluded.local_path,
                 size=excluded.size,sha256=excluded.sha256,md5=excluded.md5,status='done',verified='sha256'""",
                 (str(path.resolve()), str(path.resolve()), path.name, entry['kind'], path.stat().st_size,
                  int(path.stat().st_mtime), sha, md5, int(time.time())))
                mid = archive.con.execute("SELECT id FROM media_files WHERE origin='qq-history' AND device='' AND device_path=?", (str(path.resolve()),)).fetchone()[0]
                archive.con.execute("INSERT OR REPLACE INTO message_media VALUES(?,?,?,'linked')", (*args[1:], mid))
                archive.con.execute("UPDATE history_media SET state='linked',note='' WHERE job_key=? AND message_id=? AND elem_index=?", args)
            result['downloaded' if was_download else 'linked'] += 1
        except InterruptedError:
            raise
        except (OSError, ValueError):
            result['failed'] += 1
            with archive.con:
                archive.con.execute("UPDATE history_media SET state='failed',note='附件读取或校验未通过，可重试' WHERE job_key=? AND message_id=? AND elem_index=?", args)
    return result
