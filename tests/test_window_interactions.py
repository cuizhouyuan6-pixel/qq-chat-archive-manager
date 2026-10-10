import os
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
import pytest
from PySide6.QtCore import Qt,QMimeData,QPoint
from PySide6.QtGui import QDragEnterEvent,QDropEvent
from PySide6.QtWidgets import QApplication
from qqarchive.dialogs import MessageBox as QMessageBox
from PySide6.QtTest import QTest
from qqarchive.config import Config,load_config
from qqarchive.desktop import MainWindow,STYLE
from qqarchive.chat_windows import MIME
from qqarchive.importer import Importer,ImportRequest
from qqarchive.models import NormalizedMessage

@pytest.fixture
def ui(tmp_path,monkeypatch):
    app=QApplication.instance() or QApplication([]);app.setStyleSheet(STYLE)
    monkeypatch.setenv('QQARCHIVE_CONFIG_DIR',str(tmp_path/'config'))
    w=MainWindow(Config(data_dir=str(tmp_path/'data')))
    msgs=[NormalizedMessage('1',kind,peer,i,sender_uin='3',seq=i+1,elements=[{'type':'text','text':f'{peer} / {i}'}])
          for kind,peer in [('group','10'),('group','20'),('private','30')] for i in range(130)]
    Importer(w.arc).run(ImportRequest('test','window-fixture'),msgs);w.refresh();w.show();QTest.qWait(30)
    w.select_conversation(w.tree.topLevelItem(0).child(0).child(0))
    yield app,w
    w.chat_windows.close_all();w.closing=True;w.phone_timer.stop();w.tray.hide();w.arc.close();w.close();w.deleteLater();app.processEvents()

def test_detached_views_are_independent_and_bounded(ui):
    app,w=ui;cid=w.current['id'];before=[r['id'] for r in w.model.rows]
    one=w.detach_current();two=w.detach_current()
    pane=one.tabs.widget(0);assert pane.current['id']==cid and pane.model.rowCount()==100
    pane.page_messages(-1);assert pane.model.rowCount()==30
    assert one is two and len(w.chat_windows.windows)==1
    assert [r['id'] for r in w.model.rows]==before and two.tabs.widget(0).model.rowCount()==30
    one.close();app.processEvents();assert not w.chat_windows.windows and w.model.rowCount()==100

def test_drag_merge_split_and_close(ui):
    app,w=ui;one=w.detach_current();other=w.chat_windows.open(w.arc.conversations()[1]['id'])
    pane=one.tabs.widget(0);one=w.chat_windows.detach(pane);mime=QMimeData();mime.setData(MIME,pane.token.encode())
    bar=other.tabs.tabBar();enter=QDragEnterEvent(QPoint(20,12),Qt.MoveAction,mime,Qt.LeftButton,Qt.NoModifier)
    QApplication.sendEvent(bar.viewport(),enter);assert enter.isAccepted() and bar.ghost.isVisible()
    drop=QDropEvent(QPoint(20,12),Qt.MoveAction,mime,Qt.LeftButton,Qt.NoModifier)
    QApplication.sendEvent(bar.viewport(),drop);app.processEvents()
    assert drop.isAccepted() and other.tabs.count()==2 and len(w.chat_windows.windows)==1
    split=w.chat_windows.detach(pane);assert split.tabs.count()==1 and other.tabs.count()==1
    w.chat_windows.merge_all(other);assert other.tabs.count()==2 and len(w.chat_windows.windows)==1
    other.close_tab(0);assert other.tabs.count()==1 and len(w.chat_windows.panes)==1
    other.close();app.processEvents();assert not w.chat_windows.windows and not w.chat_windows.panes

def test_reordering_persists_across_categories_without_changing_identity(ui):
    app,w=ui;old=w.tree.ids();identity={c['id']:(c['owner'],c['kind'],c['peer']) for c in w.arc.conversations()}
    w.tree.reorder(old[-1],old[0]);assert w.tree.ids()==[old[-1],*old[:-1]]
    assert load_config().conversation_order==w.tree.ids()
    w.refresh();assert w.tree.ids()==load_config().conversation_order
    assert identity=={c['id']:(c['owner'],c['kind'],c['peer']) for c in w.arc.conversations()}

def test_topmost_custom_chrome_and_details_animation(ui):
    app,w=ui;assert w.windowFlags()&Qt.FramelessWindowHint
    geometry=w.geometry();QTest.mouseClick(w.titlebar.pin,Qt.LeftButton);app.processEvents()
    assert w.windowFlags()&Qt.WindowStaysOnTopHint
    QTest.mouseClick(w.titlebar.pin,Qt.LeftButton);app.processEvents();assert not w.windowFlags()&Qt.WindowStaysOnTopHint
    assert w.geometry()==geometry
    w.toggle_details();QTest.qWait(260);assert w.details.isVisible() and w.details.width()>150
    w.toggle_details();QTest.qWait(260);assert not w.details.isVisible()
    # Rapid reversal finishes in the requested state.
    w.toggle_details();QTest.qWait(40);w.toggle_details();QTest.qWait(260);assert not w.details.isVisible()

