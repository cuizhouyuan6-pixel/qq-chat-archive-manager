"""自动取钥（FR-03）测试。

用真实 sqlcipher3 生成的合成库做“校验器”基准：把口令按公开方法派生成 32 字节
原始密钥，验证 keyscan 的页校验、候选定位与整条扫描链路。
"""

from __future__ import annotations

import hashlib

import pytest

from qqarchive import keyscan
from qqarchive.sqlcipher import CipherParams
from ntqq_fixture import NTQQ_KEY, make_ntqq_db, sample_rows


@pytest.fixture
def real_db(tmp_path):
    """一个真实 SQLCipher 格式的合成库：返回 (路径, 第1页, 文件头长度, 原始密钥)。"""
    db = make_ntqq_db(tmp_path / "nt_msg.db", *sample_rows())
    page1, prefix = keyscan.read_page1(db)
    raw = hashlib.pbkdf2_hmac("sha512", NTQQ_KEY.encode(), page1[:keyscan.SALT_SIZE], 4000, 32)
    return db, page1, prefix, raw


# --- 第 1 页与密钥校验 ------------------------------------------------------

def test_read_page1_finds_ntqq_prefix(real_db):
    _db, page1, prefix, _raw = real_db
    assert prefix == 1024
    assert len(page1) == 4096
    assert page1[:16] != b"\x00" * 16


def test_read_page1_rejects_plain_sqlite(tmp_path):
    plain = make_ntqq_db(tmp_path / "plain.db", *sample_rows(), plain=True)
    with pytest.raises(ValueError):
        keyscan.read_page1(plain)


def test_verify_raw_key_accepts_real_key(real_db):
    db, page1, prefix, raw = real_db
    target = keyscan.DbTarget(path=str(db), owner="10001", salt=page1[:16], page1=page1, prefix=prefix)
    params = keyscan.verify_raw_key(raw.hex(), target)
    assert params is not None
    assert params.page_size == 4096
    assert params.prefix == 1024


def test_verify_raw_key_rejects_wrong_key(real_db):
    db, page1, prefix, raw = real_db
    target = keyscan.DbTarget(path=str(db), owner="10001", salt=page1[:16], page1=page1, prefix=prefix)
    wrong = bytes((b ^ 0x5A) for b in raw)
    assert keyscan.verify_raw_key(wrong.hex(), target) is None
    assert keyscan.verify_raw_key("00" * 32, target) is None
    assert keyscan.verify_raw_key("zz" * 32, target) is None  # 非法十六进制
    assert keyscan.verify_raw_key(raw.hex()[:32], target) is None  # 长度不对


def test_check_page1_matches_sqlcipher_module(real_db):
    """页校验与 sqlcipher 模块的解密实现必须一致（同一份页结构定义）。"""
    db, page1, prefix, raw = real_db
    params = CipherParams("ntqq", prefix=prefix)
    assert keyscan.check_page1(page1, raw, params) is True
    tampered = bytearray(page1)
    tampered[100] ^= 0xFF
    assert keyscan.check_page1(bytes(tampered), raw, params) is False


# --- 内存里的候选定位 -------------------------------------------------------

def test_candidate_offsets_only_after_salt():
    salt = b"S" * 16
    block = b"\x00" * 8 + salt + b"K" * 32 + b"\x00" * 8
    offs = list(keyscan.candidate_offsets(block, salt))
    assert offs == [8 + 16]
    assert block[offs[0]:offs[0] + 32] == b"K" * 32


def test_candidate_offsets_handles_multiple_and_truncated():
    salt = b"S" * 16
    block = salt + b"A" * 32 + b"\x00" * 4 + salt + b"B" * 32 + salt  # 最后一处没有 32 字节
    offs = list(keyscan.candidate_offsets(block, salt))
    assert offs == [16, 16 + 32 + 4 + 16]


def test_try_block_finds_key_at_adjacent_offset(real_db):
    """内存布局：<32 字节密钥><16 字节盐>，密钥紧跟在盐之后即可被取到。"""
    db, page1, prefix, raw = real_db
    target = keyscan.DbTarget(path=str(db), owner="1", salt=page1[:16], page1=page1, prefix=prefix)
    assert keyscan.verify_raw_key(raw.hex(), target) is not None
    block = b"\x11" * 64 + page1[:16] + raw + b"\x22" * 64
    hit = keyscan._try_block(block, page1[:16], [target], set())
    assert hit == raw.hex()


def test_try_block_finds_key_at_shifted_offset(real_db):
    """密钥不在紧邻位置时，也要能在搜索范围内找到。"""
    db, page1, prefix, raw = real_db
    target = keyscan.DbTarget(path=str(db), owner="1", salt=page1[:16], page1=page1, prefix=prefix)
    block = b"\x11" * 32 + page1[:16] + b"\x33" * 8 + raw + b"\x44" * 32
    hit = keyscan._try_block(block, page1[:16], [target], set())
    assert hit == raw.hex()


