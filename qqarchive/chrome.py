"""Shared native window chrome and small, interruptible UI animations."""
from __future__ import annotations
import sys
from PySide6.QtCore import Qt, QSize, QRectF, QPropertyAnimation, Property, QEasingCurve, Signal,QPoint
from PySide6.QtGui import QIcon, QPixmap, QPainter, QColor, QPen,QGuiApplication,QCursor,QPainterPath,QRegion
from PySide6.QtWidgets import (QMainWindow, QDialog, QWidget, QHBoxLayout, QLabel, QPushButton,
                              QVBoxLayout, QGraphicsOpacityEffect,QSizePolicy,QScrollArea,QFrame)
from .resources import resource_path
from .design import motion_ms,TOKENS

def app_icon():
    return QIcon(str(resource_path('assets', 'app.ico')))

def glyph(name):
    pix=QPixmap(24,24);pix.fill(Qt.transparent)
    p=QPainter(pix);p.setRenderHint(QPainter.Antialiasing)
    p.setPen(QPen(QColor('#45474e'),1.5,Qt.SolidLine,Qt.RoundCap,Qt.RoundJoin))
    if name=='close':p.drawLine(7,7,17,17);p.drawLine(17,7,7,17)
    elif name=='min':p.drawLine(6,13,18,13)
    elif name=='max':p.drawRoundedRect(QRectF(6,6,12,12),1.5,1.5)
    elif name=='restore':p.drawRect(6,9,10,10);p.drawLine(9,5,19,5);p.drawLine(19,5,19,15)
    elif name=='pin':
        p.drawLine(9,5,15,5);p.drawLine(10,5,9,12);p.drawLine(14,5,15,12)
        p.drawLine(9,12,6,15);p.drawLine(6,15,18,15);p.drawLine(18,15,15,12);p.drawLine(12,15,12,21)
    elif name=='detach':
        p.drawLine(5,5,11,5);p.drawLine(5,5,5,19);p.drawLine(5,19,19,19);p.drawLine(19,19,19,13)
        p.drawLine(12,12,20,4);p.drawLine(14,4,20,4);p.drawLine(20,4,20,10)
    elif name=='merge':
        p.drawRoundedRect(QRectF(4,8,16,12),2,2);p.drawLine(7,4,17,4);p.drawLine(8,12,16,12)
    p.end();return QIcon(pix)

class AnimatedButton(QPushButton):
    def __init__(self,*args,**kwargs):
        super().__init__(*args,**kwargs);self._pulse=0.;self._hover=0.
        self.animation=QPropertyAnimation(self,b'pulse',self);self.animation.setDuration(motion_ms(TOKENS['feedback_ms']))
        self.animation.setEasingCurve(QEasingCurve.OutCubic)
        self.pressed.connect(self.start_pulse)
        self.hover_animation=QPropertyAnimation(self,b'hoverLevel',self);self.hover_animation.setDuration(motion_ms(TOKENS['feedback_ms']));self.hover_animation.setEasingCurve(QEasingCurve.OutCubic)
    def get_hover(self):return self._hover
    def set_hover(self,value):self._hover=value;self.update()
    hoverLevel=Property(float,get_hover,set_hover)
    def hover_to(self,value):
        self.hover_animation.stop();self.hover_animation.setStartValue(self._hover);self.hover_animation.setEndValue(value);self.hover_animation.start()
    def enterEvent(self,event):self.hover_to(1.);super().enterEvent(event)
    def leaveEvent(self,event):self.hover_to(0.);super().leaveEvent(event)
    def setDefault(self,on):
        # Records explicit choices only: Qt's own implicit promotions call the C++
        # QPushButton::setDefault directly and never reach this override.
        self.explicit_default=bool(on);super().setDefault(on)
    def start_pulse(self):
        self.animation.stop();self.animation.setStartValue(1.);self.animation.setEndValue(0.);self.animation.start()
    def get_pulse(self):return self._pulse
    def set_pulse(self,value):self._pulse=value;self.update()
    pulse=Property(float,get_pulse,set_pulse)
    def paintEvent(self,event):
        super().paintEvent(event)
        if self._hover>0 and self.isEnabled():
            p=QPainter(self);p.setRenderHint(QPainter.Antialiasing);p.setPen(Qt.NoPen)
            p.setBrush(QColor(32,33,38,int(13*self._hover)));p.drawRoundedRect(QRectF(self.rect()).adjusted(1,1,-1,-1),9,9);p.end()
        if self.objectName()=='nav' and self.isChecked():
            p=QPainter(self);p.setPen(Qt.NoPen);p.setBrush(QColor(TOKENS['ink']));p.drawRoundedRect(QRectF(1,14,3,self.height()-28),1.5,1.5);p.end()
        if self._pulse>0:
            p=QPainter(self);p.setRenderHint(QPainter.Antialiasing);p.setPen(Qt.NoPen)
            p.setBrush(QColor(110,115,125,int(45*self._pulse)));p.drawRoundedRect(QRectF(self.rect()).adjusted(1,1,-1,-1),9,9)

