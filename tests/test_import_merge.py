import threading

import pytest

from ntqq_fixture import NTQQ_KEY, group_row, make_ntqq_db, sample_rows, text_elem
from qqarchive.discovery import SourceCandidate, discover
from qqarchive.importer import ImportCancelled, Importer, ImportRequest
from qqarchive.keystore import KeyStore
from qqarchive.models import GROUP, PRIVATE, NormalizedMessage
from qqarchive.ntqq import Filter, NtqqSource
from qqarchive.sources import KeyRequired, import_candidate
from qqarchive.sqlcipher import Key
from qqarchive.store import Archive

OWNER = "10001"


def msg(seq, t, text, sender="30003", peer="123456", kind=GROUP, **kw):
    return NormalizedMessage(owner=OWNER, kind=kind, peer=peer, time=t, sender_uin=sender, seq=seq,
                             elements=[{"type": "text", "text": text}], **kw)


def run(archive, msgs, kind="test"):
    return Importer(archive).run(ImportRequest(kind, "label-" + kind, owner=OWNER), msgs)


@pytest.fixture
def archive(tmp_path):
    a = Archive(tmp_path / "data" / "archive.db")
    yield a
    a.close()


def test_overlap_shows_once(archive):
    run(archive, [msg(1, 100, "a"), msg(2, 200, "b"), msg(3, 300, "c")])
    s = run(archive, [msg(2, 200, "b"), msg(3, 300, "c"), msg(4, 400, "d")], kind="other")
    assert (s.inserted, s.duplicates, s.conflicts) == (1, 2, 0)
    conv = archive.find_conversation(OWNER, GROUP, "123456")
    assert conv["msg_count"] == 4
    assert [m["text"] for m in archive.latest(conv["id"])] == ["a", "b", "c", "d"]
    m2 = archive.latest(conv["id"])[1]
    assert len(archive.message_sources(m2["id"])) == 2


def test_conflict_keeps_variant(archive):
    run(archive, [msg(1, 100, "原文")])
    s = run(archive, [msg(1, 100, "改过的")], kind="other")
    assert s.conflicts == 1
    conv = archive.find_conversation(OWNER, GROUP, "123456")
    m = archive.latest(conv["id"])[0]
    assert m["text"] == "原文" and m["conflict"] == 1
    v = archive.variants(m["id"])
    assert len(v) == 1 and v[0]["text"] == "改过的"
    # 再导入同一个变体：不重复记录
    s = run(archive, [msg(1, 100, "改过的")], kind="third")
    assert s.conflicts == 0 and s.duplicates == 1
    assert len(archive.variants(m["id"])) == 1


def test_private_key_uses_seq_and_time(archive):
    run(archive, [msg(5, 100, "x", kind=PRIVATE, peer="20002"), msg(5, 101, "y", kind=PRIVATE, peer="20002")])
    conv = archive.find_conversation(OWNER, PRIVATE, "20002")
    assert conv["msg_count"] == 2


def test_no_seq_marks_suspect_not_merge(archive):
    run(archive, [msg(7, 100, "hello")])
    m = NormalizedMessage(owner=OWNER, kind=GROUP, peer="123456", time=100, sender_uin="30003",
                          source_msg_id="abc", elements=[{"type": "text", "text": "hello"}])
    s = run(archive, [m], kind="json")
    assert s.inserted == 1 and s.suspects == 1
    conv = archive.find_conversation(OWNER, GROUP, "123456")
    rows = archive.latest(conv["id"])
    assert len(rows) == 2 and all(r["suspect"] for r in rows)
    # 同一来源重复导入：幂等
    s = run(archive, [m], kind="json")
    assert s.inserted == 0 and s.duplicates == 1


def test_accounts_are_separate(archive):
    run(archive, [msg(1, 100, "a")])
    other = NormalizedMessage(owner="99999", kind=GROUP, peer="123456", time=100, sender_uin="1", seq=1,
                              elements=[{"type": "text", "text": "a"}])
    Importer(archive).run(ImportRequest("t", "l", owner="99999"), [other])
    tree = archive.conversation_tree()
    assert set(tree) == {OWNER, "99999"}
    assert len(tree[OWNER]["group"]) == 1 and len(tree["99999"]["group"]) == 1


def test_failure_rolls_back_whole_batch(archive):
    def gen():
        yield msg(1, 100, "a")
        yield msg(2, 200, "b")
        raise RuntimeError("中途出错")

    with pytest.raises(RuntimeError):
        run(archive, gen())
    assert archive.count_messages() == 0
    statuses = [b["status"] for b in archive.batches()]
    assert statuses == ["failed"]


def test_cancel_rolls_back(archive):
    ev = threading.Event()

    def gen():
        yield msg(1, 100, "a")
        ev.set()
        yield msg(2, 200, "b")

    with pytest.raises(ImportCancelled):
        Importer(archive).run(ImportRequest("t", "l", owner=OWNER), gen(), cancel=ev)
    assert archive.count_messages() == 0


def test_coverage_and_seq_gaps(archive):
    run(archive, [msg(1, 100, "a"), msg(2, 200, "b")])
    run(archive, [msg(10, 5000, "x"), msg(11, 6000, "y")], kind="b")
    conv = archive.find_conversation(OWNER, GROUP, "123456")
    cov = archive.coverage(conv["id"])
    assert cov["intervals"] == [[100, 200], [5000, 6000]]
    assert cov["gaps"] == [(200, 5000)]
    assert cov["seq"]["missing"] == 7
    assert cov["seq"]["largest_gaps"][0]["prev"] == 2


