"""把 EncryptedDatabase 挂成 SQLite 只读虚拟文件系统（apsw VFS）。

SQLite 读到哪一页就解密哪一页，明文只在内存缓存中，不写临时明文库。
"""

from __future__ import annotations

import itertools
import threading
from pathlib import Path
from urllib.parse import quote

import apsw

from .sqlcipher import CipherParams, EncryptedDatabase, Key, try_open

_VFS_NAME = "qqarchive-cipher"
_registry: dict[str, EncryptedDatabase] = {}
_registry_lock = threading.Lock()
_counter = itertools.count(1)
_vfs = None


class _CipherFile:
    def __init__(self, db: EncryptedDatabase):
        self.db = db

    def xRead(self, amount, offset):
        data = self.db.read(offset, amount)
        # APSW handles short reads itself, including SQLITE_IOERR_SHORT_READ.
        # Fabricated bytes can conceal truncated sources as valid zero pages.
        return data

    def xFileSize(self):
        return self.db.size

    def xWrite(self, data, offset):
        raise apsw.ReadOnlyError("来源数据库只读")

    def xTruncate(self, size):
        raise apsw.ReadOnlyError("来源数据库只读")

    def xSync(self, flags):
        return None

    def xLock(self, level):
        return None

    def xUnlock(self, level):
        return None

    def xCheckReservedLock(self):
        return False

    def xFileControl(self, op, ptr):
        return False

    def xSectorSize(self):
        return 4096

    def xDeviceCharacteristics(self):
        return 0

    def xClose(self):
        return None


class _CipherVFS(apsw.VFS):
    def __init__(self):
        super().__init__(_VFS_NAME, base="")

    def xOpen(self, name, flags):
        fname = name.filename() if isinstance(name, apsw.URIFilename) else name
        if fname:
            token = Path(fname).name
            with _registry_lock:
                db = _registry.get(token)
            if db is not None:
                return _CipherFile(db)
        # 排序等临时文件交给默认 VFS
        return super().xOpen(name, flags)


def _ensure_vfs():
    global _vfs
    if _vfs is None:
        _vfs = _CipherVFS()
    return _vfs


class CipherConnection:
    """只读打开加密库，提供 apsw 连接。用 with 语句或 close() 释放。"""

    def __init__(self, db: EncryptedDatabase):
        _ensure_vfs()
        self.db = db
        self.token = f"qqcipher-{next(_counter)}.db"
        with _registry_lock:
            _registry[self.token] = db
        uri = f"file:{self.token}?immutable=1&mode=ro"
        try:
            self.conn = apsw.Connection(
                uri, flags=apsw.SQLITE_OPEN_READONLY | apsw.SQLITE_OPEN_URI, vfs=_VFS_NAME)
            self.conn.execute("PRAGMA temp_store=MEMORY")
        except BaseException:
            with _registry_lock:
                _registry.pop(self.token, None)
            db.close()
            raise

    @classmethod
    def open(cls, path, key: Key | str, params: CipherParams | None = None) -> "CipherConnection":
        if isinstance(key, str):
            key = Key.parse(key)
        db = EncryptedDatabase(path, key, params) if params else try_open(path, key)
        return cls(db)

    @property
    def params(self) -> CipherParams:
        return self.db.params

    def close(self) -> None:
        try:
            self.conn.close()
        finally:
            with _registry_lock:
                _registry.pop(self.token, None)
            self.db.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


class PlainConnection:
    """未加密的 SQLite 库（例如已由其他工具解密的 nt_msg.db），同样只读打开。"""

    def __init__(self, path):
        p = Path(path).resolve()
        if Path(str(p) + "-wal").exists() and Path(str(p) + "-wal").stat().st_size:
            raise ValueError("未加密数据库含 WAL，请先使用 SQLite 一致快照；避免漏掉未合并消息")
        uri = "file:" + quote(p.as_posix(), safe="/:") + "?mode=ro&immutable=1"
        self.conn = apsw.Connection(uri, flags=apsw.SQLITE_OPEN_READONLY | apsw.SQLITE_OPEN_URI)
        self.params = None

    def close(self):
        self.conn.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
