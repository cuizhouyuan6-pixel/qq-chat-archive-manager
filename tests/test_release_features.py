import hashlib
import json
import zipfile
from pathlib import Path
import pytest
from qqarchive.store import Archive
from qqarchive.importer import Importer,ImportRequest
from qqarchive.models import NormalizedMessage
from qqarchive.exporters import export_archive
from qqarchive.package import import_package

@pytest.fixture
def arc(tmp_path):
    a=Archive(tmp_path/'data'/'archive.db')
    yield a
    a.close()

def message(i,text='测试',**kw):
    return NormalizedMessage('10000','group','20000',1700000000+i,sender_uin='30000',seq=kw.pop('seq',i+1),elements=[{'type':'text','text':text}],**kw)

@pytest.mark.parametrize('fmt',['package','json','csv','txt','html'])
def test_exports_filter_and_escape(arc,tmp_path,fmt):
    Importer(arc).run(ImportRequest('test','a'),[message(0,'<script>'),message(1,'=SUM(1,2)'),message(2)])
    out=tmp_path/fmt
    result=export_archive(arc,out,fmt,start=1700000000,end=1700000001)
    assert result['messages']==2
    if fmt=='html':
        text=next(out.glob('chat-*.html')).read_text(encoding='utf-8')
        assert '&lt;script&gt;' in text and '<script>' not in text
    if fmt=='csv':assert "'=SUM" in (out/'messages.csv').read_text(encoding='utf-8-sig')

def test_roundtrip_conflicts_and_no_sequence(arc,tmp_path):
    msgs=[message(0,seq=None,source_msg_id='abc'),message(1)]
    Importer(arc).run(ImportRequest('original','a'),msgs)
    Importer(arc).run(ImportRequest('other','b'),[message(1,'冲突')])
    out=tmp_path/'package';export_archive(arc,out)
    same=import_package(arc,out)
    assert same.inserted==0 and same.conflicts==0
    b=Archive(tmp_path/'new'/'archive.db')
    try:
        stats=import_package(b,out)
        assert stats.inserted==2 and stats.conflicts==1
    finally:b.close()

def test_tampered_package_is_atomic(arc,tmp_path):
    Importer(arc).run(ImportRequest('test','a'),[message(0)])
    out=tmp_path/'package';export_archive(arc,out)
    with (out/'messages.jsonl').open('a',encoding='utf-8') as f:f.write('{}\n')
    with pytest.raises(ValueError,match='SHA-256'):import_package(arc,out)
    assert arc.count_messages()==1

def test_zip_traversal_rejected(arc,tmp_path):
    p=tmp_path/'bad.qqarchive.zip'
    with zipfile.ZipFile(p,'w') as z:z.writestr('../escape','bad')
    with pytest.raises(ValueError):import_package(arc,p)
    assert not (tmp_path/'escape').exists()

def test_bad_count_rolls_back(arc,tmp_path):
    Importer(arc).run(ImportRequest('test','a'),[message(0)])
    out=tmp_path/'package';export_archive(arc,out)
    p=out/'manifest.json';m=json.loads(p.read_text());m['count']=3;p.write_text(json.dumps(m))
    target=Archive(tmp_path/'new'/'archive.db')
    try:
        with pytest.raises(ValueError,match='条数'):import_package(target,out)
        assert target.count_messages()==0
    finally:target.close()

def test_html_is_paginated(arc,tmp_path):
    Importer(arc).run(ImportRequest('test','a'),(message(i) for i in range(1001)))
    out=tmp_path/'html';export_archive(arc,out,'html')
    assert len(list(out.glob('chat-*.html')))==3

def test_cancelled_export_is_not_published(arc,tmp_path):
    import threading
    Importer(arc).run(ImportRequest('test','a'),[message(0)])
    cancel=threading.Event();cancel.set();out=tmp_path/'cancelled'
    with pytest.raises(InterruptedError):export_archive(arc,out,cancel=cancel)
    assert not out.exists() and not list(tmp_path.glob('.export-*'))

def test_integrity_does_not_leave_transaction(arc):
    assert arc.integrity_check()==['ok']
    assert not arc.con.in_transaction
    assert Importer(arc).run(ImportRequest('test','a'),[message(0)]).inserted==1

def test_snapshot_continuous_changes_rejected(tmp_path,monkeypatch):
    import qqarchive.sources as sources
    src=tmp_path/'nt_msg.db';src.write_bytes(b'original')
    original=sources.shutil.copy2
    def changed(s,d):
        original(s,d)
        with Path(s).open('ab') as f:f.write(b'x')
    monkeypatch.setattr(sources.shutil,'copy2',changed)
    with pytest.raises(OSError,match='持续变化'):
        with sources.snapshot(src,tmp_path/'work'):pass
    assert not list((tmp_path/'work').iterdir())

def test_media_md5_mismatch_does_not_fallback_to_name(arc,tmp_path):
    from qqarchive.media import register_folder,link_media
    p=tmp_path/'assets';p.mkdir();(p/'a.jpg').write_bytes(b'wrong')
    m=message(0);m.elements=[{'type':'image','name':'a.jpg','md5':hashlib.md5(b'right').hexdigest()}]
    Importer(arc).run(ImportRequest('test','a'),[m]);register_folder(arc,p,with_hash=True)
    assert link_media(arc)=={'linked':0,'missing':1}

def test_package_keeps_attachment_without_original_md5(arc,tmp_path):
    from qqarchive.media import register_folder,link_media
    media=tmp_path/'media';media.mkdir();(media/'report.pdf').write_bytes(b'pdf-data')
    m=message(0);m.elements=[{'type':'file','name':'report.pdf'}]
    Importer(arc).run(ImportRequest('test','a'),[m]);register_folder(arc,media,with_hash=True);link_media(arc)
    out=tmp_path/'package';export_archive(arc,out,attachments=True)
    target=Archive(tmp_path/'new'/'archive.db')
    try:
        import_package(target,out)
        mid=target.latest(target.conversations()[0]['id'])[0]['id']
        linked=target.media_for_message(mid)[0]
        assert linked['status']=='linked' and Path(linked['abs_path']).read_bytes()==b'pdf-data'
    finally:target.close()

def test_frozen_adb_lookup(monkeypatch,tmp_path):
    import qqarchive.adb as adb
    monkeypatch.setattr(adb.sys,'frozen',True,raising=False);monkeypatch.setattr(adb.sys,'executable',str(tmp_path/'QQArchive.exe'))
    assert str(tmp_path/'tools'/'platform-tools'/'adb.exe') in adb.adb_candidates()
