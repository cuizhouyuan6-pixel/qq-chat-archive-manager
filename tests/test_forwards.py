import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import pytest
from qqarchive.forwards import (ForwardError, normalize, descriptor, card_descriptor,
                                LocalQQ, cache_key, save_cache, cache_path,
                                payload_from_file, recover_descriptor, uid_resolver)
from qqarchive.ntqq import parse_element
from qqarchive.protobuf import encode


def node(uin='123456', text='测试正文', **extra):
    return dict(sender={'user_id': uin, 'nickname': '测试用户'}, time=1760000000,
                message=[{'type': 'text', 'data': {'text': text}}], **extra)


def card(ref='forward-test-resource'):
    return {'app': 'com.tencent.multimsg', 'prompt': '[聊天记录]',
            'meta': {'detail': {'resid': ref, 'source': '测试转发'}}}


def test_ntqq_card_keeps_resource_without_changing_type_or_preview():
    elem = parse_element(encode({45002: 10, 99999: json.dumps(card())}))
    assert elem['type'] == 'card' and elem['text'] == '[聊天记录]'
    assert descriptor({'elements': [elem]})['id'] == 'forward-test-resource'
    assert card_descriptor({'app': 'other', 'meta': card()['meta']}) is None


def test_native_forward_xml_and_unsupported_bytes_preserved():
    xml = '<msg serviceID="35" m_resid="test-res"><item><title>123456</title></item></msg>'
    elem = parse_element(encode({45002: 16, 48153: xml}))
    assert elem['forward']['id'] == 'test-res' and elem['raw_forward_pb']
    unknown = parse_element(encode({45002: 16, 45001: 123456}))
    assert descriptor({'elements': [unknown]}) == unknown
    assert 'sender_uin' not in unknown


def test_realistic_onebot_response_identity_and_nested_nodes():
    first = node()
    first['message'].append({'type': 'forward', 'data': {'content': [
        {'type': 'node', 'data': {'user_id': '654321', 'nickname': '内部用户',
         'content': [{'type': 'text', 'data': {'text': '内部消息'}}]}}]}})
    rows = normalize({'status': 'ok', 'retcode': 0, 'data': {'messages': [first]}})
    assert rows[0]['sender_uin'] == '123456'
    assert rows[0]['children'][0]['sender_uin'] == '654321'
    assert rows[0]['children'][0]['time'] == 0


def test_uid_mapping_is_explicit_and_missing_does_not_inherit():
    rows = normalize([node('', senderUid='u_test'), node('', '我的号码是 123456')],
                     lambda uid: '7654321' if uid == 'u_test' else '')
    assert rows[0]['sender_uin'] == '7654321' and rows[0]['identity_source'] == '本地 UID 对应号码'
    assert rows[1]['sender_uin'] == ''
    assert normalize([node(True)])[0]['sender_uin'] == ''
    assert normalize([node(123456.0)])[0]['sender_uin'] == ''


def test_timestamp_units_and_nested_resource_kept():
    first = node(); first['time'] = 1760000000123
    first['message'] = [{'type': 'forward', 'data': {'id': 'unfetched-resource'}}]
    out = normalize([first])[0]
    assert out['time'] == 1760000000 and out['refs'] == ['unfetched-resource']


@pytest.mark.parametrize('payload', [[], {}, card(), {'status': 'failed'}, [{'sender': {'user_id': '123456'}}]], ids=['empty','object','card','failed','missing-body'])
def test_incomplete_results_fail_without_fabricating_rows(payload):
    with pytest.raises(ForwardError):
        normalize(payload)


def test_limits_and_cancel():
    message = node()
    for _ in range(14):
        outer = node(); outer['message'] = [{'type': 'forward', 'data': {'content': [message]}}]; message = outer
    with pytest.raises(ForwardError, match='12'):
        normalize([message])
    with pytest.raises(ForwardError, match='10,000'):
        normalize([node()] * 10001)
    cancel = threading.Event(); cancel.set()
    with pytest.raises(InterruptedError):
        normalize([node()], cancel=cancel)


