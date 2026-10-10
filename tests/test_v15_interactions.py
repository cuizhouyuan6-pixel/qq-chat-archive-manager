import time,threading
from PySide6.QtCore import Qt,QTimer,QPoint,QPointF
from PySide6.QtGui import QWheelEvent
from PySide6.QtWidgets import QApplication,QPushButton,QTreeWidgetItem,QAbstractItemView
from PySide6.QtTest import QTest
from qqarchive.chrome import FramedDialog
from qqarchive.dialogs import MessageBox
from qqarchive.config import Config,load_config
from qqarchive.session_groups import ordered_groups,move_to_group
from test_v13_interactions import ui,spin


def test_nonmodal_result_dialog_keeps_other_windows_clickable(ui):
    app,w,t=ui;other=w.detach_current();dialog=MessageBox(w);dialog.setText('普通确认');dialog.setStandardButtons(MessageBox.Yes|MessageBox.No)
    seen=[];button=QPushButton('独立窗口按钮',other);button.resize(180,40);button.show();button.clicked.connect(lambda:seen.append('clicked'))
    def act():
        seen.append(app.activeModalWidget() is None);QTest.mouseClick(button,Qt.LeftButton)
        dialog.button(MessageBox.No).click()
    QTimer.singleShot(60,act)
    assert dialog.exec()==MessageBox.No and seen==[True,'clicked']
    assert not getattr(dialog,'_shade',None)


def test_import_can_hide_and_export_is_nonmodal(ui,monkeypatch):
    app,w,t=ui;other=w.detach_current();release=threading.Event()
    monkeypatch.setattr(MessageBox,'information',lambda *args:MessageBox.Ok)
    def task(cancel,status):
        status('测试处理',12,80)
        while not release.wait(.01):
            if cancel.is_set():return
    w.run_job('导入聊天记录',task);spin(app,lambda:'12' in w.task_text.text());QTest.qWait(240)
    assert w.job_dialog.windowModality()==Qt.WindowModal and other.isEnabled()
    assert other.parentWidget() is None
    w.job_dialog.hide_button.click();app.processEvents()
    assert not w.job_dialog.isVisible() and w.task_strip.isVisible() and not w.worker.cancel.is_set()
    assert not getattr(w.job_dialog,'_shade',None)
    other.tabs.currentWidget().latest();assert other.tabs.currentWidget().model.rowCount()>0
    result=[];w.run_read_job('并行资料读取',lambda a:a.count_messages(),result.append)
    spin(app,lambda:bool(result));assert result[0]>0
    w.restore_task();assert w.job_dialog.isVisible();w.job_dialog.hide()
    release.set();spin(app,lambda:w.worker is None);assert not w.task_strip.isVisible()
    release.clear();w.run_job('导出记录',task)
    assert w.job_dialog.windowModality()==Qt.NonModal
    w.job_dialog.cancel();spin(app,lambda:w.worker is None)


def test_group_sorting_is_local_and_membership_unique():
    cfg=Config(conversation_groups={'工作':[1,2],'朋友':[3]},conversation_sort='count')
    records=[dict(id=i,msg_count=n,last_time=t,name=name,peer=str(i),kind='group',owner='1') for i,n,t,name in [(1,10,100,'Beta'),(2,40,50,'Alpha'),(3,5,200,'Gamma'),(4,60,1,'Delta')]]
    ids=lambda:{name:[c['id'] for c in cs] for name,cs in ordered_groups(records,cfg).items()}
    assert ids()=={'工作':[2,1],'朋友':[3],'':[4]}
    cfg.conversation_sort='newest';assert ids()['工作']==[1,2]
    cfg.conversation_sort='oldest';assert ids()['工作']==[2,1]
    cfg.conversation_sort='name';assert ids()['工作']==[2,1]
    move_to_group(cfg,2,'朋友');assert cfg.conversation_groups=={'工作':[1],'朋友':[3,2]}
    move_to_group(cfg,2,'');assert cfg.conversation_groups=={'工作':[1],'朋友':[3]}


def test_groups_persist_and_drag_changes_display_only(ui):
    app,w,t=ui;before={c['id']:(c['owner'],c['kind'],c['peer']) for c in w.arc.conversations()};ids=list(before)
    w.cfg.conversation_groups={'测试分组':[]};w.refresh()
    w.tree.groupMoved.emit(ids[0],'测试分组',None)
    assert load_config().conversation_groups=={'测试分组':[ids[0]]}
    assert w.tree.topLevelItem(0).child(0).child(0).data(0,Qt.UserRole)==ids[0]
    assert set(w.tree.ids())==set(ids) and len(w.tree.ids())==len(ids)
    w.session_sort.setCurrentIndex(w.session_sort.findData('count'))
    assert load_config().conversation_sort=='count'
    w.move_conversation_group(ids[0],'')
    assert not load_config().conversation_groups['测试分组']
    assert before=={c['id']:(c['owner'],c['kind'],c['peer']) for c in w.arc.conversations()}


def test_avatar_context_is_distinct_from_message_context(ui,monkeypatch):
    app,w,t=ui;index=w.model.index(98);w.chat.scrollTo(index);QTest.qWait(220)
    message=index.data(Qt.UserRole);rect=w.chat.visualRect(index);x=rect.right()-27 if message['is_self'] else rect.left()+28
    w.chat.context_at(QPoint(x,rect.top()+40));menu=w._avatar_menu
    assert [a.text() for a in menu.actions()]==['复制 QQ 号','查看详细信息']
    menu.actions()[0].trigger();assert QApplication.clipboard().text()==message['sender_uin'];menu.hide()


def test_conversation_rail_scroll_is_animated_and_interruptible(ui):
    app,w,t=ui
    for i in range(40):QTreeWidgetItem(w.tree,[f'滚动测试 {i}'])
    app.processEvents();bar=w.tree.verticalScrollBar();bar.setValue(0)
    event=QWheelEvent(QPointF(40,90),QPointF(40,90),QPoint(),QPoint(0,-120),Qt.NoButton,Qt.NoModifier,Qt.NoScrollPhase,False)
    QApplication.sendEvent(w.tree.viewport(),event);QTest.qWait(50);middle=bar.value();QTest.qWait(190)
    assert 0<middle<bar.value() and w.tree.verticalScrollMode()==QAbstractItemView.ScrollPerPixel
    other=w.detach_current();assert hasattr(other.tabs.rail,'smooth')


def test_refresh_keeps_group_collapse_state(ui):
    app,w,t=ui;w.cfg.conversation_groups={'工作':[]};w.refresh()
    w.tree.topLevelItem(0).setExpanded(False);w.refresh()
    assert not w.tree.topLevelItem(0).isExpanded()


def test_file_dialog_does_not_lock_application(ui,tmp_path):
    from qqarchive.modeless import FileDialog
    app,w,t=ui;f=tmp_path/'sample.txt';f.write_text('test',encoding='utf-8');seen=[]
    def close():
        d=next(d for d in app.topLevelWidgets() if isinstance(d,FileDialog) and d.isVisible())
        seen.append(app.activeModalWidget() is None);d.reject()
    QTimer.singleShot(90,close)
    assert FileDialog.getOpenFileName(w,'选择文件',str(tmp_path))[0]=='' and seen==[True]
