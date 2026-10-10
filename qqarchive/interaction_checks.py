"""Isolated acceptance mode for packaged chat and search interactions."""
import json,os,time
from pathlib import Path
from PySide6.QtCore import Qt,QDateTime,QPoint,QPointF
from PySide6.QtGui import QWheelEvent
from PySide6.QtWidgets import QApplication
from .dialog_checks import wait

def run(destination):
    from .desktop import MainWindow
    from .config import Config
    from .models import NormalizedMessage
    from .importer import Importer,ImportRequest
    from .search_window import SearchWindow
    from .date_widgets import CalendarDialog
    from .chrome import dialog_available_geometry
    root=Path(destination).resolve();root.mkdir(parents=True,exist_ok=True)
    os.environ['QQARCHIVE_CONFIG_DIR']=str(root/'config')
    app=QApplication.instance();app.setQuitOnLastWindowClosed(False)
    host=MainWindow(Config(data_dir=str(root/'data')));report={'passed':False,'scale':host.devicePixelRatioF()}
    try:
        t=QDateTime.fromString('2026-10-07 10:00:03','yyyy-MM-dd HH:mm:ss').toSecsSinceEpoch()
        messages=[NormalizedMessage('1','group','9',t+i,seq=i+1,sender_uin='2' if i%2 else '1',sender_name='陈同学' if i%2 else '我',is_self=not i%2,peer_name='产品设计讨论',elements=[{'type':'text','text':'周末一起出发，相机已经准备好了。' if i%2 else '收到，集合地点和时间可以在这里查询。'}]) for i in range(340)]
        messages.append(NormalizedMessage('1','private','2',t,seq=1,sender_uin='2',peer_name='陈同学',elements=[{'type':'text','text':'下周见。'}]))
        Importer(host.arc).run(ImportRequest('preview','synthetic interactions'),messages)
        host.refresh();host.resize(1200,780);host.show();host.select_conversation(host.tree.topLevelItem(0).child(0).child(0));wait(300)
        assert host.chat_tabs.count()==1 and len(host.model.rows)==100
        host.chat.verticalScrollBar().setValue(0)
        event=QWheelEvent(QPointF(160,80),QPointF(160,80),QPoint(),QPoint(0,120),Qt.NoButton,Qt.NoModifier,Qt.NoScrollPhase,False)
        host.chat.wheelEvent(event);wait(230);assert host.chat.verticalScrollBar().value()>0 and len(host.model.rows)==200
        for _ in range(12):host.latest()
        wait(250);assert host.model.rows[-1]['time']==t+339
        host.chat.hovered_row=99;host.chat.viewport().update();wait(30);host.grab().save(str(root/'chat.png'))
        search=SearchWindow(host);search.show()
        until=time.monotonic()+10
        while (not search.rows or search.senders.count()!=2) and time.monotonic()<until:wait(40)
        assert len(search.rows)==100 and search.senders.count()==2
        search.keyword.setText('周末');search.add_term();search.keyword.setText('相机');search.add_term();search.sender_input.setText('2');search.select_typed_sender();search.search()
        until=time.monotonic()+10
        while (search.read.pending or search.read.future) and time.monotonic()<until:wait(40)
        assert len(search.rows)==100 and all(m['sender_uin']=='2' for m in search.rows)
        search.dates_enabled.setChecked(True);assert search.start._value.toSecsSinceEpoch()==t
        wait(240);assert dialog_available_geometry(search).contains(search.frameGeometry())
        report['search_geometry']={'width':search.width(),'height':search.height()}
        search.grab().save(str(root/'search.png'))
        assert search.rect().contains(search.more.mapTo(search,search.more.rect().bottomRight()))
        search.grab().save(str(root/'search.png'))
        calendar=CalendarDialog(search.start);calendar.show();wait(240)
        assert dialog_available_geometry(calendar).contains(calendar.frameGeometry())
        assert calendar._body_scroll.horizontalScrollBar().maximum()==0
        calendar.grab().save(str(root/'calendar.png'));calendar.reject();wait(220)
        search.toggle_filters();search.toggle_filters();wait(260);assert search.filters.isVisible()
        mid=search.rows[0]['id'];search.jump(search.results.item(0));wait(220)
        assert search.isVisible() and host.chat.currentIndex().data(Qt.UserRole)['id']==mid
        search.reject();wait(220);assert search.read.closed and search.people.closed
        window=host.detach_current();cid=next(c['id'] for c in host.arc.conversations() if c['id']!=host.current['id'])
        assert host.chat_windows.open(cid) is window and window.tabs.count()==2
        assert window.tabs.rail.geometry().right()<window.tabs.stack.geometry().left()
        wait(220);window.grab().save(str(root/'left-rail.png'))
        pane=window.tabs.widget(0);window.tabs.rail.preview(pane,QPoint(20,20));wait(180)
        assert window.tabs.rail.ghost.isVisible();window.grab().save(str(root/'merge-preview.png'));window.tabs.rail.clear_preview()
        split=host.chat_windows.detach(pane);assert split is not window and len(host.chat_windows.windows)==2
        host.chat_windows.move(pane,window);assert window.tabs.count()==2 and len(host.chat_windows.windows)==1
        host.toggle_pin_conversation(cid);assert host.tree.ids()[0]==cid
        host.font_size.setValue(18);assert all(p.chat.itemDelegate().message_font().pixelSize()==18 for p in host.chat_windows.panes.values())
        hwnd=int(host.winId());geometry=host.geometry()
        for _ in range(6):host.titlebar.pin.click();wait(20)
        assert int(host.winId())==hwnd and host.geometry()==geometry and not host._topmost
        size=host.chat.size();host.toggle_details();wait(260);assert host.chat.size()==size;host.grab().save(str(root/'details.png'));host.toggle_details();wait(260)
        host.conv_filter.setText('周末');global_search=host.global_search()
        until=time.monotonic()+10
        while (global_search.read.pending or global_search.read.future) and time.monotonic()<until:wait(40)
        assert global_search.groups.count()==1 and global_search.group_rows[0]['matches']==170
        wait(220);global_search.grab().save(str(root/'global-search.png'));global_search.jump(global_search.results.item(0));assert global_search.isVisible()
        global_search.reject();wait(220);host.chat_windows.close_all();host.conv_filter.clear();host.toggle_pin_conversation(cid)
        other=host.tree.topLevelItem(0).child(1).child(0);host.tree.setCurrentItem(other);assert len(host.model.rows)==1 and host.chat_tabs.count()==1
        host.cfg.conversation_groups={'常用聊天':[]};host.refresh();host.move_conversation_group(cid,'常用聊天')
        host.session_sort.setCurrentIndex(host.session_sort.findData('count'));wait(240)
        assert host.cfg.conversation_groups['常用聊天']==[cid]
        host.grab().save(str(root/'groups.png'))
        window=host.detach_current();assert window.parentWidget() is None
        import threading
        release=threading.Event()
        def sample_job(cancel,status):
            status('验证、读取与合并',120,800)
            until=time.monotonic()+4
            while not release.wait(.02) and time.monotonic()<until:
                if cancel.is_set():return
        host.run_job('导入聊天记录',sample_job,lambda _:None);wait(240)
        assert host.job_dialog.isModal() and window.isEnabled()
        host.job_dialog.hide_button.click();wait(50)
        assert host.task_strip.isVisible() and not host.job_dialog.isVisible() and not host.worker.cancel.is_set()
        assert not getattr(host.job_dialog,'_shade',None)
        window.tabs.currentWidget().latest();assert window.tabs.currentWidget().model.rowCount()>0
        host.grab().save(str(root/'background-task.png'));release.set()
        until=time.monotonic()+5
        while host.worker and time.monotonic()<until:wait(40)
        assert host.worker is None and not host.task_strip.isVisible()
        from .chat_tools import avatar_menu
        menu=avatar_menu(host,host.model.rows[-1],host.chat.mapToGlobal(QPoint(80,70)));wait(200)
        assert [action.text() for action in menu.actions()]==['复制 QQ 号','查看详细信息']
        menu.grab().save(str(root/'avatar-menu.png'));menu.hide();host.chat_windows.close_all()
        # v16: verify the released picker and the real drop endpoint, including
        # the final row after a model refresh. All records here are synthetic.
        host.cfg.conversation_groups['批量验收']=[];host.render_conversations()
        picker=host.batch_group_picker('批量验收');wait(240);picker.select_all.click()
        picker.search.setText('不匹配');assert picker.selected.count()==2
        assert picker.windowModality()==Qt.NonModal
        assert dialog_available_geometry(picker).contains(picker.frameGeometry())
        assert picker.rect().contains(picker.apply.mapTo(picker,picker.apply.rect().bottomRight()))
        picker.search.clear();picker.grab().save(str(root/'batch-groups.png'))
        picker.apply.click();wait(240);assert len(host.cfg.conversation_groups['批量验收'])==2
        from .session_tree import MIME
        from .session_groups import GROUP_ROLE
        from PySide6.QtCore import QMimeData
        from PySide6.QtGui import QDropEvent
        expected=set(host.tree.ids());moved=next(iter(expected))
        target=next(host.tree.topLevelItem(i) for i in range(host.tree.topLevelItemCount()) if host.tree.topLevelItem(i).data(0,GROUP_ROLE)=='常用聊天')
        host.tree.scrollToItem(target);wait(30)
        host.tree._drag_cid=moved;host.tree._drag_token='isolated-preview-drag'
        mime=QMimeData();mime.setData(MIME,host.tree._drag_token.encode())
        drop=QDropEvent(QPointF(host.tree.visualItemRect(target).center()),Qt.MoveAction,mime,Qt.LeftButton,Qt.NoModifier)
        host.tree.dropEvent(drop);assert drop.isAccepted();host.tree._drag_cid=None;host.tree._drag_token=None
        wait(60);assert set(host.tree.ids())==expected and len(host.tree.ids())==len(expected)
        host.grab().save(str(root/'groups-after-drop.png'))
        report.update(batch_group_picker=True,group_drag_count_preserved=True,selection_survives_filter=True)
        host.navigate(4);wait(240);host.grab().save(str(root/'history.png'))
        report.update(passed=True,single_main_chat=True,multi_sender_keywords=True,calendar_seconds=True,boundary_anchor=True,latest_repeated=True,dialog_within_screen=True,search_footer_visible=True,workers_closed=True,search_persistent=True,global_counts_and_position=True,left_conversation_rail=True,merge_preview=True,reused_detached_window=True,pins=True,font_size=True,topmost_preserves_hwnd=True,details_no_squeeze=True,independent_read_during_import=True,task_strip=True,groups_and_sort=True,avatar_context_menu=True)
    finally:
        (root/'result.json').write_text(json.dumps(report,indent=2),encoding='utf-8')
        host.request_exit();wait(250)
    return 0

