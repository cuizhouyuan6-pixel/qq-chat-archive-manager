import hashlib,json,sqlite3
from pathlib import Path
from qqarchive.avatars import cache_digest,find_avatar,image_suffix
from qqarchive.profiles import read_profiles,apply_profiles
from qqarchive.discovery import _candidate
from qqarchive.keystore import KeyStore
from qqarchive.store import Archive
from qqarchive.importer import Importer,ImportRequest
from qqarchive.models import NormalizedMessage
from qqarchive.exporters import export_archive
from qqarchive.package import import_package

def make_avatar(root,kind,identity,size='s_'):
    digest=cache_digest(identity);path=root/'nt_data/avatar'/kind/digest[:2]/(size+digest)
    path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(b'\xff\xd8\xff'+b'test-avatar');return path

def test_cache_address_vectors_and_original_reference(tmp_path):
    # Independently fixed MD5 test vectors, plus rejection of UIN-only identity.
    value='123456';expected=hashlib.md5((hashlib.md5((hashlib.md5(value.encode()).hexdigest()+value).encode()).hexdigest()+value).encode()).hexdigest()
    assert cache_digest(value)==expected
    database=tmp_path/'nt_db/profile_info.db';small=make_avatar(tmp_path,'user','u_test')
    assert find_avatar(database,'private','123456')==''
    assert find_avatar(database,'private','123456','u_test')==str(small)
    big=make_avatar(tmp_path,'user','u_test','b_');assert find_avatar(database,'private','123456','u_test')==str(big)
    big.write_bytes(b'bad');assert find_avatar(database,'private','123456','u_test')==str(small)
    assert not find_avatar(database,'private','wrong','other-uid')

def test_profile_adapters_reference_cached_avatars_without_copy(tmp_path):
    database=tmp_path/'nt_db/profile_info.db';database.parent.mkdir()
    con=sqlite3.connect(database);con.executescript('CREATE TABLE profile_info_v6("1002" INTEGER,"20002" TEXT,"1000" TEXT,"20009" TEXT); INSERT INTO profile_info_v6 VALUES(8,"昵称","u_test","")');con.close()
    avatar=make_avatar(tmp_path,'user','u_test');before=avatar.stat()
    rows=read_profiles(_candidate(database,'plain','1'),KeyStore(tmp_path/'keys'))
    assert rows[0]['avatar_path']==str(avatar) and avatar.stat().st_mtime_ns==before.st_mtime_ns

def test_automatic_profile_update_preserves_explicit_avatar(tmp_path):
    a=Archive(tmp_path/'a.db')
    try:
        p=dict(owner='1',kind='group',peer='9',name='群',avatar_path='manual.png',source='user')
        apply_profiles(a,[p]);apply_profiles(a,[dict(p,avatar_path='qq-cache',source='group_info.db')])
        assert a.profile('1','group','9')['avatar_path']=='manual.png'
        apply_profiles(a,[dict(p,avatar_path='second.png')]);assert a.profile('1','group','9')['avatar_path']=='second.png'
    finally:a.close()

def test_extensionless_avatar_export_and_roundtrip(tmp_path):
    a=Archive(tmp_path/'a.db');b=Archive(tmp_path/'b.db')
    try:
        image=make_avatar(tmp_path,'group','9');assert image_suffix(image)=='.jpg'
        Importer(a).run(ImportRequest('t','t'),[NormalizedMessage('1','group','9',1700000000,seq=1)])
        apply_profiles(a,[dict(owner='1',kind='group',peer='9',name='群',avatar_path=str(image))])
        out=tmp_path/'out';export_archive(a,out,attachments=True)
        profiles=json.loads((out/'profiles.json').read_text(encoding='utf-8'));assert profiles[0]['avatar_asset'].endswith('.jpg')
        import_package(b,out);assert b.resolve_media_path(b.profile('1','group','9')['avatar_path']).read_bytes()==image.read_bytes()
    finally:a.close();b.close()
