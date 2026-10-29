"""Bounded QQ history acquisition. A traversed range is not proof of completeness.

NapCat's message_id/message_seq/real_id are adapter IDs; only real_seq is QQ's
sequence. Never conflate them. Network calls occur outside archive transactions.
"""
from __future__ import annotations
import hashlib
import json
import re
import threading
import time
from dataclasses import asdict, dataclass

from .forwards import LocalQQ, ForwardError, qq_number, card_descriptor
from .models import NormalizedMessage, MEDIA_TYPES
from .importer import Importer, ImportRequest, ImportCancelled


class HistoryError(ValueError):
    pass


@dataclass(frozen=True)
class Scope:
    owner: str
    kind: str
    peer: str
    start: int
    end: int

    def __post_init__(self):
        if not qq_number(self.owner) or not qq_number(self.peer) or self.kind not in ('group', 'private'):
            raise HistoryError('请填写正确的归档账号、会话类型和群号 / QQ 号')
        if (type(self.start) is not int or type(self.end) is not int
                or not 0 < self.start <= self.end <= 32503680000):
            raise HistoryError('起止时间无效，结束时间不能早于开始时间')

    @property
    def key(self):
        return hashlib.sha256(json.dumps(asdict(self), sort_keys=True).encode()).hexdigest()


def integer(value, *, optional=False):
    if value in (None, '') and optional:
        return None
    if isinstance(value, bool) or not re.fullmatch(r'-?[0-9]{1,20}', str(value)):
        raise HistoryError('消息包含无效的整数标识')
    n = int(value)
    if not -(1 << 63) <= n < (1 << 63):
        raise HistoryError('消息标识超出支持范围')
    return n


def segments(value):
    if isinstance(value, str):
        if '[CQ:' in value:
            raise HistoryError('请将 NapCat 的消息格式设为 array 后重试；当前页尚未导入')
        value = [{'type': 'text', 'data': {'text': value}}]
    if not isinstance(value, list) or len(value) > 1000:
        raise HistoryError('消息元素格式不受支持，当前页尚未导入')
    result = []
    for seg in value:
        if not isinstance(seg, dict) or not isinstance(seg.get('data', {}), dict):
            raise HistoryError('消息元素损坏，当前页尚未导入')
        typ, data = seg.get('type', 'unknown'), seg.get('data', {})
        if typ == 'text':
            e = {'type': 'text', 'text': str(data.get('text', ''))}
        elif typ == 'at':
            e = {'type': 'text', 'text': '@' + str(data.get('qq', '')), 'at': str(data.get('qq', ''))}
        elif typ in ('image', 'video', 'record', 'file'):
            name = str(data.get('file') or data.get('name') or '')
            digest = str(data.get('md5') or '')
            if not digest and re.fullmatch(r'[0-9a-fA-F]{32}(?:\.[a-zA-Z0-9]+)?', name):
                digest = name.split('.')[0]
            e = dict(type='voice' if typ == 'record' else typ, name=name,
                     md5=digest.lower() if re.fullmatch(r'[0-9a-fA-F]{32}', digest) else '',
                     url=str(data.get('url') or ''), path=str(data.get('path') or ''),
                     size=integer(data.get('file_size'), optional=True))
        elif typ == 'face':
            e = {'type': 'face', 'id': integer(data.get('id'), optional=True)}
        elif typ in ('json', 'xml'):
            content = str(data.get('data') or '')
            e = {'type': 'card', 'text': content}
            found = card_descriptor(content)
            if found:
                e['forward'] = found
        elif typ == 'forward':
            e = {'type': 'forward', 'text': '合并转发',
                 'forward': {'id': str(data.get('id') or ''), 'content': data.get('content') or []}}
        elif typ == 'reply':
            # Adapter reply IDs are not QQ message sequences.
            e = {'type': 'reply', 'onebot_id': str(data.get('id') or '')}
        else:
            e = {'type': 'unknown', 'code': typ, 'raw': data}
        result.append(e)
    return result


