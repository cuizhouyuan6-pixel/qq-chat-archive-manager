"""SQLCipher 4 格式的只读逐页解密。

只读取来源文件，不写回、不生成明文整库。每页先做 HMAC 认证再 AES-256-CBC 解密，
认证失败直接报错（不把损坏或错误密钥的数据当作正常内容）。

格式要点（SQLCipher 4，默认 plaintext_header_size = 0）：
- 第 1 页前 16 字节是 KDF 盐，其余部分与其他页一样加密；
- 每页末尾保留区 = IV(16) + HMAC，向上取整到 16 字节；
- HMAC 覆盖 “密文 + IV + 页号(小端 4 字节)”；
- 加密密钥 = PBKDF2(口令, 盐, kdf_iter)；HMAC 密钥 = PBKDF2(加密密钥, 盐^0x3a, 2)。

QQ NT 的数据库文件在 SQLCipher 数据前还有 1024 字节自定义文件头，这里统一用
``prefix`` 表示，自动探测。
"""

from __future__ import annotations

import hashlib
import hmac
import os
import struct
import threading
from collections import OrderedDict
from dataclasses import dataclass, replace
from pathlib import Path

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

SQLITE_MAGIC = b"SQLite format 3\x00"
WAL_MAGIC_LE = 0x377F0682
WAL_MAGIC_BE = 0x377F0683
_HMAC_SIZES = {"sha1": 20, "sha256": 32, "sha512": 64}


class CipherError(Exception):
    """密钥错误、参数不匹配或数据损坏。"""


class PageAuthError(CipherError):
    """某页 HMAC 校验失败。"""


@dataclass(frozen=True)
class CipherParams:
    name: str = "ntqq"
    page_size: int = 4096
    kdf_iter: int = 4000
    hmac_algo: str = "sha1"
    kdf_algo: str = "sha512"
    prefix: int = 0  # 来源文件中 SQLCipher 数据之前的字节数（QQ NT 为 1024）

    @property
    def hmac_size(self) -> int:
        return _HMAC_SIZES[self.hmac_algo]

    @property
    def reserve(self) -> int:
        r = 16 + self.hmac_size
        return (r + 15) // 16 * 16

    def describe(self) -> str:
        return (
            f"{self.name}: page={self.page_size} kdf_iter={self.kdf_iter} "
            f"hmac={self.hmac_algo} kdf={self.kdf_algo} header={self.prefix}"
        )


# 依次尝试的参数预设。QQ NT（Windows / Android）实测参数排第一。
PRESETS: tuple[CipherParams, ...] = (
    CipherParams("ntqq"),
    CipherParams("ntqq-sha256", hmac_algo="sha256"),
    CipherParams("ntqq-sha512", hmac_algo="sha512"),
    CipherParams("sqlcipher4", kdf_iter=256000, hmac_algo="sha512", kdf_algo="sha512"),
    CipherParams("sqlcipher3", page_size=1024, kdf_iter=64000, hmac_algo="sha1", kdf_algo="sha1"),
)
PREFIX_CANDIDATES = (1024, 0)


@dataclass(frozen=True)
class Key:
    """数据库密钥：口令（经 PBKDF2）或原始 32 字节密钥。"""

    secret: bytes
    raw: bool = False

    @classmethod
    def parse(cls, text: str | bytes) -> "Key":
        if isinstance(text, bytes):
            return cls(text)
        s = text.strip()
        low = s.lower()
        if low.startswith("x'") and low.endswith("'"):
            hexpart = s[2:-1]
            if len(hexpart) not in (64, 96):
                raise ValueError("原始密钥应为 64 位（或带盐 96 位）十六进制")
            return cls(bytes.fromhex(hexpart[:64]), raw=True)
        if not s:
            raise ValueError("密钥为空")
        return cls(s.encode("utf-8"))

    def __repr__(self) -> str:  # 防止密钥进入日志或异常信息
        return f"Key(raw={self.raw}, len={len(self.secret)})"


def derive_keys(key: Key, salt: bytes, params: CipherParams) -> tuple[bytes, bytes]:
    if key.raw:
        enc = key.secret
    else:
        enc = hashlib.pbkdf2_hmac(params.kdf_algo, key.secret, salt, params.kdf_iter, 32)
    hmac_salt = bytes(b ^ 0x3A for b in salt)
    mac = hashlib.pbkdf2_hmac(params.kdf_algo, enc, hmac_salt, 2, 32)
    return enc, mac


