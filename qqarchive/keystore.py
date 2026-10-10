"""用户手动填写的数据库密钥：校验与保存。

- 默认只保存在内存（本次运行有效）；
- 用户勾选“记住”后写入数据目录 keys\\keys.json，Windows 上用 DPAPI 按当前用户加密；
- 密钥不写日志，不进入报告与分发包。
"""

from __future__ import annotations

import base64
import hashlib
import ctypes
import json
import sys
import time
from ctypes import wintypes
from pathlib import Path

from .sqlcipher import CipherParams, Key, verify_key


class _Blob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]


def _dpapi(data: bytes, protect: bool) -> bytes:
    if sys.platform != "win32":
        raise OSError("仅 Windows 支持 DPAPI")
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    buf = ctypes.create_string_buffer(data, len(data))
    src = _Blob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    dst = _Blob()
    fn = crypt32.CryptProtectData if protect else crypt32.CryptUnprotectData
    ok = fn(ctypes.byref(src), None, None, None, None, 0x1, ctypes.byref(dst))  # CRYPTPROTECT_UI_FORBIDDEN
    if not ok:
        raise OSError("DPAPI 处理失败")
    try:
        return ctypes.string_at(dst.pbData, dst.cbData)
    finally:
        kernel32.LocalFree(dst.pbData)


class KeyStore:
    def __init__(self, keys_dir: Path):
        self.file = Path(keys_dir) / "keys.json"
        self._session: dict[str, tuple[Key, CipherParams | None]] = {}

    def _load(self) -> dict:
        if not self.file.exists():
            return {}
        try:
            return json.loads(self.file.read_text(encoding="utf-8"))
        except ValueError:
            return {}

    def _write(self, data: dict) -> None:
        self.file.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.file.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(self.file)

    def put(self, account: str, key: Key, params: CipherParams | None, remember: bool) -> None:
        self._session[account] = (key, params)
        if not remember:
            return
        blob = _dpapi(key.secret, True)
        data = self._load()
        data[account] = {
            "raw": key.raw,
            "secret": base64.b64encode(blob).decode("ascii"),
            "params": params.__dict__ if params else None,
            "saved_at": int(time.time()),
        }
        self._write(data)

    def get(self, account: str) -> tuple[Key, CipherParams | None] | None:
        if account in self._session:
            return self._session[account]
        entry = self._load().get(account)
        if not entry:
            return None
        try:
            secret = _dpapi(base64.b64decode(entry["secret"]), False)
        except (OSError, ValueError, KeyError):
            return None
        params = CipherParams(**entry["params"]) if entry.get("params") else None
        item = (Key(secret, raw=bool(entry.get("raw"))), params)
        self._session[account] = item
        return item

    def saved_accounts(self) -> list[dict]:
        return [{"id": k, "account": v.get('account',k), "source_path":v.get('source_path'), "saved_at": v.get("saved_at")} for k, v in self._load().items()]

    @staticmethod
    def source_id(path):
        return 'source:'+hashlib.sha256(str(Path(path).resolve()).casefold().encode()).hexdigest()

    def put_source(self, account, path, key, params, remember=False):
        sid=self.source_id(path)
        self.put(sid,key,params,remember)
        self._session[account]=(key,params)
        if remember:
            data=self._load();data[sid].update(account=account,source_path=str(Path(path).resolve()));self._write(data)

    def has(self, account: str) -> str:
        if account in self._session:
            return "session"
        saved=self._load()
        return "saved" if account in saved or any(v.get('account')==account for v in saved.values()) else "none"

    def forget(self, account: str) -> None:
        self._session.pop(account, None)
        data = self._load()
        if data.pop(account, None) is not None:
            self._write(data)

    def candidates(self, account: str, path=None) -> list[tuple[Key, CipherParams | None]]:
        """先试本账号的密钥，再试其他已知密钥（同一台电脑多账号时方便）。"""
        out = []
        if path:
            exact=self.get(self.source_id(path))
            if exact:out.append(exact)
        own = self.get(account)
        if own and own not in out:
            out.append(own)
        for acc in set(self._session) | set(self._load()):
            if acc != account:
                item = self.get(acc)
                if item and item not in out:
                    out.append(item)
        return out


def check_key(db_path, text: str) -> tuple[Key, CipherParams] | None:
    """校验用户填写的密钥：用数据库第 1 页 HMAC 判定，返回可用的参数组合。"""
    key = Key.parse(text)
    params = verify_key(db_path, key)
    return (key, params) if params else None


def check_raw_key(db_path, key_hex: str) -> tuple[Key, CipherParams] | None:
    """校验一个 32 字节十六进制原始密钥（自动取钥得到的就是这种）。

    仍然走 ``sqlcipher`` 的第 1 页 HMAC 校验，不信任任何来源。
    """
    try:
        raw = bytes.fromhex(key_hex.strip())
    except ValueError:
        return None
    if len(raw) != 32:
        return None
    key = Key(raw, raw=True)
    params = verify_key(db_path, key)
    return (key, params) if params else None


def store_auto_key(keystore: "KeyStore", account: str, key_hex: str, db_path,
                   remember: bool = False) -> tuple[Key, CipherParams] | None:
    """把自动取到的密钥校验后存入密钥库；校验不过就不存（不会存下错的密钥）。

    返回 (Key, CipherParams)；``None`` 表示这个密钥打不开该库。
    """
    checked = check_raw_key(db_path, key_hex)
    if not checked:
        return None
    key, params = checked
    if account:
        keystore.put(account, key, params, remember=remember)
    return (key, params)
