import time
from PySide6.QtCore import Qt,QPoint,QPointF,QMimeData
from PySide6.QtGui import QDragEnterEvent,QDragMoveEvent,QDropEvent
from PySide6.QtWidgets import QAbstractItemView,QPushButton
from PySide6.QtTest import QTest
from qqarchive.config import Config,load_config
from qqarchive.group_picker import GroupPicker
from qqarchive.session_groups import GROUP_ROLE,move_many_to_group
from qqarchive import session_tree
from test_v13_interactions import ui

def leaves(item):
    result=[]
    for i in range(item.childCount()):
        child=item.child(i);cid=child.data(0,Qt.UserRole)
        result.extend([child] if cid else leaves(child))
    return result

def check_rows(w):
    all_ids=[]
    for i in range(w.tree.topLevelItemCount()):
        root=w.tree.topLevelItem(i);rows=leaves(root)
        assert root.text(0).endswith(' · '+str(len(rows)))
        assert all(not row.isHidden() for row in rows)
        all_ids.extend(row.data(0,Qt.UserRole) for row in rows)
    assert set(all_ids)==set(w._session_rows) and len(all_ids)==len(set(all_ids))

def test_drag_lifecycle_keeps_every_row_and_never_rebuilds_export(ui,monkeypatch):
    app,w,t=ui;ids=w.tree.ids();cid=ids[0]
    w.cfg.conversation_groups={'目标':[]};w.render_conversations()
    source=next(row for i in range(w.tree.topLevelItemCount()) for row in leaves(w.tree.topLevelItem(i)) if row.data(0,Qt.UserRole)==cid)
    w.tree.setCurrentItem(source);QTest.qWait(200)
    def forbidden(*args,**kwargs):raise AssertionError('A display move must not read the database or rebuild exports')
    monkeypatch.setattr(w.arc,'conversation_tree',forbidden);monkeypatch.setattr(w.arc,'profile',forbidden)
    monkeypatch.setattr(w.export_convs,'clear',forbidden)
    export_ids=[w.export_convs.item(i).data(Qt.UserRole) for i in range(w.export_convs.count())]
    class Drag:
        def __init__(self,owner):self.owner=owner
        def setMimeData(self,mime):self.mime=mime
        def setPixmap(self,pix):pass
        def setHotSpot(self,pos):pass
        def exec(self,*args):
            pos=w.tree.visualItemRect(w.tree.topLevelItem(0)).center()
            enter=QDragEnterEvent(pos,Qt.MoveAction,self.mime,Qt.LeftButton,Qt.NoModifier);w.tree.dragEnterEvent(enter);assert enter.isAccepted()
            move=QDragMoveEvent(pos,Qt.MoveAction,self.mime,Qt.LeftButton,Qt.NoModifier);w.tree.dragMoveEvent(move);assert move.isAccepted()
            drop=QDropEvent(QPointF(pos),Qt.MoveAction,self.mime,Qt.LeftButton,Qt.NoModifier);w.tree.dropEvent(drop);assert drop.isAccepted()
            return Qt.MoveAction
        def deleteLater(self):pass
    monkeypatch.setattr(session_tree,'QDrag',Drag)
    for _ in range(3):
        w.tree.startDrag(Qt.MoveAction);app.processEvents();check_rows(w)
        assert w.cfg.conversation_groups['目标']==[cid]
        row=leaves(w.tree.topLevelItem(0))[0];w.tree.scrollToItem(row,QAbstractItemView.PositionAtCenter);app.processEvents()
        assert w.tree.visualItemRect(row).intersects(w.tree.viewport().rect())
    assert [w.export_convs.item(i).data(Qt.UserRole) for i in range(w.export_convs.count())]==export_ids
    assert not w.tree._edge_timer.isActive() and w.tree._drag_token is None

def test_foreign_drag_cannot_change_group_membership(ui):
    app,w,t=ui;before=w.tree.ids();mime=QMimeData();mime.setData(session_tree.MIME,b'foreign')
    event=QDropEvent(QPointF(15,15),Qt.MoveAction,mime,Qt.LeftButton,Qt.NoModifier)
    w.tree.dropEvent(event);assert not event.isAccepted() and w.tree.ids()==before

