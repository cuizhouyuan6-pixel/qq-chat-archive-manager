import hashlib
import pytest
from qqarchive import keyscan,discovery
from ntqq_fixture import NTQQ_KEY,make_ntqq_db,sample_rows
from test_keyscan import FakeMemory

@pytest.fixture
def target(tmp_path):
    db=make_ntqq_db(tmp_path/'nt_msg.db',*sample_rows())
    page,prefix=keyscan.read_page1(db)
    raw=hashlib.pbkdf2_hmac('sha512',NTQQ_KEY.encode(),page[:16],4000,32)
    return keyscan.DbTarget(str(db),salt=page[:16],page1=page,prefix=prefix),raw

@pytest.mark.parametrize('encoding',['ascii','utf-16-le'])
@pytest.mark.parametrize('upper',[False,True])
def test_literal_authenticated(target,encoding,upper):
    t,key=target
    value="x'"+key.hex()+t.salt.hex()+"'"
    if upper:value=value.upper()
    block=b'\0'*31+value.encode(encoding)+b'\0'*40
    assert keyscan._try_block(block,t.salt,[t],set())==key.hex()

def test_salt_match_alone_is_not_success(target):
    t,key=target
    bad=b"x'"+b'01'*32+t.salt.hex().encode()+b"'"
    assert keyscan._try_block(bad,t.salt,[t],set()) is None

def test_other_database_key_not_accepted(target):
    t,key=target
    block=("x'"+key.hex()+'00'*16+"'").encode()
    assert keyscan._try_block(block,t.salt,[t],set()) is None

@pytest.mark.parametrize('encoding',['ascii','utf-16-le'])
def test_literal_crosses_memory_window(target,encoding):
    t,key=target
    literal=("x'"+key.hex()+t.salt.hex()+"'").encode(encoding)
    block=b'\0'*1550+literal+b'\0'*1800
    mem=FakeMemory([(0x10000,block)])
    result=keyscan.scan_process(1,[t],window=1584,mem=mem)
    assert result.keys[t.salt.hex()]==key.hex()

def test_no_secret_in_display(target):
    _,key=target
    assert key.hex()[:8] not in keyscan.mask_key(key.hex())

@pytest.mark.parametrize('encoding',['utf-8-sig','utf-16','gb18030'])
def test_custom_qq_data_dir_encoding(tmp_path,encoding):
    data=tmp_path/'QQ怀旧版';data.mkdir()
    ini=tmp_path/'UserDataInfo.ini'
    ini.write_text('[UserDataSet]\nUserDataSavePathType=2\nUserDataSavePath='+str(data)+'\n',encoding=encoding)
    assert discovery.configured_qq_roots([ini])==[data]

def test_invalid_and_old_import_directory_are_not_selected(tmp_path):
    ini=tmp_path/'UserDataInfo.ini'
    ini.write_text('[UserDataImportSet]\nOldVerDataPath='+str(tmp_path),encoding='utf-8')
    assert discovery.configured_qq_roots([ini])==[]
    ini.write_text('[UserDataSet]\nUserDataSavePath=relative',encoding='utf-8')
    assert discovery.configured_qq_roots([ini])==[]

def test_custom_directory_precedes_default(tmp_path,monkeypatch):
    current=tmp_path/'current';current.mkdir()
    monkeypatch.setattr(discovery,'configured_qq_roots',lambda _: [current])
    monkeypatch.setattr(discovery,'documents_dir',lambda:tmp_path/'Documents')
    assert discovery.default_qq_roots()==[current,tmp_path/'Documents'/'Tencent Files']

def test_changed_direct_source_rolls_back(tmp_path,monkeypatch):
    from qqarchive import sources
    from qqarchive.store import Archive
    from qqarchive.ntqq import NtqqSource
    db=make_ntqq_db(tmp_path/'source.db',*sample_rows(),plain=True)
    c=discovery._candidate(db,'plain','10001')
    original=NtqqSource.messages
    def changed(self,*a,**kw):
        yield from original(self,*a,**kw)
        import os
        st=db.stat();os.utime(db,ns=(st.st_atime_ns,st.st_mtime_ns+1000000000))
    monkeypatch.setattr(NtqqSource,'messages',changed)
    arc=Archive(tmp_path/'archive.db')
    try:
        with pytest.raises(OSError,match='发生变化'):
            sources.import_candidate(arc,c,None,work_dir=tmp_path/'work')
        assert arc.stats()['messages']==0
    finally:
        arc.close()
    assert not (tmp_path/'work').exists()