class TitleLabel(QLabel):
    doubleClicked=Signal()
    def mouseDoubleClickEvent(self,event):
        if event.button()==Qt.LeftButton:self.doubleClicked.emit();event.accept()
        else:super().mouseDoubleClickEvent(event)

class WindowTitle(QLabel):
    """A long title must not force its dialog outside the available screen."""
    def __init__(self,text):
        super().__init__(text);self.setMinimumWidth(0);self.setSizePolicy(QSizePolicy.Ignored,QSizePolicy.Preferred)
    def paintEvent(self,event):
        painter=QPainter(self);painter.setPen(self.palette().windowText().color());painter.setFont(self.font())
        text=self.fontMetrics().elidedText(self.text(),Qt.ElideRight,self.contentsRect().width())
        painter.drawText(self.contentsRect(),Qt.AlignLeft|Qt.AlignVCenter,text)

class TitleBar(QWidget):
    def __init__(self,window,dialog=False):
        super().__init__(window);self.host=window;self.dialog=dialog;self.setObjectName('titlebar');self.setFixedHeight(46)
        l=QHBoxLayout(self);l.setContentsMargins(14,3,7,3);l.setSpacing(4)
        mark=QLabel();mark.setPixmap(app_icon().pixmap(25,25));mark.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.title=WindowTitle(window.windowTitle());self.title.setObjectName('windowTitle');self.title.setAttribute(Qt.WA_TransparentForMouseEvents)
        mark.setFixedWidth(25)
        if dialog:l.addWidget(mark);l.addSpacing(8);l.addWidget(self.title,1)
        else:
            mark.setParent(self);mark.hide();self.title.setParent(self);self.title.hide();l.addStretch(1)
        if not dialog:
            self.pin=self.control('pin','置顶窗口',self.toggle_pin);self.pin.setCheckable(True)
            self.control('min','最小化',window.showMinimized)
            self.max_button=self.control('max','最大化 / 还原',self.toggle_max)
        self.control('close','关闭窗口',window.close).setObjectName('windowClose')
        window.windowTitleChanged.connect(self.title.setText)
    def control(self,name,tip,fn):
        b=AnimatedButton();b.setObjectName('windowControl');b.setIcon(glyph(name));b.setIconSize(QSize(22,22));b.setFixedSize(36,34)
        b.setToolTip(tip);b.setAccessibleName(tip);b.clicked.connect(fn);self.layout().addWidget(b);return b
    def toggle_pin(self,checked):
        w=self.host;w.set_topmost(checked)
        self.pin.setToolTip('取消置顶' if checked else '置顶窗口')
    def toggle_max(self):
        self.host.showNormal() if self.host.isMaximized() else self.host.showMaximized()
    def mousePressEvent(self,event):
        if event.button()==Qt.LeftButton and self.host.windowHandle():self.host.windowHandle().startSystemMove()
        super().mousePressEvent(event)
    def mouseDoubleClickEvent(self,event):
        if event.button()==Qt.LeftButton and not self.dialog:self.toggle_max();event.accept()
    def sync(self):
        if not self.dialog:self.max_button.setIcon(glyph('restore' if self.host.isMaximized() else 'max'))