def test_try_block_finds_key_at_unaligned_offset(real_db):
    """盐落在奇数地址上时（后面的密钥也偏移 1 字节）同样要能取到。"""
    db, page1, prefix, raw = real_db
    target = keyscan.DbTarget(path=str(db), owner="1", salt=page1[:16], page1=page1, prefix=prefix)
    block = b"\x11" * 31 + page1[:16] + raw + b"\x22" * 32
    assert (block.find(page1[:16]) % 2) == 1, "构造的盐应当落在奇数偏移"
    assert keyscan._try_block(block, page1[:16], [target], set()) == raw.hex()


def test_try_block_prefers_near_offset_over_earlier_noise(real_db):
    """盐出现两次时，靠后那一处才是真密钥：不能因为前面那处全是零字节就放弃。"""
    db, page1, prefix, raw = real_db
    target = keyscan.DbTarget(path=str(db), owner="1", salt=page1[:16], page1=page1, prefix=prefix)
    noise = b"\x00" * 512  # 先来一处“盐 + 一大片零”
    block = noise[:0] + page1[:16] + noise + page1[:16] + raw + b"\x00" * 64
    hit = keyscan._try_block(block, page1[:16], [target], set())
    assert hit == raw.hex()


def test_try_block_returns_none_for_noise(real_db):
    db, page1, prefix, _raw = real_db
    target = keyscan.DbTarget(path=str(db), owner="1", salt=page1[:16], page1=page1, prefix=prefix)
    block = page1[:16] + b"\x99" * 96
    assert keyscan._try_block(block, page1[:16], [target], set()) is None


# --- 扫描链路（用假的进程内存，不需要真的读别的进程） ----------------------

class FakeMemory:
    """按区域返回预设内存的假 ProcessMemory。

    可以跨区域连续读（和真实的 ``ProcessMemory.read`` 一样），
    这样才测得出“模式跨区域边界”的情形。
    """

    def __init__(self, chunks, *, step: int | None = None):
        """chunks 为 bytes 列表（用 step 均匀排布），或 (起始地址, bytes) 列表。"""
        if chunks and isinstance(chunks[0], tuple):
            self.regions_data = [(int(b), bytes(d)) for b, d in chunks]
        else:
            step = step or 0x100000
            self.regions_data = [(i * step, bytes(c)) for i, c in enumerate(chunks)]

    def regions(self):
        return [(base, len(data)) for base, data in self.regions_data]

    def read(self, address, size):
        out = bytearray()
        pos, end = address, address + size
        for base, data in self.regions_data:
            region_end = base + len(data)
            if region_end <= pos or base >= end:
                continue
            start = max(pos, base) - base
            stop = min(end, region_end) - base
            out += data[start:stop]
            pos = base + stop
            if pos >= end:
                break
        return bytes(out)

    def iter_readable(self, chunk=None):
        for base, data in self.regions_data:
            yield base, data

    def close(self):
        pass


def _fake_regions(pattern: bytes, first: int, second: int, *, keep_in_first: int = 40):
    """把 pattern 放在两个**相邻**内存区域之间，让它正好被区域边界切断。

    返回 (FakeMemory, pattern 起始的绝对地址)。
    """
    # 第一区域：前段填零，末尾 keep_in_first 字节留给 pattern 的前半
    first_chunk = b"\x00" * (first - keep_in_first) + pattern[:keep_in_first]
    # 第二区域：紧接着放 pattern 的后半，再补零
    second_chunk = pattern[keep_in_first:] + b"\x00" * (second - (len(pattern) - keep_in_first))
    start = first - keep_in_first  # pattern 起点落在第一区域末尾
    memory = FakeMemory([(0, first_chunk), (first, second_chunk)])
    return memory, start


def test_scan_process_finds_key_across_blocks(real_db):
    """密钥跨内存块（区域）边界时也能找到——这正是窗口重叠的意义。"""
    db, page1, prefix, raw = real_db
    target = keyscan.DbTarget(path=str(db), owner="7", salt=page1[:16], page1=page1, prefix=prefix)
    pattern = page1[:16] + raw  # 盐在前、密钥在后
    fake, start = _fake_regions(pattern, 0x8000, 0x8000, keep_in_first=20)
    # 先确认“跨边界合并后”的窗口里确实能看到完整模式
    merged = fake.read(start, 0x1000)
    assert pattern in merged
    assert keyscan._try_block(merged, page1[:16], [target], set()) == raw.hex()
    res = keyscan.scan_process(999, [target], window=0x1000, mem=fake)
    assert res.keys.get(page1[:16].hex()) == raw.hex()
    assert res.bytes_scanned > 0


