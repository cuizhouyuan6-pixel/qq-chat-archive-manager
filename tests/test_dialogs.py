import os
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
import pytest
from PySide6.QtCore import Qt,QPoint,QRect,QTimer
from PySide6.QtWidgets import QApplication,QWidget,QMessageBox as NativeMessageBox,QInputDialog as NativeInput,QLineEdit,QDialogButtonBox
from PySide6.QtTest import QTest
from qqarchive.dialogs import MessageBox,InputDialog
from qqarchive.chrome import FramedDialog,dialog_available_geometry
from qqarchive.theme import STYLE
from qqarchive.flow_widgets import install_popup_motion

@pytest.fixture
def ui():
    app=QApplication.instance() or QApplication([]);app.setStyle('Fusion');app.setStyleSheet(STYLE)
    install_popup_motion();parent=QWidget();parent.setGeometry(55,50,680,480);parent.show()
    yield app,parent
    for d in parent.findChildren(FramedDialog):d.done(0)
    parent.close();QTest.qWait(240);parent.deleteLater();app.processEvents()

def show(parent,text,buttons=MessageBox.Yes|MessageBox.No):
    d=MessageBox(parent);d.setWindowTitle('密钥已获取 · 下一步');d.setText(text);d.setIcon(MessageBox.Question)
    d.setStandardButtons(buttons);d.show();QTest.qWait(230);return d

@pytest.mark.parametrize('text',[
    '没有发现正在运行的电脑 QQ，请登录后重试。',
    '是否关闭电脑 QQ，并自动导入当前范围的聊天记录？\n\n范围：0 个已选会话\n时间：全部时间\n附件：引用原路径，不复制文件\n\n选择“是”会结束电脑 QQ 的进程，请先保存未发送内容。\n选择“否”会使用 Windows 账户加密保存密钥，稍后可在下方选择会话和时间。',
    '读取失败：D:\\聊天记录\\'+('very_long_database_folder_'*90)+'\\nt_msg.db',
    '\n'.join(f'第 {i} 行：完整错误内容及处理建议。' for i in range(90)),
    '',
])
def test_full_text_and_buttons_remain_accessible(ui,text):
    app,parent=ui;d=show(parent,text)
    assert d.content.toPlainText()==text
    assert dialog_available_geometry(d).contains(d.frameGeometry())
    assert 360<=d.width()<=640
    assert d.content.horizontalScrollBar().maximum()==0
    for b in d.buttons():
        assert b.isVisible() and d.rect().contains(QRect(b.mapTo(d,QPoint()),b.size()))
    assert all(not d.mask().contains(p) for p in (QPoint(0,0),QPoint(d.width()-1,0),QPoint(0,d.height()-1),QPoint(d.width()-1,d.height()-1)))
    d.content.verticalScrollBar().setValue(d.content.verticalScrollBar().maximum())
    last=d.content.document().lastBlock()
    cursor=d.content.textCursor();cursor.setPosition(last.position());d.content.setTextCursor(cursor);d.content.ensureCursorVisible()
    assert d.content.viewport().rect().intersects(d.content.cursorRect())
    if len(text)<400:assert d.content.verticalScrollBar().maximum()==0
    else:assert d.content.verticalScrollBar().maximum()>0

def test_position_centers_on_parent_and_nested_dialog(ui):
    app,parent=ui;d=show(parent,'第一层确认')
    assert (d.geometry().center()-parent.frameGeometry().center()).manhattanLength()<=2
    child=show(parent,'第二层确认')
    assert child.parentWidget() is parent
    assert (child.geometry().center()-parent.frameGeometry().center()).manhattanLength()<=2
    child.reject();QTest.qWait(220);assert d.isVisible() and not getattr(d,'_shade',None)
    parent.move(650,420);d.hide();d.show();QTest.qWait(230)
    assert dialog_available_geometry(d).contains(d.frameGeometry())

