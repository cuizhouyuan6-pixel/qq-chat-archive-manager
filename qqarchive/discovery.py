"""来源发现（FR-05）：扫描电脑 QQ 数据目录、用户添加的备份目录，列出可导入来源。"""

from __future__ import annotations

import ctypes
import configparser
import hashlib
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

from .ntqq import owner_from_path
from .sqlcipher import SQLITE_MAGIC, detect_header

MAX_SCAN_FILES = 20000
MAX_DEPTH = 8


def documents_dir() -> Path:
    if sys.platform == "win32":
        buf = ctypes.create_unicode_buffer(260)
        if ctypes.windll.shell32.SHGetFolderPathW(None, 5, None, 0, buf) == 0 and buf.value:
            return Path(buf.value)
    return Path.home() / "Documents"


def configured_qq_roots(ini_paths: list[Path]) -> list[Path]:
    """QQ 自身指定的数据目录；旧版 Windows QQ 配置可能使用 CP936。"""
    roots = []
    for ini in ini_paths:
        try:
            if ini.stat().st_size > 256 * 1024:
                continue
            raw = ini.read_bytes()
            encodings = ('utf-16',) if raw.startswith((b'\xff\xfe', b'\xfe\xff')) else ('utf-8-sig', 'gb18030')
            for encoding in encodings:
                try:
                    text = raw.decode(encoding)
                    break
                except UnicodeError:
                    continue
            else:
                continue
            parser = configparser.ConfigParser(interpolation=None)
            parser.read_string(text)
            value = parser.get('UserDataSet', 'UserDataSavePath', fallback='').strip().strip('"')
            path = Path(os.path.expandvars(value))
            if value and path.is_absolute() and path.is_dir() and path not in roots:
                roots.append(path)
        except (OSError, configparser.Error):
            continue
    return roots


def default_qq_roots() -> list[Path]:
    ini_paths = []
    public = os.environ.get('PUBLIC')
    roaming = os.environ.get('APPDATA')
    if public:
        ini_paths.append(Path(public) / 'Documents' / 'Tencent' / 'QQ' / 'UserDataInfo.ini')
    if roaming:
        ini_paths.append(Path(roaming) / 'Tencent' / 'QQ' / 'UserDataInfo.ini')
    roots = configured_qq_roots(ini_paths)
    default = documents_dir() / 'Tencent Files'
    if default not in roots:
        roots.append(default)
    return roots


@dataclass
class SourceCandidate:
    path: str
    kind: str  # pc / backup / plain / package
    owner: str
    size: int
    mtime: int
    header: str  # ntqq / encrypted / sqlite / package
    rand: str | None = None
    fingerprint: str = ""
    status: str = "new"  # new / imported / changed
    key_status: str = "none"  # none / session / saved / plain
    note: str = ""
    extra: dict = field(default_factory=dict)

    @property
    def label(self) -> str:
        return self.path

    @property
    def needs_key(self) -> bool:
        return self.header in ("ntqq", "encrypted", "official-backup")


def source_fingerprint(path: Path) -> str:
    """路径 + 大小 + 修改时间（含 -wal），用于判断“已导入 / 有更新”。"""
    path = Path(path)
    parts = [str(path.resolve()).lower()]
    for p in (path, Path(str(path) + "-wal")):
        if p.exists():
            st = p.stat()
            parts.append(f"{st.st_size}:{st.st_mtime_ns}")
    return hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()


def _candidate(path: Path, kind: str, owner: str = "") -> SourceCandidate | None:
    try:
        st = path.stat()
        hdr = detect_header(path)
    except OSError:
        return None
    header = hdr["kind"]
    if header == "unknown":
        return None
    from .official_backup import inspect_header,LIMITATION
    info=inspect_header(path) if header=='ntqq' else None
    if info:
        return SourceCandidate(str(path),'official-backup',owner,st.st_size,int(st.st_mtime),
                               'official-backup',fingerprint=source_fingerprint(path),
                               key_status='unsupported',note=LIMITATION,extra=info)
    if header == "sqlite" and kind != "package":
        kind = "plain"
    return SourceCandidate(
        path=str(path), kind=kind, owner=owner or owner_from_path(path), size=st.st_size,
        mtime=int(st.st_mtime), header=header, rand=hdr.get("rand"), fingerprint=source_fingerprint(path),
    )


