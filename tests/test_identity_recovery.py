from qqarchive.importer import Importer,ImportRequest
from qqarchive.models import NormalizedMessage
from qqarchive.store import Archive

def message(random=1, sender='20002', when=1700000000, text='同样内容'):
    return NormalizedMessage(owner='10001',kind='group',peer='30003',time=when,
        seq=8,msg_random=random,sender_uin=sender,source_msg_id=str(random),elements=[{'type':'text','text':text}])

def test_reused_sequence_preserves_different_messages_and_repeat_is_idempotent(tmp_path):
    with_archive=Archive(tmp_path/'archive.db')
    try:
        a=with_archive
        rows=[message(),message(2),message(3,sender='40004'),message(4,when=1700000008)]
        stats=Importer(a).run(ImportRequest('ntqq-pc','source',owner='10001'),rows)
        assert stats.inserted==4 and stats.conflicts==0 and stats.duplicates==0
        stats=Importer(a).run(ImportRequest('ntqq-pc','source',owner='10001'),list(reversed(rows)))
        assert stats.inserted==0 and stats.duplicates==4
        assert a.count_messages()==4
    finally:with_archive.close()

def test_existing_legacy_row_is_kept_and_new_identity_is_added(tmp_path):
    a=Archive(tmp_path/'archive.db')
    try:
        Importer(a).run(ImportRequest('test','original'),[message()])
        original=dict(a.con.execute('SELECT * FROM messages').fetchone())
        assert original['merge_key']=='g:30003:8'
        stats=Importer(a).run(ImportRequest('test','recovery'),[message(),message(2)])
        assert stats.inserted==1 and stats.duplicates==1
        assert dict(a.con.execute('SELECT * FROM messages WHERE id=?',(original['id'],)).fetchone())==original
    finally:a.close()

def test_same_identity_changed_content_remains_a_variant(tmp_path):
    a=Archive(tmp_path/'archive.db')
    try:
        Importer(a).run(ImportRequest('test','one'),[message(),message(2)])
        stats=Importer(a).run(ImportRequest('test','two'),[message(2,text='实际内容版本')])
        assert stats.inserted==0 and stats.conflicts==1
        assert a.count_messages()==2
    finally:a.close()

def test_bulk_chunk_with_sequence_collisions_has_same_result(tmp_path):
    a=Archive(tmp_path/'archive.db')
    try:
        rows=[message(i+1) for i in range(110)]
        stats=Importer(a).run(ImportRequest('test','many'),rows)
        assert stats.inserted==110 and stats.conflicts==0
        stats=Importer(a).run(ImportRequest('test','again'),rows)
        assert stats.duplicates==110
    finally:a.close()
