"""Fixed-width overlay panels: animation never reflows the text behind them."""
from PySide6.QtCore import Qt,QEvent,QPoint,QPropertyAnimation,QEasingCurve
from PySide6.QtWidgets import QFrame
from .design import motion_ms

class SlidePanel(QFrame):
    def __init__(self,parent,width=290):
        super().__init__(parent);self.panel_width=width;self.opened=False;self.setObjectName('details')
        self.motion=QPropertyAnimation(self,b'pos',self);self.motion.setEasingCurve(QEasingCurve.OutCubic)
        self.motion.finished.connect(self.settled);parent.installEventFilter(self);self.hide()
    def reveal(self,opened):
        self.motion.stop();was_visible=self.isVisible();self.opened=opened;p=self.parentWidget()
        self.resize(min(self.panel_width,p.width()),p.height())
        end=QPoint(p.width()-self.width() if opened else p.width(),0)
        if not was_visible:self.move(p.width(),0)
        self.show();self.raise_();self.motion.setDuration(motion_ms(220));self.motion.setStartValue(self.pos());self.motion.setEndValue(end);self.motion.start()
    def settled(self):
        if not self.opened:self.hide()
    def eventFilter(self,obj,event):
        if obj is self.parentWidget() and event.type()==QEvent.Resize:
            self.motion.stop();self.resize(min(self.panel_width,obj.width()),obj.height());self.move(obj.width()-self.width() if self.opened else obj.width(),0);self.setVisible(self.opened)
        return False
