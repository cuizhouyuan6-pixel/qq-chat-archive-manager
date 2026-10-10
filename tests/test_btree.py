import struct,threading
from types import SimpleNamespace
import apsw,pytest
from qqarchive.btree import TableReader,RecordFormatError,varint
from qqarchive.ntqq import NtqqSource,WANTED
from qqarchive.sqlcipher import Key
from qqarchive.cipherdb import CipherConnection
from ntqq_fixture import make_ntqq_db,NTQQ_KEY,group_row,text_elem

def test_authenticated_reader_matches_sqlite_with_overflow(tmp_path):
    rows=[group_row(i,i,123,555,1700000000+i,[text_elem('长消息'+('词'*8000 if i%17==0 else str(i)))]) for i in range(1,601)]
    p=make_ntqq_db(tmp_path/'nt_msg.db',rows)
    with NtqqSource(p,'10000',Key.parse(NTQQ_KEY)) as src:
        reader=TableReader(src._conn.db,src.conn)
        expected=list(src.messages());src.table_reader=reader
        assert list(src.messages())==expected
        assert src.count()==len(rows)
        assert src.conversations()[0].count==len(rows)
        assert len(list(reader.rows('group_msg_table',set(WANTED),after=200)))==400
        event=threading.Event();event.set();reader.cancel=event
        with pytest.raises(InterruptedError):list(reader.rows('group_msg_table',set(WANTED)))

def lock_page_source(tmp_path):
    p=tmp_path/'plain.db';con=apsw.Connection(str(p));con.execute('PRAGMA page_size=4096');con.execute('CREATE TABLE messages(id INTEGER PRIMARY KEY, text TEXT, number REAL)')
    values=[(i,('记录'+str(i))*200, i/3) for i in range(1,61)];con.executemany('INSERT INTO messages VALUES(?,?,?)',values);con.close()
    data=p.read_bytes();pages={i//4096+1:bytearray(data[i:i+4096]) for i in range(0,len(data),4096)}
    root=pages[2];assert root[0]==5
    ptr=int.from_bytes(root[12:14],'big');old=int.from_bytes(root[ptr:ptr+4],'big');reserved=262145
    root[ptr:ptr+4]=reserved.to_bytes(4,'big');pages[reserved]=pages.pop(old);pages[1][28:32]=reserved.to_bytes(4,'big')
    class Virtual:
        params=SimpleNamespace(page_size=4096);page_count=reserved;size=reserved*4096
        def page(self,n):return bytes(pages.get(n,b'\0'*4096))
        def read(self,offset,amount):
            result=bytearray()
            while amount:
                n=offset//4096+1;within=offset%4096;take=min(amount,4096-within)
                result+=self.page(n)[within:within+take];offset+=take;amount-=take
            return bytes(result)
        def close(self):pass
    return Virtual(),pages,values

def test_nonstandard_lock_page_is_read_without_skipping(tmp_path):
    db,pages,expected=lock_page_source(tmp_path)
    with CipherConnection(db) as mounted:
        with pytest.raises(apsw.CorruptError):list(mounted.conn.execute('SELECT * FROM messages'))
        reader=TableReader(db,mounted.conn)
        got=[(r['id'],r['text'],r['number']) for _,r in reader.rows('messages',{'id','text','number'})]
        assert got==expected and reader.used_lock_page

def test_cycle_is_rejected_instead_of_skipping(tmp_path):
    db,pages,_=lock_page_source(tmp_path);root=pages[2];ptr=int.from_bytes(root[12:14],'big');root[ptr:ptr+4]=(2).to_bytes(4,'big')
    with CipherConnection(db) as mounted:
        with pytest.raises(RecordFormatError,match='重复或循环'):list(TableReader(db,mounted.conn).rows('messages',{'id'}))

def test_varint_signed_and_truncated():
    assert varint(bytes([255]*9))==(2**64-1,9)
    with pytest.raises(RecordFormatError):varint(b'\x80')


def test_bounded_lookup_reads_lock_page_and_missing_keys(tmp_path):
    db,pages,expected=lock_page_source(tmp_path)
    with CipherConnection(db) as mounted:
        reader=TableReader(db,mounted.conn)
        for rid,text,number in reversed(expected):
            assert reader.row('messages',rid,{'id','text','number'})=={'id':rid,'text':text,'number':number}
        assert reader.used_lock_page
        assert reader.row('messages',0,{'id'}) is None
        assert reader.row('messages',1000,{'id'}) is None
        event=threading.Event();event.set();reader.cancel=event
        with pytest.raises(InterruptedError):reader.row('messages',1,{'id'})


def test_bounded_lookup_rejects_cycles(tmp_path):
    db,pages,_=lock_page_source(tmp_path);root=pages[2];ptr=int.from_bytes(root[12:14],'big');root[ptr:ptr+4]=(2).to_bytes(4,'big')
    with CipherConnection(db) as mounted:
        with pytest.raises(RecordFormatError,match='重复或循环'):TableReader(db,mounted.conn).row('messages',1,{'id'})
