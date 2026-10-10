"""Independent chats share a QQ-style conversation rail; drags use local IDs."""
from __future__ import annotations
import uuid
from PySide6.QtCore import Qt,QMimeData,QPoint,Signal,QEvent,QPropertyAnimation
from PySide6.QtGui import QDrag,QCursor
from PySide6.QtWidgets import (QWidget,QVBoxLayout,QHBoxLayout,QLabel,QListWidget,QListWidgetItem,
    QAbstractItemView,QStackedWidget,QMenu,QApplication,QGraphicsOpacityEffect)
from .chrome import ChromeWindow,fade_in
from .chat_actions import ChatActions
from .store import conversation_title
from .message_view import MessageView
from .session_delegate import SessionDelegate,ROW,session_data
from .design import motion_ms
MIME='application/x-qqarchive-chat-tab'

class ChatPane(ChatActions,QWidget):
    def __init__(self,owner,cid):
        super().__init__();self.owner=owner;self.token=uuid.uuid4().hex;self.current=self.arc.conversation(cid)
        from .desktop import ChatModel,BubbleDelegate
        l=QVBoxLayout(self);l.setContentsMargins(0,0,0,0);l.setSpacing(0)
        head=QWidget();head.setObjectName('chatHeader');hl=QVBoxLayout(head);hl.setContentsMargins(24,18,24,16)
        self.chat_title=QLabel(conversation_title(self.current));self.chat_title.setObjectName('title')
        self.chat_subtitle=QLabel(f"{self.current['peer']} · 归档账号 {self.current['owner']}");self.chat_subtitle.setObjectName('muted')
        hl.addWidget(self.chat_title);hl.addWidget(self.chat_subtitle);l.addWidget(head)
        self.model=ChatModel();self.chat=MessageView();self.chat.message_font_size=owner.cfg.chat_font_size
        self.chat.setObjectName('messages');self.chat.setModel(self.model);self.chat.setItemDelegate(BubbleDelegate(self.model,self.chat))
        self.chat.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel);self.chat.doubleClicked.connect(self.message_detail)
        l.addWidget(self.chat,1);l.addWidget(self.chat_toolbar());self.connect_chat_actions();self.latest()
    @property
    def arc(self):return self.owner.arc
    def statusBar(self):return self.owner.statusBar()
    def guard(self,fn):return self.owner.guard(fn)
    def open_file(self,path):return self.owner.open_file(path)
    def conversation_changed(self):
        host=self.owner.chat_windows.source(self)
        if host:host.tabs.update_row(self);host.update_title()

class ChatSidebar(QListWidget):
    def __init__(self,host):
        super().__init__();self.host=host;self.start=QPoint();self.press_index=-1;self.ghost=None;self.preview_index=-1
        self.setObjectName('chatSidebar');self.setFixedWidth(246);self.setItemDelegate(SessionDelegate(self));self.setMouseTracking(True)
        self.setAcceptDrops(True);self.setDefaultDropAction(Qt.MoveAction);self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setContextMenuPolicy(Qt.CustomContextMenu);self.customContextMenuRequested.connect(self.menu)
        from .smooth_scroll import SmoothScroll
        self.smooth=SmoothScroll(self)
    def tabAt(self,pos):return self.indexAt(pos).row()
    def mousePressEvent(self,e):
        self.start=e.position().toPoint();self.press_index=self.tabAt(self.start)
        if e.button()==Qt.LeftButton and self.press_index>=0 and self.start.x()>self.viewport().width()-30:
            self.host.close_tab(self.press_index);self.press_index=-1;return
        super().mousePressEvent(e)
    def mouseMoveEvent(self,e):
        if e.buttons()&Qt.LeftButton and self.press_index>=0 and (e.position().toPoint()-self.start).manhattanLength()>QApplication.startDragDistance():
            pane=self.host.tabs.widget(self.press_index);index=self.press_index;self.press_index=-1
            if not pane:return
            drag=QDrag(self.host.manager.owner);mime=QMimeData();mime.setData(MIME,pane.token.encode());drag.setMimeData(mime)
            drag.setPixmap(self.viewport().grab(self.visualItemRect(self.item(index))));drag.setHotSpot(QPoint(80,30))
            result=drag.exec(Qt.MoveAction);drag.deleteLater();self.host.manager.clear_previews()
            if result==Qt.IgnoreAction and not self.host.frameGeometry().contains(QCursor.pos()) and not QApplication.mouseButtons():
                self.host.manager.detach(pane,QCursor.pos())
            return
        super().mouseMoveEvent(e)
    def preview(self,pane,pos=None):
        index=self.tabAt(pos) if pos is not None else self.count()
        if index<0:index=self.count()
        self.preview_index=index
        if self.ghost is None:
            self.ghost=QLabel(self.viewport());self.ghost.setAttribute(Qt.WA_TransparentForMouseEvents);self.ghost.setWordWrap(True)
            self.ghost.setStyleSheet('background:#E9ECF0;color:#555B66;border:1px dashed #9298A3;border-radius:10px;padding:12px;')
            effect=QGraphicsOpacityEffect(self.ghost);self.ghost.setGraphicsEffect(effect);self.ghost_motion=QPropertyAnimation(effect,b'opacity',self)
            self.ghost_motion.setDuration(motion_ms(150));self.ghost_motion.setStartValue(0);self.ghost_motion.setEndValue(.88)
        self.ghost.setText(conversation_title(pane.current)+'\n释放后添加到此窗口')
        y=self.visualItemRect(self.item(index)).top() if index<self.count() else (self.visualItemRect(self.item(self.count()-1)).bottom()+1 if self.count() else 0)
        self.ghost.setGeometry(3,max(0,min(y,self.viewport().height()-80)),self.viewport().width()-6,76)
        if not self.ghost.isVisible():self.ghost.show();self.ghost_motion.start()
        self.ghost.raise_()
    def clear_preview(self):
        if self.ghost:self.ghost.hide();self.ghost_motion.stop()
        self.preview_index=-1
    def dragEnterEvent(self,e):
        pane=self.host.manager.resolve(e.mimeData())
        if pane:self.preview(pane,e.position().toPoint());e.acceptProposedAction()
    def dragMoveEvent(self,e):self.dragEnterEvent(e)
    def dragLeaveEvent(self,e):self.clear_preview();e.accept()
    def dropEvent(self,e):
        pane=self.host.manager.resolve(e.mimeData());index=self.preview_index;self.clear_preview()
        if pane:self.host.manager.move(pane,self.host,index);e.setDropAction(Qt.MoveAction);e.accept()
    def moveTab(self,source,target):self.host.tabs.reorder(source,target)
    def menu(self,pos):
        i=self.tabAt(pos)
        if i<0:return
        pane=self.host.tabs.widget(i);menu=QMenu(self);menu.addAction('拆分到新窗口',lambda:self.host.manager.detach(pane))
        pinned=pane.current['id'] in self.host.manager.owner.cfg.pinned_conversations
        menu.addAction('取消置顶聊天' if pinned else '置顶聊天',lambda:self.host.manager.owner.toggle_pin_conversation(pane.current['id']))
        menu.addSeparator();menu.addAction('关闭此聊天',lambda:self.host.close_tab(self.host.tabs.indexOf(pane)));menu.exec(self.mapToGlobal(pos))

