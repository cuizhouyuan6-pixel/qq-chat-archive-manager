import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import pytest
from qqarchive.history import Scope, HistoryClient, HistoryError, normalize, run, setup, job
from qqarchive.store import Archive
from qqarchive.importer import Importer, ImportRequest
from qqarchive.models import NormalizedMessage


def message(n, stamp=None, **kw):
    row = dict(self_id=100001, message_type='group', group_id=900001, message_id=n,
               message_seq=n, real_seq=str(n + 100), time=stamp or 1760000000 + n,
               sender={'user_id': 200001, 'nickname': '测试发送人'},
               message=[{'type': 'text', 'data': {'text': f'合成消息 {n}'}}])
    row.update(kw)
    return row


@pytest.fixture
def scope():
    return Scope('100001', 'group', '900001', 1760000002, 1760000005)


@pytest.fixture
def arc(tmp_path):
    a = Archive(tmp_path / 'archive.db')
    yield a
    a.close()


class Fake:
    def __init__(self, pages):
        self.pages = iter(pages); self.cursors = []
    def verify(self, owner): return {'user_id': owner}
    def page(self, scope, cursor, count, get_urls):
        self.cursors.append(cursor)
        result = next(self.pages)
        if isinstance(result, Exception): raise result
        return result


def test_bounded_range_merges_and_resumes_atomic_pages(arc, scope):
    first = run(arc, Fake([[message(5),message(4)]]), scope, max_pages=1, delay=0)
    assert (first['state'], first['cursor'], first['inserted']) == ('paused', '4', 2)
    client = Fake([[message(4), message(3)], [message(3), message(2), message(1)]])
    second = run(arc, client, scope, delay=0)
    assert client.cursors == ['4','3']
    assert (second['state'], second['inserted'], second['duplicates']) == ('range_reached', 4, 2)
    assert arc.con.execute('SELECT count(*) FROM messages').fetchone()[0] == 4
    assert arc.integrity_check() == ['ok']
    again = run(arc, Fake([[message(5),message(4),message(3),message(2),message(1)]]), scope, restart=True, delay=0)
    assert again['inserted'] == 0 and again['duplicates'] == 4
    assert all(json.loads(r[0])['complete'] is False for r in arc.con.execute("SELECT params FROM batches WHERE status='done'"))


@pytest.mark.parametrize('rows,note', [([], '空页'),([message(4),message(3)], '重复')])
def test_end_or_repeated_page_never_claims_completeness(arc, scope, rows, note):
    first = run(arc, Fake([[message(4),message(3)], rows]), scope, delay=0)
    assert first['state'] == 'unverified' and note in first['note']
    assert first['cursor'] == '3' and first['inserted'] == 2


def test_malformed_page_does_not_skip_checkpoint_or_partial_rows(arc, scope):
    bad = message(2, group_id=999999)
    value = run(arc, Fake([[message(5), message(4)], [message(3), bad]]), scope, delay=0)
    assert value['state'] == 'unverified' and value['cursor'] == '4'
    assert value['inserted'] == 2 and '群号不一致' in value['note']


def test_checkpoint_hook_failure_rolls_back_messages_and_fts(arc):
    setup(arc)
    def broken(con, stats):
        con.execute("INSERT INTO history_jobs(job_key,scope,state,updated) VALUES('x','{}','running',0)")
        raise RuntimeError('simulated crash')
    with pytest.raises(RuntimeError):
        Importer(arc).run(ImportRequest('qq-history','test'), [
            NormalizedMessage('1','group','2',1760000000,seq=1,elements=[{'type':'text','text':'test'}])],
            commit_hook=broken)
    assert not arc.con.execute('SELECT * FROM messages').fetchall()
    assert not arc.con.execute('SELECT * FROM history_jobs').fetchall()
    assert arc.integrity_check() == ['ok']


def test_cancellation_after_page_preserves_cursor(arc, scope):
    cancel = threading.Event()
    class Client(Fake):
        def page(self, *args):
            if len(self.cursors) == 1:
                cancel.set()
            return super().page(*args)
    value = run(arc, Client([[message(5),message(4)],[message(3),message(2)]]), scope, delay=0, cancel=cancel)
    assert value['state'] == 'paused' and value['cursor'] == '4' and value['inserted'] == 2


def test_same_second_boundary_keeps_paginating(arc, scope):
    scope = Scope(scope.owner, scope.kind, scope.peer, 1760000002, 1760000005)
    value = run(arc, Fake([[message(5),message(4,1760000002)],
                          [message(4,1760000002),message(3,1760000002)],
                          [message(3,1760000002),message(2,1760000001)]]), scope, delay=0)
    assert value['inserted'] == 3 and value['pages'] == 3


