import os,time,threading
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
import pytest
from PySide6.QtCore import Qt,QDateTime,QDate,QRect
from PySide6.QtWidgets import QApplication,QPushButton
from PySide6.QtTest import QTest
from qqarchive.store import Archive,SearchQuery
from qqarchive.importer import Importer,ImportRequest
from qqarchive.models import NormalizedMessage
from qqarchive.config import Config
from qqarchive.desktop import MainWindow,STYLE
from qqarchive.date_widgets import DateTimeField,CalendarDialog,stamp
from qqarchive.search_window import SearchWindow
from qqarchive.chat_tools import copy_attachment

@pytest.fixture
def ui(tmp_path,monkeypatch):
    app=QApplication.instance() or QApplication([]);app.setStyleSheet(STYLE)
    monkeypatch.setenv('QQARCHIVE_CONFIG_DIR',str(tmp_path/'config'))
    import qqarchive.chrome as chrome
    monkeypatch.setattr(chrome,'dialog_available_geometry',lambda d:QRect(0,0,1440,1000))
    w=MainWindow(Config(data_dir=str(tmp_path/'data')))
    stamp0=QDateTime.fromString('2026-10-07 10:00:03','yyyy-MM-dd HH:mm:ss').toSecsSinceEpoch()
    msgs=[NormalizedMessage('1','group','9',stamp0+i,seq=i+1,sender_uin='2' if i%2 else '1',sender_name='用户乙' if i%2 else '用户甲',is_self=not i%2,peer_name='测试群',elements=[{'type':'text','text':'alpha beta' if i%2 else 'alpha gamma'}]) for i in range(340)]
    msgs += [NormalizedMessage('1','group','9',0,seq=1000),NormalizedMessage('1','private','2',stamp0,seq=1,sender_uin='2',elements=[{'type':'image','name':'x.png'}])]
    Importer(w.arc).run(ImportRequest('test','v13'),msgs);w.refresh();w.show();w.select_conversation(w.tree.topLevelItem(0).child(0).child(0));QTest.qWait(30)
    yield app,w,stamp0
    w.chat_windows.close_all();w.closing=True;w.phone_timer.stop();w.key_timer.stop();w.stop_summary();w.tray.hide();w.arc.close();w.close();w.deleteLater();app.processEvents()

def spin(app,condition,seconds=5):
    until=time.monotonic()+seconds
    while not condition() and time.monotonic()<until:app.processEvents();time.sleep(.01)
    assert condition()

def test_single_chat_latest_and_bounded_wheel_paging(ui):
    app,w,t=ui;original=w.current['id'];w.chat.verticalScrollBar().setValue(0);w.load_boundary(-1)
    assert w.model.rowCount()==200 and w.model.rows[0]['time']==t+140
    w.load_boundary(-1);w.load_boundary(-1);assert w.model.rowCount()<=300
    latest=next(b for b in w.findChildren(QPushButton) if b.accessibleName()=='回到最新记录')
    for _ in range(8):QTest.mouseClick(latest,Qt.LeftButton)
    assert w.model.rows[-1]['time']==t+339
    other=w.tree.topLevelItem(0).child(1).child(0);w.tree.setCurrentItem(other)
    assert w.chat_tabs.count()==1 and not w.chat_tabs.isVisible()
    assert w.current['id']!=original and len(w.model.rows)==1
    assert not any(b.text() in ('独立窗口','更早消息') for b in w.findChildren(QPushButton))

def test_valid_time_bounds_and_calendar_clamping(ui):
    app,w,t=ui;assert stamp(0)=='时间未知';assert w.arc.time_bounds(w.current['id'])==(t,t+339)
    field=DateTimeField(QDateTime.fromSecsSinceEpoch(t));field.bind_archive(w.arc,lambda:w.current['id'])
    field.edit.setText('2020-01-01 00:00:00');assert field.normalize() and field.dateTime().toSecsSinceEpoch()==t
    field.edit.setText('2030-01-01 00:00:00');assert field.normalize() and field.dateTime().toSecsSinceEpoch()==t+339
    dialog=CalendarDialog(field);dialog.show();QTest.qWait(220)
    unknown=next(b for b in dialog.findChildren(QPushButton,'calendarUnavailable') if b.text()=='6')
    QTest.mouseClick(unknown,Qt.LeftButton);assert '无法选择' in dialog.status.text() and dialog.chosen.day()==7
    assert not hasattr(unknown,'hover_animation')
    dialog.shift_month(1);assert dialog.month.currentIndex()==10
    dialog.reject();dialog.deleteLater();app.processEvents()

