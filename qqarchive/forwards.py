"""Bounded forward-message parsing. Identities are metadata, never guessed from text.

Wire shapes follow NapCat's get_forward_msg and OneBot 11 node definitions.
This module neither logs in to QQ nor changes QQ's databases.
"""
from __future__ import annotations

import hashlib
import json
import re
import threading
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, build_opener, ProxyHandler, HTTPRedirectHandler
from xml.etree import ElementTree

MAX_BYTES = 8 * 1024 * 1024
MAX_NODES = 10000
MAX_DEPTH = 12


class ForwardError(ValueError):
    pass


def qq_number(value):
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        return ''
    text = str(value)
    return text if re.fullmatch(r'[1-9][0-9]{0,19}', text) else ''


def bounded_json(raw):
    if len(raw) > MAX_BYTES:
        raise ForwardError('转发正文超过 8 MB，请分批处理')
    try:
        return json.loads(raw)
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise ForwardError('文件不是有效的转发 JSON') from exc


def card_descriptor(value):
    """Only recognize explicit forward card fields; numbers in previews are text."""
    if isinstance(value, bytes):
        if len(value) > MAX_BYTES:
            return None
        try:
            value = value.decode('utf-8')
        except UnicodeError:
            return None
    if isinstance(value, str):
        if len(value) > MAX_BYTES:
            return None
        value = value.strip()
        if value.startswith('{'):
            try:
                value = bounded_json(value)
            except ForwardError:
                return None
        elif value.startswith('<msg') and '<!DOCTYPE' not in value.upper() and '<!ENTITY' not in value.upper():
            try:
                root = ElementTree.fromstring(value)
            except (ElementTree.ParseError, ValueError):
                return None
            if root.get('serviceID') == '35' and root.get('m_resid'):
                return {'id': root.get('m_resid'), 'title': '合并转发聊天记录'}
            return None
        else:
            return None
    if not isinstance(value, dict) or value.get('app') != 'com.tencent.multimsg':
        return None
    meta = value.get('meta')
    detail = meta.get('detail') if isinstance(meta, dict) else None
    if not isinstance(detail, dict):
        return None
    ref = detail.get('resid')
    return {'id': str(ref) if isinstance(ref, (str, int)) else '',
            'title': str(detail.get('source') or '合并转发聊天记录')[:500]}


def descriptor(message):
    for element in message.get('elements', []):
        if not isinstance(element, dict):
            continue
        if isinstance(element.get('forward'), dict):
            return element['forward']
        if element.get('type') == 'forward':
            data = element.get('data', element)
            return data if isinstance(data, dict) else {}
        if element.get('type') in ('card', 'json'):
            data = element.get('data', element.get('text', ''))
            if isinstance(data, dict) and 'data' in data:
                data = data['data']
            found = card_descriptor(data)
            if found is not None:
                return found
    return None


