import hashlib
import pytest
from qqarchive.adb import Adb, AdbError, RunResult, RemoteFile, remote_checksum
from qqarchive.media_pull import pull_file, pull_tree, register_pulled, PullOptions, match_file, PullError
from qqarchive.store import Archive
from test_media_pull import FakeAdb, FakePhone

def test_raw_binary_channel_never_uses_shell(monkeypatch):
    adb = Adb('fake')
    expected = bytes(range(256)) * 32
    calls = []
    def run(args, **kw):
        calls.append(args[0])
        return RunResult(0, expected if args[0] == 'exec-out' else expected.replace(b'\n', b'\r\n'), b'')
    monkeypatch.setattr(adb, 'run', run)
    assert adb.read_chunk('/sdcard/a.bin', 0, len(expected)) == expected
    assert calls == ['exec-out']

def test_checksum_fallback_handles_real_nonzero_exit(monkeypatch):
    adb=Adb('fake')
    monkeypatch.setattr(adb, 'shell', lambda command, **kw: RunResult(127,b'',b'not found') if command.startswith('sha256sum') else RunResult(0,b'a'*32+b'  file',b''))
    assert remote_checksum(adb, '/sdcard/a') == ('md5', 'a'*32)

def test_checksum_mismatch_retries_from_zero(tmp_path):
    phone=FakePhone({'/sdcard/a': b'abc'*100})
    adb=FakeAdb(phone)
    real=adb.read_chunk
    adb.read_chunk=lambda p,o,n,**kw: b'x'*n
    target=tmp_path/'a'
    with pytest.raises(PullError):
        pull_file(adb,RemoteFile('/sdcard/a',300,1700000000),target)
    adb.read_chunk=real
    info=pull_file(adb,RemoteFile('/sdcard/a',300,1700000000),target)
    assert info['resumed']==0 and target.read_bytes()==b'abc'*100

def test_separate_directories_with_same_name_do_not_overwrite(tmp_path):
    phone=FakePhone({'/sdcard/A/x.jpg':b'AAA','/sdcard/B/x.jpg':b'BBB'})
    adb=FakeAdb(phone)
    a=pull_tree(adb,'/sdcard/A',tmp_path,device='S')
    b=pull_tree(adb,'/sdcard/B',tmp_path,device='S')
    from pathlib import Path
    assert a.records[0]['local_path']!=b.records[0]['local_path']
    assert Path(a.records[0]['local_path']).read_bytes()==b'AAA'

def test_same_size_remote_change_is_not_skipped(tmp_path):
    phone=FakePhone({'/sdcard/A/x.jpg':b'AAA'})
    adb=FakeAdb(phone)
    arc=Archive(tmp_path/'archive.db')
    try:
        call=lambda: pull_tree(adb,'/sdcard/A',tmp_path,device='S',archive=arc,on_file=lambda x:register_pulled(arc,x))
        assert call().files==1
        phone.files['/sdcard/A/x.jpg']=b'BBB'
        assert call().files==1
        assert call().skipped==1
    finally:
        arc.close()

def test_unknown_date_does_not_bypass_date_filter():
    assert match_file(RemoteFile('/a.jpg',10,0),PullOptions(start=1)) is None

def test_remote_changes_during_read_not_published(tmp_path):
    phone=FakePhone({'/sdcard/a':b'a'*10})
    adb=FakeAdb(phone)
    def changed(*args):
        phone.mtimes['/sdcard/a']=1700000001
    with pytest.raises(PullError,match='发生变化'):
        pull_file(adb,RemoteFile('/sdcard/a',10,1700000000),tmp_path/'a',progress=changed)
    assert not (tmp_path/'a').exists()