def sender_run(destination):
    """Search sender roster at the current QT_SCALE_FACTOR: row layout, ticking,
    removal and context menus driven by real Qt events. Synthetic data only."""
    from PySide6.QtCore import QEvent,QPoint
    from PySide6.QtGui import QImage,QColor,QMouseEvent,QKeyEvent,QContextMenuEvent
    from PySide6.QtWidgets import QStyleOptionViewItem
    from .desktop import MainWindow
    from .config import Config
    from .models import NormalizedMessage
    from .importer import Importer,ImportRequest
    from .search_window import SearchWindow
    from .session_delegate import SENDER
    from .chat_tools import avatar_menu
    root=Path(destination).resolve();root.mkdir(parents=True,exist_ok=True)
    os.environ['QQARCHIVE_CONFIG_DIR']=str(root/'config')
    app=QApplication.instance();app.setQuitOnLastWindowClosed(False)
    host=MainWindow(Config(data_dir=str(root/'data')));report={'passed':False,'scale':host.devicePixelRatioF()}
    def settle(view,until=10):
        end=time.monotonic()+until
        while (view.read.pending or view.read.future or view.people.pending or view.people.future or view.debounce.isActive()) and time.monotonic()<end:wait(40)
    def click(widget,pos,button=Qt.LeftButton):
        local=QPointF(pos);world=QPointF(widget.mapToGlobal(pos))
        for kind in (QEvent.MouseButtonPress,QEvent.MouseButtonRelease):
            QApplication.sendEvent(widget,QMouseEvent(kind,local,world,button,button if kind==QEvent.MouseButtonPress else Qt.NoButton,Qt.NoModifier))
        wait(60)
    def context(view,pos):
        QApplication.sendEvent(view.viewport(),QContextMenuEvent(QContextMenuEvent.Mouse,pos,view.viewport().mapToGlobal(pos)));wait(120)
        return [a.text() for a in search.context_menu.actions()]
    def enter(text):
        search.sender_input.setText(text);QApplication.sendEvent(search.sender_input,QKeyEvent(QEvent.KeyPress,Qt.Key_Return,Qt.NoModifier));wait(60)
        assert search.isVisible() and not search.sender_input.text()
    try:
        t=QDateTime.fromString('2026-10-07 10:00:03','yyyy-MM-dd HH:mm:ss').toSecsSinceEpoch()
        people=[('2','陈同学'),('3','Alexander Chen'),('4','林晓（设计组）'),('5','李')]
        messages=[NormalizedMessage('1','group','9',t+i,seq=i+1,sender_uin=people[i%4][0],sender_name=people[i%4][1],peer_name='产品设计讨论',elements=[{'type':'text','text':f'第 {i} 条讨论'}]) for i in range(80)]
        Importer(host.arc).run(ImportRequest('preview','synthetic senders'),messages)
        picture=root/'data'/'avatar-2.png';image=QImage(64,64,QImage.Format_RGB32);image.fill(QColor('#7DA7D9'));image.save(str(picture))
        with host.arc.con:
            host.arc.con.execute("INSERT INTO profiles(owner,kind,peer,uid,name,avatar_path,source) VALUES('1','private','2','','陈同学',?,'user')",(str(picture),))
            host.arc.con.execute("INSERT INTO profiles(owner,kind,peer,uid,name,avatar_path,source) VALUES('1','private','88888','','资料用户',?,'user')",(str(picture),))
        host.refresh();host.resize(1200,780);host.show();host.select_conversation(host.tree.topLevelItem(0).child(0).child(0));wait(300)
        search=SearchWindow(host);search.show();settle(search);wait(240)
        from PySide6.QtWidgets import QAbstractItemView
        from PySide6.QtGui import QWheelEvent
        from .smooth_scroll import SmoothScroll
        for view in (search.results,search.groups,search.senders,search.sender_selected):
            assert view.verticalScrollMode()==QAbstractItemView.ScrollPerPixel and any(isinstance(c,SmoothScroll) for c in view.children())
        bar=search.results.verticalScrollBar();end=time.monotonic()+5
        while bar.maximum()<=96 and time.monotonic()<end:wait(20)   # delayed item layout
        bar.setValue(0);point=QPointF(200,200)
        QApplication.sendEvent(search.results.viewport(),QWheelEvent(point,QPointF(search.results.viewport().mapToGlobal(point.toPoint())),QPoint(),QPoint(0,-120),Qt.NoButton,Qt.NoModifier,Qt.NoScrollPhase,False))
        animating=any(s.animation.state()==s.animation.State.Running for s in search.smooth);wait(320)
        assert animating and bar.value()==min(bar.maximum(),96),(animating,bar.value())
        report['results_wheel_pixels']=bar.value();bar.setValue(0)
        roster=search.senders;delegate=roster.itemDelegate()
        assert roster.count()==4,roster.count()
        problems=delegate.problems(roster);report['layout_problems']=problems;assert not problems,problems
        assert all(roster.visualItemRect(roster.item(i)).right()<=roster.viewport().width() for i in range(roster.count()))
        report['row_height']=roster.visualItemRect(roster.item(0)).height()
        search.grab().save(str(root/'sender-roster.png'))
        row=next(i for i in range(roster.count()) if roster.item(i).data(SENDER)['uin']=='3');item=roster.item(row)
        opt=QStyleOptionViewItem();opt.initFrom(roster.viewport());opt.rect=roster.visualItemRect(item);opt.widget=roster
        click(roster.viewport(),delegate.parts(opt,roster.indexFromItem(item))['check'].center());settle(search)
        assert item.checkState()==Qt.Checked and '3' in search.chosen_senders and search.rows and {m['sender_uin'] for m in search.rows}=={'3'}
        chip=search.sender_selected.item(0);click(search.sender_selected.viewport(),search.sender_selected.visualItemRect(chip).center());settle(search)
        assert '3' not in search.chosen_senders and item.checkState()==Qt.Unchecked
        two=next(roster.item(i) for i in range(roster.count()) if roster.item(i).data(SENDER)['uin']=='2')
        assert context(roster,roster.visualItemRect(two).center())==['复制 QQ 号','查看详细信息']
        search.context_menu.grab().save(str(root/'sender-menu.png'));search.context_menu.hide()
        enter('99999');settle(search)
        manual=next(search.sender_selected.item(i) for i in range(search.sender_selected.count()) if search.sender_selected.item(i).data(Qt.UserRole)=='99999')
        assert context(search.sender_selected,search.sender_selected.visualItemRect(manual).center())==['复制 QQ 号'];search.context_menu.hide()
        enter('88888');settle(search)
        upgraded=next(search.sender_selected.item(i) for i in range(search.sender_selected.count()) if search.sender_selected.item(i).data(Qt.UserRole)=='88888')
        assert upgraded.text().startswith('资料用户 · 88888'),upgraded.text()
        assert context(search.sender_selected,search.sender_selected.visualItemRect(upgraded).center())==['复制 QQ 号','查看详细信息'];search.context_menu.hide()
        assert search.sender_selected.count()==2 and '99999' in search.chosen_senders
        chosen=search.sender_selected;chosen.scrollToTop();wait(60)
        assert all(chosen.viewport().rect().contains(chosen.visualItemRect(chosen.item(i))) for i in range(2)),'chosen entries clipped'
        wait(200);search.grab().save(str(root/'sender-chosen.png'))
        long_name='語り継がれる創造神・語り継がれる創造神・語り継がれる創造神'
        with host.arc.con:host.arc.con.execute("INSERT INTO profiles(owner,kind,peer,uid,name,avatar_path,source) VALUES('1','private','3243017387','',?,?,'user')",(long_name,str(picture)))
        enter('3243017387');settle(search)
        entry=next(chosen.item(i) for i in range(chosen.count()) if chosen.item(i).data(Qt.UserRole)=='3243017387');text=search.chosen_display('3243017387')
        assert text.endswith(' · 3243017387  ×') and '…' in text and long_name in entry.toolTip(),text
        assert chosen.horizontalScrollBarPolicy()==Qt.ScrollBarAlwaysOff and chosen.visualItemRect(entry).width()<=chosen.viewport().width()
        chosen.scrollToItem(entry);wait(200);search.grab().save(str(root/'sender-long-name.png'))
        report['long_name_entry']=text
        menu=avatar_menu(host,host.model.rows[-1],host.chat.mapToGlobal(QPoint(80,70)));wait(120)
        assert [a.text() for a in menu.actions()]==['复制 QQ 号','查看详细信息'];menu.hide()
        search.reject();wait(230)
        report.update(passed=True,roster_layout=True,tick_by_click=True,chip_removal=True,roster_menu=True,manual_menu=True,upgraded_menu=True,chat_avatar_menu_unchanged=True,smooth_scroll_all_lists=True,long_name_keeps_qq=True)
    finally:
        (root/'result.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
        host.request_exit();wait(250)
    return 0