def normalize(payload, resolve_uid=None, cancel=None):
    """Normalize whole responses, message arrays, and OneBot custom nodes.

    Missing sender IDs stay missing. Nested nodes keep their own identities and
    do not become messages in the receiving conversation.
    """
    if isinstance(payload, dict) and ('status' in payload or 'retcode' in payload):
        if payload.get('status', 'ok') != 'ok' or payload.get('retcode', 0) != 0:
            raise ForwardError('QQ 接口未返回转发正文，请确认记录仍可在 QQ 中展开')
        payload = payload.get('data')
    if isinstance(payload, dict):
        payload = payload.get('messages', payload.get('content'))
    if not isinstance(payload, list):
        raise ForwardError('未找到转发消息列表；卡片摘要不能作为完整正文')
    count = 0

    def nodes(items, depth):
        nonlocal count
        if depth > MAX_DEPTH:
            raise ForwardError('转发嵌套超过 12 层，已停止解析')
        result = []
        for node in items:
            if cancel and cancel.is_set():
                raise InterruptedError('已取消读取')
            count += 1
            if count > MAX_NODES:
                raise ForwardError('单次转发超过 10,000 条，请分批处理')
            if not isinstance(node, dict):
                raise ForwardError('转发消息格式不完整')
            if node.get('type') == 'node':
                node = node.get('data')
            if not isinstance(node, dict):
                raise ForwardError('转发节点格式不完整')
            sender = node.get('sender') if isinstance(node.get('sender'), dict) else {}
            uin = qq_number(sender.get('user_id')) or qq_number(node.get('user_id')) or qq_number(node.get('senderUin'))
            uid = str(node.get('senderUid') or sender.get('uid') or node.get('sender_uid') or '')[:200]
            identity = '记录内号码' if uin else '未获取到 QQ 号'
            if not uin and uid and resolve_uid:
                uin = qq_number(resolve_uid(uid))
                if uin:
                    identity = '本地 UID 对应号码'
            name = str(sender.get('nickname') or node.get('nickname') or node.get('sendNickName') or '')[:1000]
            body = node.get('message', node.get('content', node.get('elements')))
            if body is None:
                raise ForwardError('转发节点缺少消息正文')
            elements, children, refs = [], [], []
            if isinstance(body, str):
                elements.append({'type': 'text', 'text': body})
            elif isinstance(body, list):
                for segment in body:
                    if not isinstance(segment, dict):
                        raise ForwardError('转发消息元素格式不完整')
                    kind = segment.get('type', '')
                    data = segment.get('data', segment)
                    if not isinstance(data, dict):
                        raise ForwardError('转发消息元素格式不完整')
                    if kind == 'node':
                        children.extend(nodes([segment], depth + 1))
                    elif kind == 'forward':
                        content = data.get('content', data.get('messages'))
                        if isinstance(content, list):
                            children.extend(nodes(content, depth + 1))
                        elif data.get('id'):
                            refs.append(str(data['id'])[:2048])
                        elements.append({'type': 'text', 'text': '[合并转发] '})
                    elif kind == 'json' and (card := card_descriptor(data.get('data'))):
                        if card['id']:
                            refs.append(card['id'])
                        elements.append({'type': 'text', 'text': '[合并转发] '})
                    elif kind == 'text':
                        elements.append({'type': 'text', 'text': str(data.get('text', ''))})
                    elif kind == 'at':
                        elements.append({'type': 'text', 'text': '@' + str(data.get('qq', '未知用户'))})
                    else:
                        # Keep media descriptions; never fetch URLs or open paths from a forward.
                        label = {'image': '图片', 'video': '视频', 'file': '文件', 'record': '语音', 'face': '表情', 'reply': '引用'}.get(kind, '其他消息')
                        elements.append({'type': 'text', 'text': '[' + label + '] ' + str(data.get('name', ''))})
            else:
                raise ForwardError('消息正文格式不支持')
            raw_time = node.get('time', node.get('msgTime', 0))
            try:
                stamp = int(raw_time)
                if stamp > 100000000000:
                    stamp //= 1000
                if not 0 < stamp < 32503680000:
                    stamp = 0
            except (ValueError, TypeError, OverflowError):
                stamp = 0
            result.append(dict(sender_uin=uin, sender_uid=uid, sender_name=name,
                               sender_card=str(sender.get('card') or node.get('sendMemberName') or '')[:1000],
                               identity_source=identity, time=stamp, text=''.join(e['text'] for e in elements),
                               elements=elements, children=children, refs=refs))
        return result

    result = nodes(payload, 0)
    if not result:
        raise ForwardError('接口没有返回任何转发消息；未保存空结果')
    return result


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class LocalQQ:
    """Explicit loopback-only, read-only OneBot calls. Tokens stay in memory."""
    def __init__(self, endpoint, token='', cancel=None):
        try:
            parsed = urlsplit(endpoint)
            port = parsed.port
        except ValueError as exc:
            raise ForwardError('本机接口地址不正确') from exc
        if (parsed.scheme != 'http' or parsed.hostname not in ('127.0.0.1', 'localhost', '::1')
                or parsed.username or parsed.password or parsed.path not in ('', '/') or parsed.query or parsed.fragment):
            raise ForwardError('仅支持本机 HTTP 接口，例如 http://127.0.0.1:3000')
        self.endpoint = endpoint.rstrip('/')
        self.token = token
        self.request_timeout = 8
        self.response_timeout = 20
        self.cancel = cancel or threading.Event()
        self.opener = build_opener(ProxyHandler({}), NoRedirect())

    def call(self, action, args):
        if action not in ('get_login_info', 'get_forward_msg', 'get_group_msg_history', 'get_friend_msg_history'):
            raise ForwardError('不支持的读取操作')
        if self.cancel.is_set():
            raise InterruptedError('已取消读取')
        headers = {'Content-Type': 'application/json'}
        if self.token:
            headers['Authorization'] = 'Bearer ' + self.token
        req = Request(self.endpoint + '/' + action, json.dumps(args).encode(), headers, method='POST')
        try:
            with self.opener.open(req, timeout=self.request_timeout) as response:
                data = bytearray()
                deadline = time.monotonic() + self.response_timeout
                while True:
                    if self.cancel.is_set():
                        raise InterruptedError('已取消读取')
                    if time.monotonic() > deadline:
                        raise ForwardError('读取超时，请稍后重试')
                    block = response.read1(min(65536, MAX_BYTES + 1 - len(data)))
                    if not block:
                        break
                    data.extend(block)
                    if len(data) > MAX_BYTES:
                        raise ForwardError('接口返回过大，请分批处理')
        except HTTPError as exc:
            raise ForwardError('接口认证失败，请检查令牌' if exc.code in (401, 403) else '本机接口请求失败，请检查接口版本和转发标识') from exc
        except TimeoutError as exc:
            raise ForwardError('QQ 接口读取超时，可能正在加载历史或解析引用消息；请稍后重试') from exc
        except (URLError, OSError) as exc:
            raise ForwardError('无法连接 QQ 本机接口，请先启动并配置 NapCat HTTP 服务') from exc
        result = bounded_json(data)
        if not isinstance(result, dict) or result.get('status') != 'ok' or result.get('retcode', 0) != 0:
            raise ForwardError('QQ 接口未返回有效结果，记录可能未加载或已失效')
        return result

    def fetch(self, ref, owner):
        if not ref or len(ref) > 2048:
            raise ForwardError('缺少有效转发标识；请提供原始卡片或转发正文 JSON')
        logged = self.call('get_login_info', {}).get('data', {})
        if not qq_number(owner) or qq_number(logged.get('user_id')) != str(owner):
            raise ForwardError('本机接口登录账号与归档账号不一致，已停止读取')
        return self.call('get_forward_msg', {'message_id': ref})