def normalize(row, scope):
    if not isinstance(row, dict):
        raise HistoryError('接口返回了无法解析的消息')
    if row.get('self_id') is not None and qq_number(row['self_id']) != scope.owner:
        raise HistoryError('接口返回的归档账号不一致，已停止')
    if row.get('message_type') != scope.kind:
        raise HistoryError('接口返回了其他会话类型，已停止')
    if scope.kind == 'group' and qq_number(row.get('group_id')) != scope.peer:
        raise HistoryError('接口返回的群号不一致，已停止')
    sender = row.get('sender') or {}
    if not isinstance(sender, dict):
        raise HistoryError('发送人信息格式不正确')
    uin = qq_number(sender.get('user_id') or row.get('user_id'))
    if scope.kind == 'private':
        if uin and uin not in (scope.owner, scope.peer):
            raise HistoryError('私聊发送人不属于所选会话，已停止')
        target = row.get('target_id')
        if target is not None and qq_number(target) not in (scope.owner, scope.peer):
            raise HistoryError('私聊接收人不属于所选会话，已停止')
    stamp = integer(row.get('time'))
    if stamp > 100000000000:  # explicit millisecond timestamp
        stamp //= 1000
    if stamp <= 0 or stamp > int(time.time()) + 86400:
        raise HistoryError('消息时间异常，保留当前断点，请检查接口版本')
    anchor = str(integer(row.get('message_id')))
    if anchor == '0':
        raise HistoryError('消息缺少可继续读取的标识')
    seq = integer(row.get('real_seq'), optional=True)
    if seq is not None and seq <= 0:
        seq = None
    # Include identity fields for idempotence without treating short adapter IDs
    # as globally stable native IDs. Weak matches remain visible as suspects.
    identity = [scope.owner, scope.kind, scope.peer, anchor, stamp, uin]
    sid = hashlib.sha256(json.dumps(identity).encode()).hexdigest()
    return NormalizedMessage(
        scope.owner, scope.kind, scope.peer, stamp, sender_uin=uin,
        sender_uid=str(sender.get('uid') or ''), sender_name=str(sender.get('nickname') or ''),
        sender_card=str(sender.get('card') or ''), is_self=uin == scope.owner,
        seq=seq, msg_random=integer(row.get('msg_random'), optional=True),
        source_msg_id=sid, peer_name=str(row.get('group_name') or ''),
        elements=segments(row.get('message'))), anchor


class HistoryClient(LocalQQ):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # QQ can resolve referenced messages before returning a history page.
        self.request_timeout = 30
        self.response_timeout = 60

    def verify(self, owner):
        data = self.call('get_login_info', {}).get('data')
        if not isinstance(data, dict) or qq_number(data.get('user_id')) != owner:
            raise HistoryError('本机接口登录账号与归档账号不一致')
        return data

    def page(self, scope, cursor, count=100, get_urls=False):
        self.verify(scope.owner)
        args = {('group_id' if scope.kind == 'group' else 'user_id'): scope.peer,
                'count': count, 'reverse_order': True, 'parse_mult_msg': False,
                'disable_get_url': not get_urls, 'quick_reply': True}
        if cursor:
            args['message_seq'] = cursor
        action = 'get_group_msg_history' if scope.kind == 'group' else 'get_friend_msg_history'
        result = self.call(action, args).get('data')
        self.verify(scope.owner)
        if not isinstance(result, dict) or not isinstance(result.get('messages'), list):
            raise HistoryError('接口未返回消息数组；当前断点保留')
        rows = result['messages']
        if len(rows) > 500:
            raise HistoryError('接口单页返回超出限制，请调整接口版本')
        return rows


