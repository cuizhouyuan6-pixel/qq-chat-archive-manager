import os,time,threading
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
import pytest
from PySide6.QtCore import Qt,QPoint,QTimer
from PySide6.QtWidgets import QApplication,QLabel,QMenu
from PySide6.QtTest import QTest
from qqarchive.search_window import SearchWindow
from qqarchive.store import SearchQuery
from qqarchive.config import load_config
from qqarchive.session_delegate import ROW
from test_v13_interactions import ui,spin

def test_main_avatar_click_dispatches_to_real_host(ui,monkeypatch):
    from qqarchive.chrome import FramedDialog
    app,w,t=ui;calls=[];dialogs=[]
    def job(title,task,done,**kw):
        calls.append(title);done(task(w.arc))
    def execute(dialog):
        dialogs.append([label.text() for label in dialog.findChildren(QLabel)])
        return 0
    monkeypatch.setattr(w,'run_read_job',job);monkeypatch.setattr(FramedDialog,'exec',execute)
    index=w.model.index(98);w.chat.scrollTo(index);QTest.qWait(220)
    m=index.data(Qt.UserRole);rect=w.chat.visualRect(index);x=rect.right()-27 if m['is_self'] else rect.left()+28
    QTest.mouseClick(w.chat.viewport(),Qt.LeftButton,pos=QPoint(x,rect.top()+40))
    assert [a.text() for a in w._avatar_menu.actions()]==['复制 QQ 号','查看详细信息']
    w._avatar_menu.actions()[1].trigger();w._avatar_menu.hide()
    assert calls==['读取用户资料'] and any('QQ 号：' in value for labels in dialogs for value in labels)
    assert w.archive_host is w and w.owner is not w

def test_main_message_menu_and_attachment_use_window_host(ui,monkeypatch,tmp_path):
    from qqarchive.chat_tools import message_menu,save_attachment
    app,w,t=ui;actions=[]
    def menu_exec(menu,*_):
        actions.extend(a.text() for a in menu.actions());menu.actions()[0].trigger()
    class TestMenu(QMenu):
        def exec(self,*args):return menu_exec(self,*args)
    monkeypatch.setattr('qqarchive.chat_tools.QMenu',TestMenu)
    message_menu(w,w.model.rows[-1],QPoint());assert '复制消息内容' in actions
    assert QApplication.clipboard().text()==w.model.rows[-1]['text']

def test_search_is_modeless_persistent_and_enters_exact_message(ui):
    app,w,t=ui;d=w.search_dialog();spin(app,lambda:bool(d.rows));QTest.qWait(220)
    assert not d.isModal() and d.isWindow() and not getattr(d,'_shade',None)
    mid=d.rows[-1]['id'];d.jump(d.results.item(d.results.count()-1));QTest.qWait(230)
    assert d.isVisible() and not d.read.closed and w.chat.currentIndex().data(Qt.UserRole)['id']==mid
    assert w.search_dialog() is d
    d.reject();QTest.qWait(230);assert not w.search_windows

def test_global_search_counts_and_selected_conversation(ui):
    from qqarchive.importer import Importer,ImportRequest
    from qqarchive.models import NormalizedMessage
    app,w,t=ui
    Importer(w.arc).run(ImportRequest('test','global-new'),[NormalizedMessage('1','private','3',t+2000,seq=1,sender_uin='3',elements=[{'type':'text','text':'alpha beta'}])]);w.refresh()
    groups=w.arc.search_conversations(SearchQuery(keyword='alpha'))
    assert sorted(g['matches'] for g in groups)==[1,340]
    w.conv_filter.setText('alpha');d=w.global_search();spin(app,lambda:d.groups.count()==2 and bool(d.rows))
    assert d.groups.item(0).data(ROW)['subtitle']=='1 条相关聊天记录'
    assert len(d.rows)==1;target=d.rows[0];d.jump(d.results.item(0));assert w.current['id']==target['conversation_id']
    assert d.isVisible() and w.chat.currentIndex().data(Qt.UserRole)['id']==target['id']
    d.groups.setCurrentRow(1);spin(app,lambda:d.read.pending is None and d.read.future is None)
    assert len(d.rows)==100 and all(m['conversation_id']==d.cid for m in d.rows)
    d.search(True);spin(app,lambda:d.read.pending is None and d.read.future is None);assert d.offset==100
    d.reject();QTest.qWait(230)