def detect_header(path: str | os.PathLike) -> dict:
    """读取文件头，判断是普通 SQLite、带 QQ NT 文件头的加密库，还是未知加密库。"""
    p = Path(path)
    with p.open("rb") as f:
        head = f.read(2048)
    info = {"kind": "unknown", "rand": None, "size": p.stat().st_size}
    if head.startswith(SQLITE_MAGIC):
        info["kind"] = "sqlite"
        return info
    marker = head.find(b"QQ_NT DB")
    if marker >= 0 and marker < 1024:
        info["kind"] = "ntqq"
        rest = head[marker + len(b"QQ_NT DB"):1024]
        rand = bytearray()
        started = False
        for b in rest:
            if 0x21 <= b <= 0x7E:
                rand.append(b)
                started = True
            elif started:
                break
        info["rand"] = rand.decode("ascii") if rand else None
        return info
    if head.startswith(b"SQLite header 3"):
        info["kind"] = "ntqq"
        return info
    info["kind"] = "encrypted"
    return info


class _PageCipher:
    def __init__(self, key: Key, salt: bytes, params: CipherParams):
        self.params = params
        self.enc_key, self.mac_key = derive_keys(key, salt, params)
        self._digest = getattr(hashlib, params.hmac_algo)

    def decrypt(self, pgno: int, raw: bytes) -> bytes:
        p = self.params
        ps = p.page_size
        if len(raw) != ps:
            raise CipherError(f"第 {pgno} 页长度不完整")
        offset = 16 if pgno == 1 else 0
        size = ps - p.reserve
        iv = raw[size:size + 16]
        tag = raw[size + 16:size + 16 + p.hmac_size]
        mac = hmac.new(self.mac_key, raw[offset:size + 16], self._digest)
        mac.update(struct.pack("<I", pgno))
        if not hmac.compare_digest(mac.digest(), tag):
            raise PageAuthError(f"第 {pgno} 页认证失败（密钥或参数不正确，或文件损坏）")
        dec = Cipher(algorithms.AES(self.enc_key), modes.CBC(iv)).decryptor()
        plain = dec.update(raw[offset:size]) + dec.finalize()
        if pgno == 1:
            return SQLITE_MAGIC + plain + raw[size:]
        return plain + raw[size:]


def _wal_checksum(data: bytes, s0: int, s1: int, big_endian: bool) -> tuple[int, int]:
    fmt = (">" if big_endian else "<") + f"{len(data) // 4}I"
    words = struct.unpack(fmt, data)
    for i in range(0, len(words), 2):
        s0 = (s0 + words[i] + s1) & 0xFFFFFFFF
        s1 = (s1 + words[i + 1] + s0) & 0xFFFFFFFF
    return s0, s1


def read_wal_index(wal_path: Path, page_size: int) -> tuple[dict[int, int], int | None]:
    """解析 WAL，返回 {页号: 帧数据在 WAL 中的偏移} 和提交后的库页数。

    只采用校验和连续、盐值一致、并且已提交的帧；不修改 WAL 文件。
    """
    if not wal_path.exists() or wal_path.stat().st_size < 32:
        return {}, None
    with wal_path.open("rb") as f:
        hdr = f.read(32)
        magic, _ver, pgsz, _ckpt, salt1, salt2, c1, c2 = struct.unpack(">8I", hdr)
        if magic not in (WAL_MAGIC_LE, WAL_MAGIC_BE) or pgsz != page_size:
            return {}, None
        big = magic == WAL_MAGIC_BE
        s0, s1 = _wal_checksum(hdr[:24], 0, 0, big)
        if (s0, s1) != (c1, c2):
            return {}, None
        frames: dict[int, int] = {}
        pending: dict[int, int] = {}
        db_pages: int | None = None
        off = 32
        while True:
            fh = f.read(24)
            if len(fh) < 24:
                break
            data = f.read(page_size)
            if len(data) < page_size:
                break
            pgno, commit, fs1, fs2, fc1, fc2 = struct.unpack(">6I", fh)
            if (fs1, fs2) != (salt1, salt2) or pgno == 0:
                break
            s0, s1 = _wal_checksum(fh[:8], s0, s1, big)
            s0, s1 = _wal_checksum(data, s0, s1, big)
            if (s0, s1) != (fc1, fc2):
                break
            pending[pgno] = off + 24
            if commit:
                frames.update(pending)
                pending.clear()
                db_pages = commit
            off += 24 + page_size
    return frames, db_pages


