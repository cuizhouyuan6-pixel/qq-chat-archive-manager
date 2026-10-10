"""程序配置与数据目录（FR-13）。

配置文件位于 %APPDATA%\\QQArchive\\config.json（可用环境变量 QQARCHIVE_CONFIG_DIR 覆盖），
只保存路径与选项，不保存密钥。修改数据目录后在下次启动时生效。
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable


def config_dir() -> Path:
    env = os.environ.get("QQARCHIVE_CONFIG_DIR")
    if env:
        return Path(env)
    base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
    return Path(base) / "QQArchive"


def default_data_dir() -> Path:
    env = os.environ.get("QQARCHIVE_CONFIG_DIR")
    if env:
        return Path(env) / "data"
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    return Path(base) / "QQArchive" / "data"


@dataclass
class Config:
    data_dir: str = ""
    pending_data_dir: str = ""
    adb_path: str = ""
    key_tool_command: str = ""
    key_tool_timeout: int = 180
    qq_roots: list[str] = field(default_factory=list)
    backup_dirs: list[str] = field(default_factory=list)
    remember_keys: bool = False
    key_check_minutes: int = 15
    import_mode: str = 'guided'
    media_dirs: list[str] = field(default_factory=list)
    save_folder: str = ''
    save_folder_history: list[str] = field(default_factory=list)
    # 自动取钥（FR-03）：只读 QQ 进程内存，用数据库第 1 页 HMAC 校验后才采用
    auto_key_enabled: bool = True
    auto_key_on_start: bool = False  # 启动时自动尝试（默认关，由用户主动触发）
    auto_key_process_names: list[str] = field(default_factory=lambda: ["QQ.exe"])
    auto_key_window_mb: int = 8  # 一次读取的内存窗口大小
    auto_key_max_pids: int = 3  # 最多扫描几个 QQ 进程
    # USB 转移手机附件（FR-10）
    conversation_order: list[int] = field(default_factory=list)
    pinned_conversations: list[int] = field(default_factory=list)
    conversation_groups: dict[str,list[int]] = field(default_factory=dict)
    conversation_sort: str = 'manual'
    chat_font_size: int = 14
    phone_device: str = ""  # 记住上次选择的手机序列号
    phone_kinds: list[str] = field(default_factory=lambda: ["image", "video", "voice", "file"])
    phone_days: int = 0  # 只转移最近 N 天（0 = 不限）
    phone_verify: bool = True
    phone_auto_on_connect: bool = False
    phone_include_cache: bool = False
    # Detect QQ's candidate LAN migration connections; prompt before recording.
    transfer_detection: bool = True

    @property
    def data_path(self) -> Path:
        return Path(self.data_dir) if self.data_dir else default_data_dir()


def config_file() -> Path:
    return config_dir() / "config.json"


def load_config(apply_pending: bool = True) -> Config:
    path = config_file()
    cfg = Config()
    if path.exists():
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            known = Config.__dataclass_fields__.keys()
            cfg = Config(**{k: v for k, v in raw.items() if k in known})
        except (ValueError, TypeError):
            # 配置损坏时保留原文件以便排查，使用默认配置启动
            path.replace(path.with_suffix(".broken.json"))
    if apply_pending and cfg.pending_data_dir:
        cfg.data_dir = cfg.pending_data_dir
        cfg.pending_data_dir = ""
        save_config(cfg)
    if not cfg.data_dir:
        cfg.data_dir = str(default_data_dir())
    return cfg


def save_config(cfg: Config) -> None:
    path = config_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(asdict(cfg), ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def ensure_layout(data_dir: Path) -> None:
    for sub in ("media", "keys", "logs", "backups", "exports"):
        (data_dir / sub).mkdir(parents=True, exist_ok=True)


def sha256_file(path: Path, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with Path(path).open("rb") as f:
        while True:
            b = f.read(chunk)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


class MigrationError(Exception):
    pass


def migrate_data_dir(old: Path, new: Path, progress: Callable[[int, int, str], None] | None = None) -> int:
    """把整个数据目录复制到新位置，逐个文件 SHA-256 核对。不删除旧目录。

    调用前必须关闭所有归档库连接。返回复制的文件数。
    """
    old, new = Path(old).resolve(), Path(new).resolve()
    if old == new:
        raise MigrationError("新旧数据目录相同")
    if str(new).startswith(str(old) + os.sep):
        raise MigrationError("新目录不能位于旧数据目录内部")
    if new.exists() and any(new.iterdir()):
        raise MigrationError("新目录不是空目录，为避免覆盖已有文件，请选择空目录")
    db = old / "archive.db"
    if db.exists():
        con = sqlite3.connect(db)
        try:
            con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        finally:
            con.close()
    files = [p for p in old.rglob("*") if p.is_file() and not p.name.endswith(("-wal", "-shm"))]
    total = len(files)
    new.mkdir(parents=True, exist_ok=True)
    for i, src in enumerate(files, 1):
        rel = src.relative_to(old)
        dst = new / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        if sha256_file(src) != sha256_file(dst):
            raise MigrationError(f"校验失败：{rel}")
        if progress:
            progress(i, total, str(rel))
    ensure_layout(new)
    return total
