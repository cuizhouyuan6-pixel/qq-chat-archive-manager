"""自动取钥（FR-03）：从本机运行中的 QQ 进程内存里取出数据库密钥，并用真实
SQLCipher 页面校验后才算成功。

原理（依据公开研究与实测，见需求文档 10 节参考资料）：

1. QQ NT 的库是 SQLCipher 4，第 1 页开头的 16 字节是**明文盐**；
2. 优先识别 SQLCipher 的 x'<64 hex key><32 hex salt>' 字面量（ASCII / UTF-16LE）；
3. 用目标库的盐筛选候选；旧的“二进制盐后邻近字节”启发式只作补充；
4. 候选密钥再用 ``sqlcipher`` 的页结构对第 1 页做 HMAC 认证——**只有真正能打开
   这个数据库的密钥才会被接受**，扫到的其他 32 字节数据不会误报。

安全边界（NFR-01 / NFR-02 / 约束 3.4）：

- 只读内存，不写、不打断、不结束 QQ 进程，不修改 QQ 的任何文件；
- 只在用户主动点击后执行；调用方负责先向用户说明并取得同意；
- 密钥不写日志：本模块的报告文本不显示密钥或其前缀；
- 取到的密钥交给 ``keystore`` 决定是否落盘，本模块自己不保存。
"""

from __future__ import annotations

import hashlib
import hmac
import os
import struct
import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Callable, Iterable, Iterator

from .sqlcipher import CipherParams, PRESETS
from .winmem import ProcessInfo, ProcessMemory, WinMemError, find_processes

# --- SQLCipher 4 页结构常量（与 sqlcipher.py 保持一致） ---------------------
SALT_SIZE = 16
RAW_KEY_SIZE = 32
HMAC_MASK = 0x3A

DEFAULT_WINDOW = 8 * 1024 * 1024  # 一次读 8 MB：够快也够省内存
DEFAULT_PROCESS_NAMES = ("QQ.exe", "QQ")  # QQ NT 主进程

# 盐之后未必紧邻密钥（不同 QQ 版本的内存布局不完全一致），在盐后面这个范围内
# 逐个偏移试。每次尝试都要做一次 HMAC——这是唯一能判定真假的手段，
# 所以宁可多试几个偏移，也不放过正确的密钥。
KEY_SEARCH_SPAN = 64
KEY_SEARCH_STEP = 2


def mask_key(hex_key: str) -> str:
    """把密钥变成可以安全显示、安全记录的形式。"""
    return "已隐藏"


# ---------------------------------------------------------------------------
# 目标库：读第 1 页（含明文盐）
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class DbTarget:
    """一个待取钥的数据库：文件路径 + 账号 + 第 1 页。"""

    path: str
    owner: str = ""
    salt: bytes = b""
    page1: bytes = b""
    prefix: int = 1024
    page_size: int = 4096

    @property
    def salt_hex(self) -> str:
        return self.salt.hex()


def read_page1(path: str | os.PathLike, *, prefix_candidates: Iterable[int] = (1024, 0),
               page_size: int = 4096) -> tuple[bytes, int]:
    """读出一个加密库的第 1 页与实际文件头长度。

    与 ``sqlcipher.detect_header`` 不同，这里不要求文件开头有 ``QQ_NT DB`` 标记：
    只要在某个偏移处读到的 16 字节盐不像空数据，就认为取的位置对
    （盐对不对最终由 HMAC 校验决定，这里只是取一个候选位置）。
    """
    p = Path(path)
    size = p.stat().st_size
    if size < SALT_SIZE + 64:
        raise ValueError(f"文件过小，不是有效的加密数据库：{p.name}")
    with p.open("rb") as f:
        head = f.read(2048)
        if head[:16] == b"SQLite format 3\x00":
            raise ValueError(f"{p.name} 是未加密的数据库，无需取钥")
        marker = head.find(b"QQ_NT DB")
        order = list(prefix_candidates)
        if 0 <= marker < 1024 and 1024 in order:
            order.remove(1024)
            order.insert(0, 1024)
        for prefix in order:
            if prefix + page_size > size:
                continue
            f.seek(prefix)
            page1 = f.read(page_size)
            if len(page1) == page_size and page1[:SALT_SIZE] != b"\x00" * SALT_SIZE:
                return page1, prefix
    raise ValueError(f"读不出 {p.name} 的第 1 页，可能不是 SQLCipher 数据库")