class EncryptedDatabase:
    """以只读方式打开一个 SQLCipher 库文件，按页提供明文。线程安全。"""

    def __init__(self, path: str | os.PathLike, key: Key, params: CipherParams, *, use_wal: bool = True,
                 cache_pages: int = 4096):
        self.path = Path(path)
        self.params = params
        self._fh = self.path.open("rb")
        self._lock = threading.Lock()
        self._fh.seek(params.prefix)
        salt = self._fh.read(16)
        if len(salt) < 16:
            self._fh.close()
            raise CipherError("文件过小，不是有效的加密数据库")
        self._cipher = _PageCipher(key, salt, params)
        body = self.path.stat().st_size - params.prefix
        self._file_pages = body // params.page_size
        self._wal_frames: dict[int, int] = {}
        self._wal_fh = None
        db_pages = None
        if use_wal:
            wal = Path(str(self.path) + "-wal")
            self._wal_frames, db_pages = read_wal_index(wal, params.page_size)
            if self._wal_frames:
                self._wal_fh = wal.open("rb")
        self.page_count = db_pages if db_pages else self._file_pages
        self._cache: OrderedDict[int, bytes] = OrderedDict()
        self._cache_max = cache_pages
        # 先认证第一页：密钥或参数不对时立即失败
        try:
            self.page(1)
        except BaseException:
            self.close()
            raise

    @property
    def size(self) -> int:
        return self.page_count * self.params.page_size

    @property
    def wal_frames(self) -> int:
        return len(self._wal_frames)

    def _raw(self, pgno: int) -> bytes:
        ps = self.params.page_size
        if pgno in self._wal_frames:
            self._wal_fh.seek(self._wal_frames[pgno])
            return self._wal_fh.read(ps)
        self._fh.seek(self.params.prefix + (pgno - 1) * ps)
        return self._fh.read(ps)

    def page(self, pgno: int) -> bytes:
        with self._lock:
            hit = self._cache.get(pgno)
            if hit is not None:
                self._cache.move_to_end(pgno)
                return hit
            if pgno < 1 or pgno > max(self.page_count, 1):
                raise CipherError(f"页号 {pgno} 超出范围")
            data = self._cipher.decrypt(pgno, self._raw(pgno))
            if pgno == 1 and data[18:20] == b"\x02\x02":
                # WAL 模式标记改为回滚日志模式：WAL 已在页层合并，SQLite 无需再找 -wal 文件
                data = data[:18] + b"\x01\x01" + data[20:]
            self._cache[pgno] = data
            if len(self._cache) > self._cache_max:
                self._cache.popitem(last=False)
            return data

    def read(self, offset: int, amount: int) -> bytes:
        ps = self.params.page_size
        out = bytearray()
        end = min(offset + amount, self.size)
        pos = offset
        while pos < end:
            pgno = pos // ps + 1
            within = pos % ps
            take = min(ps - within, end - pos)
            out += self.page(pgno)[within:within + take]
            pos += take
        return bytes(out)

    def close(self) -> None:
        with self._lock:
            self._fh.close()
            if self._wal_fh:
                self._wal_fh.close()
            self._cache.clear()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def try_open(path: str | os.PathLike, key: Key, params_list=None, prefixes=None) -> EncryptedDatabase:
    """依次尝试参数预设与文件头偏移，返回第一个通过第 1 页 HMAC 认证的组合。"""
    path = Path(path)
    header = detect_header(path)
    if header["kind"] == "sqlite":
        raise CipherError("该文件是未加密的 SQLite 数据库，无需密钥")
    if prefixes is None:
        prefixes = (1024, 0) if header["kind"] == "ntqq" else (0, 1024)
    last: Exception | None = None
    for params in params_list or PRESETS:
        for prefix in prefixes:
            candidate = replace(params, prefix=prefix)
            try:
                return EncryptedDatabase(path, key, candidate)
            except CipherError as exc:
                last = exc
    raise CipherError("密钥无法打开该数据库（已尝试全部参数预设）") from last


def verify_key(path: str | os.PathLike, key: Key) -> CipherParams | None:
    try:
        db = try_open(path, key)
    except CipherError:
        return None
    params = db.params
    db.close()
    return params