def cache_key(owner, message):
    identity = [str(owner), message.get('id'), message.get('source_msg_id'), message.get('conversation_id'), message.get('time')]
    return hashlib.sha256(json.dumps(identity).encode()).hexdigest()


def resource_key(owner, ref):
    return hashlib.sha256(json.dumps(['resource', str(owner), str(ref)]).encode()).hexdigest()


def cache_path(data_dir, key):
    if not re.fullmatch('[a-f0-9]{64}', key):
        raise ForwardError('转发缓存标识无效')
    return Path(data_dir) / 'forwards' / (key + '.json')


def read_file(path):
    with Path(path).open('rb') as f:
        return bounded_json(f.read(MAX_BYTES + 1))


def save_cache(data_dir, key, payload, source):
    import os
    import tempfile
    raw = json.dumps({'format': 'qqarchive-forward-1', 'source': source, 'saved_at': int(time.time()), 'payload': payload}, ensure_ascii=False).encode('utf-8')
    if len(raw) > MAX_BYTES:
        raise ForwardError('转发正文超过本地缓存大小限制')
    path = cache_path(data_dir, key)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix='.forward-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as f:
            f.write(raw)
        os.replace(tmp, path)
    finally:
        Path(tmp).unlink(missing_ok=True)


def payload_from_file(path):
    data = read_file(path)
    return data.get('payload') if isinstance(data, dict) and data.get('format') == 'qqarchive-forward-1' else data


def uid_resolver(archive, owner):
    memo = {}
    def resolve(uid):
        if uid not in memo:
            rows = archive.con.execute("SELECT DISTINCT peer FROM profiles WHERE owner=? AND kind='private' AND uid=? LIMIT 2", (owner, uid)).fetchall()
            memo[uid] = rows[0][0] if len(rows) == 1 else ''
        return memo[uid]
    return resolve


def recover_descriptor(path, owner, kind, peer, source_id, expected_time, keys=(), cancel=None):
    """Read one original row, checking identity; never scan or copy a whole DB."""
    from .ntqq import NtqqSource, TABLES, row_to_message, WANTED
    from .source_guard import StableSource
    from .sqlcipher import detect_header
    if kind not in TABLES or not re.fullmatch(r'-?[0-9]{1,20}', str(source_id)):
        raise ForwardError('这条记录缺少原始消息 ID，无法从原库准确定位；请打开转发 JSON')
    source_id = int(source_id)
    if not -(1 << 63) <= source_id < (1 << 63):
        raise ForwardError('原始消息 ID 超出此数据库支持的范围')
    pairs = [(None, None)] if detect_header(path)['kind'] == 'sqlite' else list(keys)[:8]
    if not pairs:
        raise ForwardError('原库已加密，请先在导入页面获取并验证该库的密钥，再返回重试')
    source = None
    with StableSource(path):
        for key, params in pairs:
            if cancel and cancel.is_set():
                raise InterruptedError('已取消读取')
            try:
                source = NtqqSource(path, owner, key, params)
                break
            except (ValueError, OSError):
                continue
        if source is None:
            raise ForwardError('已有密钥不能打开所选原库，请在导入页面重新验证')
        try:
            columns = source._columns[kind]
            if not {'40001', '40030', '40050', '40800'} <= columns:
                raise ForwardError('所选数据库不包含该类型的原始消息表')
            deadline = time.monotonic() + 8
            source.conn.set_progress_handler(lambda: bool(cancel and cancel.is_set()) or time.monotonic() > deadline, 1000)
            names = [name for name in WANTED if name in columns]
            select = ','.join('"' + name + '"' for name in names)
            raw = next(iter(source.conn.execute(f'SELECT {select} FROM {TABLES[kind]} WHERE "40001"=? LIMIT 1', (int(source_id),))), None)
            if raw is None:
                raise ForwardError('所选原库中找不到这条消息；请确认账号和数据库来源')
            message = row_to_message(kind, dict(zip(names, raw)), owner)
            if message.peer != str(peer) or message.time != expected_time:
                raise ForwardError('原始消息的聊天或时间不匹配，已停止补读')
            found = descriptor(message.to_dict())
            if not found or not found.get('id'):
                raise ForwardError('已定位原始消息，但此格式没有可解析的转发资源标识；请使用转发 JSON')
            return found
        except __import__('apsw').InterruptError as exc:
            if cancel and cancel.is_set():
                raise InterruptedError('已取消补读') from exc
            raise ForwardError('原库定位超过 8 秒，已停止；可改用转发 JSON') from exc
        finally:
            source.close()
