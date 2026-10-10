"""导入事务：批次登记、跨来源合并去重、冲突变体、覆盖范围（方案 5.4）。

整批在一个事务内完成，任何异常或取消都整批回滚；回滚后另行登记一条失败批次，
便于追溯。重复导入同一来源结果一致（幂等）。
"""

from __future__ import annotations

import json
import threading
from dataclasses import asdict, dataclass, field
from typing import Callable, Iterable

from .merge import content_hash, diff_summary, fingerprint, merge_key, identity_conflicts, collision_key
from .models import GROUP, NormalizedMessage, dumps_elements
from .store import Archive, now


class ImportCancelled(Exception):
    pass


@dataclass
class ImportStats:
    batch_id: int = 0
    read: int = 0
    inserted: int = 0
    duplicates: int = 0
    conflicts: int = 0
    suspects: int = 0
    skipped: int = 0
    conversations: set = field(default_factory=set)

    def summary(self) -> str:
        return (f"读取 {self.read} 条：新增 {self.inserted}，重复 {self.duplicates}，"
                f"冲突 {self.conflicts}，疑似重复 {self.suspects}，跳过 {self.skipped}；"
                f"涉及 {len(self.conversations)} 个会话")

    def to_dict(self) -> dict:
        d = asdict(self)
        d["conversations"] = len(self.conversations)
        return d


@dataclass
class ImportRequest:
    source_kind: str
    source_label: str
    source_fingerprint: str = ""
    owner: str = ""
    range_start: int | None = None
    range_end: int | None = None
    params: dict = field(default_factory=dict)


