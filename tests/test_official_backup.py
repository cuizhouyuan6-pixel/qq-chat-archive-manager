from qqarchive.official_backup import inspect_header
from qqarchive.discovery import scan_backup_dir,discover
from qqarchive.sources import open_ntqq,KeyRequired
from ntqq_fixture import make_ntqq_db
import pytest

def test_extension_only_backups_are_listed_and_not_reported_as_imported(tmp_path):
    f=tmp_path/('A'*32)
    header=b'SQLite header 3\0'+(1024).to_bytes(2,'big')+bytes(1006)
    f.write_bytes(header+bytes(4096))
    info=inspect_header(f);assert info['extension_size']==1024 and info['page_alignment_4096']
    candidates=scan_backup_dir(tmp_path);assert len(candidates)==1
    c=candidates[0];assert c.kind=='official-backup' and c.key_status=='unsupported' and c.needs_key
    assert not c.extra['authenticated'] and not c.extra['importable']
    with pytest.raises(KeyRequired,match='不能直接导入'):open_ntqq(c,None)
    assert discover([], [tmp_path])[0].key_status=='unsupported'

def test_known_pc_ntqq_not_misclassified(tmp_path):
    f=make_ntqq_db(tmp_path/'nt_msg.db')
    assert inspect_header(f) is None
    assert scan_backup_dir(tmp_path)[0].kind=='backup'
