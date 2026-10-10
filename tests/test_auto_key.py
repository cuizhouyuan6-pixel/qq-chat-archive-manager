"""自动化流程里的自动取钥与手机转移（FR-03 / FR-10）测试。

取钥部分不碰真实 QQ 进程：把 keyscan 的“扫描”换成一个返回已知密钥的假实现，
这样验证的是**编排、校验、入库、报告话术**这几层。
"""

from __future__ import annotations

import hashlib
import sys

import pytest

from qqarchive import auto, keyscan
from qqarchive.config import Config
from qqarchive.discovery import SourceCandidate
from qqarchive.keystore import KeyStore
from ntqq_fixture import NTQQ_KEY, make_ntqq_db, sample_rows


@pytest.fixture
def real_db(tmp_path):
    db = make_ntqq_db(tmp_path / "nt_msg.db", *sample_rows())
    page1, prefix = keyscan.read_page1(db)
    raw = hashlib.pbkdf2_hmac("sha512", NTQQ_KEY.encode(), page1[:keyscan.SALT_SIZE], 4000, 32)
    return db, page1, prefix, raw


@pytest.fixture
def candidate(real_db):
    db, _page1, _prefix, _raw = real_db
    return SourceCandidate(path=str(db), kind="pc", owner="10001", size=db.stat().st_size,
                           mtime=0, header="ntqq")


@pytest.fixture
def cfg(config_home, tmp_path):
    c = Config()
    c.data_dir = str(tmp_path / "data")
    c.remember_keys = False
    return c


# ---------------------------------------------------------------------------
# 编排：把“扫到的密钥”接进密钥库
# ---------------------------------------------------------------------------

def _fake_extract(real_db, *, hit: bool):
    """构造一个假的 extract_keys：要么给出正确密钥，要么什么都不给。"""
    db, page1, prefix, raw = real_db
    salt = page1[:keyscan.SALT_SIZE]

    def fake(targets, **kwargs):
        res = keyscan.ScanResult(pids_scanned=[4321], bytes_scanned=1024 * 1024, seconds=0.5)
        if hit:
            res.keys[salt.hex()] = raw.hex()
        return res

    return fake


def test_acquire_keys_success_stores_and_reports(cfg, candidate, real_db, monkeypatch):
    _db, _page1, _prefix, raw = real_db
    monkeypatch.setattr(keyscan, "extract_keys", _fake_extract(real_db, hit=True))
    store = KeyStore(cfg.data_path / "keys")
    rep = auto.acquire_keys(cfg, [candidate], store)

    assert rep.ok
    assert rep.accounts == ["10001"]
    assert rep.process_count == 1
    assert rep.scanned_mb == 1
    # 密钥进了密钥库，能打开那个库
    got = store.get("10001")
    assert got is not None
    key, params = got
    assert key.secret == raw
    assert params.prefix == 1024
    # 报告文本不出现完整密钥
    text = rep.text()
    assert raw.hex() not in text
    assert "自动获取密钥成功" in text
    assert "只在本次运行有效" in text


def test_acquire_keys_respects_remember_flag(cfg, candidate, real_db, monkeypatch):
    monkeypatch.setattr(keyscan, "extract_keys", _fake_extract(real_db, hit=True))
    store = KeyStore(cfg.data_path / "keys")
    cfg.remember_keys = True
    auto.acquire_keys(cfg, [candidate], store)
    assert store.has("10001") == "session"
    assert store.file.exists(), "勾选了记住才写文件"
    assert store.saved_accounts()


def test_acquire_keys_failure_explains_next_step(cfg, candidate, real_db, monkeypatch):
    monkeypatch.setattr(keyscan, "extract_keys", _fake_extract(real_db, hit=False))
    monkeypatch.setattr(keyscan, "diagnose", lambda targets, **kw: ["QQ 10001：没有登录"])
    store = KeyStore(cfg.data_path / "keys")
    rep = auto.acquire_keys(cfg, [candidate], store)

    assert not rep.ok
    assert rep.found == 0
    assert store.get("10001") is None
    text = rep.text()
    assert "没有成功" in text
    assert "手动填写密钥" in text
    assert "QQ 10001：没有登录" in text


def test_acquire_keys_skips_when_disabled(cfg, candidate):
    cfg.auto_key_enabled = False
    rep = auto.acquire_keys(cfg, [candidate], KeyStore(cfg.data_path / "keys"))
    assert not rep.ok
    assert any("设置中关闭" in e and "手动填写" in e for e in rep.errors), rep.errors


def test_acquire_keys_without_encrypted_sources(cfg):
    plain = SourceCandidate(path="x", kind="plain", owner="1", size=1, mtime=0, header="sqlite")
    rep = auto.acquire_keys(cfg, [plain], KeyStore(cfg.data_path / "keys"))
    assert not rep.ok
    assert any("都不需要密钥" in ln for ln in rep.lines)


def test_acquire_keys_never_stores_unverified_key(cfg, candidate, real_db, monkeypatch):
    """扫到的密钥打不开这个库时，绝不能存进密钥库。"""
    db, page1, _prefix, _raw = real_db
    salt = page1[:keyscan.SALT_SIZE]

    def fake(targets, **kwargs):
        res = keyscan.ScanResult(pids_scanned=[1])
        res.keys[salt.hex()] = "ab" * 32  # 错密钥
        return res

    monkeypatch.setattr(keyscan, "extract_keys", fake)
    store = KeyStore(cfg.data_path / "keys")
    rep = auto.acquire_keys(cfg, [candidate], store)
    assert not rep.ok
    assert store.get("10001") is None
    assert not store.file.exists()


