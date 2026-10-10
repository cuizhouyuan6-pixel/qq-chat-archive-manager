import sqlite3, threading
import pytest
from qqarchive.discovery import _candidate
from qqarchive.store import Archive
from qqarchive.ntqq import row_to_message
from qqarchive.importer import Importer,ImportRequest,ImportCancelled
from qqarchive.recovery import recover_identities
from ntqq_fixture import make_ntqq_db,group_row,text_elem


def source(tmp_path):
    rows=[group_row(i,8,30003,20002,1700000000+i,[text_elem('消息')]) for i in range(1,5)]
    path=make_ntqq_db(tmp_path/'nt_msg.db',rows,plain=True)
    with sqlite3.connect(path) as c:
        c.execute('CREATE INDEX group_msg_table_idx40027_40003 ON group_msg_table("40027","40003")')
        c.execute('CREATE INDEX c2c_msg_table_idx40027_40003 ON c2c_msg_table("40027","40003")')
    c.close()
    candidate=_candidate(path,'plain','10001')
    return candidate,rows


def test_recovery_is_append_only_scoped_and_idempotent(tmp_path):
    candidate,rows=source(tmp_path);a=Archive(tmp_path/'archive.db')
    try:
        first=row_to_message('group',rows[0],candidate.owner)
        Importer(a).run(ImportRequest('ntqq-pc',candidate.path,owner=candidate.owner,range_end=1700000003),[first])
        original=dict(a.con.execute('SELECT * FROM messages').fetchone())
        audit=recover_identities(a,candidate,None)
        assert audit['audit']['recoverable']==2 and audit['audit']['excluded']==1
        assert a.count_messages()==1
        result=recover_identities(a,candidate,None,apply=True)
        assert result['import']['inserted']==2
        assert a.count_messages()==3
        assert dict(a.con.execute('SELECT * FROM messages WHERE id=?',(original['id'],)).fetchone())==original
        assert recover_identities(a,candidate,None)['audit']['recoverable']==0
    finally:a.close()


def test_recovery_cancellation_rolls_back(tmp_path):
    candidate,rows=source(tmp_path);a=Archive(tmp_path/'archive.db')
    try:
        Importer(a).run(ImportRequest('ntqq-pc',candidate.path,owner=candidate.owner),[row_to_message('group',rows[0],candidate.owner)])
        cancel=threading.Event();cancel.set()
        with pytest.raises(ImportCancelled):recover_identities(a,candidate,None,apply=True,cancel=cancel)
        assert a.count_messages()==1
    finally:a.close()
