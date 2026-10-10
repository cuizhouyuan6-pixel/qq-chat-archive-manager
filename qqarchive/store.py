"""归档库（schema v2）：会话、批次、消息、来源追溯、冲突变体、覆盖范围、附件、全文索引。

原则：只追加，不删除聊天数据；任何“去重”都通过合并键与隐藏标记完成。
"""

from __future__ import annotations

import os
import re
import sqlite3
import time
import threading
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .models import GROUP, KIND_LABEL, loads_elements
from .version import SCHEMA_VERSION

_sender_cache=OrderedDict()
_sender_cache_lock=threading.Lock()

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS profiles(
 owner TEXT NOT NULL, kind TEXT NOT NULL, peer TEXT NOT NULL, uid TEXT NOT NULL DEFAULT '',
 name TEXT NOT NULL DEFAULT '', avatar_path TEXT NOT NULL DEFAULT '', source TEXT NOT NULL DEFAULT '',
 PRIMARY KEY(owner,kind,peer)
);
CREATE INDEX IF NOT EXISTS ix_profile_uid ON profiles(owner,kind,uid);
CREATE TABLE IF NOT EXISTS phone_runs(
 id INTEGER PRIMARY KEY,started_at INTEGER NOT NULL,finished_at INTEGER NOT NULL,
 status TEXT NOT NULL,files INTEGER NOT NULL DEFAULT 0,bytes INTEGER NOT NULL DEFAULT 0,
 skipped INTEGER NOT NULL DEFAULT 0,failed INTEGER NOT NULL DEFAULT 0,summary TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS accounts(
  uin TEXT PRIMARY KEY,
  nickname TEXT NOT NULL DEFAULT '',
  uid TEXT NOT NULL DEFAULT '',
  source TEXT NOT NULL DEFAULT '',
  last_import_at INTEGER,
  created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS conversations(
  id INTEGER PRIMARY KEY,
  owner TEXT NOT NULL,
  kind TEXT NOT NULL CHECK(kind IN ('group','private')),
  peer TEXT NOT NULL,
  name TEXT NOT NULL DEFAULT '',
  custom_name INTEGER NOT NULL DEFAULT 0,
  peer_uid TEXT NOT NULL DEFAULT '',
  msg_count INTEGER NOT NULL DEFAULT 0,
  conflict_count INTEGER NOT NULL DEFAULT 0,
  first_time INTEGER,
  last_time INTEGER,
  created_at INTEGER NOT NULL,
  UNIQUE(owner, kind, peer)
);
CREATE TABLE IF NOT EXISTS batches(
  id INTEGER PRIMARY KEY,
  source_kind TEXT NOT NULL,
  source_label TEXT NOT NULL,
  source_fingerprint TEXT NOT NULL DEFAULT '',
  owner TEXT NOT NULL DEFAULT '',
  range_start INTEGER,
  range_end INTEGER,
  params TEXT NOT NULL DEFAULT '{}',
  status TEXT NOT NULL,
  started_at INTEGER NOT NULL,
  finished_at INTEGER,
  read_count INTEGER NOT NULL DEFAULT 0,
  inserted INTEGER NOT NULL DEFAULT 0,
  duplicates INTEGER NOT NULL DEFAULT 0,
  conflicts INTEGER NOT NULL DEFAULT 0,
  suspects INTEGER NOT NULL DEFAULT 0,
  skipped INTEGER NOT NULL DEFAULT 0,
  note TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS messages(
  id INTEGER PRIMARY KEY,
  conversation_id INTEGER NOT NULL REFERENCES conversations(id),
  merge_key TEXT NOT NULL,
  exact_key INTEGER NOT NULL DEFAULT 1,
  time INTEGER NOT NULL,
  seq INTEGER,
  msg_random INTEGER,
  source_msg_id TEXT,
  sender_uin TEXT NOT NULL DEFAULT '',
  sender_uid TEXT NOT NULL DEFAULT '',
  sender_name TEXT NOT NULL DEFAULT '',
  sender_card TEXT NOT NULL DEFAULT '',
  is_self INTEGER NOT NULL DEFAULT 0,
  msg_type TEXT NOT NULL DEFAULT 'text',
  text TEXT NOT NULL DEFAULT '',
  elements TEXT NOT NULL DEFAULT '[]',
  reply_seq INTEGER,
  content_hash TEXT NOT NULL,
  fingerprint TEXT NOT NULL,
  batch_id INTEGER NOT NULL REFERENCES batches(id),
  hidden INTEGER NOT NULL DEFAULT 0,
  conflict INTEGER NOT NULL DEFAULT 0,
  suspect INTEGER NOT NULL DEFAULT 0
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_msg_key ON messages(conversation_id, merge_key) WHERE hidden = 0;
CREATE INDEX IF NOT EXISTS ix_msg_conv_time ON messages(conversation_id, hidden, time, id);
CREATE INDEX IF NOT EXISTS ix_msg_fp ON messages(conversation_id, fingerprint);
CREATE INDEX IF NOT EXISTS ix_msg_sender ON messages(sender_uin);
CREATE INDEX IF NOT EXISTS ix_msg_time ON messages(time);
CREATE INDEX IF NOT EXISTS ix_msg_seq ON messages(conversation_id, seq);
CREATE TABLE IF NOT EXISTS message_sources(
  message_id INTEGER NOT NULL,
  batch_id INTEGER NOT NULL,
  source_msg_id TEXT,
  PRIMARY KEY(message_id, batch_id)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS message_variants(
  id INTEGER PRIMARY KEY,
  message_id INTEGER NOT NULL REFERENCES messages(id),
  batch_id INTEGER NOT NULL,
  content_hash TEXT NOT NULL,
  sender_uin TEXT NOT NULL DEFAULT '',
  sender_name TEXT NOT NULL DEFAULT '',
  text TEXT NOT NULL DEFAULT '',
  elements TEXT NOT NULL DEFAULT '[]',
  diff TEXT NOT NULL DEFAULT '',
  created_at INTEGER NOT NULL,
  UNIQUE(message_id, content_hash)
);
CREATE TABLE IF NOT EXISTS coverage(
  conversation_id INTEGER NOT NULL,
  batch_id INTEGER NOT NULL,
  start_time INTEGER NOT NULL,
  end_time INTEGER NOT NULL,
  count INTEGER NOT NULL,
  PRIMARY KEY(conversation_id, batch_id)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS media_files(
  id INTEGER PRIMARY KEY,
  origin TEXT NOT NULL,
  device TEXT NOT NULL DEFAULT '',
  device_path TEXT NOT NULL,
  local_path TEXT NOT NULL,
  name TEXT NOT NULL,
  kind TEXT NOT NULL DEFAULT 'other',
  size INTEGER,
  mtime INTEGER,
  sha256 TEXT,
  md5 TEXT,
  status TEXT NOT NULL DEFAULT 'pending',
  verified TEXT NOT NULL DEFAULT '',
  pulled_at INTEGER,
  UNIQUE(origin, device, device_path)
);
CREATE INDEX IF NOT EXISTS ix_media_md5 ON media_files(md5);
CREATE INDEX IF NOT EXISTS ix_media_name ON media_files(name COLLATE NOCASE);
CREATE TABLE IF NOT EXISTS message_media(
  message_id INTEGER NOT NULL,
  elem_index INTEGER NOT NULL,
  media_id INTEGER,
  status TEXT NOT NULL,
  PRIMARY KEY(message_id, elem_index)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS ix_message_media_media ON message_media(media_id);
CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
  text, content='messages', content_rowid='id', tokenize='trigram'
);
CREATE TRIGGER IF NOT EXISTS trg_msg_ai AFTER INSERT ON messages BEGIN
  INSERT INTO messages_fts(rowid, text) VALUES (new.id, new.text);
END;
CREATE TRIGGER IF NOT EXISTS trg_msg_ad AFTER DELETE ON messages BEGIN
  INSERT INTO messages_fts(messages_fts, rowid, text) VALUES ('delete', old.id, old.text);
END;
CREATE TRIGGER IF NOT EXISTS trg_msg_au AFTER UPDATE OF text ON messages BEGIN
  INSERT INTO messages_fts(messages_fts, rowid, text) VALUES ('delete', old.id, old.text);
  INSERT INTO messages_fts(rowid, text) VALUES (new.id, new.text);
END;
"""

# Sender filters inside one conversation (search detail pages, also sender+time).
# Without it a rare sender's page walks the whole conversation via
# ix_msg_conv_time. scripts/bench_search.py, 1,000,000 synthetic messages,
# largest chat 164,546, warm OS cache, median of 5 (2026-10-10): rare sender in
# the largest chat 0.058 s -> 0.001 s; other scenarios unchanged within noise.
# Import +2.4..8.3 % in four interleaved pairs over two sessions (25.3->26.7,
# 25.7->26.3, 34.7->37.6, 34.2->35.4 s), file +4.6 %, building it on that
# existing archive 0.85 s. New archives create it with the schema; existing ones
# get it from a cancellable background job (MainWindow.ensure_search_index),
# never from the schema script that runs on the GUI thread at every open.
# No schema version change: older versions open and maintain the extra index.
SEARCH_INDEX='ix_msg_conv_sender_time'
SEARCH_INDEX_SQL='CREATE INDEX IF NOT EXISTS ix_msg_conv_sender_time ON messages(conversation_id, sender_uin, hidden, time)'

# 旧版本 → 新版本的升级步骤：{旧版本号: 函数(con)}。升级前自动备份数据库文件。
MIGRATIONS: dict[int, Callable[[sqlite3.Connection], None]] = {}


class SchemaError(Exception):
    pass


def now() -> int:
    return int(time.time())


def conversation_title(conv: dict) -> str:
    if conv.get("name"):
        return conv["name"]
    return f"{KIND_LABEL.get(conv.get('kind'), '')} {conv.get('peer', '')}".strip()


@dataclass
class SearchQuery:
    keyword: str = ""
    nickname: str = ""
    uin: str = ""
    start: int | None = None
    end: int | None = None
    owner: str | None = None
    conversation_id: int | None = None
    limit: int = 200
    offset: int = 0
    keywords: tuple[str,...] = ()
    senders: tuple[str,...] = ()
    types: tuple[str,...] = ()


def _like_escape(s: str) -> str:
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


class Archive:
    def __init__(self, path: str | os.PathLike, *, backup_dir: str | os.PathLike | None = None, read_only=False):
        self.path = Path(path)
        if not read_only:self.path.parent.mkdir(parents=True, exist_ok=True)
        self.data_dir = self.path.parent
        self.backup_dir = Path(backup_dir) if backup_dir else self.data_dir / "backups"
        self.con = sqlite3.connect(self.path.resolve().as_uri()+'?mode=ro' if read_only else self.path,
                                   uri=read_only, timeout=2 if read_only else 30, check_same_thread=False)
        self.con.row_factory = sqlite3.Row
        if read_only:
            self.con.execute('PRAGMA query_only=ON')
            return
        self.con.execute("PRAGMA journal_mode=WAL")
        self.con.execute("PRAGMA synchronous=NORMAL")
        self.con.execute("PRAGMA foreign_keys=ON")
        self.con.execute("PRAGMA temp_store=MEMORY")
        self.con.execute("PRAGMA cache_size=-65536")
        self._init_schema()

    # ---------- 结构与升级 ----------
    def _schema_version(self) -> int:
        has = self.con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='settings'").fetchone()
        if not has:
            return 0
        row = self.con.execute("SELECT value FROM settings WHERE key='schema'").fetchone()
        return int(row[0]) if row else 0

    def _init_schema(self) -> None:
        v = self._schema_version()
        if v > SCHEMA_VERSION:
            raise SchemaError(f"归档库版本 {v} 高于本程序支持的 {SCHEMA_VERSION}，请使用新版程序打开")
        if v == 0:
            with self.con:
                self.con.executescript(SCHEMA_SQL)
                self.con.execute(SEARCH_INDEX_SQL)   # empty table: instant
                self.con.execute("INSERT OR REPLACE INTO settings VALUES('schema', ?)", (str(SCHEMA_VERSION),))
                self.con.execute("INSERT OR REPLACE INTO settings VALUES('created_at', ?)", (str(now()),))
            return
        if v < SCHEMA_VERSION:
            self.backup(f"升级前-v{v}")
            for step in range(v, SCHEMA_VERSION):
                fn = MIGRATIONS.get(step)
                if fn is None:
                    raise SchemaError(f"缺少 v{step} → v{step + 1} 的升级步骤")
                with self.con:
                    fn(self.con)
                    self.con.execute("INSERT OR REPLACE INTO settings VALUES('schema', ?)", (str(step + 1),))
        with self.con:
            self.con.executescript(SCHEMA_SQL)

    def backup(self, label: str) -> Path:
        self.backup_dir.mkdir(parents=True, exist_ok=True)
        safe = re.sub(r"[^\w\-]+", "_", label)
        dst = self.backup_dir / f"archive-{time.strftime('%Y%m%d-%H%M%S')}-{safe}.db"
        target = sqlite3.connect(dst)
        try:
            self.con.backup(target)
        finally:
            target.close()
        return dst

    def close(self) -> None:
        self.con.close()

    def has_search_index(self) -> bool:
        return self.con.execute("SELECT 1 FROM sqlite_master WHERE type='index' AND name=?", (SEARCH_INDEX,)).fetchone() is not None

    def build_search_index(self, cancel: threading.Event | None = None) -> bool:
        """Add the sender index to an existing archive; one atomic statement.

        Cancelling interrupts it and SQLite rolls the statement back, so the
        archive is left exactly as before and the next start tries again."""
        if self.has_search_index():
            return False
        if cancel is not None:
            self.con.set_progress_handler(lambda: int(cancel.is_set()), 1000)
        try:
            self.con.execute(SEARCH_INDEX_SQL)
        except sqlite3.OperationalError as exc:
            if cancel is not None and cancel.is_set():
                raise InterruptedError("已取消建立搜索索引，归档未改变，下次启动时会再次尝试") from exc
            raise
        finally:
            self.con.set_progress_handler(None, 0)
        return True

    def setting(self, key: str, default: str | None = None) -> str | None:
        row = self.con.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return row[0] if row else default

    # ---------- 账号与会话 ----------
    def upsert_account(self, uin: str, nickname: str = "", source: str = "", touch: bool = False) -> None:
        if not uin:
            return
        self.con.execute(
            "INSERT INTO accounts(uin, nickname, source, created_at) VALUES(?,?,?,?) "
            "ON CONFLICT(uin) DO UPDATE SET nickname=CASE WHEN excluded.nickname!='' THEN excluded.nickname "
            "ELSE accounts.nickname END, source=CASE WHEN excluded.source!='' THEN excluded.source ELSE accounts.source END",
            (uin, nickname or "", source or "", now()),
        )
        if touch:
            self.con.execute("UPDATE accounts SET last_import_at=? WHERE uin=?", (now(), uin))

    def accounts(self) -> list[dict]:
        rows = self.con.execute(
            "SELECT a.*, (SELECT count(*) FROM conversations c WHERE c.owner=a.uin) AS conv_count, "
            "(SELECT coalesce(sum(msg_count),0) FROM conversations c WHERE c.owner=a.uin) AS msg_count "
            "FROM accounts a ORDER BY a.uin"
        )
        return [dict(r) for r in rows]

    def set_account_nickname(self, uin: str, nickname: str) -> None:
        with self.con:
            self.con.execute("UPDATE accounts SET nickname=? WHERE uin=?", (nickname, uin))

    def conversation_id(self, owner: str, kind: str, peer: str, name_hint: str = "", peer_uid: str = "") -> int:
        row = self.con.execute(
            "SELECT id, name, custom_name, peer_uid FROM conversations WHERE owner=? AND kind=? AND peer=?",
            (owner, kind, peer),
        ).fetchone()
        if row:
            if name_hint and not row["custom_name"] and row["name"] != name_hint:
                self.con.execute("UPDATE conversations SET name=? WHERE id=?", (name_hint, row["id"]))
            if peer_uid and not row["peer_uid"]:
                self.con.execute("UPDATE conversations SET peer_uid=? WHERE id=?", (peer_uid, row["id"]))
            return row["id"]
        cur = self.con.execute(
            "INSERT INTO conversations(owner, kind, peer, name, peer_uid, created_at) VALUES(?,?,?,?,?,?)",
            (owner, kind, peer, name_hint or "", peer_uid or "", now()),
        )
        return cur.lastrowid

    def conversation(self, conv_id: int) -> dict | None:
        row = self.con.execute("SELECT * FROM conversations WHERE id=?", (conv_id,)).fetchone()
        return dict(row) if row else None

    def profile(self,owner,kind,peer,uid='') -> dict:
        row=self.con.execute('SELECT * FROM profiles WHERE owner=? AND kind=? AND peer=?',(owner,kind,peer)).fetchone()
        if row is None and uid:
            row=self.con.execute('SELECT * FROM profiles WHERE owner=? AND kind=? AND uid=? LIMIT 1',(owner,kind,uid)).fetchone()
        return dict(row) if row else {}

    def sender_profile(self,uin,owners=(),uid=''):
        """First profile of a QQ number across archived accounts, in the caller's order.

        Profiles are per account (remarks and cached avatars differ), so global
        search tries the preferred accounts first, then every other account."""
        if not uin or uin=='0':return {}
        if not uid:
            row=self.con.execute("SELECT sender_uid FROM messages INDEXED BY ix_msg_sender WHERE sender_uin=? AND sender_uid!='' LIMIT 1",(uin,)).fetchone()
            uid=row[0] if row else ''
        known=[r[0] for r in self.con.execute('SELECT uin FROM accounts UNION SELECT owner FROM conversations ORDER BY 1')]
        for owner in dict.fromkeys([o for o in owners if o]+known):
            found=self.profile(owner,'private',uin,uid)
            if found:return found
        return {}

    def find_conversation(self, owner: str, kind: str, peer: str) -> dict | None:
        row = self.con.execute(
            "SELECT * FROM conversations WHERE owner=? AND kind=? AND peer=?", (owner, kind, peer)
        ).fetchone()
        return dict(row) if row else None

    def conversations(self, owner: str | None = None) -> list[dict]:
        if owner is None:
            rows = self.con.execute("SELECT * FROM conversations ORDER BY owner, kind, last_time DESC")
        else:
            rows = self.con.execute(
                "SELECT * FROM conversations WHERE owner=? ORDER BY kind, last_time DESC", (owner,)
            )
        return [dict(r) for r in rows]

    def conversation_tree(self) -> dict[str, dict[str, list[dict]]]:
        """{归档账号: {'group': [...], 'private': [...]}}，各组内按最后消息时间倒序。"""
        tree: dict[str, dict[str, list[dict]]] = {}
        for a in self.accounts():
            tree.setdefault(a["uin"], {"group": [], "private": []})
        for c in self.conversations():
            tree.setdefault(c["owner"], {"group": [], "private": []})[c["kind"]].append(c)
        for groups in tree.values():
            for lst in groups.values():
                lst.sort(key=lambda c: -(c["last_time"] or 0))
        return tree

    def rename_conversation(self, conv_id: int, name: str) -> None:
        with self.con:
            self.con.execute(
                "UPDATE conversations SET name=?, custom_name=? WHERE id=?", (name.strip(), 1 if name.strip() else 0, conv_id)
            )

    def refresh_conversation_stats(self, conv_ids) -> None:
        for cid in conv_ids:
            self.con.execute(
                "UPDATE conversations SET msg_count=(SELECT count(*) FROM messages WHERE conversation_id=?1 AND hidden=0), "
                "conflict_count=(SELECT count(*) FROM messages WHERE conversation_id=?1 AND hidden=0 AND conflict=1), "
                "first_time=(SELECT min(time) FROM messages WHERE conversation_id=?1 AND hidden=0), "
                "last_time=(SELECT max(time) FROM messages WHERE conversation_id=?1 AND hidden=0) WHERE id=?1",
                (cid,),
            )

    # ---------- 批次 ----------
    def batches(self, conversation_id: int | None = None, limit: int = 500, offset: int = 0) -> list[dict]:
        if conversation_id is None:
            rows = self.con.execute("SELECT * FROM batches ORDER BY id DESC LIMIT ? OFFSET ?", (limit,offset))
        else:
            rows = self.con.execute(
                "SELECT b.*, cv.start_time AS cov_start, cv.end_time AS cov_end, cv.count AS cov_count "
                "FROM coverage cv JOIN batches b ON b.id=cv.batch_id WHERE cv.conversation_id=? ORDER BY b.id DESC",
                (conversation_id,),
            )
        return [dict(r) for r in rows]

    def imported_fingerprints(self) -> dict[str, dict]:
        rows = self.con.execute(
            "SELECT source_fingerprint, max(finished_at) AS at, source_label FROM batches "
            "WHERE status='done' AND source_fingerprint!='' GROUP BY source_fingerprint"
        )
        return {r["source_fingerprint"]: dict(r) for r in rows}

    def imported_labels(self) -> dict[str, dict]:
        rows = self.con.execute(
            "SELECT source_label, max(finished_at) AS at FROM batches WHERE status='done' GROUP BY source_label"
        )
        return {r["source_label"]: dict(r) for r in rows}

    # ---------- 消息 ----------
    _MSG_COLS = ("m.id, m.conversation_id, m.time, m.seq, m.sender_uin, m.sender_name, m.sender_card, m.is_self, "
                 "m.msg_type, m.text, m.elements, m.reply_seq, m.conflict, m.suspect, m.batch_id, m.merge_key, m.exact_key, m.source_msg_id, m.msg_random, m.sender_uid")

    @staticmethod
    def _row(r) -> dict:
        d = dict(r)
        d["elements"] = loads_elements(d.get("elements"))
        return d

    def latest(self, conv_id: int, limit: int = 100) -> list[dict]:
        rows = self.con.execute(
            f"SELECT {self._MSG_COLS} FROM messages m WHERE m.conversation_id=? AND m.hidden=0 "
            "ORDER BY m.time DESC, m.id DESC LIMIT ?",
            (conv_id, limit),
        ).fetchall()
        return [self._row(r) for r in reversed(rows)]

    def time_bounds(self,conv_id=None):
        clause='hidden=0 AND time>0 AND time<253402300800';args=[]
        if conv_id is not None:clause+=' AND conversation_id=?';args.append(conv_id)
        limits=[]
        for order in ('ASC','DESC'):
            row=self.con.execute(f'SELECT time FROM messages WHERE {clause} ORDER BY time {order},id {order} LIMIT 1',args).fetchone()
            limits.append(row[0] if row else None)
        return tuple(limits)

    def day_has_messages(self,start,end,conv_id=None):
        sql='SELECT 1 FROM messages WHERE hidden=0 AND time>=? AND time<?';args=[start,end]
        if conv_id is not None:sql+=' AND conversation_id=?';args.append(conv_id)
        return self.con.execute(sql+' LIMIT 1',args).fetchone() is not None

    def at_time(self,conv_id,t):
        row=self.con.execute('SELECT id FROM messages WHERE conversation_id=? AND hidden=0 AND time>=? ORDER BY time,id LIMIT 1',(conv_id,t)).fetchone()
        return self.around(row[0]) if row else self.latest(conv_id)

    def user_history(self,owner,uin,current_id=None,uid='',cancel=None):
        if cancel:self.con.set_progress_handler(lambda:int(cancel.is_set()),1000)
        profile=self.profile(owner,'private',uin,uid)
        # An unresolvable identity must never query every anonymous sender.
        if not uin or uin=='0':return dict(profile=profile,groups=[],last=None,private=None)
        groups=[dict(r) for r in self.con.execute('SELECT c.*,max(m.time) AS spoken_at FROM messages m INDEXED BY ix_msg_sender '
            "JOIN conversations c ON c.id=m.conversation_id WHERE m.sender_uin=? AND m.hidden=0 AND c.owner=? AND c.kind='group' "
            'GROUP BY c.id ORDER BY spoken_at DESC',(uin,owner))]
        row=self.con.execute('SELECT id,time FROM messages INDEXED BY ix_msg_sender WHERE sender_uin=? AND conversation_id=? AND hidden=0 ORDER BY time DESC,id DESC LIMIT 1',(uin,current_id)).fetchone() if current_id else None
        private=self.find_conversation(owner,'private',uin)
        if private is None and uid:private=self.find_conversation(owner,'private',uid)
        return dict(profile=profile,groups=groups,last=dict(row) if row else None,private=private)

    def phone_history(self,limit=100,offset=0):
        return [dict(r) for r in self.con.execute('SELECT * FROM phone_runs ORDER BY id DESC LIMIT ? OFFSET ?',(limit,offset))]

    def earliest(self, conv_id: int, limit: int = 100) -> list[dict]:
        rows = self.con.execute(
            f"SELECT {self._MSG_COLS} FROM messages m WHERE m.conversation_id=? AND m.hidden=0 "
            "ORDER BY m.time, m.id LIMIT ?",
            (conv_id, limit),
        ).fetchall()
        return [self._row(r) for r in rows]

    def before(self, conv_id: int, t: int, mid: int, limit: int = 100) -> list[dict]:
        rows = self.con.execute(
            f"SELECT {self._MSG_COLS} FROM messages m WHERE m.conversation_id=? AND m.hidden=0 "
            "AND (m.time, m.id) < (?, ?) ORDER BY m.time DESC, m.id DESC LIMIT ?",
            (conv_id, t, mid, limit),
        ).fetchall()
        return [self._row(r) for r in reversed(rows)]

    def after(self, conv_id: int, t: int, mid: int, limit: int = 100) -> list[dict]:
        rows = self.con.execute(
            f"SELECT {self._MSG_COLS} FROM messages m WHERE m.conversation_id=? AND m.hidden=0 "
            "AND (m.time, m.id) > (?, ?) ORDER BY m.time, m.id LIMIT ?",
            (conv_id, t, mid, limit),
        ).fetchall()
        return [self._row(r) for r in rows]

    def range(self, conv_id: int, start: int | None = None, end: int | None = None, batch: int = 5000):
        """按时间顺序流式读取一个会话（导出用）。"""
        t, mid = (start if start is not None else -(1 << 62)), -1
        while True:
            rows = self.con.execute(
                f"SELECT {self._MSG_COLS} FROM messages m WHERE m.conversation_id=? AND m.hidden=0 "
                "AND (m.time, m.id) > (?, ?) AND (? IS NULL OR m.time <= ?) ORDER BY m.time, m.id LIMIT ?",
                (conv_id, t, mid, end, end, batch),
            ).fetchall()
            if not rows:
                return
            for r in rows:
                yield self._row(r)
            t, mid = rows[-1]["time"], rows[-1]["id"]

    def around(self, message_id: int, before: int = 60, after: int = 60) -> list[dict]:
        m = self.message(message_id)
        if not m:
            return []
        older = self.before(m["conversation_id"], m["time"], m["id"], before)
        newer = self.after(m["conversation_id"], m["time"], m["id"], after)
        return older + [m] + newer

    def message(self, message_id: int) -> dict | None:
        r = self.con.execute(f"SELECT {self._MSG_COLS} FROM messages m WHERE m.id=?", (message_id,)).fetchone()
        return self._row(r) if r else None

    def message_by_seq(self, conv_id: int, seq: int) -> dict | None:
        r = self.con.execute(
            f"SELECT {self._MSG_COLS} FROM messages m WHERE m.conversation_id=? AND m.seq=? AND m.hidden=0 LIMIT 1",
            (conv_id, seq),
        ).fetchone()
        return self._row(r) if r else None

    def variants(self, message_id: int) -> list[dict]:
        rows = self.con.execute(
            "SELECT v.*, b.source_label, b.source_kind FROM message_variants v JOIN batches b ON b.id=v.batch_id "
            "WHERE v.message_id=? ORDER BY v.id",
            (message_id,),
        )
        out = []
        for r in rows:
            d = dict(r)
            d["elements"] = loads_elements(d["elements"])
            out.append(d)
        return out

    def message_sources(self, message_id: int) -> list[dict]:
        rows = self.con.execute(
            "SELECT s.batch_id, s.source_msg_id, b.source_kind, b.source_label, b.started_at FROM message_sources s "
            "JOIN batches b ON b.id=s.batch_id WHERE s.message_id=? ORDER BY s.batch_id",
            (message_id,),
        )
        return [dict(r) for r in rows]

    def count_messages(self) -> int:
        return self.con.execute("SELECT count(*) FROM messages WHERE hidden=0").fetchone()[0]

    # ---------- 搜索 ----------
    def _search_conditions(self, q: SearchQuery):
        conds = ["m.hidden=0"]
        args: list = []
        for kw in dict.fromkeys([*(q.keyword or "").split(),*q.keywords]):
            if len(kw) >= 3:
                conds.append("m.id IN (SELECT rowid FROM messages_fts WHERE messages_fts MATCH ?)")
                args.append('"' + kw.replace('"', '""') + '"')
            else:
                conds.append("m.text LIKE ? ESCAPE '\\'")
                args.append(f"%{_like_escape(kw)}%")
        if q.nickname.strip():
            like = f"%{_like_escape(q.nickname.strip())}%"
            conds.append("(m.sender_name LIKE ? ESCAPE '\\' OR m.sender_card LIKE ? ESCAPE '\\')")
            args += [like, like]
        if q.uin.strip():
            conds.append("m.sender_uin = ?")
            args.append(q.uin.strip())
        if q.senders:
            conds.append('m.sender_uin IN ('+','.join('?' for _ in q.senders)+')');args.extend(q.senders)
        if q.types:
            kinds=[k for k in q.types if k!='link'];parts=[]
            if kinds:parts.append('m.msg_type IN ('+','.join('?' for _ in kinds)+')');args.extend(kinds)
            if 'link' in q.types:parts.append("(m.text LIKE '%http://%' OR m.text LIKE '%https://%')")
            if parts:conds.append('('+' OR '.join(parts)+')')
        if q.start is not None:
            conds.append("m.time >= ?")
            args.append(int(q.start))
        if q.end is not None:
            conds.append("m.time <= ?")
            args.append(int(q.end))
        if q.conversation_id is not None:
            conds.append("m.conversation_id = ?")
            args.append(q.conversation_id)
        elif q.owner:
            conds.append("c.owner = ?")
            args.append(q.owner)
        return conds,args

    def search_conversations(self,q:SearchQuery) -> list[dict]:
        conds,args=self._search_conditions(q)
        # Global short-keyword scans must walk the table sequentially. Letting the
        # planner visit conversations first caused millions of random row reads.
        # Re-measured with ix_msg_conv_sender_time (scripts/bench_hints.py, 1M
        # synthetic, 2026-10-10): for SearchWindow's owner-less queries the sender
        # hint and no hint pick the same plan (~0.011 s), NOT INDEXED ~0.26 s; a
        # 2-character keyword is still fastest with NOT INDEXED (0.30 vs 0.34 s).
        hint='INDEXED BY ix_msg_sender' if q.senders or q.uin.strip() else 'NOT INDEXED'
        conds=[condition.replace('c.owner = ?', 'm.conversation_id IN (SELECT id FROM conversations WHERE owner=?)') for condition in conds]
        rows=self.con.execute("SELECT c.*,hit.matches,hit.matched_at FROM (SELECT m.conversation_id,count(*) AS matches,max(m.time) AS matched_at FROM messages m "+hint+" WHERE "+' AND '.join(conds)+" GROUP BY m.conversation_id) hit JOIN conversations c ON c.id=hit.conversation_id ORDER BY hit.matched_at DESC,c.id LIMIT 1000",args)
        return [dict(r) for r in rows]

    def search(self,q:SearchQuery) -> list[dict]:
        conds,args=self._search_conditions(q)
        sql = (f"SELECT {self._MSG_COLS}, c.owner, c.kind, c.peer, c.name AS conv_name FROM messages m "
               f"JOIN conversations c ON c.id=m.conversation_id WHERE {' AND '.join(conds)} "
               "ORDER BY m.time DESC, m.id DESC LIMIT ? OFFSET ?")
        args += [q.limit, q.offset]
        return [self._row(r) for r in self.con.execute(sql, args)]

    def senders(self,conv_id=None,term='',limit=100):
        # A large group may have millions of messages but only a few thousand
        # people. Read its roster once in the cancellable worker, then filter
        # that small result. File/WAL signatures invalidate it after any writer.
        signature=[]
        for path in (self.path,Path(str(self.path)+'-wal')):
            try:s=path.stat();signature.append((s.st_size,s.st_mtime_ns))
            except FileNotFoundError:signature.append(None)
        key=(str(self.path.resolve()),conv_id,tuple(signature))
        with _sender_cache_lock:
            rows=_sender_cache.get(key)
            if rows is not None:_sender_cache.move_to_end(key)
        if rows is None:
            conds=["m.hidden=0","m.sender_uin!=''","m.sender_uin!='0'"];args=[]
            hint='NOT INDEXED'
            if conv_id is not None:conds.append('m.conversation_id=?');args.append(conv_id);hint='INDEXED BY ix_msg_conv_time'
            # uid feeds profile()'s fallback for identities stored by NT UID only;
            # max() prefers any recorded UID over the empty default.
            rows=[dict(r) for r in self.con.execute('SELECT m.sender_uin,max(m.sender_name) AS nickname,max(m.sender_card) AS card,max(m.sender_uid) AS uid FROM messages m '+hint+' WHERE '+ ' AND '.join(conds)+' GROUP BY m.sender_uin ORDER BY m.sender_uin',args)]
            after=[]
            for path in (self.path,Path(str(self.path)+'-wal')):
                try:s=path.stat();after.append((s.st_size,s.st_mtime_ns))
                except FileNotFoundError:after.append(None)
            if after==signature:
                with _sender_cache_lock:
                    _sender_cache[key]=rows
                    while len(_sender_cache)>8:_sender_cache.popitem(last=False)
        value=term.casefold()
        return [dict(r) for r in rows if not value or any(value in str(r[k]).casefold() for k in ('sender_uin','nickname','card'))][:limit]

    # ---------- 覆盖与缺口 ----------
    def coverage(self, conv_id: int) -> dict:
        rows = self.con.execute(
            "SELECT start_time, end_time FROM coverage WHERE conversation_id=? ORDER BY start_time", (conv_id,)
        ).fetchall()
        merged: list[list[int]] = []
        for s, e in rows:
            if merged and s <= merged[-1][1] + 1:
                merged[-1][1] = max(merged[-1][1], e)
            else:
                merged.append([s, e])
        gaps = [(merged[i][1], merged[i + 1][0]) for i in range(len(merged) - 1)]
        seq = self.con.execute(
            "SELECT min(seq), max(seq), count(DISTINCT seq) FROM messages WHERE conversation_id=? AND hidden=0 "
            "AND seq IS NOT NULL AND seq > 0",
            (conv_id,),
        ).fetchone()
        conv = self.conversation(conv_id) or {}
        seq_info = None
        if conv.get("kind") == GROUP and seq[0] is not None:
            missing = (seq[1] - seq[0] + 1) - seq[2]
            jumps = self.con.execute(
                "SELECT prev, seq, ptime, time FROM (SELECT seq, time, lag(seq) OVER (ORDER BY seq) AS prev, "
                "lag(time) OVER (ORDER BY seq) AS ptime FROM (SELECT DISTINCT seq, min(time) AS time FROM messages "
                "WHERE conversation_id=? AND hidden=0 AND seq>0 GROUP BY seq)) WHERE seq - prev > 1 "
                "ORDER BY seq - prev DESC LIMIT 20",
                (conv_id,),
            ).fetchall()
            seq_info = {
                "min": seq[0], "max": seq[1], "count": seq[2], "missing": missing,
                "largest_gaps": [dict(prev=j[0], next=j[1], prev_time=j[2], next_time=j[3]) for j in jumps],
            }
        return {"intervals": merged, "gaps": gaps, "seq": seq_info}

    # ---------- 统计 ----------
    def stats(self) -> dict:
        one = lambda sql: self.con.execute(sql).fetchone()[0]  # noqa: E731
        return {
            "messages": one("SELECT count(*) FROM messages WHERE hidden=0"),
            "conversations": one("SELECT count(*) FROM conversations"),
            "accounts": one("SELECT count(*) FROM accounts"),
            "batches": one("SELECT count(*) FROM batches WHERE status='done'"),
            "conflicts": one("SELECT count(*) FROM messages WHERE conflict=1 AND hidden=0"),
            "suspects": one("SELECT count(*) FROM messages WHERE suspect=1 AND hidden=0"),
            "media": one("SELECT count(*) FROM media_files WHERE status='done'"),
            "media_missing": one("SELECT count(*) FROM message_media WHERE status='missing'"),
            "media_linked": one("SELECT count(*) FROM message_media WHERE status='linked'"),
        }

    def overview(self) -> dict:
        """UI summary from transactional conversation totals, never scan message bodies."""
        row=self.con.execute('SELECT count(*),coalesce(sum(msg_count),0),coalesce(sum(conflict_count),0) FROM conversations').fetchone()
        links=dict(self.con.execute('SELECT status,count(*) FROM message_media GROUP BY status'))
        return dict(conversations=row[0],messages=row[1],conflicts=row[2],
                    accounts=self.con.execute('SELECT count(*) FROM accounts').fetchone()[0],
                    media=self.con.execute("SELECT count(*) FROM media_files WHERE status='done'").fetchone()[0],
                    media_linked=links.get('linked',0),media_missing=links.get('missing',0))

    def integrity_check(self) -> list[str]:
        out = [r[0] for r in self.con.execute("PRAGMA integrity_check")]
        try:
            with self.con:
                self.con.execute("INSERT INTO messages_fts(messages_fts, rank) VALUES('integrity-check', 1)")
        except sqlite3.DatabaseError as exc:
            out.append(f"全文索引异常：{exc}")
        return out

    def rebuild_fts(self) -> None:
        with self.con:
            self.con.execute("INSERT INTO messages_fts(messages_fts) VALUES('rebuild')")

    # ---------- 附件 ----------
    def resolve_media_path(self, local_path: str) -> Path:
        p = Path(local_path)
        return p if p.is_absolute() else self.data_dir / p

    def relative_media_path(self, p: Path) -> str:
        p = Path(p).resolve()
        try:
            return p.relative_to(self.data_dir.resolve()).as_posix()
        except ValueError:
            return str(p)

    def media_for_message(self, message_id: int) -> dict[int, dict]:
        rows = self.con.execute(
            "SELECT mm.elem_index, mm.status, f.* FROM message_media mm LEFT JOIN media_files f ON f.id=mm.media_id "
            "WHERE mm.message_id=?",
            (message_id,),
        )
        out = {}
        for r in rows:
            d = dict(r)
            if d.get("local_path"):
                d["abs_path"] = str(self.resolve_media_path(d["local_path"]))
            out[d["elem_index"]] = d
        return out

    def media_for_messages(self, ids: list[int]) -> dict[int, dict[int, dict]]:
        out: dict[int, dict[int, dict]] = {}
        for i in range(0, len(ids), 500):
            part = ids[i:i + 500]
            rows = self.con.execute(
                "SELECT mm.message_id, mm.elem_index, mm.status, f.local_path, f.name, f.size, f.kind "
                "FROM message_media mm LEFT JOIN media_files f ON f.id=mm.media_id "
                f"WHERE mm.message_id IN ({','.join('?' * len(part))})",
                part,
            )
            for r in rows:
                d = dict(r)
                if d.get("local_path"):
                    d["abs_path"] = str(self.resolve_media_path(d["local_path"]))
                out.setdefault(d["message_id"], {})[d["elem_index"]] = d
        return out

    def missing_media_count(self, conv_id: int) -> int:
        return self.con.execute(
            "SELECT count(*) FROM message_media mm JOIN messages m ON m.id=mm.message_id "
            "WHERE m.conversation_id=? AND mm.status='missing'",
            (conv_id,),
        ).fetchone()[0]