def derive_hmac_key(enc_key: bytes, salt: bytes, params: CipherParams) -> bytes:
    hmac_salt = bytes(b ^ HMAC_MASK for b in salt)
    return hashlib.pbkdf2_hmac(params.kdf_algo, enc_key, hmac_salt, 2, 32)


def check_page1(page1: bytes, enc_key: bytes, params: CipherParams) -> bool:
    """用第 1 页的 HMAC 判断 ``enc_key`` 是不是这个库的原始密钥。

    页结构定义与 ``sqlcipher.py`` 完全一致：数据区 = 去掉 16 字节盐与页尾保留区，
    HMAC 覆盖「密文 + IV + 页号(小端 4 字节)」。
    """
    ps = params.page_size
    if len(page1) < ps:
        return False
    size = ps - params.reserve
    if size <= SALT_SIZE:
        return False
    content = page1[SALT_SIZE:size]
    iv = page1[size:size + 16]
    tag = page1[size + 16:size + 16 + params.hmac_size]
    mac_key = derive_hmac_key(enc_key, page1[:SALT_SIZE], params)
    digest = getattr(hashlib, params.hmac_algo)
    mac = hmac.new(mac_key, content + iv + struct.pack("<I", 1), digest)
    return hmac.compare_digest(mac.digest(), tag)


def params_for(target: DbTarget) -> list[CipherParams]:
    """按目标库的页大小与文件头长度展开参数预设（HMAC 算法逐个试）。"""
    return [replace(p, page_size=target.page_size, prefix=target.prefix) for p in PRESETS]


def verify_raw_key(key_hex: str, target: DbTarget) -> CipherParams | None:
    """候选原始密钥对目标库逐预设校验，返回能通过的参数组合。"""
    try:
        raw = bytes.fromhex(key_hex)
    except ValueError:
        return None
    if len(raw) != RAW_KEY_SIZE:
        return None
    for p in params_for(target):
        if check_page1(target.page1, raw, p):
            return p
    return None


# ---------------------------------------------------------------------------
# 内存搜索
# ---------------------------------------------------------------------------

@dataclass
class ScanResult:
    """一次扫描的统计结果。``keys`` 内含密钥，不要直接打印。"""

    pids_scanned: list[int] = field(default_factory=list)
    bytes_scanned: int = 0
    seconds: float = 0.0
    keys: dict[str, str] = field(default_factory=dict, repr=False)  # salt_hex -> key_hex
    errors: list[str] = field(default_factory=list)

    def summary(self) -> str:
        head = (f"扫描 {len(self.pids_scanned)} 个 QQ 进程、{self.bytes_scanned / 1048576:.0f} MB 内存，"
                f"用时 {self.seconds:.1f} 秒，找到 {len(self.keys)} 个密钥")
        return head + ("；" + "；".join(self.errors) if self.errors else "")

    def safe_keys(self) -> dict[str, str]:
        """隐藏全部密钥字符的显示版本。"""
        return {salt: mask_key(k) for salt, k in self.keys.items()}


def candidate_offsets(block: bytes, salt: bytes) -> Iterator[int]:
    """在一个内存块里找出所有“盐之后紧跟 32 字节”的候选起点。"""
    start = 0
    while True:
        idx = block.find(salt, start)
        if idx < 0:
            return
        pos = idx + len(salt)
        if pos + RAW_KEY_SIZE <= len(block):
            yield pos
        start = idx + 1


def literal_keys(block: bytes, salt: bytes) -> Iterator[bytes]:
    """只接受完整 SQLCipher 原始密钥字面量；盐匹配仍须通过页面 HMAC。"""
    hexadecimal = b'0123456789abcdefABCDEF'
    for wide in (False, True):
        for salt_text in (salt.hex(), salt.hex().upper()):
            needle = salt_text.encode('utf-16-le' if wide else 'ascii')
            step = 2 if wide else 1
            start = 0
            while True:
                idx = block.find(needle, start)
                if idx < 0:
                    break
                start = idx + step
                begin, end = idx - 66 * step, idx + 33 * step
                if begin < 0 or end > len(block):
                    continue
                literal = block[begin:end]
                if wide:
                    if any(literal[1::2]):
                        continue
                    literal = literal[::2]
                if literal[:2] not in (b"x'", b"X'") or literal[-1:] != b"'":
                    continue
                encoded = literal[2:-1]
                if len(encoded) != 96 or any(c not in hexadecimal for c in encoded):
                    continue
                if encoded[64:].lower() != salt.hex().encode():
                    continue
                yield bytes.fromhex(encoded[:64].decode('ascii'))