class Importer:
    _columns = ('conversation_id, merge_key, exact_key, time, seq, msg_random, source_msg_id, '
                'sender_uin, sender_uid, sender_name, sender_card, is_self, msg_type, text, elements, '
                'reply_seq, content_hash, fingerprint, batch_id, suspect')
    def __init__(self, archive: Archive):
        self.archive = archive
        self.con = archive.con
        self._conv_cache: dict[tuple, int] = {}
        self._conv_names: dict[tuple,str] = {}
        self._fts_trigger = None
        self._fts_first = self._fts_last = None
        self._pending=[];self._pending_bytes=0

    def _begin_search_batch(self):
        # FTS5 flushes its pending terms at the end of each SQL statement. The
        # per-row insert trigger therefore writes/merges tiny index segments.
        # Suspend ONLY our known trigger inside the existing atomic transaction,
        # then index bounded row ranges in single statements. Other connections
        # see the old committed schema/index until COMMIT; ROLLBACK restores both.
        row=self.con.execute("SELECT sql FROM sqlite_master WHERE type='trigger' AND name='trg_msg_ai'").fetchone()
        expected="CREATE TRIGGER trg_msg_ai AFTER INSERT ON messages BEGIN INSERT INTO messages_fts(rowid, text) VALUES (new.id, new.text); END"
        normalize=lambda s:''.join(s.replace('IF NOT EXISTS','').split()).rstrip(';').casefold()
        self._fts_trigger=row[0] if row and normalize(row[0])==normalize(expected) else None
        self._fts_first=self._fts_last=None
        extra=self.con.execute("SELECT 1 FROM sqlite_master WHERE type='trigger' AND tbl_name='messages' AND name NOT IN ('trg_msg_ai','trg_msg_ad','trg_msg_au')").fetchone()
        if extra:self._fts_trigger=None
        if self._fts_trigger:
            self.con.execute('DROP TRIGGER trg_msg_ai')
            self.con.execute('CREATE TEMP TABLE qqarchive_import_stage AS SELECT '+self._columns+' FROM messages WHERE 0')

    def _flush_search_batch(self):
        if self._fts_first is not None:
            self.con.execute('INSERT INTO messages_fts(rowid,text) SELECT id,text FROM messages WHERE id BETWEEN ? AND ? ORDER BY id',
                             (self._fts_first,self._fts_last))
            self._fts_first=self._fts_last=None

    def _values(self,batch,cid,m,source_kind):
        key,exact=merge_key(m,source_kind)
        return (cid,key,int(exact),int(m.time),m.seq,m.msg_random,m.source_msg_id,m.sender_uin,
                m.sender_uid,m.sender_name,m.sender_card,int(m.is_self),m.msg_type,m.text,
                dumps_elements(m.elements),m.reply_seq,content_hash(m),fingerprint(m),batch,0)

    def _queue(self,batch,cid,m,source_kind,stats):
        values=self._values(batch,cid,m,source_kind)
        self._pending.append((m,values));self._pending_bytes+=4*(len(values[13])+len(values[14]))+512
        if len(self._pending)>=2000 or self._pending_bytes>=8*1024*1024:
            self._flush_pending(batch,source_kind,stats)

    def _flush_pending(self,batch,source_kind,stats):
        pending=self._pending;self._pending=[];self._pending_bytes=0
        if not pending:return
        clean=bool(self._fts_trigger) and len(pending)>=32
        keys=set();fingerprints={}
        for _,v in pending:
            key=(v[0],v[1]);fp=(v[0],v[17])
            if key in keys or (fp in fingerprints and not(v[2] and fingerprints[fp])):clean=False
            keys.add(key);fingerprints[fp]=v[2]
        if clean:
            self.con.execute('DELETE FROM temp.qqarchive_import_stage')
            self.con.executemany('INSERT INTO temp.qqarchive_import_stage VALUES('+','.join('?'*20)+')',(v for _,v in pending))
            # Keep the bounded incoming chunk on the outside of each lookup.
            # Without this constraint SQLite may scan the entire archive again
            # for each chunk, making large/incremental imports quadratic.
            collision=self.con.execute('SELECT 1 FROM temp.qqarchive_import_stage s WHERE EXISTS(SELECT 1 FROM messages m WHERE m.conversation_id=s.conversation_id AND m.merge_key=s.merge_key AND m.hidden=0) LIMIT 1').fetchone()
            suspect=None if collision else self.con.execute('SELECT 1 FROM temp.qqarchive_import_stage s WHERE EXISTS(SELECT 1 FROM messages m INDEXED BY ix_msg_fp WHERE m.conversation_id=s.conversation_id AND m.fingerprint=s.fingerprint AND m.hidden=0 AND (m.exact_key=0 OR s.exact_key=0)) LIMIT 1').fetchone()
            clean=not collision and not suspect
        if clean:
            first=self.con.execute('SELECT coalesce(max(id),0)+1 FROM messages').fetchone()[0]
            self.con.execute('INSERT INTO messages('+self._columns+') SELECT '+self._columns+' FROM temp.qqarchive_import_stage ORDER BY rowid')
            last=self.con.execute('SELECT last_insert_rowid()').fetchone()[0]
            self.con.execute('INSERT INTO message_sources SELECT m.id,?,s.source_msg_id FROM temp.qqarchive_import_stage s CROSS JOIN messages m ON m.conversation_id=s.conversation_id AND m.merge_key=s.merge_key WHERE m.hidden=0',(batch,))
            if self._fts_first is None:self._fts_first=first
            self._fts_last=last;stats.inserted+=len(pending)
        else:
            # Overlapping batches, conflicts and weak identities retain the same
            # order-sensitive merge decisions as single-message imports.
            for m,v in pending:self._one(batch,v[0],m,source_kind,stats,v)

    def _conv(self, m: NormalizedMessage) -> int:
        k = (m.owner, m.kind, m.peer)
        cid = self._conv_cache.get(k)
        if cid is None:
            name = m.peer_name
            cid = self.archive.conversation_id(m.owner, m.kind, m.peer, name, m.peer_uid)
            self._conv_cache[k] = cid
            self._conv_names[k] = name
        elif m.peer_name and self._conv_names.get(k)!=m.peer_name:
            self.archive.conversation_id(m.owner, m.kind, m.peer, m.peer_name)
            self._conv_names[k]=m.peer_name
        return cid

    def run(self, req: ImportRequest, messages: Iterable[NormalizedMessage], *,
            progress: Callable[[ImportStats], None] | None = None,
            cancel: threading.Event | None = None, progress_every: int = 2000) -> ImportStats:
        self._conv_cache.clear()
        self._conv_names.clear()
        self._pending=[];self._pending_bytes=0
        stats = ImportStats()
        cov: dict[int, list[int]] = {}
        started = now()
        params = json.dumps(req.params, ensure_ascii=False)
        old_cache=self.con.execute('PRAGMA cache_size').fetchone()[0]
        try:
            # Only the importer gets up to 512 MiB, released again after the task.
            self.con.execute('PRAGMA cache_size=-524288')
            self.con.execute("BEGIN IMMEDIATE")
            self._begin_search_batch()
            cur = self.con.execute(
                "INSERT INTO batches(source_kind, source_label, source_fingerprint, owner, range_start, range_end, "
                "params, status, started_at) VALUES(?,?,?,?,?,?,?,'running',?)",
                (req.source_kind, req.source_label, req.source_fingerprint, req.owner, req.range_start,
                 req.range_end, params, started),
            )
            batch = cur.lastrowid
            stats.batch_id = batch
            owners = set()
            for m in messages:
                if cancel is not None and cancel.is_set():
                    raise ImportCancelled("用户取消导入")
                stats.read += 1
                if not m.peer or not m.owner:
                    stats.skipped += 1
                    continue
                owners.add(m.owner)
                cid = self._conv(m)
                stats.conversations.add(cid)
                self._queue(batch, cid, m, req.source_kind, stats)
                if self._fts_first is not None and self._fts_last-self._fts_first>=1999:
                    self._flush_search_batch()
                c = cov.get(cid)
                if c is None:
                    cov[cid] = [m.time, m.time, 1]
                else:
                    c[0] = min(c[0], m.time)
                    c[1] = max(c[1], m.time)
                    c[2] += 1
                if progress and stats.read % progress_every == 0:
                    progress(stats)
            self._flush_pending(batch,req.source_kind,stats)
            self._flush_search_batch()
            if self._fts_trigger:
                self.con.execute('DROP TABLE temp.qqarchive_import_stage')
                self.con.execute(self._fts_trigger)
            if cancel is not None and cancel.is_set():raise ImportCancelled('用户取消导入')
            for cid, (s, e, n) in cov.items():
                self.con.execute("INSERT INTO coverage VALUES(?,?,?,?,?)", (cid, batch, s, e, n))
            for o in owners | ({req.owner} if req.owner else set()):
                self.archive.upsert_account(o, source=req.source_kind, touch=True)
            self.archive.refresh_conversation_stats(stats.conversations)
            if cancel is not None and cancel.is_set():raise ImportCancelled('用户取消导入')
            self.con.execute(
                "UPDATE batches SET status='done', finished_at=?, read_count=?, inserted=?, duplicates=?, "
                "conflicts=?, suspects=?, skipped=? WHERE id=?",
                (now(), stats.read, stats.inserted, stats.duplicates, stats.conflicts, stats.suspects,
                 stats.skipped, batch),
            )
            self.con.execute("COMMIT")
        except BaseException as exc:
            if self.con.in_transaction:
                self.con.execute("ROLLBACK")
            self._conv_cache.clear()
            status = "cancelled" if isinstance(exc, ImportCancelled) else "failed"
            with self.con:
                self.con.execute(
                    "INSERT INTO batches(source_kind, source_label, source_fingerprint, owner, range_start, "
                    "range_end, params, status, started_at, finished_at, read_count, note) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                    (req.source_kind, req.source_label, req.source_fingerprint, req.owner, req.range_start,
                     req.range_end, params, status, started, now(), stats.read, type(exc).__name__),
                )
            raise
        finally:
            self._pending=[];self._pending_bytes=0
            self.con.execute(f'PRAGMA cache_size={int(old_cache)}')
        if progress:
            progress(stats)
        return stats

    def _one(self, batch: int, cid: int, m: NormalizedMessage, source_kind: str, stats: ImportStats, values=None) -> None:
        values=values or self._values(batch,cid,m,source_kind)
        key,exact,h,fp=values[1],bool(values[2]),values[16],values[17]
        row = self.con.execute(
            "SELECT id, content_hash, sender_uin, sender_uid, time, msg_random, text FROM messages WHERE conversation_id=? AND merge_key=? AND hidden=0",
            (cid, key),
        ).fetchone()
        if row is not None and identity_conflicts(row, m):
            key = collision_key(key, m)
            values = values[:1] + (key,) + values[2:]
            row = self.con.execute(
                'SELECT id,content_hash,sender_uin,sender_uid,time,msg_random,text FROM messages '
                'WHERE conversation_id=? AND merge_key=? AND hidden=0', (cid,key)
            ).fetchone()
        if row is None:
            suspect = 0
            other = self.con.execute(
                "SELECT id, exact_key FROM messages INDEXED BY ix_msg_fp WHERE conversation_id=? AND fingerprint=? AND merge_key!=? "
                "AND hidden=0 LIMIT 1",
                (cid, fp, key),
            ).fetchone()
            if other is not None and (not exact or not other["exact_key"]):
                suspect = 1
                self.con.execute("UPDATE messages SET suspect=1 WHERE id=?", (other["id"],))
                stats.suspects += 1
            cur = self.con.execute(
                "INSERT INTO messages(conversation_id, merge_key, exact_key, time, seq, msg_random, source_msg_id, "
                "sender_uin, sender_uid, sender_name, sender_card, is_self, msg_type, text, elements, reply_seq, "
                "content_hash, fingerprint, batch_id, suspect) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                values[:-1]+(suspect,),
            )
            self.con.execute("INSERT INTO message_sources VALUES(?,?,?)", (cur.lastrowid, batch, m.source_msg_id))
            if self._fts_trigger:
                if self._fts_first is None:self._fts_first=cur.lastrowid
                self._fts_last=cur.lastrowid
            stats.inserted += 1
            return
        mid = row["id"]
        self.con.execute("INSERT OR IGNORE INTO message_sources VALUES(?,?,?)", (mid, batch, m.source_msg_id))
        if row["content_hash"] == h:
            stats.duplicates += 1
            return
        cur = self.con.execute(
            "INSERT OR IGNORE INTO message_variants(message_id, batch_id, content_hash, sender_uin, sender_name, "
            "text, elements, diff, created_at) VALUES(?,?,?,?,?,?,?,?,?)",
            (mid, batch, h, m.sender_uin, m.display_name, m.text, dumps_elements(m.elements),
             diff_summary(dict(row), m), now()),
        )
        if cur.rowcount:
            self.con.execute("UPDATE messages SET conflict=1 WHERE id=?", (mid,))
            stats.conflicts += 1
        else:
            stats.duplicates += 1