class ChatStack(QWidget):
    currentChanged=Signal(int)
    def __init__(self,host):
        super().__init__();self.host=host;self.panes=[];self.rail=ChatSidebar(host);self.stack=QStackedWidget()
        l=QHBoxLayout(self);l.setContentsMargins(0,0,0,0);l.setSpacing(0);l.addWidget(self.rail);l.addWidget(self.stack,1)
        self.rail.currentRowChanged.connect(self.selected)
    def count(self):return len(self.panes)
    def widget(self,i):return self.panes[i] if 0<=i<len(self.panes) else None
    def indexOf(self,pane):return self.panes.index(pane) if pane in self.panes else -1
    def currentIndex(self):return self.rail.currentRow()
    def currentWidget(self):return self.widget(self.currentIndex())
    def tabBar(self):return self.rail
    def setCurrentIndex(self,i):
        self.rail.setCurrentRow(i)
        if i>=0:self.stack.setCurrentIndex(i)
    def setCurrentWidget(self,pane):self.setCurrentIndex(self.indexOf(pane))
    def selected(self,i):
        if i<0:return
        self.stack.setCurrentIndex(i);self.currentChanged.emit(i)
        pane=self.widget(i)
        if pane:fade_in(pane.chat_title);fade_in(pane.chat.viewport())
    def insertTab(self,i,pane,title):
        i=min(max(0,i),self.count());self.rail.blockSignals(True);self.panes.insert(i,pane);self.stack.insertWidget(i,pane)
        self.rail.insertItem(i,QListWidgetItem(title));self.update_row(pane);self.rail.setCurrentRow(i);self.rail.blockSignals(False);self.selected(i);return i
    def removeTab(self,i):
        if not 0<=i<self.count():return
        self.rail.blockSignals(True);pane=self.panes.pop(i);self.stack.removeWidget(pane);self.rail.takeItem(i)
        index=min(i,self.count()-1);self.rail.setCurrentRow(index);self.rail.blockSignals(False);self.selected(index)
    def reorder(self,source,target):
        if source==target or not 0<=source<self.count():return
        pane=self.widget(source);self.removeTab(source);self.insertTab(min(target,self.count()),pane,conversation_title(pane.current));self.sort_pins()
    def sort_pins(self):
        positions={cid:i for i,cid in enumerate(self.host.manager.owner.cfg.pinned_conversations)}
        ordered=sorted(self.panes,key=lambda p:positions.get(p.current['id'],len(positions)))
        if ordered==self.panes:return
        current=self.currentWidget();self.setUpdatesEnabled(False);self.rail.blockSignals(True)
        try:
            for pane in self.panes:self.stack.removeWidget(pane)
            self.rail.clear();self.panes=ordered
            for pane in ordered:
                self.stack.addWidget(pane);self.rail.addItem(QListWidgetItem());self.update_row(pane)
            index=self.indexOf(current);self.rail.setCurrentRow(index);self.stack.setCurrentIndex(index)
        finally:self.rail.blockSignals(False);self.setUpdatesEnabled(True)
    def update_row(self,pane):
        i=self.indexOf(pane)
        if i<0:return
        item=self.rail.item(i);item.setText(conversation_title(pane.current))
        item.setData(ROW,session_data(pane.arc,pane.current,pane.current['id'] in self.host.manager.owner.cfg.pinned_conversations,True))
        item.setToolTip(f"{pane.current['owner']} · {pane.current['peer']}\n拖动调整顺序或合并窗口；右键可拆分")
    def tabToolTip(self,i):return self.rail.item(i).toolTip()