def _try_block(block: bytes, salt: bytes, targets: list[DbTarget], seen: set[bytes]) -> str | None:
    """在一个内存块里尝试候选密钥，命中就返回密钥十六进制。

    先试紧邻盐的位置（公开资料中 QQ 的常见布局），再在 ``KEY_SEARCH_SPAN``
    范围内按 ``KEY_SEARCH_STEP`` 逐个偏移试。遍历顺序按「离盐由近到远」，
    而不是按「第几处盐」——否则前面某处盐后面的一大片零字节会先把搜索额度用光，
    真正藏着密钥的那一处就轮不上了。每个候选都要通过第 1 页 HMAC，
    所以放宽范围不会带来误报，只会多花一点校验时间。
    """
    for cand in literal_keys(block, salt):
        if cand in seen:
            continue
        seen.add(cand)
        for t in targets:
            if verify_raw_key(cand.hex(), t) is not None:
                return cand.hex()
    offsets: set[int] = set()
    for pos in candidate_offsets(block, salt):
        # 紧邻的 16 个位置逐字节试（不假设对齐），再往后按 2 字节步长放宽
        for extra in range(0, min(KEY_SEARCH_SPAN, 16) + 1):
            offsets.add(pos + extra)
        for extra in range(18, KEY_SEARCH_SPAN + 1, KEY_SEARCH_STEP):
            offsets.add(pos + extra)
    for pos in sorted(offsets):
        if pos + RAW_KEY_SIZE > len(block):
            continue
        cand = block[pos:pos + RAW_KEY_SIZE]
        if cand in seen:
            continue
        seen.add(cand)
        hex_key = cand.hex()
        for t in targets:
            if verify_raw_key(hex_key, t) is not None:
                return hex_key
    return None


def find_salt_hits(pid: int, salt: bytes, *, window: int = DEFAULT_WINDOW, limit: int = 8,
                   cancel=None) -> list[int]:
    """定位盐在进程内存里出现的位置（诊断用，不返回密钥）。"""
    hits: list[int] = []
    try:
        with ProcessMemory(pid) as mem:
            tail = b''
            previous_end = None
            for base, block in mem.iter_readable(chunk=window):
                if cancel is not None and cancel.is_set():
                    break
                combined = (tail if previous_end == base else b'') + block
                prefix_len = len(combined) - len(block)
                needles = (salt, salt.hex().encode(), salt.hex().upper().encode(),
                           salt.hex().encode('utf-16-le'), salt.hex().upper().encode('utf-16-le'))
                for needle in set(needles):
                    off = 0
                    while True:
                        idx = combined.find(needle, off)
                        if idx < 0:
                            break
                        address = base - prefix_len + idx
                        if address not in hits:
                            hits.append(address)
                        if len(hits) >= limit:
                            return hits
                        off = idx + 1
                tail = combined[-198:]
                previous_end = base + len(block)
    except WinMemError:
        return hits
    return hits