def scan_qq_root(root: Path) -> list[SourceCandidate]:
    out = []
    root = Path(root)
    if not root.is_dir():
        return out
    dirs = [root] + [d for d in root.iterdir() if d.is_dir()]
    for d in dirs:
        db = d / "nt_qq" / "nt_db" / "nt_msg.db"
        if db.is_file():
            c = _candidate(db, "pc", d.name if d.name.isdigit() else "")
            if c:
                media = qq_media_dirs(d)
                if media:
                    c.extra["media_dirs"] = [str(m) for m in media]
                out.append(c)
    return out


def qq_media_dirs(account_dir: Path) -> list[Path]:
    """电脑 QQ 账号目录下存放图片、视频、语音、文件的位置（存在的才返回）。

    手机 QQ 用官方“迁移聊天记录到电脑”后，附件也会落在这里。
    """
    out = []
    nt_data = Path(account_dir) / "nt_qq" / "nt_data"
    for sub in ("Pic", "Video", "Ptt", "File", "Emoji"):
        p = nt_data / sub
        if p.is_dir():
            out.append(p)
    recv = Path(account_dir) / "FileRecv"
    if recv.is_dir():
        out.append(recv)
    return out


def _looks_like_plain_ntqq(path: Path) -> bool:
    import sqlite3
    try:
        con = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
        try:
            names = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        finally:
            con.close()
    except sqlite3.DatabaseError:
        return False
    return bool(names & {"group_msg_table", "c2c_msg_table"})


def scan_backup_dir(root: Path) -> list[SourceCandidate]:
    """在用户添加的目录中查找 QQ NT 消息库（含已解密的 SQLite）与本程序归档包。"""
    out: list[SourceCandidate] = []
    root = Path(root)
    if not root.exists():
        return out
    if root.is_file():
        files = [root]
    else:
        files = []
        base_depth = len(root.parts)
        for dirpath, dirnames, filenames in os.walk(root):
            if len(Path(dirpath).parts) - base_depth >= MAX_DEPTH:
                dirnames[:] = []
            for name in filenames:
                files.append(Path(dirpath) / name)
                if len(files) >= MAX_SCAN_FILES:
                    break
            if len(files) >= MAX_SCAN_FILES:
                break
    for f in files:
        name = f.name.lower()
        if name.endswith(("-wal", "-shm", "-journal")):
            continue
        if name == "manifest.json" and (f.parent / "messages.jsonl").exists():
            c = SourceCandidate(str(f.parent), "package", "", 0, int(f.stat().st_mtime), "package",
                                fingerprint=source_fingerprint(f))
            out.append(c)
            continue
        if name.endswith(".qqarchive.zip"):
            st = f.stat()
            out.append(SourceCandidate(str(f), "package", "", st.st_size, int(st.st_mtime), "package",
                                       fingerprint=source_fingerprint(f)))
            continue
        try:
            if f.stat().st_size < 4096:
                continue
            with f.open("rb") as fh:
                head = fh.read(1100)
        except OSError:
            continue
        is_named = name.startswith("nt_msg") or "msg" in name
        if head.startswith(SQLITE_MAGIC):
            if (is_named or name.endswith(".db")) and _looks_like_plain_ntqq(f):
                c = _candidate(f, "plain")
                if c:
                    out.append(c)
        elif head.startswith(b'SQLite header 3\0') or b"QQ_NT DB" in head[:1024] or (is_named and name.endswith(".db")):
            c = _candidate(f, "backup")
            if c:
                out.append(c)
    return out


def discover(qq_roots, backup_dirs, archive=None, keystore=None) -> list[SourceCandidate]:
    found: dict[str, SourceCandidate] = {}
    for r in qq_roots:
        for c in scan_qq_root(Path(r)):
            found[str(Path(c.path).resolve()).lower()] = c
    for d in backup_dirs:
        for c in scan_backup_dir(Path(d)):
            found.setdefault(str(Path(c.path).resolve()).lower(), c)
    items = list(found.values())
    if archive is not None:
        fps = archive.imported_fingerprints()
        labels = archive.imported_labels()
        for c in items:
            if c.fingerprint in fps:
                c.status = "imported"
            elif c.label in labels:
                c.status = "changed"
    for c in items:
        if c.kind=='official-backup':continue
        if not c.needs_key:
            c.key_status = "plain"
        elif keystore is not None and c.owner:
            c.key_status = keystore.has(c.owner)
    items.sort(key=lambda c: (c.kind != "pc", c.owner, c.path))
    return items