def test_search_multi_sender_and_terms_and_visible_selection(ui):
    app,w,t=ui
    rows=w.arc.search(SearchQuery(keywords=('alpha','beta'),senders=('1','2'),types=('text',),limit=500))
    assert len(rows)==170 and all(r['sender_uin']=='2' for r in rows)
    d=SearchWindow(w);d.show();spin(app,lambda:bool(d.rows) and d.senders.count()==2)
    d.keyword.setText('alpha');d.add_term();d.keyword.setText('beta');d.add_term()
    d.sender_input.setText('2');d.select_typed_sender();d.search();spin(app,lambda:d.read.future is None and d.read.pending is None)
    assert len(d.rows)==100 and all(r['sender_uin']=='2' for r in d.rows)
    d.sender_input.setText('不存在');d.load_senders();spin(app,lambda:d.people.future is None and d.people.pending is None)
    assert d.sender_selected.count()==1 and d.chosen_senders=={'2':'2'}
    d.remove_sender(d.sender_selected.item(0));assert not d.chosen_senders
    d.reject();QTest.qWait(230);assert d.read.closed and d.people.closed

def test_user_history_is_scoped_and_locatable(ui):
    app,w,t=ui;info=w.arc.user_history('1','2',w.current['id'])
    assert len(info['groups'])==1 and info['private']['peer']=='2' and info['last']['time']==t+339
    w.set_messages(w.arc.around(info['last']['id']),False);w.focus_message(info['last']['id'])
    assert w.chat.currentIndex().data(Qt.UserRole)['id']==info['last']['id']
    assert w.arc.user_history('other','2',None)['groups']==[]
    assert w.arc.user_history('1','',w.current['id'])['groups']==[]

def test_save_as_preserves_source_and_cancelled_destination(tmp_path):
    source=tmp_path/'source.bin';source.write_bytes(b'1234'*5000);destination=tmp_path/'copy.bin';destination.write_bytes(b'keep')
    cancel=threading.Event();cancel.set()
    with pytest.raises(InterruptedError):copy_attachment(source,destination,cancel,lambda *a:None)
    assert destination.read_bytes()==b'keep' and not list(tmp_path.glob('.saving-*'))
    cancel.clear();copy_attachment(source,destination,cancel,lambda *a:None)
    assert source.read_bytes()==destination.read_bytes()

def test_phone_history_is_separate_even_if_transfer_fails(ui,monkeypatch):
    import qqarchive.auto as auto
    app,w,t=ui;monkeypatch.setattr(auto,'_pull_phone_media',lambda *a,**kw:auto.PhoneReport(errors=['没有连接手机']))
    auto.pull_phone_media(w.cfg,w.arc)
    assert len(w.arc.phone_history())==1 and w.arc.phone_history()[0]['status']=='partial'
    assert len(w.arc.batches())==1
    w.history_tabs.setCurrentIndex(1);w.render_history()
    assert '手机附件' in w.history_list.item(0).text()

def test_wheel_at_boundary_keeps_visible_anchor(ui):
    from PySide6.QtGui import QWheelEvent
    from PySide6.QtCore import QPointF,QPoint
    app,w,t=ui;bar=w.chat.verticalScrollBar();bar.setValue(0)
    previous=w.model.rows[0]['id']
    event=QWheelEvent(QPointF(160,80),QPointF(160,80),QPoint(),QPoint(0,120),Qt.NoButton,Qt.NoModifier,Qt.NoScrollPhase,False)
    w.chat.wheelEvent(event);QTest.qWait(220)
    assert w.model.rowCount()==200 and bar.value()>0
    anchor=next(i for i,row in enumerate(w.model.rows) if row['id']==previous)
    assert abs(w.chat.visualRect(w.model.index(anchor)).top())<130

def test_search_changed_filters_restart_paging_and_history_pages(ui):
    app,w,t=ui;d=SearchWindow(w);d.show();spin(app,lambda:bool(d.rows))
    d.search(True);spin(app,lambda:d.read.future is None and d.read.pending is None);assert d.offset==100
    d.keyword.setText('beta');d.search(True);spin(app,lambda:d.read.future is None and d.read.pending is None);assert d.offset==0
    d.dates_enabled.setChecked(True);assert d.start._value.toSecsSinceEpoch()==t and d.end._value.toSecsSinceEpoch()==t+339
    d.reject();QTest.qWait(230)
    with w.arc.con:
        for n in range(35):w.arc.con.execute("INSERT INTO phone_runs(started_at,finished_at,status,files,bytes,skipped,failed,summary) VALUES(?,?,'done',1,10,0,0,'test')",(t+n,t+n))
    w.history_tabs.setCurrentIndex(1);assert w.history_list.count()==30 and w.history_next.isEnabled()
    w.page_history(1);assert w.history_list.count()==5 and not w.history_next.isEnabled()
    w.history_tabs.setCurrentIndex(0);assert w.history_offset==0 and w.history_list.count()==1

def test_sender_cache_reuses_roster_and_invalidates_after_import(ui):
    app,w,t=ui;cid=w.current['id'];assert len(w.arc.senders(cid))==2
    traced=[];w.arc.con.set_trace_callback(traced.append)
    assert len(w.arc.senders(cid,'乙'))==1
    assert not any('GROUP BY m.sender_uin' in sql for sql in traced)
    w.arc.con.set_trace_callback(None)
    Importer(w.arc).run(ImportRequest('test','new participant'),[NormalizedMessage('1','group','9',t+999,seq=2000,sender_uin='3',sender_name='用户丙')])
    assert len(w.arc.senders(cid))==3
