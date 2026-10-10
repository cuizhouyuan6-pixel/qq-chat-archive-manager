"""Interruptible pixel scrolling for the conversation rails."""
from PySide6.QtCore import QObject,QEvent,QPropertyAnimation,QEasingCurve
from PySide6.QtWidgets import QAbstractItemView
from .design import motion_ms

class SmoothScroll(QObject):
    def __init__(self,view):
        super().__init__(view);self.view=view;view.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)
        self.animation=QPropertyAnimation(view.verticalScrollBar(),b'value',self)
        self.animation.setEasingCurve(QEasingCurve.OutCubic)
        view.viewport().installEventFilter(self);view.verticalScrollBar().sliderPressed.connect(self.animation.stop)
    def eventFilter(self,obj,event):
        if event.type() in (QEvent.MouseButtonPress,QEvent.Hide):self.animation.stop()
        if event.type()!=QEvent.Wheel:return False
        delta=event.pixelDelta().y() or event.angleDelta().y()*.8
        if not delta:return False
        bar=self.view.verticalScrollBar();origin=self.animation.endValue() if self.animation.state()==QPropertyAnimation.Running else bar.value()
        target=max(bar.minimum(),min(bar.maximum(),round(origin-delta)));self.animation.stop()
        duration=motion_ms(170)
        if not duration or event.pixelDelta().y():bar.setValue(target)
        else:
            self.animation.setDuration(duration);self.animation.setStartValue(bar.value());self.animation.setEndValue(target);self.animation.start()
        event.accept();return True

