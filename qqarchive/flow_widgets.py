"""Reusable prerequisite steps, selection summaries and local popup motion."""
from PySide6.QtCore import Qt,QPropertyAnimation,QEasingCurve,QObject,QEvent,QTimer,QPoint,QParallelAnimationGroup
from PySide6.QtWidgets import QFrame,QWidget,QVBoxLayout,QHBoxLayout,QLabel,QDialog,QMenu,QApplication,QListWidget
from .chrome import AnimatedButton,FramedDialog,position_dialog
from .design import motion_ms,TOKENS
from .icons import line_icon

class Step(QFrame):
    def __init__(self,number,title,description=''):
        super().__init__();self.setObjectName('card');self.expanded=False;self.available=True
        layout=QVBoxLayout(self);layout.setContentsMargins(20,12,20,16);layout.setSpacing(8)
        self.header=AnimatedButton(f'{number:02d}   {title}');self.header.setObjectName('stepHeader');self.header.setCheckable(True)
        self.header.setAccessibleName(title);self.header.clicked.connect(lambda checked:self.expand(checked))
        self.header.setIcon(line_icon('collapse'))
        self.note=QLabel('');self.note.setObjectName('muted');self.note.setWordWrap(True)
        layout.addWidget(self.header);layout.addWidget(self.note)
        self.body=QWidget();self.body.setObjectName('stepBody');self.box=QVBoxLayout(self.body);self.box.setContentsMargins(4,8,4,4);self.box.setSpacing(12)
        if description:
            desc=QLabel(description);desc.setWordWrap(True);desc.setObjectName('muted');self.box.addWidget(desc)
        layout.addWidget(self.body)
        self.animation=QPropertyAnimation(self.body,b'maximumHeight',self);self.animation.setDuration(motion_ms(TOKENS['expand_ms']));self.animation.setEasingCurve(QEasingCurve.OutCubic)
        self.animation.finished.connect(self._finished);self.body.hide()
    def _finished(self):
        if self.expanded:self.body.setMaximumHeight(16777215)
        else:self.body.hide()
    def expand(self,opened=True,animate=True):
        self.animation.stop();start=self.body.height() if self.body.isVisible() else 0
        self.expanded=bool(opened);self.header.setChecked(self.expanded)
        self.header.setIcon(line_icon('expand' if opened else 'collapse'))
        end=max(1,self.body.sizeHint().height()) if opened else 0
        if not animate or not self.isVisible() or not motion_ms(TOKENS['expand_ms']):
            self.body.setMaximumHeight(16777215 if opened else 0);self.body.setVisible(opened);return
        self.body.setMaximumHeight(start);self.body.show();self.animation.setStartValue(start);self.animation.setEndValue(end);self.animation.start()
    def unlock(self,available,summary=''):
        changed=self.available!=bool(available);self.available=bool(available);self.note.setText(summary)
        self.setVisible(self.available)
        if changed and available:self.expand(True)
        elif not available:self.expand(False,False)
    def complete(self,text):self.note.setText('已完成 · '+text)

class SelectionBar(QWidget):
    def __init__(self,view:QListWidget,changed=None):
        super().__init__();self.view=view;self.changed=changed
        l=QHBoxLayout(self);l.setContentsMargins(0,0,0,0);l.setSpacing(8)
        self.summary=QLabel('已选 0 项');self.summary.setObjectName('selectionSummary');l.addWidget(self.summary);l.addStretch()
        for title,state in [('全选',Qt.Checked),('取消全选',Qt.Unchecked)]:
            b=AnimatedButton(title);b.clicked.connect(lambda _,s=state:self.choose(s));l.addWidget(b)
        view.itemChanged.connect(self.refresh);view.model().rowsInserted.connect(self.refresh);view.model().rowsRemoved.connect(self.refresh)
    def choose(self,state):
        self.view.blockSignals(True)
        for i in range(self.view.count()):self.view.item(i).setCheckState(state)
        self.view.blockSignals(False);self.refresh()
    def refresh(self,*_):
        if getattr(self,'suspended',False):return
        n=sum(self.view.item(i).checkState()==Qt.Checked for i in range(self.view.count()))
        self.summary.setText(f'已选 {n} / {self.view.count()} 项')
        if self.changed:self.changed(n)

class PopupMotion(QObject):
    def open_popup(self,obj):
        if not obj.isVisible() or getattr(obj,'_finishing',False):return
        if isinstance(obj,FramedDialog):
            if not getattr(obj,'_positioned',False):position_dialog(obj);obj._positioned=True
            parent=obj.parentWidget()
            if parent and obj.windowModality()!=Qt.NonModal:
                if getattr(obj,'_shade',None):obj._shade.hide();obj._shade.deleteLater()
                owner=parent.window();shade=QWidget(owner);shade.setObjectName('dialogShade');shade.setAttribute(Qt.WA_StyledBackground)
                shade.setStyleSheet('QWidget#dialogShade{background:rgba(32,33,38,28);border:0;}')
                shade.setGeometry(owner.rect());shade.show();shade.raise_();obj._shade=shade;obj.raise_()
        active=getattr(obj,'_popup_motion',None)
        if active:active.stop()
        else:
            active=QPropertyAnimation(obj,b'windowOpacity',obj);active.setEasingCurve(QEasingCurve.OutCubic);obj._popup_motion=active
        active.setDuration(motion_ms(TOKENS['popup_ms']));active.setStartValue(0.);active.setEndValue(1.);active.start()
    def eventFilter(self,obj,event):
        if not isinstance(obj,(FramedDialog,QMenu)):return False
        if event.type()==QEvent.Show:
            # Hide the first unlaid-out frame before the native window paints.
            obj.setWindowOpacity(0. if motion_ms(TOKENS['popup_ms']) else 1.)
            if not getattr(obj,'_popup_start',None):
                timer=QTimer(obj);timer.setSingleShot(True);timer.timeout.connect(lambda:self.open_popup(obj));obj._popup_start=timer
            obj._popup_start.start(0)
        elif event.type()==QEvent.Hide:
            if getattr(obj,'_popup_start',None):obj._popup_start.stop()
            if getattr(obj,'_shade',None):obj._shade.hide();obj._shade.deleteLater();obj._shade=None
            if getattr(obj,'_popup_motion',None):obj._popup_motion.stop()
            obj.setWindowOpacity(1.)
            # The real dialog fades before hiding. Never create an extra screenshot
            # window here: it steals Z-order and flashes during modeless navigation.
        return False

def install_popup_motion():
    app=QApplication.instance()
    if app and not hasattr(app,'_popup_filter'):
        app._popup_filter=PopupMotion(app);app.installEventFilter(app._popup_filter)

