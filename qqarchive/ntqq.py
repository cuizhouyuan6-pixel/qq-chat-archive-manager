"""QQ NT 消息库（nt_msg.db）来源适配器：只读、流式、按会话与时间筛选。

列号含义参考 QQBackup/nt_msg_db_util 的字段研究（group_msg_table / c2c_msg_table）：
40001 消息ID，40002 随机值，40003 序号(msgSeq)，40013 方向(0 他人，1/2 本人，3 系统)，
40020 发送者 UID，40021 群号字符串 / 对方 UID，40030 群号 / 对方 QQ 号，40033 发送者 QQ 号，
40050 发送时间(秒)，40090 群名片，40093 昵称，40800 消息体(Protobuf)，40850 被引用消息序号。

消息体元素字段：45002 元素类型，45101 文本，45402 文件名，45405 大小，45406 MD5，
45411/45412 宽高，47601/47602 表情 ID/文字，47402 引用序号，47413 引用摘要。
元素类型值与 QQ NT 内核一致：1 文本 2 图片 3 文件 4 语音 5 视频 6 表情 7 回复 8 灰条提示
10 卡片 11 商城表情 16 合并转发 21 音视频通话。
这些字段由公开研究整理，未覆盖的类型保留为 unknown，不丢弃。
"""

from __future__ import annotations

import json
import os
import apsw
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterator

from . import protobuf as pb
from .cipherdb import CipherConnection, PlainConnection
from .models import GROUP, PRIVATE, NormalizedMessage
from .sqlcipher import CipherParams, Key, detect_header

TABLES = {GROUP: "group_msg_table", PRIVATE: "c2c_msg_table"}
WANTED = ["40001", "40002", "40003", "40006", "40010", "40011", "40013", "40020", "40021", "40030", "40033",
          "40050", "40090", "40093", "40800", "40850"]

_TYPE_MAP = {1: "text", 2: "image", 3: "file", 4: "voice", 5: "video", 6: "face", 7: "reply", 8: "system",
             10: "card", 11: "mface", 16: "forward", 21: "call"}


def _first(d: dict, field: int, default=None):
    v = d.get(field)
    return v[0] if v else default


def _str(v) -> str:
    if v is None:
        return ""
    if isinstance(v, bytes):
        try:
            return v.decode("utf-8")
        except UnicodeDecodeError:
            return v.decode("utf-8", "replace")
    return str(v)


def _md5(v) -> str:
    if not v:
        return ""
    if isinstance(v, bytes):
        if len(v) == 16:
            return v.hex()
        try:
            s = v.decode("ascii").strip().lower()
            if len(s) == 32 and all(c in "0123456789abcdef" for c in s):
                return s
        except UnicodeDecodeError:
            pass
        return ""
    return str(v).lower()


def _readable(v) -> str | None:
    if not isinstance(v, bytes) or not v or len(v) > 4000:
        return None
    try:
        s = v.decode("utf-8")
    except UnicodeDecodeError:
        return None
    if not s.strip() or any(ord(c) < 32 and c not in "\n\t" for c in s):
        return None
    if s.startswith(("http://", "https://", "/", "\\")) or ":\\" in s[:4]:
        return None
    return s


def _ark_prompt(d: dict) -> str:
    for vals in d.values():
        for v in vals:
            s = _readable(v)
            if s and s.lstrip().startswith("{"):
                try:
                    j = json.loads(s)
                except ValueError:
                    continue
                if isinstance(j, dict):
                    return str(j.get("prompt") or j.get("desc") or "")
    return ""


def _fallback_text(d: dict) -> str:
    for f in (45101, 80900, 47602, 48153, 45923, 47413):
        s = _readable(_first(d, f))
        if s:
            return s
    return ""


def parse_element(buf: bytes) -> dict:
    d = pb.decode(buf)
    code = _first(d, 45002, 0)
    t = _TYPE_MAP.get(code, "unknown")
    e: dict = {"type": t}
    if t == "text":
        e["text"] = _str(_first(d, 45101))
    elif t in ("image", "file", "voice", "video"):
        e["name"] = _str(_first(d, 45402))
        size = _first(d, 45405)
        if size:
            e["size"] = int(size)
        md5 = _md5(_first(d, 45406))
        if md5:
            e["md5"] = md5
        if t == "image":
            w, h = _first(d, 45411), _first(d, 45412)
            if w and h:
                e["w"], e["h"] = int(w), int(h)
        path = _readable(_first(d, 45812)) or _str(_first(d, 45812))
        if path:
            e["src_path"] = path
    elif t == "face":
        fid = _first(d, 47601)
        if fid is not None:
            e["id"] = int(fid)
        e["text"] = _str(_first(d, 47602))
    elif t == "reply":
        seq = _first(d, 47402)
        if seq:
            e["seq"] = int(seq)
        e["text"] = _str(_first(d, 47413))
    elif t == "card":
        e["text"] = _ark_prompt(d) or _fallback_text(d)
    elif t in ("system", "mface", "forward", "call"):
        e["text"] = _fallback_text(d)
    else:
        e["code"] = int(code)
        txt = _fallback_text(d)
        if txt:
            e["text"] = txt
    return e


