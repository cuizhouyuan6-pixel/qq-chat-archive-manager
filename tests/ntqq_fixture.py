"""用真实 SQLCipher（sqlcipher3）生成带 QQ NT 文件头的合成 nt_msg.db，供测试使用。

数据全部为虚构，不含任何真实聊天记录。
"""

from __future__ import annotations

import os
from pathlib import Path

from qqarchive.protobuf import encode

NTQQ_KEY = "#8testtesttest@J"  # 16 位口令，格式模仿 QQ NT，纯测试值
GROUP_COLS = ["40001", "40002", "40003", "40010", "40011", "40012", "40013", "40020", "40021", "40027",
              "40030", "40033", "40050", "40058", "40090", "40093", "40800", "40850"]
C2C_COLS = ["40001", "40002", "40003", "40010", "40011", "40012", "40013", "40020", "40021", "40027",
            "40030", "40033", "40050", "40058", "40090", "40093", "40800", "40850"]


def text_elem(text: str) -> dict:
    return {45001: 1, 45002: 1, 45101: text}


def image_elem(name: str, md5: bytes, w=640, h=480, size=12345) -> dict:
    return {45001: 2, 45002: 2, 45402: name, 45405: size, 45406: md5, 45411: w, 45412: h}


def file_elem(name: str, size: int, md5: bytes = b"") -> dict:
    d = {45001: 3, 45002: 3, 45402: name, 45405: size}
    if md5:
        d[45406] = md5
    return d


def face_elem(face_id: int, text: str) -> dict:
    return {45001: 4, 45002: 6, 47601: face_id, 47602: text}


def reply_elem(seq: int, summary: str = "") -> dict:
    return {45001: 5, 45002: 7, 47402: seq, 47413: summary}


def body(*elems: dict) -> bytes:
    return encode({40800: list(elems)})


def group_row(msg_id, seq, group, sender_uin, t, elems, *, sender_uid="u_x", card="", nick="", direction=0,
              rand=0):
    return {
        "40001": msg_id, "40002": rand or msg_id * 7, "40003": seq, "40010": 2, "40011": 2, "40012": 0,
        "40013": direction, "40020": sender_uid, "40021": str(group), "40027": 1000 + group % 97,
        "40030": group, "40033": sender_uin, "40050": t, "40058": t - t % 86400, "40090": card,
        "40093": nick, "40800": body(*elems), "40850": 0,
    }


def c2c_row(msg_id, seq, peer, sender_uin, t, elems, *, peer_uid="u_peer", nick="", direction=0):
    return {
        "40001": msg_id, "40002": msg_id * 11, "40003": seq, "40010": 1, "40011": 2, "40012": 0,
        "40013": direction, "40020": "u_self" if direction else peer_uid, "40021": peer_uid,
        "40027": 2000 + peer % 97, "40030": peer, "40033": sender_uin, "40050": t, "40058": t - t % 86400,
        "40090": "", "40093": nick, "40800": body(*elems), "40850": 0,
    }


def _create_tables(con):
    gcols = ", ".join(f'"{c}"' for c in GROUP_COLS)
    ccols = ", ".join(f'"{c}"' for c in C2C_COLS)
    con.execute(f"CREATE TABLE group_msg_table ({gcols}, PRIMARY KEY(\"40001\"))")
    con.execute(f"CREATE TABLE c2c_msg_table ({ccols}, PRIMARY KEY(\"40001\"))")


def _insert(con, table, cols, rows):
    q = f'INSERT INTO {table} ({", ".join(chr(34) + c + chr(34) for c in cols)}) VALUES ({", ".join("?" * len(cols))})'
    for r in rows:
        con.execute(q, [r.get(c) for c in cols])


def ntqq_header(rand: str = "6tPaJ9GP") -> bytes:
    head = b"SQLite header 3\x00" + b"\x00" * 16 + b"QQ_NT DB\x00" + rand.encode() + b"\x00"
    return head + b"\x00" * (1024 - len(head))


def make_ntqq_db(path: Path, group_rows=(), c2c_rows=(), *, key: str = NTQQ_KEY, header: bool = True,
                 wal_rows=None, plain: bool = False) -> Path:
    """生成合成库。wal_rows 给出时，保留一部分未检查点的 WAL 帧，用来测试 WAL 读取。"""
    import sqlcipher3

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".sqlcipher")
    for p in (tmp, Path(str(tmp) + "-wal"), Path(str(tmp) + "-shm")):
        if p.exists():
            p.unlink()
    con = sqlcipher3.connect(str(tmp), isolation_level=None)
    if not plain:
        con.execute("PRAGMA cipher_page_size = 4096")
        con.execute(f"PRAGMA key = '{key}'")
        con.execute("PRAGMA kdf_iter = 4000")
        con.execute("PRAGMA cipher_hmac_algorithm = HMAC_SHA1")
        con.execute("PRAGMA cipher_kdf_algorithm = PBKDF2_HMAC_SHA512")
    _create_tables(con)
    con.execute("BEGIN")
    _insert(con, "group_msg_table", GROUP_COLS, group_rows)
    _insert(con, "c2c_msg_table", C2C_COLS, c2c_rows)
    con.execute("COMMIT")
    wal_bytes = None
    if wal_rows:
        con.execute("PRAGMA journal_mode = WAL")
        con.execute("PRAGMA wal_autocheckpoint = 0")
        con.execute("BEGIN")
        _insert(con, "group_msg_table", GROUP_COLS, wal_rows)
        con.execute("COMMIT")
        wal_bytes = Path(str(tmp) + "-wal").read_bytes()
        main_bytes = tmp.read_bytes()
    con.close()
    if wal_rows:
        data = main_bytes
    else:
        data = tmp.read_bytes()
    for p in (tmp, Path(str(tmp) + "-wal"), Path(str(tmp) + "-shm")):
        if p.exists():
            p.unlink()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes((ntqq_header() if header and not plain else b"") + data)
    wal_path = Path(str(path) + "-wal")
    if wal_bytes:
        wal_path.write_bytes(wal_bytes)
    elif wal_path.exists():
        os.remove(wal_path)
    return path


def sample_rows(group=123456, owner=10001, peer=20002, base_time=1_700_000_000):
    """一份小而全的样例：文本、图片、文件、表情、回复、本人消息与私聊。"""
    md5 = bytes.fromhex("0123456789abcdef0123456789abcdef")
    g = [
        group_row(1, 101, group, 30003, base_time, [text_elem("大家好")], nick="张三", card="三哥"),
        group_row(2, 102, group, owner, base_time + 60, [text_elem("你好呀"), face_elem(14, "/微笑")],
                  nick="我自己", direction=1, sender_uid="u_self"),
        group_row(3, 103, group, 30003, base_time + 120,
                  [image_elem("0123456789ABCDEF0123456789ABCDEF.jpg", md5)], nick="张三"),
        group_row(4, 104, group, 40004, base_time + 86400, [reply_elem(101, "大家好"), text_elem("收到")],
                  nick="李四"),
        group_row(5, 105, group, 40004, base_time + 86460, [file_elem("报告.pdf", 204800)], nick="李四"),
    ]
    c = [
        c2c_row(11, 1, peer, peer, base_time + 30, [text_elem("在吗")], nick="王五"),
        c2c_row(12, 2, peer, owner, base_time + 90, [text_elem("在的")], direction=1),
    ]
    return g, c
