import pytest

from ntqq_fixture import NTQQ_KEY, group_row, make_ntqq_db, sample_rows, text_elem
from qqarchive.cipherdb import CipherConnection
from qqarchive.sqlcipher import CipherError, Key, detect_header, verify_key


def test_reads_real_sqlcipher_output_with_ntqq_header(tmp_path):
    g, c = sample_rows()
    db = make_ntqq_db(tmp_path / "nt_msg.db", g, c)
    info = detect_header(db)
    assert info["kind"] == "ntqq" and info["rand"] == "6tPaJ9GP"
    with CipherConnection.open(db, NTQQ_KEY) as cc:
        assert cc.params.prefix == 1024 and cc.params.hmac_algo == "sha1"
        rows = list(cc.conn.execute('SELECT "40003" FROM group_msg_table ORDER BY 1'))
        assert [r[0] for r in rows] == [101, 102, 103, 104, 105]
        assert next(cc.conn.execute("SELECT count(*) FROM c2c_msg_table"))[0] == 2


def test_without_header(tmp_path):
    g, c = sample_rows()
    db = make_ntqq_db(tmp_path / "x.db", g, c, header=False)
    with CipherConnection.open(db, NTQQ_KEY) as cc:
        assert cc.params.prefix == 0
        assert next(cc.conn.execute("SELECT count(*) FROM group_msg_table"))[0] == 5


def test_wrong_key_rejected(tmp_path):
    g, c = sample_rows()
    db = make_ntqq_db(tmp_path / "nt_msg.db", g, c)
    assert verify_key(db, Key.parse("not-the-right-k")) is None
    with pytest.raises(CipherError):
        CipherConnection.open(db, "not-the-right-k")


def test_uncheckpointed_wal_frames_are_read(tmp_path):
    g, c = sample_rows()
    extra = [group_row(100 + i, 200 + i, 123456, 30003, 1_700_100_000 + i, [text_elem(f"WAL{i}")]) for i in range(30)]
    db = make_ntqq_db(tmp_path / "nt_msg.db", g, c, wal_rows=extra)
    with CipherConnection.open(db, NTQQ_KEY) as cc:
        assert cc.db.wal_frames > 0
        assert next(cc.conn.execute("SELECT count(*) FROM group_msg_table"))[0] == 35


def test_tampered_page_fails_closed(tmp_path):
    g, c = sample_rows()
    db = make_ntqq_db(tmp_path / "nt_msg.db", g, c)
    data = bytearray(db.read_bytes())
    data[1024 + 4096 + 100] ^= 0xFF  # 改坏第 2 页
    db.write_bytes(bytes(data))
    with CipherConnection.open(db, NTQQ_KEY) as cc:
        with pytest.raises(Exception):
            list(cc.conn.execute('SELECT * FROM group_msg_table'))


def test_source_file_is_not_modified(tmp_path):
    g, c = sample_rows()
    db = make_ntqq_db(tmp_path / "nt_msg.db", g, c)
    before = db.read_bytes()
    with CipherConnection.open(db, NTQQ_KEY) as cc:
        list(cc.conn.execute("SELECT * FROM group_msg_table"))
    assert db.read_bytes() == before


def test_key_repr_hides_secret():
    k = Key.parse("secret-password!")
    assert "secret" not in repr(k)
    raw = Key.parse("x'" + "ab" * 32 + "'")
    assert raw.raw and len(raw.secret) == 32