def test_rapid_moves_sort_and_filter_preserve_counts_and_chat(ui):
    app,w,t=ui;current=w.current['id'];last_ids=[m['id'] for m in w.model.rows]
    ids=w.tree.ids();w.cfg.conversation_groups={'A':[],'B':[]}
    for n in range(12):w.move_conversation_group(ids[n%len(ids)],'A' if n%2 else 'B');check_rows(w)
    for mode in ('count','name','oldest','newest','manual'):
        w.session_sort.setCurrentIndex(w.session_sort.findData(mode));check_rows(w)
    w.conv_filter.setText('不会出现的名称')
    assert all(' · 0 / ' in w.tree.topLevelItem(i).text(0) for i in range(w.tree.topLevelItemCount()))
    w.conv_filter.clear();check_rows(w)
    assert w.current['id']==current and [m['id'] for m in w.model.rows]==last_ids

def test_batch_picker_search_keeps_visible_selection_and_applies_once(ui):
    app,w,t=ui;ids=w.tree.ids();w.cfg.conversation_groups={'批量目标':[],'原分组':[ids[1]]};w.render_conversations()
    d=w.batch_group_picker('批量目标');QTest.qWait(220)
    assert d.windowModality()==Qt.NonModal and app.activeModalWidget() is None
    assert w.batch_group_picker('批量目标') is d
    d.items[ids[0]].setCheckState(Qt.Checked);d.search.setText('找不到');assert d.selected.count()==1
    d.remove_item(d.selected.item(0));assert not d.chosen
    d.search.clear();d.select_all.click();assert set(d.chosen)==set(ids)
    d.search.setText('找不到');assert d.selected.count()==len(ids)
    d.apply.click();d.apply.click();QTest.qWait(230)
    assert load_config().conversation_groups=={'批量目标':ids,'原分组':[]}
    check_rows(w);assert w.group_picker is None

def test_batch_picker_cancel_target_changes_and_no_groups(ui):
    app,w,t=ui;ids=w.tree.ids();w.cfg.conversation_groups={'A':[ids[0]],'B':[]};w.render_conversations()
    d=w.batch_group_picker('A');d.select_all.click();assert ids[0] not in d.chosen
    assert not (d.items[ids[0]].flags()&Qt.ItemIsEnabled)
    d.target.setCurrentIndex(d.target.findData('B'));d.select_all.click();assert len(d.chosen)==len(ids)
    d.reject();QTest.qWait(230);assert w.cfg.conversation_groups=={'A':[ids[0]],'B':[]}
    w.cfg.conversation_groups={};d=w.batch_group_picker();assert not d.apply.isEnabled() and '先新建' in d.status.text()
    d.reject();QTest.qWait(230)

def test_batch_membership_is_unique_and_scoped_to_selected_ids():
    cfg=Config(conversation_groups={'A':[1,2,3],'B':[4,5]})
    move_many_to_group(cfg,[2,4,2],'B');assert cfg.conversation_groups=={'A':[1,3],'B':[5,2,4]}
    move_many_to_group(cfg,[2,4], '');assert cfg.conversation_groups=={'A':[1,3],'B':[5]}

def test_bulk_selection_of_large_list_is_linear_and_keeps_last_item(ui):
    app,w,t=ui
    records=[dict(id=i+100,owner='1',kind='group',peer=str(i),name='聊天 '+str(i),msg_count=i) for i in range(1200)]
    d=GroupPicker(records,{'收藏':[]},w);d.show();QTest.qWait(220)
    started=time.monotonic();d.choose_visible(True);elapsed=time.monotonic()-started
    assert len(d.chosen)==1200 and d.selected.count()==1200 and elapsed<3
    last=d.list.item(d.list.count()-1);d.list.scrollToItem(last);app.processEvents()
    assert d.list.visualItemRect(last).intersects(d.list.viewport().rect())
    assert d.rect().contains(d.apply.mapTo(d,d.apply.rect().bottomRight()))
    d.reject();QTest.qWait(230)

def test_create_group_from_picker_and_click_checkbox(ui,monkeypatch):
    from qqarchive.dialogs import InputDialog
    app,w,t=ui;w.cfg.conversation_groups={};d=w.batch_group_picker();QTest.qWait(220)
    monkeypatch.setattr(InputDialog,'getText',lambda *a,**kw:('新增目标',True))
    button=next(b for b in d.findChildren(QPushButton) if b.text()=='新建分组')
    QTest.mouseClick(button,Qt.LeftButton);assert d.target.currentText()=='新增目标'
    first=d.list.item(0);rect=d.list.visualItemRect(first)
    # Native check indicator remains clickable with single-chat main selection.
    QTest.mouseClick(d.list.viewport(),Qt.LeftButton,pos=QPoint(rect.left()+18,rect.center().y()))
    assert first.checkState()==Qt.Checked and d.selected.count()==1 and d.apply.isEnabled()
    d.reject();QTest.qWait(230);assert w.cfg.conversation_groups=={'新增目标':[]}