SCHEMA = """
CREATE TABLE IF NOT EXISTS history_jobs(
 job_key TEXT PRIMARY KEY, scope TEXT NOT NULL, state TEXT NOT NULL,
 cursor TEXT NOT NULL DEFAULT '', oldest INTEGER, pages INTEGER NOT NULL DEFAULT 0,
 received INTEGER NOT NULL DEFAULT 0, inserted INTEGER NOT NULL DEFAULT 0,
 duplicates INTEGER NOT NULL DEFAULT 0, conflicts INTEGER NOT NULL DEFAULT 0,
 suspects INTEGER NOT NULL DEFAULT 0, note TEXT NOT NULL DEFAULT '',
 updated INTEGER NOT NULL, last_page TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS history_media(
 job_key TEXT NOT NULL, message_id INTEGER NOT NULL, elem_index INTEGER NOT NULL,
 kind TEXT NOT NULL, url TEXT NOT NULL, local_path TEXT NOT NULL, name TEXT NOT NULL,
 md5 TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'pending', note TEXT NOT NULL DEFAULT '',
 PRIMARY KEY(job_key,message_id,elem_index)
);
"""


def setup(archive):
    archive.con.executescript(SCHEMA)


def job(archive, scope):
    row = archive.con.execute('SELECT * FROM history_jobs WHERE job_key=?', (scope.key,)).fetchone()
    return dict(row) if row else None


def summary(value):
    return (f"{value['note']}\n已读取 {value['received']:,} 条 · 新增 {value['inserted']:,} · "
            f"重复 {value['duplicates']:,} · 冲突 {value['conflicts']:,} · 疑似重复 {value['suspects']:,}\n"
            "仅表示接口已返回的记录；不能证明此时间段的云端历史完整。")


def queue_media(con, messages, scope):
    # Map by the committed import provenance, never by text or filename.
    from .models import loads_elements
    from .merge import merge_key, collision_key, identity_conflicts, content_hash
    for m in messages:
        if not any(e.get('type') in MEDIA_TYPES for e in m.elements):
            continue
        cid = con.execute('SELECT id FROM conversations WHERE owner=? AND kind=? AND peer=?',
                          (scope.owner, scope.kind, scope.peer)).fetchone()[0]
        key, _ = merge_key(m, 'qq-history')
        r = con.execute('SELECT * FROM messages WHERE conversation_id=? AND merge_key=? AND hidden=0', (cid,key)).fetchone()
        if r is not None and identity_conflicts(r, m):
            r = con.execute('SELECT * FROM messages WHERE conversation_id=? AND merge_key=? AND hidden=0',
                            (cid,collision_key(key,m))).fetchone()
        # Variants may have different element positions. Never attach them to the
        # primary message's elements. Its original attachment references survive.
        if r is None or r['content_hash'] != content_hash(m):
            continue
        for i, e in enumerate(m.elements):
            if e.get('type') not in MEDIA_TYPES:
                continue
            con.execute("""INSERT INTO history_media
                        (job_key,message_id,elem_index,kind,url,local_path,name,md5)
                        VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(job_key,message_id,elem_index)
                        DO UPDATE SET url=excluded.url,local_path=excluded.local_path""",
                        (scope.key, r['id'], i, e['type'], e.get('url') or '',
                         e.get('path') or '', e.get('name') or '', e.get('md5') or ''))
            con.execute("INSERT OR IGNORE INTO message_media VALUES(?,?,NULL,'missing')", (r['id'], i))


