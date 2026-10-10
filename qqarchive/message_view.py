"""Bounded chat viewport with smooth wheel movement and disposable crossfades."""
from PySide6.QtCore import Qt,Signal,QPropertyAnimation,QEasingCurve,QPoint
from PySide6.QtWidgets import QListView,QLabel,QGraphicsOpacityEffect
from .design import motion_ms

class MessageView(QListView):
    boundary=Signal(int)
    avatarClicked=Signal(object)
    avatarContext=Signal(object,object)
    contextMessage=Signal(object,object)
    def __init__(self):
        super().__init__();self.setMouseTracking(True);self.viewport().setMouseTracking(True)
        self.hovered_row=-1;self.hover_alpha=0.;self.overlay=None;self.transition=None
        self.wheel_animation=QPropertyAnimation(self.verticalScrollBar(),b'value',self)
        self.wheel_animation.setDuration(motion_ms(170));self.wheel_animation.setEasingCurve(QEasingCurve.OutCubic)
        self.setContextMenuPolicy(Qt.CustomContextMenu)
        self.customContextMenuRequested.connect(self.context_at)
    def wheelEvent(self,event):
        bar=self.verticalScrollBar();delta=event.pixelDelta().y() or event.angleDelta().y()*.8
        if not delta:return super().wheelEvent(event)
        origin=self.wheel_animation.endValue() if self.wheel_animation.state()==QPropertyAnimation.Running else bar.value()
        previous=bar.value()
        if delta>0 and bar.value()<=bar.minimum()+2:self.boundary.emit(-1)
        elif delta<0 and bar.value()>=bar.maximum()-2:self.boundary.emit(1)
        # Loading a boundary page moves the scrollbar to keep the visible row
        # anchored. Apply this wheel delta to that new position, not the old 0.
        target=origin+(bar.value()-previous)-delta
        target=max(bar.minimum(),min(bar.maximum(),round(target)))
        self.wheel_animation.stop()
        if motion_ms(170)==0 or event.pixelDelta().y():bar.setValue(target)
        else:
            self.wheel_animation.setStartValue(bar.value());self.wheel_animation.setEndValue(target);self.wheel_animation.start()
        event.accept()
    def mouseMoveEvent(self,event):
        index=self.indexAt(event.position().toPoint());row=index.row() if index.isValid() else -1
        if row!=self.hovered_row:self.hovered_row=row;self.viewport().update()
        super().mouseMoveEvent(event)
    def leaveEvent(self,event):
        self.hovered_row=-1;self.viewport().update();super().leaveEvent(event)
    def mousePressEvent(self,event):
        self.wheel_animation.stop()
        index=self.indexAt(event.position().toPoint())
        if index.isValid() and event.button()==Qt.LeftButton:
            m=index.data(Qt.UserRole);rect=self.visualRect(index);x=rect.right()-43 if m['is_self'] else rect.left()+12
            if x<=event.position().x()<=x+32 and rect.top()+24<=event.position().y()<=rect.top()+56:
                self.avatarClicked.emit(m);event.accept();return
        super().mousePressEvent(event)
    def context_at(self,point):
        index=self.indexAt(point)
        if not index.isValid():return
        message=index.data(Qt.UserRole);rect=self.visualRect(index);x=rect.right()-43 if message['is_self'] else rect.left()+12
        signal=self.avatarContext if x<=point.x()<=x+32 and rect.top()+24<=point.y()<=rect.top()+56 else self.contextMessage
        signal.emit(message,self.viewport().mapToGlobal(point))
    def begin_transition(self):
        self.wheel_animation.stop()
        if self.transition:self.transition.stop()
        if self.overlay:self.overlay.hide();self.overlay.deleteLater();self.overlay=None
        if not self.isVisible() or not motion_ms(190):return
        pixmap=self.viewport().grab();overlay=QLabel(self.viewport());overlay.setPixmap(pixmap)
        overlay.setGeometry(self.viewport().rect());overlay.setAttribute(Qt.WA_TransparentForMouseEvents)
        effect=QGraphicsOpacityEffect(overlay);overlay.setGraphicsEffect(effect)
        animation=QPropertyAnimation(effect,b'opacity',overlay);animation.setDuration(motion_ms(190));animation.setStartValue(1.);animation.setEndValue(0.)
        animation.setEasingCurve(QEasingCurve.OutCubic);self.overlay=overlay;self.transition=animation
        def finished():
            overlay.hide();overlay.deleteLater()
            if self.overlay is overlay:self.overlay=None;self.transition=None
            self.viewport().update()
        animation.finished.connect(finished);overlay.show();animation.start()