def test_cache_roundtrip_is_scoped_and_does_not_overwrite_source(tmp_path):
    message = dict(id=1, conversation_id=2, source_msg_id='500', time=1700000000)
    key = cache_key('123456', message)
    assert key != cache_key('654321', message)
    assert key != cache_key('123456', dict(message, source_msg_id='501'))
    payload = {'messages': [node()]}
    save_cache(tmp_path, key, payload, '本地 JSON')
    assert normalize(payload_from_file(cache_path(tmp_path, key)))[0]['sender_uin'] == '123456'
    with pytest.raises(ForwardError):
        cache_path(tmp_path, '../bad')
    assert not list((tmp_path / 'forwards').glob('.forward-*'))


@pytest.mark.parametrize('url', ['http://example.com:3000','http://127.0.0.1.evil/','https://127.0.0.1:3000',
                                 'http://user:pass@localhost:3000', 'http://localhost:3000/x', 'http://localhost:3000/?token=x'])
def test_remote_or_ambiguous_endpoints_rejected(url):
    with pytest.raises(ForwardError):
        LocalQQ(url)


@pytest.fixture
def server():
    state = {'calls': [], 'owner': '123456', 'failed': False, 'redirect': False}
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args): pass
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            state['calls'].append((self.path, body, self.headers.get('Authorization')))
            if state['redirect']:
                self.send_response(302); self.send_header('Location', 'http://example.com/'); self.end_headers(); return
            result = {'status': 'failed'} if state['failed'] else {'status': 'ok', 'retcode': 0, 'data':
                     {'user_id': state['owner']} if self.path == '/get_login_info' else {'messages': [node()]}}
            raw = json.dumps(result).encode()
            self.send_response(200); self.send_header('Content-Length', str(len(raw))); self.end_headers(); self.wfile.write(raw)
    srv = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=srv.serve_forever, daemon=True); thread.start()
    yield f'http://127.0.0.1:{srv.server_port}', state
    srv.shutdown(); srv.server_close(); thread.join()


def test_http_end_to_end_and_account_guard(server):
    url, state = server
    out = LocalQQ(url, 'test-token').fetch('test-resource', '123456')
    assert normalize(out)[0]['sender_uin'] == '123456'
    assert state['calls'][1] == ('/get_forward_msg', {'message_id': 'test-resource'}, 'Bearer test-token')
    state['calls'].clear()
    with pytest.raises(ForwardError, match='账号不一致'):
        LocalQQ(url).fetch('test-resource', '111111')
    assert len(state['calls']) == 1
    state['redirect'] = True
    with pytest.raises(ForwardError):
        LocalQQ(url).fetch('test-resource', '123456')
    assert len(state['calls']) == 2


def test_failed_http_does_not_count_as_empty_success(server):
    url, state = server; state['failed'] = True
    with pytest.raises(ForwardError):
        LocalQQ(url).fetch('resource', '123456')


def test_original_row_recovery_does_not_copy_or_modify_source(tmp_path):
    import hashlib
    from ntqq_fixture import make_ntqq_db, group_row
    path = make_ntqq_db(tmp_path / 'nt_msg.db', group_rows=[
        group_row(73, 99, 10000, 123456, 1760000000, [{45002: 10, 99999: json.dumps(card())}])], plain=True)
    before = hashlib.sha256(path.read_bytes()).digest()
    assert recover_descriptor(path, '123456', 'group', '10000', '73', 1760000000)['id'] == 'forward-test-resource'
    with pytest.raises(ForwardError, match='不匹配'):
        recover_descriptor(path, '123456', 'group', '20000', '73', 1760000000)
    assert hashlib.sha256(path.read_bytes()).digest() == before


def test_uid_mapping_cannot_cross_accounts_or_choose_ambiguous(tmp_path):
    from qqarchive.store import Archive
    arc = Archive(tmp_path / 'archive.db')
    try:
        arc.con.executemany('INSERT INTO profiles(owner,kind,peer,uid) VALUES(?,?,?,?)',
                            [('1','private','123456','u_test'),('2','private','222222','u_test'),
                             ('1','private','333333','u_conflict'),('1','private','444444','u_conflict')])
        assert uid_resolver(arc,'1')('u_test') == '123456'
        assert uid_resolver(arc,'3')('u_test') == ''
        assert uid_resolver(arc,'1')('u_conflict') == ''
    finally:
        arc.close()