def test_real_seq_used_for_cross_source_merge_but_adapter_id_is_not(arc, scope):
    original = NormalizedMessage(scope.owner, scope.kind, scope.peer, 1760000004,
             seq=104,sender_uin='200001',elements=[{'type':'text','text':'合成消息 4'}])
    Importer(arc).run(ImportRequest('ntqq','test'), [original])
    value = run(arc, Fake([[message(4),message(1)]]), scope, delay=0)
    assert value['duplicates'] == 1 and value['inserted'] == 0
    row = message(4); row.pop('real_seq')
    parsed, anchor = normalize(row, scope)
    assert parsed.seq is None and anchor == '4'
    row2 = message(9,1760000004,real_seq='104')
    row2['sender']['user_id'] = 200002
    value = run(arc, Fake([[row2,message(1)]]), scope, restart=True, delay=0)
    assert value['inserted'] == 1  # same sequence, different sender must survive


@pytest.mark.parametrize('patch', [
    {'self_id':100002}, {'group_id':123}, {'message_type':'private'},
    {'time':0}, {'time':'NaN'}, {'message_id':None}, {'message_id':True},
    {'message':'[CQ:image,file=some-file]'}, {'message':None},
])
def test_reject_bad_rows(scope, patch):
    with pytest.raises(HistoryError):
        normalize(message(4, **patch), scope)


def test_private_outbound_inbound_and_timestamp(scope):
    s = Scope(scope.owner,'private','200001',scope.start,scope.end)
    row = message(4,message_type='private')
    row['time'] *= 1000
    m,_ = normalize(row,s)
    assert m.time == 1760000004 and not m.is_self
    row['sender']['user_id'] = 100001
    assert normalize(row,s)[0].is_self
    row['target_id'] = 999999
    with pytest.raises(HistoryError): normalize(row,s)


@pytest.fixture
def http():
    state = {'calls':[], 'owner':100001, 'rows':[], 'switch_after':False}
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args): pass
        def do_POST(self):
            body=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            state['calls'].append((self.path,body))
            data={'user_id':state['owner']} if self.path=='/get_login_info' else {'messages':state['rows']}
            if self.path!='/' and self.path!='/get_login_info' and state['switch_after']:
                state['owner']=100002
            raw=json.dumps({'status':'ok','retcode':0,'data':data}).encode()
            self.send_response(200);self.send_header('Content-Length',str(len(raw)));self.end_headers();self.wfile.write(raw)
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    yield f'http://127.0.0.1:{server.server_port}',state
    server.shutdown();server.server_close();thread.join()


def test_http_scope_anchor_and_account_switch(arc,scope,http):
    url,state=http;state['rows']=[message(4),message(1)]
    result=run(arc,HistoryClient(url),scope,delay=0)
    assert result['inserted']==1
    args=next(args for action,args in state['calls'] if action=='/get_group_msg_history')
    assert args['group_id']==scope.peer and args['parse_mult_msg'] is False
    assert args['reverse_order'] is True and args['disable_get_url'] is True
    state['switch_after']=True;state['rows']=[message(5)]
    result=run(arc,HistoryClient(url),scope,restart=True,delay=0)
    assert result['inserted']==0 and result['cursor']=='' and '账号不一致' in result['note']


def test_account_mismatch_does_not_reset_previous_checkpoint(arc,scope,http):
    run(arc,Fake([[message(5),message(4)]]),scope,max_pages=1,delay=0)
    url,state=http;state['owner']=100002
    with pytest.raises(HistoryError):
        run(arc,HistoryClient(url),scope,restart=True,delay=0)
    assert job(arc,scope)['cursor']=='4'


def test_local_media_is_referenced_not_copied(arc,scope,tmp_path):
    path=tmp_path/'existing.png';path.write_bytes(b'synthetic-media')
    row=message(4,message=[{'type':'image','data':{'path':str(path),'file':'existing.png'}}])
    value=run(arc,Fake([[row,message(1)]]),scope,delay=0)
    assert value['media']['linked']==1 and path.read_bytes()==b'synthetic-media'
    assert not (tmp_path/'历史补全附件').exists()
    linked=arc.con.execute("SELECT local_path FROM media_files").fetchone()[0]
    assert linked==str(path)


def test_missing_attachments_are_reported_not_fabricated(arc,scope):
    row=message(4,message=[{'type':'image','data':{'file':'missing.png'}}])
    value=run(arc,Fake([[row,message(1)]]),scope,delay=0)
    assert value['media']['missing']==1
    assert arc.con.execute("SELECT status FROM message_media").fetchone()[0]=='missing'


def test_slow_page_retries_shrink_without_advancing_cursor(arc,scope):
    from qqarchive.forwards import ForwardError
    class Client(Fake):
        def __init__(self): super().__init__([]); self.counts=[]
        def page(self, scope, cursor, count, get_urls):
            self.counts.append((cursor,count))
            if len(self.counts)<3: raise ForwardError('slow')
            return [message(4),message(1)]
    client=Client()
    class FastEvent:
        def is_set(self): return False
        def wait(self, seconds): return False
    result=run(arc,client,scope,cancel=FastEvent(),delay=0,page_size=100)
    assert client.counts==[('',100),('',50),('',25)]
    assert result['pages']==1 and result['inserted']==1
