import threading
from PySide6.QtCore import Qt, QDateTime
from PySide6.QtTest import QTest
from test_v13_interactions import ui, spin
from test_history import message, Fake
from qqarchive.history import run
from qqarchive.history_window import open_history
from qqarchive.history_media import check_url, local_file, process
import pytest


def test_history_connection_guard_and_native_workflow(ui, monkeypatch):
    app, host, stamp = ui
    window = open_history(host)
    assert window.windowModality() == Qt.NonModal
    assert not window.begin.isEnabled() and not window.range_step.available
    assert open_history(host) is window
    window.owner.setText('100001')
    class Client(Fake):
        def __init__(self,*args): super().__init__([[message(4),message(1)]])
    monkeypatch.setattr('qqarchive.history_window.HistoryClient', Client)
    window.connect_qq()
    spin(app, lambda: not window.busy)
    assert window.begin.isEnabled() and window.range_step.available
    window.peer.setText('900001')
    window.start.setDateTime(QDateTime.fromSecsSinceEpoch(1760000002))
    window.end.setDateTime(QDateTime.fromSecsSinceEpoch(1760000005))
    window.start_job()
    spin(app, lambda:host.worker is None and '已读取' in window.note.text())
    assert '新增 1' in window.note.text() and window.past.count() == 1
    assert host.arc.find_conversation('100001','group','900001')['msg_count'] == 1
    window.owner.setText('100002')
    assert not window.begin.isEnabled()
    window.token.setText('synthetic-test-only')
    window.reject(); QTest.qWait(240)
    assert not window.token.text()


def test_invalid_date_does_not_start_background_import(ui):
    app,host,_=ui
    window=open_history(host);window.owner.setText('100001')
    window.verified=window.identity();window.invalidate();window.peer.setText('900001')
    window.start.edit.setText('invalid')
    window.start_job()
    assert host.worker is None and '日期' in window.note.text()
    window.reject();QTest.qWait(240)


@pytest.mark.parametrize('url', [
    'http://qpic.cn/a', 'https://qpic.cn.evil.example/a', 'https://127.0.0.1/a',
    'file:///some-file', 'https://token@qpic.cn/a', 'https://qpic.cn:8443/a',
])
def test_download_host_policy(url):
    with pytest.raises(ValueError): check_url(url)


def test_media_private_dns_and_paths_rejected(monkeypatch):
    monkeypatch.setattr('socket.getaddrinfo', lambda *a,**k:[(2,1,6,'',('127.0.0.1',443))])
    with pytest.raises(ValueError): check_url('https://qpic.cn/image')
    assert local_file('relative-file.png') is None
    assert local_file('//server/private/image.png') is None


def test_download_cancel_cleanup_and_existing_file_protection(tmp_path, monkeypatch):
    from qqarchive.history_media import download
    class Response:
        headers = {'Content-Length':'4'}
        def __enter__(self): return self
        def __exit__(self,*a): pass
        def close(self): pass
        def read1(self,_): return b'data'
    class Open:
        def open(self,*a,**k): return Response()
    monkeypatch.setattr('qqarchive.history_media.check_url',lambda v:v)
    monkeypatch.setattr('qqarchive.history_media.build_opener',lambda *a:Open())
    stop=threading.Event();stop.set()
    target=tmp_path/'only-generated.bin'
    with pytest.raises(InterruptedError): download('https://qpic.cn/test',target,stop,lambda *a:None)
    assert not list(tmp_path.glob('*.part')) and not target.exists()


def test_unknown_media_url_keeps_text_and_reports_failure(tmp_path):
    from qqarchive.store import Archive
    from qqarchive.history import Scope
    arc=Archive(tmp_path/'archive.db')
    try:
        row=message(4,message=[{'type':'image','data':{'file':'x.png','url':'https://example.invalid/a'}}])
        result=run(arc,Fake([[row,message(1)]]),Scope('100001','group','900001',1760000002,1760000005),
                   delay=0,media_kinds=('image',))
        assert result['inserted']==1 and result['media']['failed']==1
        assert not (tmp_path/'历史补全附件').exists()
    finally:arc.close()