def test_pins_sort_multiple_and_survive_restart(ui):
    app,w,t=ui;ids=w.tree.ids();w.toggle_pin_conversation(ids[-1]);w.toggle_pin_conversation(ids[0])
    assert w.tree.ids()[:2]==[ids[-1],ids[0]] and load_config().pinned_conversations==[ids[-1],ids[0]]
    w.toggle_pin_conversation(ids[-1]);assert w.tree.ids()[0]==ids[0] and load_config().pinned_conversations==[ids[0]]
    assert len(w.tree.ids())==len(set(w.tree.ids()))==len(ids)

def test_reuse_left_rail_and_font_in_all_windows(ui):
    app,w,t=ui;one=w.detach_current();other_id=next(c['id'] for c in w.arc.conversations() if c['id']!=w.current['id'])
    two=w.chat_windows.open(other_id);QTest.qWait(220)
    assert one is two and one.tabs.count()==2 and one.tabs.rail.x()<one.tabs.stack.x()
    assert w.chat_windows.open(other_id) is one and one.tabs.count()==2
    w.font_size.setValue(22);w.save_settings();assert load_config().chat_font_size==22
    for reader in [w,*w.chat_windows.panes.values()]:assert reader.chat.itemDelegate().message_font().pixelSize()==22
    pane=one.tabs.widget(0);one.tabs.rail.preview(pane,QPoint(20,20));QTest.qWait(180)
    assert one.tabs.rail.ghost.isVisible() and pane.chat_title.text() in one.tabs.rail.ghost.text()
    one.tabs.rail.clear_preview();assert not one.tabs.rail.ghost.isVisible()

def test_details_overlay_does_not_resize_chat(ui):
    app,w,t=ui;before=w.chat.size();widths=[]
    w.toggle_details()
    for _ in range(5):QTest.qWait(45);widths.append(w.details.width());assert w.chat.size()==before
    assert len(set(widths))==1 and w.details.isVisible()
    for _ in range(8):w.toggle_details();QTest.qWait(12)
    QTest.qWait(260);assert w.details.opened and w.chat.size()==before
    w.toggle_details();QTest.qWait(260);assert not w.details.isVisible()


def test_global_short_keyword_uses_one_message_scan(ui):
    app,w,t=ui;sqls=[];w.arc.con.set_trace_callback(sqls.append)
    groups=w.arc.search_conversations(SearchQuery(keyword='al',owner='1'))
    w.arc.con.set_trace_callback(None);assert groups[0]['matches']==340
    statement=next(s for s in sqls if s.startswith('SELECT c.*,hit.matches'))
    plan=[str(r[3]) for r in w.arc.con.execute('EXPLAIN QUERY PLAN '+statement)]
    assert sum('SCAN m'==line for line in plan)==1 and not any('ix_msg_conv_time' in line for line in plan)

def test_rich_search_preview_escapes_markup_and_highlights(ui):
    from qqarchive.search_delegate import PREVIEW
    app,w,t=ui;d=w.search_dialog();spin(app,lambda:bool(d.rows))
    delegate=d.results.itemDelegate();data=d.results.item(0).data(PREVIEW)
    doc=delegate.document(dict(data,text='<script>alpha</script>',terms=['alpha']),240)
    assert '<script>alpha</script>' in doc.toPlainText() and 'background-color' in doc.toHtml()
    d.reject();QTest.qWait(230)

def test_detached_pin_orders_rail_without_changing_current_chat(ui):
    app,w,t=ui;window=w.detach_current();first=window.tabs.widget(0)
    other_id=next(c['id'] for c in w.arc.conversations() if c['id']!=first.current['id'])
    w.chat_windows.open(other_id);current=window.tabs.currentWidget()
    w.toggle_pin_conversation(other_id)
    assert window.tabs.widget(0) is current and window.tabs.currentWidget() is current
    window.tabs.reorder(0,1)
    assert window.tabs.widget(0) is current
    window.tabs.setCurrentWidget(first);QTest.qWait(220)
    assert first.chat.viewport().graphicsEffect() is None and hasattr(first.chat.viewport(),'_fade_cover')