class ChatWindow(ChromeWindow):
    def __init__(self,manager):
        super().__init__(None);self.manager=manager;self.animate_open=True;self.setAttribute(Qt.WA_DeleteOnClose)
        self.setWindowTitle('独立聊天 · QQ聊天记录管理器');self.resize(1060,760);self.setMinimumSize(720,480)
        self.tabs=ChatStack(self);self.tabs.currentChanged.connect(self.update_title);self.install_chrome(self.tabs);self.setAcceptDrops(True)
    def event(self,e):
        if e.type()==QEvent.WindowActivate and hasattr(self,'manager'):self.manager.active=self
        return super().event(e)
    def update_title(self,*_):
        pane=self.tabs.currentWidget()
        if pane:self.setWindowTitle(conversation_title(pane.current)+f' · {self.tabs.count()} 个聊天')
    def add(self,pane,index=-1):
        self.tabs.insertTab(index if index>=0 else self.tabs.count(),pane,conversation_title(pane.current));self.tabs.sort_pins();self.update_title()
    def close_tab(self,index):
        pane=self.tabs.widget(index)
        if pane:self.tabs.removeTab(index);self.manager.panes.pop(pane.token,None);pane.deleteLater()
        if not self.tabs.count():self.close()
    def dragEnterEvent(self,e):
        pane=self.manager.resolve(e.mimeData())
        if pane:self.tabs.rail.preview(pane);e.acceptProposedAction()
    def dragMoveEvent(self,e):self.dragEnterEvent(e)
    def dragLeaveEvent(self,e):self.tabs.rail.clear_preview();e.accept()
    def dropEvent(self,e):
        pane=self.manager.resolve(e.mimeData());self.tabs.rail.clear_preview()
        if pane:self.manager.move(pane,self);e.setDropAction(Qt.MoveAction);e.accept()
    def closeEvent(self,e):
        for pane in self.tabs.panes:self.manager.panes.pop(pane.token,None)
        if self in self.manager.windows:self.manager.windows.remove(self)
        if self.manager.active is self:self.manager.active=self.manager.windows[-1] if self.manager.windows else None
        e.accept()

class ChatWindows:
    def __init__(self,owner):self.owner=owner;self.windows=[];self.panes={};self.active=None
    def new(self):
        w=ChatWindow(self);self.windows.append(w);self.active=w;return w
    def open(self,cid):
        pane=next((p for p in self.panes.values() if p.current and p.current['id']==cid),None)
        if pane:w=self.source(pane);w.tabs.setCurrentWidget(pane)
        else:
            pane=ChatPane(self.owner,cid);self.panes[pane.token]=pane
            w=self.active if self.active in self.windows else (self.windows[-1] if self.windows else self.new());w.add(pane)
        w.show();w.raise_();w.activateWindow();self.active=w;return w
    def source(self,pane):return next((w for w in self.windows if w.tabs.indexOf(pane)>=0),None)
    def move(self,pane,target,index=-1):
        if pane.token not in self.panes or target not in self.windows:return
        old=self.source(pane)
        if old is target:
            if index>=0:target.tabs.reorder(target.tabs.indexOf(pane),index)
            target.tabs.setCurrentWidget(pane);return
        if old:old.tabs.removeTab(old.tabs.indexOf(pane))
        target.add(pane,index);target.show();target.raise_();target.activateWindow();self.active=target
        if old and not old.tabs.count():old.close()
    def detach(self,pane,pos=None):
        old=self.source(pane)
        if old and old.tabs.count()==1:
            if pos:old.move(pos-QPoint(120,60))
            old.show();old.raise_();return old
        target=self.new()
        if pos:target.move(pos-QPoint(120,60))
        self.move(pane,target);return target
    def merge_all(self,target):
        for w in list(self.windows):
            if w is not target:
                for pane in list(w.tabs.panes):self.move(pane,target)
    def resolve(self,mime):
        if not mime.hasFormat(MIME):return None
        try:return self.panes.get(bytes(mime.data(MIME)).decode('ascii'))
        except UnicodeDecodeError:return None
    def clear_previews(self):
        for w in self.windows:w.tabs.rail.clear_preview()
    def set_enabled(self,enabled):
        for w in self.windows:w.setEnabled(enabled)
    def close_all(self):
        for w in list(self.windows):w.close()
        self.panes.clear()
