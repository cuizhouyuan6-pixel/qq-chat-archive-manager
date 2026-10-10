"""Application-owned message dialogs with bounded, DPI-independent layouts.

Do not reparent QMessageBox's private layout: Qt's updateSize() owns that grid.
The facade preserves the button values/roles used by the existing workflows.
"""
from __future__ import annotations
import math
from PySide6.QtCore import Qt,QSize,QPointF
from PySide6.QtGui import QPainter,QColor,QPen,QPixmap,QPolygonF,QTextOption,QTextCursor
from PySide6.QtWidgets import (QApplication,QWidget,QVBoxLayout,QHBoxLayout,QLabel,
    QTextEdit,QLineEdit,QDialogButtonBox,QMessageBox as NativeMessageBox,QStyle,QSizePolicy)
from .chrome import FramedDialog,TitleBar,AnimatedButton,dialog_available_geometry,position_dialog


def modal_parent(parent):
    active=QApplication.activeModalWidget()
    if parent and active and active.isVisible():
        ancestor=active
        while ancestor:
            if ancestor is parent:return active
            ancestor=ancestor.parentWidget()
    return parent


def status_pixmap(icon):
    pix=QPixmap(96,96);pix.setDevicePixelRatio(3);pix.fill(Qt.transparent)
    painter=QPainter(pix);painter.setRenderHint(QPainter.Antialiasing)
    color=QColor('#A45C61' if icon in (NativeMessageBox.Warning,NativeMessageBox.Critical) else '#6E7480')
    painter.setPen(QPen(color,1.7,Qt.SolidLine,Qt.RoundCap,Qt.RoundJoin))
    if icon==NativeMessageBox.Warning:
        painter.drawPolygon(QPolygonF([QPointF(16,4),QPointF(29,27),QPointF(3,27)]))
        painter.drawLine(16,12,16,19);painter.drawPoint(16,23)
    else:
        painter.drawEllipse(4,4,24,24)
        if icon==NativeMessageBox.Critical:painter.drawLine(12,12,20,20);painter.drawLine(20,12,12,20)
        elif icon==NativeMessageBox.Question:
            font=painter.font();font.setPixelSize(19);painter.setFont(font);painter.drawText(0,0,32,32,Qt.AlignCenter,'?')
        else:painter.drawPoint(16,10);painter.drawLine(16,14,16,23)
    painter.end();return pix