def test_untrusted_tab_payload_is_ignored(ui):
    app,w=ui;mime=QMimeData();mime.setData(MIME,b'not-a-known-tab');assert w.chat_windows.resolve(mime) is None
    mime.setData(MIME,b'\xff');assert w.chat_windows.resolve(mime) is None

def test_background_work_keeps_detached_readers_available(ui):
    app,w=ui;other=w.detach_current()
    import time
    monkey=pytest.MonkeyPatch();monkey.setattr(QMessageBox,'information',lambda *a:QMessageBox.Ok)
    try:
        w.run_job('测试',lambda c,s:time.sleep(.08));assert other.isEnabled()
        while w.worker:QTest.qWait(20)
        assert other.isEnabled()
    finally:monkey.undo()

def test_repeated_navigation_reuses_animation(ui):
    app,w=ui;w.navigate(1);first=w.pages.currentWidget()._fade
    for _ in range(20):w.navigate(2);w.navigate(1)
    assert w.pages.currentWidget()._fade is first

def test_detached_search_jump_updates_tab_identity(ui):
    app,w=ui;other=w.detach_current();pane=other.tabs.widget(0)
    target=w.arc.conversations()[-1];pane.current=target;pane.conversation_changed()
    assert target['peer'] in other.tabs.tabToolTip(0)
    assert w.current['id']!=target['id']

def test_job_dialog_custom_close_hides_without_cancelling(ui):
    from qqarchive.desktop import Worker,JobDialog
    app,w=ui;worker=Worker(lambda c,s:None);dialog=JobDialog('任务',worker,w)
    dialog.show();app.processEvents();assert dialog.windowFlags()&Qt.FramelessWindowHint
    dialog.close();app.processEvents();assert not worker.cancel.is_set() and not dialog.isVisible()
    dialog.timer.stop();dialog.accept();dialog.deleteLater()

def test_switch_by_keyboard_tabs_and_rapid_selection(ui):
    app,w=ui;cat=w.tree.topLevelItem(0).child(0);a,b=cat.child(0),cat.child(1)
    w.tree.setCurrentItem(a);w.tree.setFocus();QTest.keyClick(w.tree,Qt.Key_Down)
    assert w.current['id']==b.data(0,Qt.UserRole)
    assert {r['conversation_id'] for r in w.model.rows}=={w.current['id']}
    for _ in range(8):
        for item in (a,b):
            w.tree.setCurrentItem(item)
            assert {r['conversation_id'] for r in w.model.rows}=={item.data(0,Qt.UserRole)}
    for index in range(w.chat_tabs.count()):
        w.chat_tabs.setCurrentIndex(index)
        assert {r['conversation_id'] for r in w.model.rows}=={w.chat_tabs.tabData(index)}
    QTest.qWait(220)
    assert w.chat.graphicsEffect() is None
    assert w.chat.visualRect(w.model.index(99)).isValid()

def test_empty_conversation_does_not_keep_previous_messages(ui):
    app,w=ui
    with w.arc.con:cid=w.arc.conversation_id('1','group','empty','空会话')
    w.refresh();item=__import__('PySide6.QtWidgets',fromlist=['QTreeWidgetItem']).QTreeWidgetItem();item.setData(0,Qt.UserRole,cid)
    w.select_conversation(item)
    assert w.current['id']==cid and w.model.rowCount()==0 and w.chat_title.text()=='空会话'

def test_summary_does_not_block_job_completion(ui,monkeypatch):
    import threading,time
    import qqarchive.desktop as desktop
    app,w=ui;entered=threading.Event();release=threading.Event()
    original=desktop.read_summary
    def delayed(path,cancel):
        entered.set()
        while not release.wait(.01):
            if cancel.is_set():raise InterruptedError()
        return original(path,cancel)
    monkeypatch.setattr(desktop,'read_summary',delayed)
    w.refresh()
    until=time.monotonic()+3
    while not entered.is_set() and time.monotonic()<until:QTest.qWait(20)
    assert entered.is_set()
    done=[];w.run_job('完成',lambda c,s:42,done.append)
    until=time.monotonic()+3
    while w.worker and time.monotonic()<until:QTest.qWait(20)
    assert done==[42] and w.pages.isEnabled()
    release.set()