def test_scan_process_reports_stats_even_when_missing(real_db):
    db, page1, prefix, _raw = real_db
    target = keyscan.DbTarget(path=str(db), owner="7", salt=page1[:16], page1=page1, prefix=prefix)
    fake = FakeMemory([b"\x00" * 0x2000])
    res = keyscan.scan_process(999, [target], window=0x1000, mem=fake)
    assert res.keys == {}
    assert res.pids_scanned == [999]
    assert "0 个密钥" in res.summary()


def test_scan_result_never_exposes_full_key(real_db):
    db, page1, prefix, raw = real_db
    target = keyscan.DbTarget(path=str(db), owner="7", salt=page1[:16], page1=page1, prefix=prefix)
    fake = FakeMemory([page1[:16] + raw + b"\x00" * 0x4000])
    res = keyscan.scan_process(999, [target], window=0x1000, mem=fake)
    assert res.keys, "应该命中"
    safe = res.safe_keys()
    shown = safe[page1[:16].hex()]
    assert shown != raw.hex()
    assert raw.hex()[:8] not in shown
    assert raw.hex() not in res.summary()


def test_mask_key():
    assert keyscan.mask_key("0123456789abcdef") == "已隐藏"
    assert keyscan.mask_key("") == "已隐藏"
    assert keyscan.mask_key("abc") == "已隐藏"


# --- 上层：来源候选 → 目标 → 命中 -------------------------------------------

class FakeCandidate:
    def __init__(self, path, owner="", needs_key=True):
        self.path = str(path)
        self.owner = owner
        self.needs_key = needs_key


def test_targets_from_candidates_skips_plain_and_unreadable(tmp_path, real_db):
    db, _page1, _prefix, _raw = real_db
    plain = make_ntqq_db(tmp_path / "plain2.db", *sample_rows(), plain=True)
    cands = [FakeCandidate(db, "10001"), FakeCandidate(plain, "10002"),
             FakeCandidate(tmp_path / "missing.db", "10003"),
             FakeCandidate(db, "10004", needs_key=False)]
    targets = keyscan.targets_from_candidates(cands)
    assert len(targets) == 1
    assert targets[0].owner == "10001"


def test_hits_from_result_maps_salt_back_to_all_databases(tmp_path, real_db):
    """同一密钥可能对应多个库，命中的密钥要套回每一个。"""
    db, page1, prefix, raw = real_db
    targets = [keyscan.DbTarget(path=str(db), owner=a, salt=page1[:16], page1=page1, prefix=prefix)
               for a in ("10001", "10002")]
    res = keyscan.ScanResult(keys={page1[:16].hex(): raw.hex()})
    hits = keyscan.hits_from_result(targets, res)
    assert sorted(h.owner for h in hits) == ["10001", "10002"]
    for h in hits:
        assert "已隐藏" in h.describe()
        assert raw.hex() not in h.describe()


def test_hits_from_result_ignores_unverified(tmp_path, real_db):
    db, page1, prefix, _raw = real_db
    targets = [keyscan.DbTarget(path=str(db), owner="10001", salt=page1[:16], page1=page1, prefix=prefix)]
    res = keyscan.ScanResult(keys={page1[:16].hex(): "aa" * 32})
    assert keyscan.hits_from_result(targets, res) == []


def test_diagnose_without_qq_reports_clear_reason(monkeypatch):
    monkeypatch.setattr(keyscan, "candidate_processes", lambda names=None: [])
    lines = keyscan.diagnose([keyscan.DbTarget(path="x", salt=b"a" * 16)])
    assert lines and "正在运行的 QQ" in lines[0]


def test_diagnose_explains_when_database_not_loaded(monkeypatch):
    from qqarchive.winmem import ProcessInfo
    monkeypatch.setattr(keyscan, "candidate_processes",
                        lambda names=None: [ProcessInfo(4321, "QQ.exe")])
    monkeypatch.setattr(keyscan, "find_salt_hits", lambda pid, salt, **kw: [])
    lines = keyscan.diagnose([keyscan.DbTarget(path="x", owner="10001", salt=b"a" * 16)])
    assert any("未能定位标记" in ln for ln in lines)


def test_verify_key_from_real_keystore_path(real_db):
    """keyscan 校验通过后，keystore 用同一条密钥也必须能打开这个库。"""
    from qqarchive.keystore import check_raw_key
    db, page1, prefix, raw = real_db
    target = keyscan.DbTarget(path=str(db), owner="1", salt=page1[:16], page1=page1, prefix=prefix)
    assert keyscan.verify_raw_key(raw.hex(), target) is not None
    checked = check_raw_key(db, raw.hex())
    assert checked is not None
    key, params = checked
    assert key.raw and len(key.secret) == 32
    assert params.prefix == 1024