def parse_body(blob: bytes | None) -> list[dict]:
    if not blob:
        return []
    try:
        top = pb.decode(bytes(blob))
    except (pb.ProtobufError, ValueError, TypeError):
        return [{"type": "unknown", "code": -1, "text": ""}]
    elems = []
    for raw in top.get(40800, []):
        if isinstance(raw, bytes):
            try:
                elems.append(parse_element(raw))
            except (pb.ProtobufError, ValueError, TypeError):
                elems.append({"type": "unknown", "code": -1})
    return elems


def _int(v) -> int | None:
    if v is None or v == "":
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def row_to_message(kind: str, row: dict, owner: str) -> NormalizedMessage:
    direction = _int(row.get("40013")) or 0
    sender_uin = str(_int(row.get("40033")) or "")
    peer = str(_int(row.get("40030")) or "")
    if kind == GROUP and not peer:
        peer = _str(row.get("40021"))
    is_self = (owner and sender_uin == owner) or direction in (1, 2)
    if is_self and (not sender_uin or sender_uin == "0") and owner:
        sender_uin = owner
    if kind == PRIVATE and not is_self and (not sender_uin or sender_uin == "0"):
        sender_uin = peer
    elements = parse_body(row.get("40800"))
    reply_seq = _int(row.get("40850")) or next((e.get("seq") for e in elements if e["type"] == "reply"), None)
    if direction == 3 and elements and all(e["type"] in ("text", "unknown") for e in elements):
        elements = [{"type": "system", "text": "".join(e.get("text", "") for e in elements)}]
    seq = _int(row.get("40003"))
    if not seq and kind == PRIVATE:
        seq = _int(row.get("40006"))
    m = NormalizedMessage(
        owner=owner,
        kind=kind,
        peer=peer,
        time=_int(row.get("40050")) or 0,
        sender_uin=sender_uin,
        sender_uid=_str(row.get("40020")),
        sender_name=_str(row.get("40093")),
        sender_card=_str(row.get("40090")) if kind == GROUP else "",
        is_self=bool(is_self),
        seq=seq or None,
        msg_random=_int(row.get("40002")),
        source_msg_id=str(row.get("40001")) if row.get("40001") is not None else None,
        elements=elements,
        reply_seq=reply_seq or None,
        peer_uid=_str(row.get("40021")) if kind == PRIVATE else "",
    )
    if kind == PRIVATE and not is_self:
        m.peer_name = m.sender_name
    return m


@dataclass
class ConversationInfo:
    kind: str
    peer: str
    count: int
    first_time: int
    last_time: int
    name: str = ""


@dataclass
class Filter:
    start: int | None = None  # 含
    end: int | None = None  # 含
    conversations: set[tuple[str, str]] | None = None  # {(kind, peer)}；None 表示全部

    def accepts(self, kind: str, peer: str, t: int) -> bool:
        if self.start is not None and t < self.start:
            return False
        if self.end is not None and t > self.end:
            return False
        if self.conversations is not None and (kind, peer) not in self.conversations:
            return False
        return True