class MessageBox(FramedDialog):
    NoIcon=NativeMessageBox.NoIcon;Information=NativeMessageBox.Information
    Warning=NativeMessageBox.Warning;Critical=NativeMessageBox.Critical;Question=NativeMessageBox.Question
    NoButton=NativeMessageBox.NoButton;Ok=NativeMessageBox.Ok;Yes=NativeMessageBox.Yes
    No=NativeMessageBox.No;Cancel=NativeMessageBox.Cancel;Close=NativeMessageBox.Close
    AcceptRole=NativeMessageBox.AcceptRole;RejectRole=NativeMessageBox.RejectRole
    ActionRole=NativeMessageBox.ActionRole;DestructiveRole=NativeMessageBox.DestructiveRole
    _standards=((Ok,'确定',QDialogButtonBox.AcceptRole),
                (Yes,'是，继续',QDialogButtonBox.YesRole),(No,'否，稍后',QDialogButtonBox.NoRole),
                (Cancel,'取消',QDialogButtonBox.RejectRole),(Close,'关闭',QDialogButtonBox.RejectRole))

    def __init__(self,parent=None):
        from .flow_widgets import install_popup_motion
        install_popup_motion()
        super().__init__(modal_parent(parent));self._framed=True;self._fitting=False
        self._text='';self._informative='';self._icon=self.NoIcon;self._clicked=None
        self._codes={};self._roles={};self._default=None;self._standard=self.NoButton
        self.setObjectName('messageDialog');self.setWindowModality(Qt.NonModal)
        self.outer=QVBoxLayout(self);self.outer.setContentsMargins(1,1,1,1);self.outer.setSpacing(0)
        self.titlebar=TitleBar(self,dialog=True);self.outer.addWidget(self.titlebar)
        self.body=QWidget();self.body.setObjectName('messageBody')
        self.content_layout=QHBoxLayout(self.body);self.content_layout.setContentsMargins(24,20,24,8);self.content_layout.setSpacing(14)
        self.icon_label=QLabel();self.icon_label.setFixedSize(32,32);self.icon_label.hide()
        self.content_layout.addWidget(self.icon_label,0,Qt.AlignTop)
        self.content=QTextEdit();self.content.setObjectName('messageContent');self.content.setReadOnly(True)
        self.content.setFrameShape(QTextEdit.NoFrame);self.content.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.content.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded);self.content.setLineWrapMode(QTextEdit.WidgetWidth)
        self.content.setWordWrapMode(QTextOption.WrapAtWordBoundaryOrAnywhere)
        self.content.document().setDocumentMargin(0);self.content.setSizePolicy(QSizePolicy.Expanding,QSizePolicy.Fixed)
        self.content.setAccessibleName('提示内容');self.content_layout.addWidget(self.content,1);self.outer.addWidget(self.body)
        self.footer=QWidget();self.footer.setObjectName('messageFooter');footer_layout=QHBoxLayout(self.footer)
        footer_layout.setContentsMargins(24,16,24,22);self.button_box=QDialogButtonBox();footer_layout.addWidget(self.button_box)
        self.button_box.clicked.connect(self._choose);self.outer.addWidget(self.footer)
        self.setMinimumWidth(360)

    def setText(self,text):self._text=str(text);self._update_text()
    def text(self):return self._text
    def setInformativeText(self,text):self._informative=str(text);self._update_text()
    def _update_text(self):
        self.content.setPlainText(self._text+ ('\n\n'+self._informative if self._informative else ''))
        self.content.moveCursor(QTextCursor.Start)
        if self.isVisible():self._fit()
    def setIcon(self,icon):
        self._icon=icon;self.icon_label.setVisible(icon!=self.NoIcon)
        if icon!=self.NoIcon:self.icon_label.setPixmap(status_pixmap(icon))
        if self.isVisible():self._fit()
    def icon(self):return self._icon
    def addButton(self,text,role):
        button=AnimatedButton(text);button.setMinimumWidth(80);button.setAutoDefault(False)
        role=QDialogButtonBox.ButtonRole(role.value if hasattr(role,'value') else role)
        if role in (QDialogButtonBox.AcceptRole,QDialogButtonBox.YesRole):button.setObjectName('primary')
        self.button_box.addButton(button,role);self._roles[button]=role
        return button
    def setStandardButtons(self,buttons):
        for button in self.button_box.buttons():self.button_box.removeButton(button);button.deleteLater()
        self._roles.clear();self._codes.clear();self._standard=buttons
        for code,text,role in self._standards:
            if buttons & code:self._codes[self.addButton(text,role)]=code
    def standardButtons(self):return self._standard
    def buttons(self):return self.button_box.buttons()
    def button(self,code):return next((b for b,c in self._codes.items() if c==code),None)
    def clickedButton(self):return self._clicked
    def setDefaultButton(self,button):
        if button in self._codes.values():button=self.button(button)
        self._default=button
        for b in self.buttons():b.setDefault(b is button)
        if button:button.setFocus()
    def _choose(self,button):
        self._clicked=button;self.done(int(self._codes.get(button,self.NoButton)))
    def reject(self):
        for code in (self.Cancel,self.No,self.Close,self.Ok):
            button=self.button(code)
            if button:self._choose(button);return
        button=next((b for b,r in self._roles.items() if r==QDialogButtonBox.RejectRole),None)
        if button:self._choose(button)
        else:self.done(int(self.NoButton))
    def _fit(self):
        if self._fitting:return
        self._fitting=True
        try:
            available=dialog_available_geometry(self);limit_w=max(240,available.width()-32);limit_h=max(160,available.height()-32)
            self.ensurePolished();self.content.ensurePolished();self.button_box.ensurePolished()
            self.content.document().setDefaultFont(self.content.font())
            # Width is chosen in Qt logical pixels. Never multiply by screen DPR.
            button_width=self.button_box.sizeHint().width()+50
            preferred=max(460,min(620,self.content.fontMetrics().horizontalAdvance(self._text)+100),button_width)
            width=min(preferred,limit_w);self.setMinimumWidth(min(360,width));self.setMaximumWidth(limit_w)
            icon_width=46 if self._icon!=self.NoIcon else 0
            gutter=self.style().pixelMetric(QStyle.PM_ScrollBarExtent)
            text_width=max(80,width-50-icon_width-gutter-4)
            doc=self.content.document().clone();doc.setTextWidth(text_width)
            natural=math.ceil(doc.size().height())+6;doc.deleteLater()
            footer_height=self.button_box.sizeHint().height()+38
            max_text=max(40,min(440,limit_h-46-28-footer_height-2))
            self.content.setFixedHeight(min(max(32,natural),max_text))
            self.outer.activate();height=self.outer.sizeHint().height()
            self.resize(width,min(height,limit_h));self.outer.activate()
            position_dialog(self)
        finally:self._fitting=False
    def showEvent(self,event):
        if not self.buttons():self.setStandardButtons(self.Ok)
        self._clicked=None;self._fit();super().showEvent(event)
        if self._default:self._default.setFocus()
        else:
            safe=self.button(self.Cancel) or self.button(self.No) or self.buttons()[0]
            self.setDefaultButton(safe)

    @classmethod
    def _run(cls,parent,title,text,icon,buttons,default=None):
        dialog=cls(parent);dialog.setWindowTitle(title);dialog.setText(text);dialog.setIcon(icon);dialog.setStandardButtons(buttons)
        if default is not None:dialog.setDefaultButton(default)
        try:return dialog.exec()
        finally:dialog.deleteLater()
    @classmethod
    def warning(cls,parent,title,text,buttons=Ok,default=None):return cls._run(parent,title,text,cls.Warning,buttons,default)
    @classmethod
    def information(cls,parent,title,text,buttons=Ok,default=None):return cls._run(parent,title,text,cls.Information,buttons,default)
    @classmethod
    def critical(cls,parent,title,text,buttons=Ok,default=None):return cls._run(parent,title,text,cls.Critical,buttons,default)
    @classmethod
    def question(cls,parent,title,text,buttons=Yes|No,default=None):return cls._run(parent,title,text,cls.Question,buttons,default)


class InputDialog:
    @staticmethod
    def getText(parent,title,label,echo=QLineEdit.Normal,text=''):
        dialog=MessageBox(parent);dialog.setWindowTitle(title);dialog.setText(label)
        row=QWidget();layout=QVBoxLayout(row);layout.setContentsMargins(24,8,24,0)
        edit=QLineEdit(str(text));edit.setEchoMode(echo);edit.setAccessibleName(label)
        edit.setMinimumWidth(0);layout.addWidget(edit);dialog.outer.insertWidget(2,row)
        dialog.setStandardButtons(MessageBox.Ok|MessageBox.Cancel)
        dialog.setDefaultButton(MessageBox.Ok)
        from PySide6.QtCore import QTimer
        QTimer.singleShot(0,edit.setFocus)
        try:
            result=dialog.exec();return edit.text(),result==MessageBox.Ok
        finally:dialog.deleteLater()