def diagnose(targets: list[DbTarget], *, names: Iterable[str] = DEFAULT_PROCESS_NAMES,
             max_pids: int = 3, cancel=None) -> list[str]:
    """取钥失败时给出可执行的排查线索（不含任何密钥内容）。

    同时检查二进制和十六进制盐。未命中可能是旧目录、未打开或布局不匹配；
    不能据此断言用户未登录，或数据库一定损坏。
    """
    lines: list[str] = []
    procs = candidate_processes(names)
    if not procs:
        return ["没有发现正在运行的 QQ。请先启动并登录电脑 QQ，再点“自动获取密钥”。"]
    for t in targets:
        where = f"QQ {t.owner}" if t.owner else Path(t.path).name
        found_any = False
        for p in procs[:max_pids]:
            try:
                hits = find_salt_hits(p.pid, t.salt, cancel=cancel)
            except WinMemError as exc:
                lines.append(f"{where}：{exc}")
                continue
            if hits:
                found_any = True
                lines.append(f"{where}：在 {p.name}（进程号 {p.pid}）内存里找到了数据库标记，"
                             f"但候选密钥没有通过认证。可能是来源变化、加密参数或版本布局不匹配。")
                break
        if not found_any:
            lines.append(f"{where}：QQ 进程内存里没有这个数据库的标记，"
                         f"当前扫描未能定位标记，请核对来源是否为 QQ 当前数据目录；旧备份、未登录账号或版本布局不同均可能导致未命中。")
    return lines


def scan_process(pid: int, targets: list[DbTarget], *, window: int = DEFAULT_WINDOW,
                 progress: Callable[[int, int], None] | None = None,
                 cancel=None, mem: ProcessMemory | None = None) -> ScanResult:
    """扫描一个进程的内存，为每个目标库找出能通过 HMAC 校验的密钥。

    盐相同的多个库（例如同一账号的多个 .db 文件）只扫一次。
    """
    res = ScanResult(pids_scanned=[pid])
    started = time.time()
    by_salt: dict[bytes, list[DbTarget]] = {}
    for t in targets:
        by_salt.setdefault(t.salt, []).append(t)
    if not by_salt:
        return res
    needed = set(by_salt)
    seen: dict[bytes, set[bytes]] = {s: set() for s in needed}
    overlap = max(RAW_KEY_SIZE + SALT_SIZE + KEY_SEARCH_SPAN, 198)  # 含 UTF-16LE 字面量
    # 窗口至少要明显大于重叠量：否则区域末尾会出现“每次只前进几个字节”的爬行读取，
    # 既慢又可能把模式切碎（这个下限同时也保证了每次读的跨度足够大）。
    span = max(int(window), overlap * 8)
    own = mem is None
    handle = None
    try:
        handle = ProcessMemory(pid) if own else mem
        regions = sorted(handle.regions())
        total_bytes = sum(size for _b, size in regions) or 1
        done = 0
        i = 0
        while i < len(regions):
            if cancel is not None and cancel.is_set():
                break
            base, size = regions[i]
            read_end = base + size
            i += 1
            # 地址连续（或只隔一小段空洞）的区域并成一次读取，减少系统调用
            while i < len(regions):
                nb, ns = regions[i]
                if nb == read_end and (read_end - base) < 16 * 1024 * 1024:
                    read_end = max(read_end, nb + ns)
                    i += 1
                else:
                    break
            pos = base
            while pos < read_end:
                if cancel is not None and cancel.is_set():
                    return _finish(res, started)
                take = min(span, read_end - pos)
                block = handle.read(pos, take)
                done += take
                if block:
                    res.bytes_scanned += len(block)
                    for salt in list(needed):
                        hit = _try_block(block, salt, by_salt[salt], seen[salt])
                        if hit:
                            res.keys[salt.hex()] = hit
                            needed.discard(salt)
                if progress:
                    progress(min(done, total_bytes), total_bytes)
                if not needed:
                    return _finish(res, started)
                # 前进一个窗口，并回退 overlap 字节以覆盖跨界模式；绝不逐字节爬行
                pos += max(take - overlap, min(take, overlap * 4))
    except WinMemError as exc:
        res.errors.append(str(exc))
    finally:
        if own and handle is not None:
            handle.close()
    return _finish(res, started)


def _finish(res: ScanResult, started: float) -> ScanResult:
    res.seconds = time.time() - started
    return res


def candidate_processes(names: Iterable[str] = DEFAULT_PROCESS_NAMES) -> list[ProcessInfo]:
    """找出可能是 QQ 主进程的进程：带 wrapper.node 模块的排在最前。"""
    try:
        procs = find_processes(list(names))
    except WinMemError:
        return []
    if not procs:
        return []
    main: list[ProcessInfo] = []
    rest: list[ProcessInfo] = []
    for p in procs:
        try:
            with ProcessMemory(p.pid) as mem:
                (main if "wrapper.node" in mem.modules() else rest).append(p)
        except WinMemError:
            rest.append(p)
    return main + rest