class NtqqSource:
    """打开一个 nt_msg.db（加密或已解密），列出会话、流式读取消息。"""

    source_kind = "ntqq"

    def __init__(self, path: str | os.PathLike, owner: str, key: Key | str | None = None,
                 params: CipherParams | None = None):
        self.path = Path(path)
        self.owner = str(owner or "")
        header = detect_header(self.path)
        if header["kind"] == "sqlite":
            self._conn = PlainConnection(self.path)
        else:
            if key is None:
                raise ValueError("该数据库已加密，需要密钥")
            self._conn = CipherConnection.open(self.path, key, params)
        self.conn = self._conn.conn
        self.params = self._conn.params
        try:self._columns = {k: self._table_columns(t) for k, t in TABLES.items()}
        except BaseException:self.close();raise
        self.table_reader=None
        db=getattr(self._conn,'db',None)
        if db and db.params.prefix==1024 and db.page_count>0x40000000//db.params.page_size:
            from .btree import TableReader
            self.table_reader=TableReader(db,self.conn)

    def set_cancel(self,cancel):
        if cancel is not None:self.conn.set_progress_handler(cancel.is_set,1000)
        if self.table_reader:self.table_reader.cancel=cancel

    def _table_columns(self, table: str) -> set[str]:
        return {r[1] for r in self.conn.execute(f"PRAGMA table_info({table})")}

    def _order_column(self, table: str) -> str:
        try:
            next(iter(self.conn.execute(f"SELECT rowid FROM {table} LIMIT 1")), None)
            return "rowid"
        except apsw.SQLError as exc:  # WITHOUT ROWID 表退回按消息 ID 分页
            if 'no such column: rowid' not in str(exc):raise
            return '"40001"'

    def has_table(self, kind: str) -> bool:
        return bool(self._columns.get(kind))

    def close(self):
        self._conn.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def conversations(self) -> list[ConversationInfo]:
        if self.table_reader:return self._direct_conversations()
        out = []
        for kind, table in TABLES.items():
            cols = self._columns[kind]
            if not cols or "40030" not in cols or "40050" not in cols:
                continue
            name_col = '"40093"' if kind == PRIVATE and "40093" in cols else "''"
            dir_col = '"40013"' if "40013" in cols else "0"
            q = (f'SELECT "40030", count(*), min("40050"), max("40050"), '
                 f"max(CASE WHEN {dir_col} NOT IN (1,2) THEN {name_col} ELSE '' END) "
                 f'FROM {table} GROUP BY "40030"')
            for peer, cnt, t0, t1, name in self.conn.execute(q):
                if peer is None:
                    continue
                out.append(ConversationInfo(kind, str(peer), cnt, t0 or 0, t1 or 0, name or ""))
        out.sort(key=lambda c: -(c.last_time or 0))
        return out

    def count(self, flt: Filter | None = None) -> int:
        if self.table_reader:
            total=0
            for kind,table in TABLES.items():
                wanted={'40030','40050'} if flt else set()
                for _,r in self.table_reader.rows(table,wanted,count_only=not flt):
                    if not flt or flt.accepts(kind,str(r.get('40030') or ''),_int(r.get('40050')) or 0):total+=1
            return total
        total = 0
        for kind, table in TABLES.items():
            if not self.has_table(kind):
                continue
            where, args = self._where(kind, flt)
            total += next(self.conn.execute(f"SELECT count(*) FROM {table}{where}", args))[0]
        return total

    def _where(self, kind: str, flt: Filter | None):
        conds, args = [], []
        if flt:
            if flt.start is not None:
                conds.append('"40050" >= ?')
                args.append(int(flt.start))
            if flt.end is not None:
                conds.append('"40050" <= ?')
                args.append(int(flt.end))
            if flt.conversations is not None:
                peers = [p for k, p in flt.conversations if k == kind]
                if not peers:
                    conds.append("0")
                else:
                    conds.append('"40030" IN (%s)' % ",".join("?" * len(peers)))
                    args.extend(int(p) if p.isdigit() else p for p in peers)
        return (" WHERE " + " AND ".join(conds)) if conds else "", args

    def messages(self, flt: Filter | None = None, batch: int = 2000,
                 progress: Callable[[int], None] | None = None) -> Iterator[NormalizedMessage]:
        """按 rowid 分页流式读取，内存占用与批大小成正比。"""
        if self.table_reader:
            for kind,table in TABLES.items():
                for _,r in self.table_reader.rows(table,set(WANTED)&self._columns[kind]):
                    peer=str(r.get('40030') or r.get('40021') or '');when=_int(r.get('40050')) or 0
                    if flt is None or flt.accepts(kind,peer,when):yield row_to_message(kind,r,self.owner)
            return
        done = 0
        for kind, table in TABLES.items():
            cols = self._columns[kind]
            if not cols:
                continue
            select = [c for c in WANTED if c in cols]
            col_sql = ", ".join(f'"{c}"' for c in select)
            where, args = self._where(kind, flt)
            last = -1 << 63
            joiner = " AND " if where else " WHERE "
            order = self._order_column(table)
            while True:
                q = (f"SELECT {order}, {col_sql} FROM {table}{where}{joiner}{order} > ? "
                     f"ORDER BY {order} LIMIT {batch}")
                rows = list(self.conn.execute(q, [*args, last]))
                if not rows:
                    break
                for r in rows:
                    last = r[0]
                    row = dict(zip(select, r[1:]))
                    m = row_to_message(kind, row, self.owner)
                    if flt is None or flt.accepts(kind, m.peer, m.time):
                        yield m
                done += len(rows)
                if progress:
                    progress(done)

    def _direct_conversations(self):
        out=[]
        for kind,table in TABLES.items():
            groups={}
            for _,r in self.table_reader.rows(table,{'40030','40050','40093','40013'}):
                peer=r.get('40030')
                if peer is None:continue
                peer=str(peer);when=_int(r.get('40050')) or 0
                value=groups.setdefault(peer,[0,when,when,'']);value[0]+=1;value[1]=min(value[1],when);value[2]=max(value[2],when)
                if kind==PRIVATE and _int(r.get('40013')) not in (1,2):value[3]=max(value[3],_str(r.get('40093')))
            out.extend(ConversationInfo(kind,peer,*values) for peer,values in groups.items())
        out.sort(key=lambda c:-(c.last_time or 0));return out


def owner_from_path(path: str | os.PathLike) -> str:
    """从 ...\\Tencent Files\\<QQ号>\\nt_qq\\nt_db\\nt_msg.db 推断归档账号。"""
    parts = Path(path).parts
    for i, part in enumerate(parts):
        if part.lower() == "nt_qq" and i > 0 and parts[i - 1].isdigit():
            return parts[i - 1]
    for part in reversed(parts[:-1]):
        if part.isdigit() and 5 <= len(part) <= 12:
            return part
    return ""
