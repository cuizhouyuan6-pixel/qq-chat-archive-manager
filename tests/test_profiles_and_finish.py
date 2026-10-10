import json,sqlite3,threading
from pathlib import Path
import pytest
from qqarchive.store import Archive
from qqarchive.models import NormalizedMessage
from qqarchive.importer import Importer,ImportRequest
from qqarchive.profiles import read_profiles,apply_profiles
from qqarchive.discovery import _candidate
from qqarchive.keystore import KeyStore
from qqarchive.media import link_media
from qqarchive.exporters import export_archive
from qqarchive.package import import_package
from qqarchive.summary import read_summary

def test_authoritative_names_and_custom_name_preserved(tmp_path):
    path=tmp_path/'group_info.db';con=sqlite3.connect(path)
    con.executescript('CREATE TABLE group_list("60001" INTEGER,"60007" TEXT); INSERT INTO group_list VALUES(9,"群名称")');con.close()
    records=read_profiles(_candidate(path,'plain','1'),KeyStore(tmp_path/'keys'))
    a=Archive(tmp_path/'archive.db')
    try:
        Importer(a).run(ImportRequest('t','t'),[NormalizedMessage('1','group','9',1,seq=1,peer_name='导出的群名')])
        assert a.conversations()[0]['name']=='导出的群名'
        assert apply_profiles(a,records)==1
        c=a.conversations()[0];assert c['name']=='群名称'
        a.rename_conversation(c['id'],'自定义');apply_profiles(a,records)
        assert a.conversation(c['id'])['name']=='自定义'
        assert read_summary(a.path,threading.Event())[0]['messages']==1
    finally:a.close()

def test_changed_profile_source_is_discarded(tmp_path,monkeypatch):
    import qqarchive.profiles as mod
    path=tmp_path/'profile_info.db';con=sqlite3.connect(path)
    con.executescript('CREATE TABLE profile_info_v6("1002" INTEGER,"20002" TEXT,"1000" TEXT,"20009" TEXT); INSERT INTO profile_info_v6 VALUES(8,"昵称","uid","备注")');con.close()
    seq=iter([('before',),('after',)]);monkeypatch.setattr(mod,'signature',lambda p:next(seq))
    with pytest.raises(OSError):read_profiles(_candidate(path,'plain','1'),KeyStore(tmp_path/'keys'))

def test_profile_package_and_html_roundtrip(tmp_path):
    a=Archive(tmp_path/'a.db');b=Archive(tmp_path/'b.db')
    try:
        Importer(a).run(ImportRequest('t','t'),[NormalizedMessage('1','group','9',1,seq=1,peer_name='群名',sender_uin='8',elements=[{'type':'text','text':'hi'}])])
        image=tmp_path/'face.png';image.write_bytes(b'local-avatar-bytes')
        apply_profiles(a,[dict(owner='1',kind=kind,peer=peer,name=name,avatar_path=str(image)) for kind,peer,name in [('group','9','群名'),('private','8','昵称')]])
        out=tmp_path/'package';export_archive(a,out,attachments=True);import_package(b,out)
        assert b.conversations()[0]['name']=='群名'
        assert b.resolve_media_path(b.profile('1','private','8')['avatar_path']).read_bytes()==image.read_bytes()
        html=tmp_path/'html';export_archive(a,html,'html',attachments=True)
        page=next(html.glob('chat-*.html')).read_text(encoding='utf-8')
        assert '群名' in page and '会话头像' in page and '发送者头像' in page
        no_media=tmp_path/'no-media';export_archive(a,no_media,attachments=False)
        assert not (no_media/'assets').exists() and str(tmp_path) not in (no_media/'profiles.json').read_text(encoding='utf-8')
    finally:a.close();b.close()

def test_media_scan_uses_linear_plan_and_can_cancel_no_matches(tmp_path):
    a=Archive(tmp_path/'a.db')
    try:
        Importer(a).run(ImportRequest('t','t'),[NormalizedMessage('1','group','9',i,seq=i,elements=[{'type':'text','text':'x'}]) for i in range(2200)])
        traced=[];a.con.set_trace_callback(traced.append);progress=[];cancel=threading.Event()
        def report(n,t,s):
            progress.append((n,t,s))
        assert link_media(a,progress=report)=={'linked':0,'missing':0}
        assert any('FROM messages NOT INDEXED' in q for q in traced) and progress
        cancel.set()
        with pytest.raises(InterruptedError):link_media(a,cancel=cancel)
        assert a.count_messages()==2200
    finally:a.close()

def test_profile_import_does_not_accept_external_avatar_reference(tmp_path):
    a=Archive(tmp_path/'a.db')
    try:
        Importer(a).run(ImportRequest('t','t'),[NormalizedMessage('1','group','9',1,seq=1)])
        out=tmp_path/'p';export_archive(a,out)
        p=json.loads((out/'profiles.json').read_text(encoding='utf-8'));p[0]['avatar_asset']='../../secret.jpg'
        (out/'profiles.json').write_text(json.dumps(p),encoding='utf-8')
        from qqarchive.exporters import digest
        manifest=json.loads((out/'manifest.json').read_text(encoding='utf-8'));manifest['files']['profiles.json']=digest(out/'profiles.json')
        (out/'manifest.json').write_text(json.dumps(manifest),encoding='utf-8')
        with pytest.raises(ValueError,match='头像'):import_package(a,out)
    finally:a.close()