def test_button_results_and_escape_are_safe(ui):
    app,parent=ui
    for code in (MessageBox.Yes,MessageBox.No,MessageBox.Cancel):
        d=show(parent,'确认',MessageBox.Yes|MessageBox.No|MessageBox.Cancel)
        QTest.mouseClick(d.button(code),Qt.LeftButton);assert d.result()==code and d.clickedButton() is d.button(code)
    d=show(parent,'关闭 QQ？');QTest.keyClick(d,Qt.Key_Escape);assert d.result()==MessageBox.No
    d=MessageBox(parent);close=d.addButton('关闭程序',MessageBox.AcceptRole);hide=d.addButton('隐藏至任务栏',MessageBox.ActionRole);cancel=d.addButton('取消',MessageBox.RejectRole)
    d.show();app.processEvents();d.close();assert d.clickedButton() is cancel
    QTest.qWait(220);d.show();QTest.mouseClick(hide,Qt.LeftButton);assert d.clickedButton() is hide
    QTest.qWait(220);d.show();QTest.mouseClick(close,Qt.LeftButton);assert d.clickedButton() is close

def test_native_qt_owned_layouts_are_not_reparented(ui):
    app,parent=ui
    for d in (NativeMessageBox(parent),NativeInput(parent)):
        if isinstance(d,NativeMessageBox):d.setText('原生布局');d.setStandardButtons(NativeMessageBox.Ok)
        else:d.setLabelText('原生输入')
        d.ensurePolished();layout=d.layout();d.show();app.processEvents()
        if layout is not None:assert d.layout() is layout
        assert d.layout().parent() is d and not hasattr(d,'_framed')
        d.reject()

def test_rapid_animation_interruptions_and_reduced_motion(ui,monkeypatch):
    app,parent=ui;d=show(parent,'重复显示')
    for _ in range(12):d.hide();d.show();app.processEvents()
    d.hide();QTest.qWait(250)
    assert not getattr(d,'_shade',None) and d.windowOpacity()==1
    assert not [w for w in parent.findChildren(QWidget,'dialogShade') if w.isVisible()]
    assert not [w for w in app.topLevelWidgets() if w.objectName()=='popupGhost']
    monkeypatch.setenv('QQARCHIVE_REDUCED_MOTION','1');d.show();app.processEvents()
    assert d.isVisible() and d.windowOpacity()==1
    assert (d.geometry().center()-parent.frameGeometry().center()).manhattanLength()<=2

def test_manual_key_input_accept_cancel_and_mask(ui):
    app,parent=ui;seen=[]
    def act(accept):
        d=next(d for d in parent.findChildren(MessageBox) if d.isVisible() and d.findChild(QLineEdit));edit=d.findChild(QLineEdit)
        assert app.activeModalWidget() is None
        seen.append(edit.echoMode());edit.setText('synthetic-key-only')
        d.button(MessageBox.Ok if accept else MessageBox.Cancel).click()
    QTimer.singleShot(10,lambda:act(True))
    assert InputDialog.getText(parent,'手动输入密钥','密钥',QLineEdit.Password)==('synthetic-key-only',True)
    QTimer.singleShot(10,lambda:act(False))
    assert InputDialog.getText(parent,'手动输入密钥','密钥',QLineEdit.Password)[1] is False
    assert seen==[QLineEdit.Password,QLineEdit.Password]

def test_long_title_and_monitor_at_negative_coordinates(ui,monkeypatch):
    app,parent=ui
    import qqarchive.chrome as chrome
    import qqarchive.dialogs as dialogs
    available=QRect(-1280,0,1280,720)
    monkeypatch.setattr(chrome,'dialog_available_geometry',lambda d:available)
    monkeypatch.setattr(dialogs,'dialog_available_geometry',lambda d:available)
    parent.move(-1100,80);d=show(parent,'确认')
    d.setWindowTitle('很长的错误标题'*120);app.processEvents();d._fit()
    assert available.contains(d.geometry()) and d.width()<=620
    assert d.titlebar.title.text()=='很长的错误标题'*120
