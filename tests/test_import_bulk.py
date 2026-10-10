import threading
import pytest
from qqarchive.importer import Importer,ImportRequest,ImportCancelled
from qqarchive.models import NormalizedMessage
from qqarchive.store import Archive

def message(i,**kwargs):
    values=dict(owner='10001',kind='private' if i%3==0 else 'group',peer=str(20000+i%7),time=1700000000+i,
                sender_uin='30001',peer_name='测试联系人',seq=i+1,source_msg_id=str(i),
                elements=[{'type':'text','text':f'批量归档 内容检索 {i}'}])
    values.update(kwargs);return NormalizedMessage(**values)

def integrity(a):
    a.con.execute("INSERT INTO messages_fts(messages_fts,rank) VALUES('integrity-check',1)");a.con.commit()
    assert a.integrity_check()==['ok']
    assert a.con.execute("SELECT 1 FROM sqlite_master WHERE name='trg_msg_ai'").fetchone()
    assert not a.con.execute("SELECT 1 FROM sqlite_temp_master WHERE name='qqarchive_import_stage'").fetchone()

class SerialImporter(Importer):
    def _begin_search_batch(self):self._fts_trigger=None;self._fts_first=self._fts_last=None
    def _queue(self,batch,cid,m,kind,stats):self._one(batch,cid,m,kind,stats)

def test_bulk_and_serial_have_identical_merge_results(tmp_path):
    fast=Archive(tmp_path/'fast.db');serial=Archive(tmp_path/'serial.db')
    batches=[list(message(i) for i in range(4501)),
             [message(i) for i in range(4200,7000)],
             [message(50,elements=[{'type':'text','text':'冲突内容'}]),message(51,seq=None,source_msg_id='weak'),
              message(52,elements=[{'type':'image','md5':'a'*32,'name':'测试.png'}])],
             [message(i,seq=None) for i in range(8000,10300)]]
    try:
        for index,batch in enumerate(batches):
            a=Importer(fast).run(ImportRequest('test',str(index)),batch)
            b=SerialImporter(serial).run(ImportRequest('test',str(index)),batch)
            assert a.to_dict()==b.to_dict()
        for table in ('messages','message_sources','coverage','conversations'):
            cols=[r[1] for r in fast.con.execute(f'PRAGMA table_info({table})') if r[1]!='created_at']
            sql='SELECT '+','.join(cols)+' FROM '+table+' ORDER BY 1,2'
            assert [tuple(r) for r in fast.con.execute(sql)]==[tuple(r) for r in serial.con.execute(sql)]
        for a in (fast,serial):
            integrity(a)
            assert a.con.execute("SELECT count(*) FROM messages_fts WHERE messages_fts MATCH '\"批量归档\"'").fetchone()[0]==9301
    finally:fast.close();serial.close()

@pytest.mark.parametrize('failure',['cancel','source_error','index_error'])
def test_atomic_rollback_restores_index_trigger_and_cache(tmp_path,failure):
    a=Archive(tmp_path/'archive.db');Importer(a).run(ImportRequest('test','existing'),[message(10000)])
    before=[tuple(r) for r in a.con.execute('SELECT * FROM messages')];cache=a.con.execute('PRAGMA cache_size').fetchone()[0]
    event=threading.Event()
    def source():
        for i in range(4500):
            if i==2500:
                if failure=='cancel':event.set()
                if failure=='source_error':raise RuntimeError('synthetic source failure')
            yield message(i)
    class FailingIndexer(Importer):
        def _flush_search_batch(self):
            super()._flush_search_batch()
            raise RuntimeError('synthetic index failure')
    try:
        cls=FailingIndexer if failure=='index_error' else Importer
        with pytest.raises((ImportCancelled,RuntimeError)):cls(a).run(ImportRequest('test','failed'),source(),cancel=event)
        assert [tuple(r) for r in a.con.execute('SELECT * FROM messages')]==before
        assert a.con.execute('PRAGMA cache_size').fetchone()[0]==cache
        integrity(a)
        assert Importer(a).run(ImportRequest('test','retry'),(message(i) for i in range(4500))).inserted==4500
        integrity(a)
    finally:a.close()

def test_custom_trigger_is_preserved_and_empty_import_works(tmp_path):
    a=Archive(tmp_path/'archive.db')
    try:
        a.con.execute('CREATE TABLE audit(mid INTEGER)')
        a.con.execute('CREATE TRIGGER local_audit AFTER INSERT ON messages BEGIN INSERT INTO audit VALUES(new.id); END');a.con.commit()
        assert Importer(a).run(ImportRequest('test','empty'),[]).inserted==0
        assert Importer(a).run(ImportRequest('test','full'),(message(i) for i in range(2200))).inserted==2200
        assert a.con.execute('SELECT count(*) FROM audit').fetchone()[0]==2200
        integrity(a)
    finally:a.close()

def test_duplicate_fingerprints_in_chunk_retain_suspect_rules(tmp_path):
    a=Archive(tmp_path/'archive.db')
    try:
        rows=[message(i) for i in range(100)]
        rows.append(message(50,seq=None,source_msg_id='weak-alias'))
        result=Importer(a).run(ImportRequest('test','weak'),rows)
        assert result.inserted==101 and result.suspects==1
        assert a.con.execute('SELECT count(*) FROM messages WHERE suspect=1').fetchone()[0]==2
        integrity(a)
    finally:a.close()

def test_dedup_lookup_uses_fingerprint_index(tmp_path):
    a=Archive(tmp_path/'archive.db')
    try:
        plan=a.con.execute('EXPLAIN QUERY PLAN SELECT id,exact_key FROM messages INDEXED BY ix_msg_fp WHERE conversation_id=? AND fingerprint=? AND merge_key!=? AND hidden=0 LIMIT 1',(1,'fingerprint','key')).fetchall()
        assert any('ix_msg_fp (conversation_id=? AND fingerprint=?)' in row[3] for row in plan)
    finally:a.close()