def find_qq_processes(names: Iterable[str] = DEFAULT_PROCESS_NAMES) -> list[ProcessInfo]:
    """列出正在运行的 QQ 进程（只枚举，不读内存）。"""
    try:
        return find_processes(list(names))
    except WinMemError:
        return []


def extract_keys(targets: list[DbTarget], *, names: Iterable[str] = DEFAULT_PROCESS_NAMES,
                 window: int = DEFAULT_WINDOW, max_pids: int = 3,
                 progress: Callable[[int, int], None] | None = None,
                 status: Callable[[str], None] | None = None, cancel=None) -> ScanResult:
    """在运行中的 QQ 进程里为目标库取钥。

    只扫描最可能是主进程的前 ``max_pids`` 个进程，避免在大内存进程上白等。
    """
    total = ScanResult()
    if not targets:
        return total
    procs = candidate_processes(names)
    if not procs:
        total.errors.append("没有发现正在运行的 QQ，请先登录电脑 QQ 后再试")
        return total
    want = len({t.salt for t in targets})
    for p in procs[:max_pids]:
        if cancel is not None and cancel.is_set():
            break
        if status:
            status(f"正在读取 QQ 进程内存（{p.name}，进程号 {p.pid}）…")
        one = scan_process(p.pid, targets, window=window, progress=progress, cancel=cancel)
        total.pids_scanned += one.pids_scanned
        total.bytes_scanned += one.bytes_scanned
        total.seconds += one.seconds
        total.keys.update(one.keys)
        total.errors += one.errors
        if len(total.keys) >= want:
            break
    return total


# ---------------------------------------------------------------------------
# 上层：为一批来源文件取钥
# ---------------------------------------------------------------------------

@dataclass
class KeyHit:
    """一个账号对应的取钥结果。``key_hex`` 是密钥，不要直接打印。"""

    owner: str
    db_path: str
    key_hex: str = field(repr=False)
    params: CipherParams

    def describe(self) -> str:
        where = f"QQ {self.owner}" if self.owner else Path(self.db_path).name
        return f"{where}：已取得密钥（{mask_key(self.key_hex)}），已通过数据库校验"


def targets_from_candidates(candidates) -> list[DbTarget]:
    """把来源候选转成取钥目标（只保留能读出第 1 页的加密库）。"""
    out: list[DbTarget] = []
    for c in candidates:
        if not getattr(c, "needs_key", False):
            continue
        try:
            page1, prefix = read_page1(c.path)
        except (OSError, ValueError):
            continue
        out.append(DbTarget(path=str(c.path), owner=str(getattr(c, "owner", "") or ""),
                            salt=page1[:SALT_SIZE], page1=page1, prefix=prefix))
    return out


def hits_from_result(targets: list[DbTarget], res: ScanResult) -> list[KeyHit]:
    """把扫描命中的密钥套回每个目标库（盐相同的库共用同一个密钥）。"""
    by_salt: dict[bytes, list[DbTarget]] = {}
    for t in targets:
        by_salt.setdefault(t.salt, []).append(t)
    hits: list[KeyHit] = []
    for salt_hex, key_hex in res.keys.items():
        try:
            salt = bytes.fromhex(salt_hex)
        except ValueError:
            continue
        for t in by_salt.get(salt, []):
            params = verify_raw_key(key_hex, t)
            if params is not None:
                hits.append(KeyHit(owner=t.owner, db_path=t.path, key_hex=key_hex, params=params))
    return hits


def acquire_for_candidates(candidates, *, names: Iterable[str] = DEFAULT_PROCESS_NAMES,
                           window: int = DEFAULT_WINDOW,
                           progress: Callable[[int, int], None] | None = None,
                           status: Callable[[str], None] | None = None,
                           cancel=None) -> tuple[list[KeyHit], ScanResult]:
    """给一批缺密钥的来源取钥，返回命中的 (账号, 密钥) 与扫描统计。"""
    targets = targets_from_candidates(candidates)
    res = extract_keys(targets, names=names, window=window, progress=progress,
                       status=status, cancel=cancel)
    return hits_from_result(targets, res), res
