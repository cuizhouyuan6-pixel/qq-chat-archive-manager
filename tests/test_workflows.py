import os,time
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication
from qqarchive.dialogs import MessageBox as QMessageBox
from PySide6.QtTest import QTest
from qqarchive.config import Config
from qqarchive.desktop import MainWindow,STYLE
from qqarchive.discovery import SourceCandidate
from qqarchive.keystore import KeyStore,check_key
from qqarchive.key_lifecycle import inspect_saved,validate
from ntqq_fixture import make_ntqq_db,NTQQ_KEY,sample_rows

@pytest.fixture
def ui(tmp_path,monkeypatch):
    app=QApplication.instance() or QApplication([]);app.setStyleSheet(STYLE)
    monkeypatch.setenv('QQARCHIVE_CONFIG_DIR',str(tmp_path/'config'))
    monkeypatch.setattr(QMessageBox,'warning',lambda *a:QMessageBox.Ok)
    w=MainWindow(Config(data_dir=str(tmp_path/'data')));w.show();w.navigate(1);QTest.qWait(30)
    yield w
    if w.key_probe:
        w.key_probe.cancel.set()
        while w.key_probe:QTest.qWait(10)
    w.closing=True;w.key_timer.stop();w.phone_timer.stop();w.tray.hide();w.arc.close();w.close();w.deleteLater();app.processEvents()

def candidate(tmp_path,key=NTQQ_KEY):
    g,c=sample_rows();p=make_ntqq_db(tmp_path/'nt_msg.db',g,c,key=key)
    return SourceCandidate(path=str(p),kind='pc',owner='10001',size=p.stat().st_size,mtime=0,header='ntqq')

def test_prerequisites_and_source_change_reset(ui,tmp_path):
    w=ui;assert not w.key_step.available and not w.scope_step.available
    c=candidate(tmp_path);w.show_sources([c]);assert w.key_step.available and not w.scope_step.available
    w.verified_sources[w.source_token(c)]=True;w.source_changed(0);assert w.scope_step.available
    w.show_sources([]);assert not w.scope_step.available and not w.key_step.available
    w.devices.addItem('未授权',None);assert not w.phone_options.available
    w.devices.addItem('手机','TEST');w.devices.setCurrentIndex(1);assert w.phone_options.available

def test_key_no_stores_dpapi_and_yes_calls_continuation(ui,tmp_path,monkeypatch):
    w=ui;c=candidate(tmp_path);w.show_sources([c]);item=check_key(c.path,NTQQ_KEY);w.keys.put_source(c.owner,c.path,*item)
    monkeypatch.setattr(QMessageBox,'question',lambda *a:QMessageBox.No)
    w.key_acquired(c);assert w.keys.source_id(c.path) in {r['id'] for r in w.keys.saved_accounts()}
    assert item[0].secret.hex() not in w.keys.file.read_text()
    calls=[];monkeypatch.setattr(w,'close_then_import',lambda:calls.append(True));monkeypatch.setattr(QMessageBox,'question',lambda *a:QMessageBox.Yes)
    w.key_acquired(c);assert calls==[True]

def test_saved_key_invalid_and_missing_are_distinct(tmp_path):
    c=candidate(tmp_path);key,params=check_key(c.path,NTQQ_KEY);store=KeyStore(tmp_path/'keys');store.put_source(c.owner,c.path,key,params,True)
    assert inspect_saved(store)[0]['state']=='valid'
    p=__import__('pathlib').Path(c.path);p.rename(p.with_suffix('.absent'))
    assert inspect_saved(store)[0]['state']=='unavailable'
    candidate(tmp_path,key='a different test key')
    assert inspect_saved(store)[0]['state']=='invalid'

def test_fast_accordion_reversal_and_reduced_motion(ui,monkeypatch):
    step=ui.source_step
    for _ in range(8):step.expand(False);QTest.qWait(4);step.expand(True)
    QTest.qWait(280);assert step.body.isVisible() and step.body.maximumHeight()==16777215
    monkeypatch.setenv('QQARCHIVE_REDUCED_MOTION','1');step.expand(False);assert step.body.isHidden();step.expand(True);assert not step.body.isHidden()

def test_navigation_reuses_motion_and_chat_bar(ui):
    for _ in range(10):ui.navigate(2);ui.navigate(0)
    QTest.qWait(260);assert ui.chat_tabs.maximumHeight()==44
    assert ui.rail.width()==212


def test_nonmodal_confirmation_rejects_changed_import_scope(ui,tmp_path,monkeypatch):
    w=ui;c=candidate(tmp_path);w.show_sources([c]);w.verified_sources[w.source_token(c)]=True
    calls=[];monkeypatch.setattr(w,'close_then_import',lambda:calls.append(True))
    monkeypatch.setattr(QMessageBox,'information',lambda *args:QMessageBox.Ok)
    def change(*args):w.owner.setText('90000');return QMessageBox.Yes
    monkeypatch.setattr(QMessageBox,'question',change);w.review_import()
    assert not calls