class FramedDialog(QDialog):
    """Keep standard QDialog acceptance/cancellation with application chrome."""
    def __init__(self,parent=None):
        super().__init__(parent,Qt.Dialog|Qt.FramelessWindowHint);self.setWindowIcon(app_icon());self._framed=False
        self.setAttribute(Qt.WA_TranslucentBackground)
        self._finishing=False;self._close_animation=None
    def exec(self):
        from .modeless import wait_result
        return wait_result(self)
    def done(self,result):
        if self._finishing:return
        self.setResult(result)
        if self.isVisible() and motion_ms(160) and not QApplicationClosing():
            self._finishing=True;self.setEnabled(False)
            active=getattr(self,'_popup_motion',None)
            if active:active.stop()
            animation=QPropertyAnimation(self,b'windowOpacity',self);animation.setDuration(motion_ms(160))
            animation.setStartValue(self.windowOpacity());animation.setEndValue(0.);self._close_animation=animation
            def finish():
                super(FramedDialog,self).done(result);self._finishing=False;self.setEnabled(True)
            animation.finished.connect(finish);animation.start();return
        super().done(result)
    def closeEvent(self,event):
        event.ignore();self.reject()
    def keyPressEvent(self,event):
        # QDialog clicks its default button on Enter and, when none was chosen,
        # promotes the first autoDefault button -- our title bar close button, added
        # on first show -- so Enter in a text or spin field closed the window (search,
        # batch picker, time locator). Only a button a dialog explicitly marked as
        # default reacts here; a focused button still handles Enter itself.
        plain=not event.modifiers() or (event.modifiers()==Qt.KeypadModifier and event.key()==Qt.Key_Enter)
        if event.key() in (Qt.Key_Return,Qt.Key_Enter) and plain:
            target=next((b for b in self.findChildren(QPushButton) if b.window() is self and getattr(b,'explicit_default',False) and b.isVisible()),None)
            if target is not None and target.isEnabled():target.click()
            return
        super().keyPressEvent(event)
    def paintEvent(self,event):
        painter=QPainter(self);painter.setRenderHint(QPainter.Antialiasing)
        painter.setCompositionMode(QPainter.CompositionMode_Source);painter.fillRect(self.rect(),Qt.transparent)
        painter.setCompositionMode(QPainter.CompositionMode_SourceOver)
        painter.setPen(QColor(TOKENS['line']));painter.setBrush(QColor(TOKENS['panel'] if self.objectName()=='messageDialog' else TOKENS['background']))
        painter.drawRoundedRect(QRectF(self.rect()).adjusted(.5,.5,-.5,-.5),TOKENS['dialog_radius'],TOKENS['dialog_radius'])
    def resizeEvent(self,event):
        super().resizeEvent(event)
        # Clip descendants as well as the background; a stylesheet radius alone
        # leaves scroll viewports and footer widgets covering the lower corners.
        path=QPainterPath();path.addRoundedRect(QRectF(self.rect()),TOKENS['dialog_radius'],TOKENS['dialog_radius'])
        self.setMask(QRegion(path.toFillPolygon().toPolygon()))
    def showEvent(self,event):
        if not self._framed and self.layout():
            requested=self.size()+QSize(0,46)
            self._framed=True;body=QWidget();body.setObjectName('dialogBody');body.setLayout(self.layout())
            outer=QVBoxLayout(self);outer.setContentsMargins(1,1,1,1);outer.setSpacing(0)
            self.titlebar=TitleBar(self,dialog=True);outer.addWidget(self.titlebar)
            if getattr(self,'fixed_body',False):
                # Complex dialogs scroll their result/filter panes separately;
                # keep the footer in the window's fixed layout.
                outer.addWidget(body,1);self._body_widget=body
            else:
                scroll=QScrollArea();scroll.setObjectName('dialogScroll');scroll.viewport().setObjectName('dialogViewport')
                scroll.setFrameShape(QFrame.NoFrame);scroll.setWidgetResizable(True);scroll.setWidget(body)
                outer.addWidget(scroll,1);self._body_scroll=scroll
            self.setObjectName('windowShell');self.setSizeGripEnabled(False)
            body.ensurePolished();body.layout().activate();requested.setHeight(max(requested.height(),body.sizeHint().height()+48))
            bounds=dialog_available_geometry(self).adjusted(16,16,-16,-16)
            self.setMinimumSize(min(self.minimumWidth(),bounds.width()),min(self.minimumHeight(),bounds.height()))
            self.setMaximumSize(bounds.size());self.resize(requested.boundedTo(bounds.size()))
        super().showEvent(event)

def dialog_available_geometry(dialog):
    parent=dialog.parentWidget()
    if parent:
        owner=parent.window();screen=QGuiApplication.screenAt(owner.frameGeometry().center()) or owner.screen()
    else:screen=QGuiApplication.screenAt(QCursor.pos()) or QGuiApplication.primaryScreen()
    return screen.availableGeometry()

def position_dialog(dialog):
    bounds=dialog_available_geometry(dialog).adjusted(16,16,-16,-16)
    parent=dialog.parentWidget()
    center=parent.window().frameGeometry().center() if parent and parent.window().isVisible() else bounds.center()
    x=center.x()-dialog.width()//2;y=center.y()-dialog.height()//2
    x=max(bounds.left(),min(x,bounds.right()-dialog.width()+1))
    y=max(bounds.top(),min(y,bounds.bottom()-dialog.height()+1))
    dialog.move(QPoint(x,y))