def run(archive, client, scope, *, cancel=None, status=None, restart=False,
        max_pages=2000, page_size=20, media_kinds=(), media_root=None, delay=.25, media_only=False):
    cancel = cancel or threading.Event()
    status = status or (lambda *args: None)
    if not 1 <= max_pages <= 10000 or not 2 <= page_size <= 100:
        raise HistoryError('分页参数超出允许范围')
    setup(archive)
    client.verify(scope.owner)
    state = job(archive, scope)
    if restart or not state:
        with archive.con:
            archive.con.execute("""INSERT INTO history_jobs(job_key,scope,state,updated)
             VALUES(?,?,'ready',?) ON CONFLICT(job_key) DO UPDATE SET
             state='ready',cursor='',oldest=NULL,pages=0,received=0,inserted=0,
             duplicates=0,conflicts=0,suspects=0,note='',last_page='',updated=excluded.updated""",
             (scope.key, json.dumps(asdict(scope)), int(time.time())))
        state = job(archive, scope)

    def mark(code, note):
        with archive.con:
            archive.con.execute('UPDATE history_jobs SET state=?,note=?,updated=? WHERE job_key=?',
                                (code, note, int(time.time()), scope.key))
        return job(archive, scope)

    try:
        client.verify(scope.owner)
        if state['state'] != 'range_reached' and not media_only:
            mark('running', '正在读取接口可返回的历史')
            for _ in range(max_pages):
                if cancel.is_set():
                    raise InterruptedError()
                status(f"读取历史 · 已提交 {state['pages']} 页 · 新增 {state['inserted']:,} 条", state['received'], 0)
                rows = None
                for attempt in range(3):
                    try:
                        rows = client.page(scope, state['cursor'], page_size, bool(media_kinds))
                        break
                    except ForwardError:
                        if attempt == 2:
                            raise
                        page_size = max(10, page_size // 2)
                        status('接口暂未响应 · 正在重试，已提交记录保留', state['received'], 0)
                        if cancel.wait(1 + attempt):
                            raise InterruptedError()
                if not rows:
                    mark('unverified', '接口返回空页，停止位置之前的历史尚未验证')
                    break
                parsed = [normalize(r, scope) for r in rows]  # whole-page validation before any write
                # Order by native sequence within a second; adapter IDs are not sequence numbers.
                earliest = min(enumerate(parsed), key=lambda v: (v[1][0].time,
                               v[1][0].seq if v[1][0].seq is not None else v[0]))[1]
                oldest, cursor = earliest[0].time, earliest[1]
                page_hash = hashlib.sha256(json.dumps(sorted(m.source_msg_id for m, _ in parsed)).encode()).hexdigest()
                if page_hash == state['last_page'] or cursor == state['cursor']:
                    mark('unverified', '接口重复返回同一位置，未取得更早消息；可在 QQ 加载历史后重试')
                    break
                if state['oldest'] is not None and oldest > state['oldest']:
                    mark('unverified', '接口返回顺序未向更早时间推进，已停止以避免遗漏')
                    break
                selected = [m for m, _ in parsed if scope.start <= m.time <= scope.end]
                reached = oldest < scope.start  # same-second messages may span pages
                req = ImportRequest('qq-history', 'QQ 历史补全', owner=scope.owner,
                                    range_start=scope.start, range_end=scope.end,
                                    params={'job': scope.key, 'page': state['pages'] + 1, 'complete': False})
                def commit(con, stats):
                    queue_media(con, selected, scope)
                    con.execute("""UPDATE history_jobs SET cursor=?,oldest=?,pages=pages+1,
                     received=received+?,inserted=inserted+?,duplicates=duplicates+?,
                     conflicts=conflicts+?,suspects=suspects+?,last_page=?,state=?,note=?,updated=?
                     WHERE job_key=?""", (cursor, oldest, len(rows), stats.inserted, stats.duplicates,
                     stats.conflicts, stats.suspects, page_hash, 'range_reached' if reached else 'running',
                     '已遍历至开始时间之前，范围完整性仍未验证' if reached else '已保存本页与断点',
                     int(time.time()), scope.key))
                Importer(archive).run(req, selected, cancel=cancel, commit_hook=commit)
                state = job(archive, scope)
                if reached:
                    break
                if cancel.wait(delay):
                    raise InterruptedError()
            else:
                mark('paused', '已达到本次页数上限，可继续读取')
        if cancel.is_set():
            raise InterruptedError()
        from .history_media import process
        media = process(archive, scope.key, set(media_kinds), media_root, cancel, status)
        state = job(archive, scope)
        state['media'] = media
        return state
    except (InterruptedError, ImportCancelled):
        return mark('paused', '已暂停，已提交页面保留；可继续读取')
    except (ForwardError, HistoryError) as exc:
        return mark('unverified', str(exc) + '；已提交页面保留，未能确认剩余历史')
    except Exception:
        mark('failed', '本地保存失败；已提交页面保留，断点未跳过失败页')
        raise