def test_search_combinations(archive):
    run(archive, [
        msg(1, 100, "今天天气不错", sender="1", sender_name="张三"),
        msg(2, 200, "明天下雨", sender="2", sender_name="李四"),
        msg(3, 300, "天气预报说今天有雨", sender="2", sender_name="李四", sender_card="四哥"),
    ])
    from qqarchive.store import SearchQuery
    q = lambda **kw: [m["seq"] for m in archive.search(SearchQuery(**kw))]  # noqa: E731
    assert sorted(q(keyword="雨")) == [2, 3]  # 单字
    assert sorted(q(keyword="天气")) == [1, 3]  # 两字
    assert q(keyword="天气不错") == [1]  # FTS
    assert q(keyword="天气", nickname="四哥") == [3]
    assert sorted(q(uin="2")) == [2, 3]
    assert q(uin="2", start=250) == [3]
    assert q(keyword="100%") == []


def test_ntqq_end_to_end(tmp_path, archive):
    g, c = sample_rows()
    db = make_ntqq_db(tmp_path / "Tencent Files" / OWNER / "nt_qq" / "nt_db" / "nt_msg.db", g, c)
    with NtqqSource(db, OWNER, Key.parse(NTQQ_KEY)) as src:
        convs = {(x.kind, x.peer): x for x in src.conversations()}
        assert convs[(GROUP, "123456")].count == 5
        assert convs[(PRIVATE, "20002")].name == "王五"
        msgs = list(src.messages())
    by_seq = {m.seq: m for m in msgs if m.kind == GROUP}
    assert by_seq[102].is_self and by_seq[102].text == "你好呀[微笑]"
    assert by_seq[101].sender_card == "三哥"
    img = by_seq[103].elements[0]
    assert img["type"] == "image" and img["md5"] == "0123456789abcdef0123456789abcdef" and img["w"] == 640
    assert by_seq[104].reply_seq == 101
    assert by_seq[105].elements[0] == {"type": "file", "name": "报告.pdf", "size": 204800}

    ks = KeyStore(tmp_path / "keys")
    cand = discover([tmp_path / "Tencent Files"], [], archive, ks)[0]
    assert cand.kind == "pc" and cand.owner == OWNER and cand.status == "new" and cand.key_status == "none"
    with pytest.raises(KeyRequired):
        import_candidate(archive, cand, ks, work_dir=tmp_path / "work")
    ks.put(OWNER, Key.parse(NTQQ_KEY), None, remember=False)
    s = import_candidate(archive, cand, ks, work_dir=tmp_path / "work")
    assert s.inserted == 7
    assert not (tmp_path / "work").exists()  # 默认原址读取，不创建整库快照
    cand = discover([tmp_path / "Tencent Files"], [], archive, ks)[0]
    assert cand.status == "imported" and cand.key_status == "session"
    s = import_candidate(archive, cand, ks, work_dir=tmp_path / "work")
    assert s.inserted == 0 and s.duplicates == 7


def test_time_and_conversation_filter(tmp_path, archive):
    g, c = sample_rows()
    db = make_ntqq_db(tmp_path / "nt_msg.db", g, c)
    cand = SourceCandidate(str(db), "backup", OWNER, 0, 0, "ntqq")
    ks = KeyStore(tmp_path / "keys")
    ks.put(OWNER, Key.parse(NTQQ_KEY), None, remember=False)
    flt = Filter(start=1_700_000_000, end=1_700_000_100, conversations={(GROUP, "123456")})
    s = import_candidate(archive, cand, ks, flt=flt, work_dir=tmp_path / "w")
    assert s.inserted == 2
    assert {c["peer"] for c in archive.conversations()} == {"123456"}


def test_plain_decrypted_db(tmp_path, archive):
    g, c = sample_rows()
    db = make_ntqq_db(tmp_path / "plain" / "nt_msg_plain.db", g, c, plain=True)
    cands = discover([], [tmp_path / "plain"], archive)
    assert len(cands) == 1 and cands[0].kind == "plain" and cands[0].key_status == "plain"
    s = import_candidate(archive, cands[0], None, owner=OWNER, work_dir=tmp_path / "w")
    assert s.inserted == 7


def test_keystore_dpapi_roundtrip(tmp_path):
    ks = KeyStore(tmp_path / "keys")
    ks.put("123", Key.parse("abcdefgh12345678"), None, remember=True)
    text = (tmp_path / "keys" / "keys.json").read_text(encoding="utf-8")
    assert "abcdefgh12345678" not in text
    fresh = KeyStore(tmp_path / "keys")
    assert fresh.get("123")[0].secret == b"abcdefgh12345678"
    fresh.forget("123")
    assert KeyStore(tmp_path / "keys").get("123") is None


def test_schema_upgrade_backs_up_first(tmp_path, monkeypatch):
    import qqarchive.store as store
    path = tmp_path / "d" / "archive.db"
    a = Archive(path)
    with a.con:
        a.con.execute("UPDATE settings SET value='1' WHERE key='schema'")
    a.close()
    called = []
    monkeypatch.setitem(store.MIGRATIONS, 1, lambda con: called.append(1))
    a = Archive(path)
    assert called == [1] and a.setting("schema") == "2"
    assert len(list((tmp_path / "d" / "backups").glob("archive-*.db"))) == 1
    a.close()


def test_refuses_newer_schema(tmp_path):
    from qqarchive.store import SchemaError
    path = tmp_path / "archive.db"
    a = Archive(path)
    with a.con:
        a.con.execute("UPDATE settings SET value='99' WHERE key='schema'")
    a.close()
    with pytest.raises(SchemaError):
        Archive(path)