class ChromeWindow(QMainWindow):
    def __init__(self,parent=None):
        super().__init__(parent,Qt.Window|Qt.FramelessWindowHint)
        self.setWindowIcon(app_icon())
        self._topmost=False
    def set_topmost(self,enabled):
        # Qt's setWindowFlag recreates a visible native window. Set only Z-order
        # on Windows, preserving the HWND, activation, geometry and paint state.
        if sys.platform=='win32' and QGuiApplication.platformName()=='windows':
            import ctypes
            from ctypes import wintypes
            call=ctypes.windll.user32.SetWindowPos
            call.argtypes=[wintypes.HWND,wintypes.HWND,ctypes.c_int,ctypes.c_int,ctypes.c_int,ctypes.c_int,wintypes.UINT];call.restype=wintypes.BOOL
            if not call(int(self.winId()),-1 if enabled else -2,0,0,0,0,0x0001|0x0002|0x0010):raise ctypes.WinError()
        else:
            state=self.windowState();geometry=self.geometry();self.setWindowFlag(Qt.WindowStaysOnTopHint,enabled);self.setGeometry(geometry);self.setWindowState(state);self.show()
        self._topmost=bool(enabled)
    def install_chrome(self,body):
        shell=QWidget();shell.setObjectName('windowShell');l=QVBoxLayout(shell);l.setContentsMargins(1,1,1,1);l.setSpacing(0)
        self.titlebar=TitleBar(self);l.addWidget(self.titlebar);l.addWidget(body,1);self.setCentralWidget(shell)
    def changeEvent(self,event):
        super().changeEvent(event)
        if hasattr(self,'titlebar'):self.titlebar.sync()
    def showEvent(self,event):
        super().showEvent(event)
        if getattr(self,'animate_open',False) and not getattr(self,'_revealed',False):
            self._revealed=True;self.setWindowOpacity(0. if motion_ms(180) else 1.)
            animation=QPropertyAnimation(self,b'windowOpacity',self);animation.setDuration(motion_ms(180));animation.setStartValue(0.);animation.setEndValue(1.);self._reveal_animation=animation;animation.start()
        if sys.platform=='win32':
            try:
                import ctypes
                rounded=ctypes.c_int(2)
                ctypes.windll.dwmapi.DwmSetWindowAttribute(ctypes.c_void_p(int(self.winId())),33,ctypes.byref(rounded),4)
            except (OSError,AttributeError):pass
    def nativeEvent(self,event_type,message):
        if sys.platform=='win32' and not self.isMaximized():
            import ctypes
            from ctypes import wintypes
            msg=wintypes.MSG.from_address(int(message))
            if msg.message==0x84: # WM_NCHITTEST: only borders, leaving all child widgets interactive.
                rect=wintypes.RECT();ctypes.windll.user32.GetWindowRect(ctypes.c_void_p(int(self.winId())),ctypes.byref(rect))
                x=ctypes.c_short(msg.lParam&0xffff).value;y=ctypes.c_short((msg.lParam>>16)&0xffff).value
                edge=round(6*self.devicePixelRatioF());left=x<rect.left+edge;right=x>=rect.right-edge;top=y<rect.top+edge;bottom=y>=rect.bottom-edge
                hit=13 if top and left else 14 if top and right else 16 if bottom and left else 17 if bottom and right else 10 if left else 11 if right else 12 if top else 15 if bottom else 0
                if hit:return True,hit
        return super().nativeEvent(event_type,message)

def fade_in(widget):
    # Never attach opacity effects to scrolling viewports or nested pages:
    # Qt can retain displaced/stale viewport pixmaps after a model reset.
    animation=getattr(widget,'_fade',None)
    if animation:animation.stop()
    else:
        cover=QWidget(widget);cover.setAttribute(Qt.WA_TransparentForMouseEvents)
        cover.setStyleSheet('background:'+TOKENS['background']+';border:0;')
        effect=QGraphicsOpacityEffect(cover);cover.setGraphicsEffect(effect)
        animation=QPropertyAnimation(effect,b'opacity',widget);animation.setEasingCurve(QEasingCurve.OutCubic)
        animation.finished.connect(cover.hide);widget._fade=animation;widget._fade_cover=cover
    cover=widget._fade_cover;cover.setGeometry(widget.rect());cover.show();cover.raise_()
    animation.setDuration(motion_ms(180));animation.setStartValue(1.);animation.setEndValue(0.);animation.start()

def QApplicationClosing():
    from PySide6.QtWidgets import QApplication
    return not QApplication.instance() or QApplication.closingDown()
