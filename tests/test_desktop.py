import os
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
import time
from pathlib import Path
import pytest
from PySide6.QtWidgets import QApplication
from qqarchive.dialogs import MessageBox as QMessageBox
from qqarchive.config import Config
from qqarchive.desktop import MainWindow,STYLE,JobDialog,Worker
from qqarchive.models import NormalizedMessage
from qqarchive.importer import Importer,ImportRequest

@pytest.fixture(scope='module')
def app():
    a=QApplication.instance() or QApplication([]);a.setStyleSheet(STYLE);return a

@pytest.fixture
def window(app,tmp_path,monkeypatch):
    monkeypatch.setenv('QQARCHIVE_CONFIG_DIR',str(tmp_path/'config'))
    monkeypatch.setattr(QMessageBox,'information',lambda *a:QMessageBox.Ok)
    monkeypatch.setattr(QMessageBox,'warning',lambda *a:QMessageBox.Ok)
    monkeypatch.setattr(QMessageBox,'exec',lambda *a:QMessageBox.Close)
    w=MainWindow(Config(data_dir=str(tmp_path/'data')));w.show();app.processEvents()
    yield w
    w.closing=True;w.phone_timer.stop();w.tray.hide();w.arc.close();w.close();w.deleteLater();app.processEvents()

def wait_job(app,w):
    deadline=time.monotonic()+10
    while w.worker and time.monotonic()<deadline:app.processEvents();time.sleep(.01)
    assert w.worker is None

def test_chat_navigation_and_message_selection(app,window):
    w=window
    msgs=[NormalizedMessage('10000','group','20000',i,sender_uin='30000',seq=i+1,elements=[{'type':'text','text':f'测试 {i}'}]) for i in range(230)]
    Importer(w.arc).run(ImportRequest('test','fixture'),msgs);w.refresh()
    w.select_conversation(w.tree.topLevelItem(0).child(0).child(0));assert w.model.rowCount()==100
    w.page_messages(-1);assert w.model.rows[0]['time']==30
    w.latest();assert w.model.rows[-1]['time']==229
    w.filter_tree('不存在');assert w.tree.topLevelItem(0).child(0).child(0).isHidden()
    for i in range(6):w.navigate(i);assert w.pages.currentIndex()==i

def test_job_completion_releases_ui(app,window):
    result=[]
    window.run_job('测试任务',lambda c,s:(s('进行中',1,2),42)[1],result.append)
    wait_job(app,window)
    assert result==[42] and window.pages.isEnabled()

def test_job_error_releases_ui(app,window):
    def fail(c,s):raise ValueError('预期异常')
    window.run_job('失败任务',fail);wait_job(app,window);assert window.pages.isEnabled()

def test_auto_phone_triggers_once_per_connection(app,window,monkeypatch):
    import qqarchive.desktop as desktop
    from qqarchive.adb import Device
    window.cfg.phone_auto_on_connect=True;window.cfg.phone_device='TEST'
    monkeypatch.setattr(desktop,'find_adb',lambda p:'fake')
    class Fake:
        def __init__(self,*a):pass
        def authorized_devices(self):return [Device('TEST','device')]
    monkeypatch.setattr(desktop,'Adb',Fake)
    called=[];monkeypatch.setattr(window,'start_phone',lambda automatic=False:called.append(automatic))
    for _ in range(2):
        window.auto_phone();until=time.monotonic()+5
        while getattr(window,'probe',None) and time.monotonic()<until:app.processEvents();time.sleep(.01)
    assert called==[True]
