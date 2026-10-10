"""电脑端附件管理：收件箱入库、已有文件夹登记、与聊天消息关联（FR-11）。

收件箱：用户把照片、视频等文件复制到“照片视频收件箱”文件夹，程序自动
1) 计算 MD5 / SHA-256；2) 内容相同的只保留一份；3) 按“类型/年-月”归档到媒体库；
4) 复制后核对 SHA-256，确认无误才从收件箱移走（同盘直接改名，不经过删除）。
收件箱和媒体库都在数据目录内，任何文件都不会被删除：重复的文件移到媒体库的“_重复文件”。

登记文件夹：对电脑 QQ 自己的附件目录等只做登记（不复制、不移动），用于关联与预览。
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .models import MEDIA_TYPES, loads_elements
from .store import Archive, now

KIND_DIR = {"image": "图片", "video": "视频", "voice": "语音", "file": "文件"}
_EXT = {
    "image": {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".heic", ".heif", ".tif", ".tiff"},
    "video": {".mp4", ".mov", ".3gp", ".mkv", ".avi", ".webm", ".m4v", ".wmv"},
    "voice": {".amr", ".silk", ".slk", ".mp3", ".m4a", ".aac", ".ogg", ".wav", ".wma"},
}
_IGNORE = {"thumbs.db", "desktop.ini", ".ds_store"}
_HEX32 = re.compile(r"^[0-9a-fA-F]{32}$")
INBOX_NAME = "照片视频收件箱"
LIBRARY_NAME = "媒体库"


def classify(name: str) -> str:
    ext = Path(name).suffix.lower()
    for kind, exts in _EXT.items():
        if ext in exts:
            return kind
    return "file"


def hash_file(path: Path, chunk: int = 1 << 20) -> tuple[str, str]:
    md5, sha = hashlib.md5(), hashlib.sha256()
    with Path(path).open("rb") as f:
        while True:
            b = f.read(chunk)
            if not b:
                break
            md5.update(b)
            sha.update(b)
    return md5.hexdigest(), sha.hexdigest()


def _unique_target(folder: Path, name: str) -> Path:
    target = folder / name
    if not target.exists():
        return target
    stem, suffix = Path(name).stem, Path(name).suffix
    i = 2
    while True:
        t = folder / f"{stem} ({i}){suffix}"
        if not t.exists():
            return t
        i += 1


def _safe_name(name: str) -> str:
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip(" .")
    return name or "未命名"


def _move_verified(src: Path, dst: Path, sha: str) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.replace(src, dst)  # 同一磁盘：改名，文件内容不经过复制
        return
    except OSError:
        pass
    shutil.copy2(src, dst)
    if hash_file(dst)[1] != sha:
        dst.unlink(missing_ok=True)
        raise OSError("复制后校验不一致，原文件保留在收件箱")
    src.unlink()


@dataclass
class InboxResult:
    added: int = 0
    duplicates: int = 0
    failed: int = 0
    bytes: int = 0
    failed_names: list | None = None

    def summary(self) -> str:
        s = f"新收入 {self.added} 个文件，重复 {self.duplicates} 个"
        if self.failed:
            s += f"，{self.failed} 个没能处理（仍在收件箱里）"
        return s


def inbox_files(inbox: Path) -> list[Path]:
    if not inbox.exists():
        return []
    out = []
    for dirpath, _dirs, files in os.walk(inbox):
        for n in files:
            if n.lower() in _IGNORE or n.endswith((".tmp", ".crdownload", ".part")):
                continue
            out.append(Path(dirpath) / n)
    return sorted(out)


def ingest_inbox(archive: Archive, inbox: Path, library: Path, *,
                 progress: Callable[[int, int, str], None] | None = None,
                 cancel: threading.Event | None = None, settle_seconds: float = 3.0) -> InboxResult:
    """处理收件箱。正在复制中的文件（几秒内大小还在变化）留到下次处理。"""
    res = InboxResult(failed_names=[])
    files = inbox_files(inbox)
    total = len(files)
    for i, f in enumerate(files, 1):
        if cancel is not None and cancel.is_set():
            break
        if progress:
            progress(i, total, f.name)
        try:
            st = f.stat()
            if settle_seconds and time.time() - st.st_mtime < settle_seconds:
                continue
            md5, sha = hash_file(f)
            row = archive.con.execute(
                "SELECT id FROM media_files WHERE sha256=? AND status='done' LIMIT 1", (sha,)
            ).fetchone()
            if row:
                _move_verified(f, _unique_target(library / "_重复文件", _safe_name(f.name)), sha)
                res.duplicates += 1
                continue
            kind = classify(f.name)
            month = time.strftime("%Y-%m", time.localtime(st.st_mtime))
            target = _unique_target(library / KIND_DIR[kind] / month, _safe_name(f.name))
            _move_verified(f, target, sha)
            with archive.con:
                archive.con.execute(
                    "INSERT OR IGNORE INTO media_files(origin, device, device_path, local_path, name, kind, size, "
                    "mtime, sha256, md5, status, verified, pulled_at) VALUES('inbox','',?,?,?,?,?,?,?,?,'done',"
                    "'sha256',?)",
                    (f"sha256:{sha}", archive.relative_media_path(target), f.name, kind, st.st_size,
                     int(st.st_mtime), sha, md5, now()),
                )
            res.added += 1
            res.bytes += st.st_size
        except OSError:
            res.failed += 1
            res.failed_names.append(f.name)
    _remove_empty_dirs(inbox)
    return res


def _remove_empty_dirs(root: Path) -> None:
    if not root.exists():
        return
    for dirpath, _dirs, _files in sorted(os.walk(root), key=lambda x: -len(x[0])):
        p = Path(dirpath)
        if p != root:
            try:
                p.rmdir()  # 只删除空文件夹
            except OSError:
                pass


def register_folder(archive: Archive, folder: Path, *, with_hash: bool = False,
                    progress: Callable[[int, int, str], None] | None = None,
                    cancel: threading.Event | None = None) -> int:
    """登记一个已有文件夹里的附件（不复制、不移动）。返回新登记的数量。"""
    folder = Path(folder)
    files = (Path(d) / n for d, _s, ns in os.walk(folder) for n in ns if n.lower() not in _IGNORE)
    added = 0
    total = 0
    if progress:progress(0,0,'正在枚举附件')
    with archive.con:
        for i, f in enumerate(files, 1):
            if cancel is not None and cancel.is_set():
                break
            if progress and i % 200 == 0:
                progress(i, total, f.name)
            try:
                st = f.stat()
            except OSError:
                continue
            md5 = sha = None
            if with_hash:
                md5, sha = hash_file(f)
            elif _HEX32.match(f.stem):
                md5 = f.stem.lower()  # QQ 图片常以内容 MD5 命名
            cur = archive.con.execute(
                "INSERT INTO media_files(origin, device, device_path, local_path, name, kind, size, mtime, sha256, "
                "md5, status, verified, pulled_at) VALUES('local','',?,?,?,?,?,?,?,?,'done',?,?) "
                "ON CONFLICT(origin, device, device_path) DO UPDATE SET size=excluded.size, mtime=excluded.mtime, "
                "sha256=coalesce(excluded.sha256, media_files.sha256), md5=coalesce(excluded.md5, media_files.md5)",
                (str(f), str(f), f.name, classify(f.name), st.st_size, int(st.st_mtime), sha, md5,
                 "sha256" if with_hash else "", now()),
            )
            added += cur.rowcount
    if progress:progress(added,0,'附件登记完成' if cancel is None or not cancel.is_set() else '附件登记已取消')
    return added


def link_media(archive: Archive, conversation_id: int | None = None, *, cancel=None, progress=None) -> dict:
    """把消息里的图片/视频/语音/文件引用与已登记的附件对应起来；找不到的标记为缺失。"""
    import time
    import sqlite3
    linked = missing = 0
    last=0.
    def pulse(force=False):
        nonlocal last
        if cancel is not None and cancel.is_set():return 1
        if progress and (force or time.monotonic()-last>=.25):
            last=time.monotonic();progress(linked+missing,0,'扫描并关联附件引用')
        return 0
    if pulse(True):raise InterruptedError('附件关联已取消；已提交的聊天记录保留')
    by_md5: dict[str, int] = {}
    by_name: dict[str, int] = {}
    for r in archive.con.execute("SELECT id, md5, name FROM media_files WHERE status='done' ORDER BY id"):
        if cancel is not None and cancel.is_set():raise InterruptedError('附件关联已取消')
        if r["md5"]:
            by_md5.setdefault(r["md5"].lower(), r["id"])
        n = r["name"].lower()
        by_name[n] = r["id"] if n not in by_name else None
    access='NOT INDEXED' if conversation_id is None else 'INDEXED BY ix_msg_conv_time'
    sql = (f"SELECT id, elements FROM messages {access} WHERE hidden=0 AND (msg_type IN ('image','file','voice','video') "
           "OR elements LIKE '%\"type\":\"image\"%' OR elements LIKE '%\"type\":\"file\"%' "
           "OR elements LIKE '%\"type\":\"video\"%' OR elements LIKE '%\"type\":\"voice\"%')")
    args: list = []
    if conversation_id is not None:
        sql += " AND conversation_id=?"
        args.append(conversation_id)
    pending=[]
    insert='INSERT OR REPLACE INTO message_media(message_id,elem_index,media_id,status) VALUES(?,?,?,?)'
    archive.con.set_progress_handler(pulse,10000)
    try:
        with archive.con:
            for r in archive.con.execute(sql,args):
                if pulse():raise InterruptedError('附件关联已取消；已提交的聊天记录保留')
                for idx,e in enumerate(loads_elements(r['elements'])):
                    if e.get('type') not in MEDIA_TYPES:continue
                    md5=(e.get('md5') or '').lower();name=(e.get('name') or '').lower()
                    media_id=by_md5.get(md5) if md5 else by_name.get(name)
                    if not md5 and media_id is None and _HEX32.match(Path(name).stem):media_id=by_md5.get(Path(name).stem.lower())
                    status='linked' if media_id else 'missing'
                    pending.append((r['id'],idx,media_id,status))
                    if media_id:linked+=1
                    else:missing+=1
                    if len(pending)>=2000:archive.con.executemany(insert,pending);pending.clear()
            if pulse():raise InterruptedError('附件关联已取消；已提交的聊天记录保留')
            if pending:archive.con.executemany(insert,pending)
    except sqlite3.OperationalError:
        if cancel is not None and cancel.is_set():raise InterruptedError('附件关联已取消；已提交的聊天记录保留') from None
        raise
    finally:archive.con.set_progress_handler(None,0)
    if progress:progress(linked+missing,linked+missing,'附件关联完成')
    return {"linked": linked, "missing": missing}


def missing_media(archive: Archive, conversation_id: int | None = None, limit: int = 2000) -> list[dict]:
    """缺失附件清单：哪条消息里的哪个图片/视频/文件还没有找到对应文件。"""
    sql = ("SELECT m.id AS message_id, m.time, m.sender_uin, m.sender_name, m.sender_card, m.elements, "
           "mm.elem_index, c.id AS conversation_id, c.owner, c.kind, c.peer, c.name AS conv_name "
           "FROM message_media mm JOIN messages m ON m.id=mm.message_id JOIN conversations c ON c.id=m.conversation_id "
           "WHERE mm.status='missing'")
    args: list = []
    if conversation_id is not None:
        sql += " AND m.conversation_id=?"
        args.append(conversation_id)
    sql += " ORDER BY m.time DESC LIMIT ?"
    args.append(limit)
    out = []
    for r in archive.con.execute(sql, args):
        d = dict(r)
        elems = loads_elements(d.pop("elements"))
        e = elems[d["elem_index"]] if d["elem_index"] < len(elems) else {}
        d["type"] = e.get("type", "")
        d["name"] = e.get("name", "")
        d["size"] = e.get("size")
        d["md5"] = e.get("md5", "")
        out.append(d)
    return out


def auto_register_qq_media(archive: Archive, media_dirs: list[str], **kw) -> int:
    added = 0
    for d in media_dirs:
        if Path(d).is_dir():
            added += register_folder(archive, Path(d), **kw)
    return added


def media_summary(archive: Archive) -> dict:
    rows = archive.con.execute(
        "SELECT kind, count(*) AS n, coalesce(sum(size),0) AS bytes FROM media_files WHERE status='done' GROUP BY kind"
    )
    return {r["kind"]: {"count": r["n"], "bytes": r["bytes"]} for r in rows}


def human_size(n: int | None) -> str:
    if not n:
        return "0 B"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"