def test_key_warning_mentions_readonly_and_risk():
    text = auto.key_warning()
    assert "只读" in text
    assert "不修改" in text
    assert "密钥" in text


def test_run_all_imports_after_auto_key(cfg, candidate, real_db, monkeypatch):
    """一键流程：自动取到密钥后，该来源应当被导入。"""
    from qqarchive.store import Archive
    monkeypatch.setattr(keyscan, "extract_keys", _fake_extract(real_db, hit=True))
    archive = Archive(cfg.data_path / "archive.db")
    try:
        store = KeyStore(cfg.data_path / "keys")
        check = auto.CheckResult(sources=[candidate], ready=[], need_key=[candidate])
        rep = auto.run_all(cfg, archive, store, check, auto_key=True)
        assert any("新增" in ln for ln in rep.lines), rep.text()
        assert archive.stats()["messages"] > 0
    finally:
        archive.close()


def test_run_all_without_auto_key_reports_missing_key(cfg, candidate):
    from qqarchive.store import Archive
    archive = Archive(cfg.data_path / "archive.db")
    try:
        store = KeyStore(cfg.data_path / "keys")
        check = auto.CheckResult(sources=[candidate], ready=[], need_key=[candidate])
        rep = auto.run_all(cfg, archive, store, check, auto_key=False)
        assert rep.lines == [] or all("新增" not in ln for ln in rep.lines)
    finally:
        archive.close()


# ---------------------------------------------------------------------------
# 手机转移编排
# ---------------------------------------------------------------------------

def test_pull_phone_media_without_adb_gives_hint(cfg, monkeypatch):
    monkeypatch.setattr("qqarchive.adb.find_adb", lambda configured="": None)
    rep = auto.pull_phone_media(cfg)
    assert rep.files == 0
    assert rep.errors and "adb" in rep.errors[0]
    assert "platform-tools" in rep.errors[0]


def test_pull_phone_media_end_to_end(cfg, monkeypatch):
    """用假手机跑通编排：找目录 → 拉取 → 登记 → 汇总话术。"""
    import test_media_pull as t

    phone = t.FakePhone({
        "/sdcard/Pictures/QQ/a.jpg": b"JPEGDATA" * 10,
        "/sdcard/Tencent/QQfile_recv/报告.pdf": b"PDF" * 100,
    })
    fake = t.FakeAdb(phone)
    monkeypatch.setattr("qqarchive.adb.find_adb", lambda configured="": "fake-adb")
    monkeypatch.setattr("qqarchive.adb.Adb", lambda *a, **k: fake)

    from qqarchive.store import Archive
    archive = Archive(cfg.data_path / "archive.db")
    try:
        rep = auto.pull_phone_media(cfg, archive)
        assert rep.files == 2
        assert rep.bytes == 80 + 300
        assert rep.skipped == 0 and rep.failed == 0
        assert any("手机附件" in line for line in rep.lines)
        assert archive.con.execute(
            "SELECT count(*) AS n FROM media_files WHERE origin='phone'").fetchone()["n"] == 2
    finally:
        archive.close()


def test_pull_phone_media_reports_no_media_dir(cfg, monkeypatch):
    import test_media_pull as t
    phone = t.FakePhone({"/sdcard/其他/文件.txt": b"x"})
    fake = t.FakeAdb(phone)
    monkeypatch.setattr("qqarchive.adb.find_adb", lambda configured="": "fake-adb")
    monkeypatch.setattr("qqarchive.adb.Adb", lambda *a, **k: fake)
    rep = auto.pull_phone_media(cfg)
    assert rep.files == 0
    assert any("没有找到 QQ 的附件目录" in e for e in rep.errors)


def test_pull_phone_media_respects_kind_filter(cfg, monkeypatch):
    import test_media_pull as t
    phone = t.FakePhone({
        "/sdcard/Pictures/QQ/a.jpg": b"JPEGDATA" * 10,
        "/sdcard/Tencent/QQfile_recv/报告.pdf": b"PDF" * 100,
    })
    fake = t.FakeAdb(phone)
    monkeypatch.setattr("qqarchive.adb.find_adb", lambda configured="": "fake-adb")
    monkeypatch.setattr("qqarchive.adb.Adb", lambda *a, **k: fake)
    cfg.phone_kinds = ["image"]
    rep = auto.pull_phone_media(cfg)
    assert rep.files == 1


def test_phone_media_root_under_data_dir(cfg):
    root = auto.phone_media_root(cfg.data_path)
    assert root.parent == cfg.data_path
    assert "手机媒体" in str(root)


# ---------------------------------------------------------------------------
# 实机自检脚本
# ---------------------------------------------------------------------------

def test_verify_script_imports_and_parses(monkeypatch, capsys):
    """自检脚本要能导入且不崩溃（本机没有 adb / QQ 未登录时也应优雅报告）。"""
    import importlib.util
    from pathlib import Path

    script = Path(__file__).resolve().parent.parent / "scripts" / "verify_key_and_phone.py"
    assert script.is_file()
    spec = importlib.util.spec_from_file_location("verify_script", script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    assert callable(module.main)
    # 跳过真实扫描，只检查脚本骨架能跑完并给出结论
    monkeypatch.setattr(sys, "argv", ["verify", "--no-key", "--no-phone"])
    assert module.main() == 0
    out = capsys.readouterr().out
    assert "结论" in out
    assert "自检" in out
