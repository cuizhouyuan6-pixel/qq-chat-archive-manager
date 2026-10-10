"""Owned conversation drag/drop: Qt must never delete the source row."""
import uuid
from PySide6.QtCore import Qt,Signal,QMimeData,QPoint,QTimer
from PySide6.QtWidgets import QTreeWidget,QAbstractItemView
from PySide6.QtGui import QPalette,QColor,QDrag,QPainter,QPen
from .session_groups import GROUP_ROLE
from .smooth_scroll import SmoothScroll

MIME='application/x-qqarchive-conversation-group'

class SessionTree(QTreeWidget):
    orderChanged=Signal(list)
    groupMoved=Signal(int,str,object)
    def __init__(self):
        super().__init__();self.setAnimated(True);self.setDragEnabled(True);self.setAcceptDrops(True)
        # Stock InternalMove startDrag removes the selected row after exec().
        # Our drop handler already moves it, so own the QDrag lifecycle instead.
        self.setDragDropMode(QAbstractItemView.DragDrop);self.setDefaultDropAction(Qt.MoveAction)
        self.smooth=SmoothScroll(self);self._drag_token=None;self._drag_cid=None;self._drop_pos=None
        self._edge_timer=QTimer(self);self._edge_timer.setInterval(30);self._edge_timer.timeout.connect(self._scroll_drag)
        palette=self.palette();palette.setColor(QPalette.Highlight,QColor('#F4F5F7'));palette.setColor(QPalette.HighlightedText,QColor('#202126'));self.setPalette(palette)
    def group_of(self,item):
        while item:
            group=item.data(0,GROUP_ROLE)
            if group is not None:return group
            item=item.parent()
        return ''
    def ids(self):
        result=[]
        def walk(parent):
            for n in range(parent.childCount()):
                child=parent.child(n);cid=child.data(0,Qt.UserRole)
                if cid:result.append(cid)
                walk(child)
        walk(self.invisibleRootItem());return result
    def reorder(self,cid,before=None):
        # Cross-category moves are stored as a global display order. Database owner/kind stays unchanged.
        ids=self.ids()
        if cid not in ids or before==cid:return
        ids.remove(cid)
        if before in ids:ids.insert(ids.index(before),cid)
        else:ids.append(cid)
        self.orderChanged.emit(ids)
    def startDrag(self,actions):
        item=self.currentItem();cid=item.data(0,Qt.UserRole) if item else None
        if not cid:return
        self.smooth.animation.stop();self._drag_token=uuid.uuid4().hex;self._drag_cid=cid
        mime=QMimeData();mime.setData(MIME,self._drag_token.encode())
        drag=QDrag(self);drag.setMimeData(mime)
        drag.setPixmap(self.viewport().grab(self.visualItemRect(item)));drag.setHotSpot(QPoint(60,28))
        try:drag.exec(Qt.MoveAction,Qt.MoveAction)
        finally:
            self._drag_token=None;self._drag_cid=None;self.clear_drop();drag.deleteLater()
    def valid_drag(self,event):
        return bool(self._drag_cid and self._drag_token and event.mimeData().hasFormat(MIME)
                    and bytes(event.mimeData().data(MIME))==self._drag_token.encode())
    def dragEnterEvent(self,event):
        if self.valid_drag(event):event.setDropAction(Qt.MoveAction);event.accept()
        else:event.ignore()
    def dragMoveEvent(self,event):
        if not self.valid_drag(event):event.ignore();self.clear_drop();return
        self._drop_pos=event.position().toPoint();event.setDropAction(Qt.MoveAction);event.accept()
        self._edge_timer.start();self.viewport().update()
    def _scroll_drag(self):
        if self._drop_pos is None:return
        y=self._drop_pos.y();height=self.viewport().height();bar=self.verticalScrollBar()
        delta=-16 if y<28 else (16 if y>height-28 else 0)
        if delta:bar.setValue(bar.value()+delta);self.viewport().update()
    def clear_drop(self):
        self._drop_pos=None;self._edge_timer.stop();self.viewport().update()
    def dragLeaveEvent(self,event):self.clear_drop();event.accept()
    def dropEvent(self,event):
        if not self.valid_drag(event):event.ignore();self.clear_drop();return
        cid=self._drag_cid;pos=event.position().toPoint();target=self.itemAt(pos)
        before=target.data(0,Qt.UserRole) if target else None;group=self.group_of(target)
        if before==cid:event.ignore();self.clear_drop();return
        if before and pos.y()>self.visualItemRect(target).center().y():
            parent=target.parent();index=parent.indexOfChild(target)+1
            before=parent.child(index).data(0,Qt.UserRole) if index<parent.childCount() else None
        self.clear_drop();event.setDropAction(Qt.MoveAction);event.accept()
        # One mutation only. startDrag performs no native removeRows afterwards.
        self.groupMoved.emit(cid,group,before)
    def paintEvent(self,event):
        super().paintEvent(event)
        if self._drop_pos is None:return
        item=self.itemAt(self._drop_pos)
        if not item:return
        rect=self.visualItemRect(item).adjusted(1,1,-2,-2);p=QPainter(self.viewport())
        p.setRenderHint(QPainter.Antialiasing);p.setPen(QPen(QColor('#6E7480'),1.5,Qt.DashLine))
        if item.data(0,Qt.UserRole):
            y=rect.bottom() if self._drop_pos.y()>rect.center().y() else rect.top()
            p.drawLine(rect.left(),y,rect.right(),y)
        else:
            p.setBrush(QColor(110,116,128,18));p.drawRoundedRect(rect,6,6)
