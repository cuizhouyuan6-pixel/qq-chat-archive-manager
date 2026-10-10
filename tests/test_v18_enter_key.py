import os
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
from PySide6.QtCore import Qt,QTimer
from PySide6.QtWidgets import QApplication,QLineEdit,QSpinBox
from PySide6.QtTest import QTest
from shiboken6 import isValid
from qqarchive.chrome import FramedDialog
from test_v13_interactions import ui,spin

def opened(title):
    return next((w for w in QApplication.topLevelWidgets() if isinstance(w,FramedDialog) and isValid(w) and w.windowTitle()==title and w.isVisible()),None)

def test_batch_picker_enter_in_search_keeps_window_and_selection(ui):
    app,w,t=ui;w.cfg.conversation_groups={'回车测试':[]};w.render_conversations()
    picker=w.batch_group_picker('回车测试');QTest.qWait(250);picker.select_all.click();before=picker.selected.count();closed=[];picker.finished.connect(closed.append)
    picker.search.setFocus();picker.search.setText('不存在的聊天');QTest.keyClick(picker.search,Qt.Key_Return);QTest.qWait(250)
    assert before==2 and not closed and picker.isVisible() and picker.selected.count()==before
    assert w.cfg.conversation_groups['回车测试']==[]
    picker.reject();QTest.qWait(230)

def test_locate_time_enter_runs_locate(ui):
    from qqarchive.chat_tools import locate_time
    from qqarchive.date_widgets import stamp
    app,w,t=ui;seen={}
    def act():
        d=opened('按时间定位');edit=d.findChildren(QLineEdit)[0];d.finished.connect(lambda r:seen.update(result=r))
        edit.setFocus();edit.setText(stamp(t+120));QTest.keyClick(edit,Qt.Key_Return)
    QTimer.singleShot(300,act);locate_time(w);QTest.qWait(250)
    assert seen.get('result')==1 and w.chat.currentIndex().data(Qt.UserRole)['time']==t+120

def test_calendar_enter_in_time_box_stays_open_and_focused_day_still_works(ui):
    from qqarchive.date_widgets import DateTimeField,CalendarDialog
    from PySide6.QtCore import QDateTime
    app,w,t=ui;field=DateTimeField(QDateTime.fromSecsSinceEpoch(t));field.bind_archive(w.arc,lambda:w.current['id'])
    dialog=CalendarDialog(field);dialog.show();QTest.qWait(250);closed=[];dialog.finished.connect(closed.append)
    seconds=dialog.findChildren(QSpinBox)[-1];seconds.setFocus();QTest.keyClick(seconds,Qt.Key_Return);QTest.qWait(250)
    assert not closed and dialog.isVisible()
    from qqarchive.chrome import AnimatedButton
    day=next(b for b in dialog.findChildren(AnimatedButton) if b.objectName()=='calendarDay' and b.text()=='7')
    day.setFocus();QTest.keyClick(day,Qt.Key_Return);QTest.qWait(50)
    assert dialog.chosen.day()==7 and dialog.isVisible() and not closed
    dialog.reject();QTest.qWait(230)

def test_message_box_enter_still_presses_explicit_safe_default(ui):
    from qqarchive.dialogs import MessageBox
    app,w,t=ui;box=MessageBox(w);box.setWindowTitle('确认');box.setText('继续？');box.setStandardButtons(MessageBox.Yes|MessageBox.No);box.setDefaultButton(MessageBox.No)
    box.show();QTest.qWait(250);result=[];box.finished.connect(result.append)
    box.button(MessageBox.Yes).setFocus();QTest.keyClick(box.button(MessageBox.Yes),Qt.Key_Return);QTest.qWait(250)
    assert result==[MessageBox.No]
