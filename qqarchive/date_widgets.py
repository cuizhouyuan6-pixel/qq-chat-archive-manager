"""Native adaptation of the reference project's calendar and second-precision editor."""
import calendar
import datetime as dt
from PySide6.QtCore import Qt,QDateTime,QDate,QTime,Signal
from PySide6.QtWidgets import QWidget,QVBoxLayout,QHBoxLayout,QGridLayout,QLabel,QLineEdit,QSpinBox,QComboBox,QPushButton
from .chrome import AnimatedButton,FramedDialog
from .icons import line_icon

FORMAT='yyyy-MM-dd HH:mm:ss'

def stamp(value):
    if not value or value<=0:return '时间未知'
    result=QDateTime.fromSecsSinceEpoch(int(value))
    return result.toString(FORMAT) if result.isValid() else '时间未知'

class DateTimeField(QWidget):
    dateTimeChanged=Signal(QDateTime)
    def __init__(self,value=None):
        super().__init__();self.arc=None;self.scope=lambda:None;self._value=value or QDateTime.currentDateTime()
        layout=QVBoxLayout(self);layout.setContentsMargins(0,0,0,0);layout.setSpacing(4)
        row=QHBoxLayout();row.setSpacing(0);self.edit=QLineEdit();self.edit.setPlaceholderText('YYYY-MM-DD HH:mm:ss');self.edit.setMinimumWidth(200)
        self.toggle=AnimatedButton('');self.toggle.setIcon(line_icon('calendar'));self.toggle.setToolTip('选择日期和时间');self.toggle.setFixedWidth(40)
        row.addWidget(self.edit,1);row.addWidget(self.toggle);layout.addLayout(row)
        self.note=QLabel('');self.note.setObjectName('muted');self.note.setWordWrap(True);layout.addWidget(self.note)
        self.setDateTime(self._value);self.edit.editingFinished.connect(self.normalize);self.toggle.clicked.connect(self.open_calendar)
    def bind_archive(self,arc,scope):self.arc=arc;self.scope=scope
    def bounds(self):return self.arc.time_bounds(self.scope()) if self.arc else (None,None)
    def day_available(self,date):
        if not self.arc:return True
        start=QDateTime(date,QTime(0,0)).toSecsSinceEpoch();end=QDateTime(date.addDays(1),QTime(0,0)).toSecsSinceEpoch()
        return self.arc.day_has_messages(start,end,self.scope())
    def setDateTime(self,value):
        self._value=value;self.edit.setText(value.toString(FORMAT));self.dateTimeChanged.emit(value)
    def dateTime(self):
        self.normalize();return self._value
    def setDisplayFormat(self,_):pass
    def setCalendarPopup(self,_):pass
    def normalize(self):
        value=QDateTime.fromString(self.edit.text().strip(),FORMAT)
        if not value.isValid():self.note.setText('请输入完整日期与时、分、秒');return False
        low,high=self.bounds();timestamp=value.toSecsSinceEpoch();self.note.setText('')
        if self.arc and low is None:self.note.setText('当前范围内没有带有效时间的聊天记录');return False
        if low is not None and timestamp<low:value=QDateTime.fromSecsSinceEpoch(low);self.note.setText('已调整至最早一条记录的时间')
        if high is not None and timestamp>high:value=QDateTime.fromSecsSinceEpoch(high);self.note.setText('已调整至最后一条记录的时间')
        if not self.day_available(value.date()):self.note.setText('这一天没有已归档消息，请选择标记日期');return False
        self.setDateTime(value);return True
    def open_calendar(self):
        self.normalize();dialog=CalendarDialog(self);dialog.exec()

class CalendarDialog(FramedDialog):
    def __init__(self,field):
        super().__init__(field.window());self.field=field;self.chosen=field._value.date();self.setWindowTitle('选择日期与时间');self.resize(420,460)
        l=QVBoxLayout(self);l.setContentsMargins(20,18,20,18);l.setSpacing(14)
        header=QHBoxLayout();self.year=QSpinBox();self.year.setRange(1970,9999);self.year.setValue(self.chosen.year());self.year.setSuffix(' 年')
        self.month=QComboBox();self.month.addItems([f'{i} 月' for i in range(1,13)]);self.month.setCurrentIndex(self.chosen.month()-1)
        previous=AnimatedButton('‹');following=AnimatedButton('›');previous.setAccessibleName('上个月');following.setAccessibleName('下个月')
        for w in (previous,self.year,self.month,following):header.addWidget(w)
        l.addLayout(header);self.days=QGridLayout();self.days.setSpacing(4);l.addLayout(self.days)
        for col,day in enumerate('一二三四五六日'):
            label=QLabel(day);label.setAlignment(Qt.AlignCenter);label.setObjectName('muted');self.days.addWidget(label,0,col)
        time_row=QHBoxLayout();time_row.addWidget(QLabel('时间'));time_row.addStretch();self.parts=[]
        value=field._value.time()
        for name,maximum,n in [('时',23,value.hour()),('分',59,value.minute()),('秒',59,value.second())]:
            part=QSpinBox();part.setRange(0,maximum);part.setValue(n);part.setSuffix(' '+name);part.setMinimumWidth(82);self.parts.append(part);time_row.addWidget(part)
        l.addLayout(time_row);self.status=QLabel('深色日期表示有已归档消息');self.status.setWordWrap(True);self.status.setObjectName('muted');l.addWidget(self.status)
        footer=QHBoxLayout();cancel=AnimatedButton('取消');done=AnimatedButton('完成');done.setObjectName('primary');cancel.clicked.connect(self.reject);done.clicked.connect(self.finish)
        footer.addStretch();footer.addWidget(cancel);footer.addWidget(done);l.addLayout(footer)
        self.year.valueChanged.connect(self.render_calendar);self.month.currentIndexChanged.connect(self.render_calendar)
        previous.clicked.connect(lambda:self.shift_month(-1));following.clicked.connect(lambda:self.shift_month(1));self.render_calendar()
    def shift_month(self,direction):
        value=QDate(self.year.value(),self.month.currentIndex()+1,1).addMonths(direction)
        self.year.blockSignals(True);self.month.blockSignals(True);self.year.setValue(value.year());self.month.setCurrentIndex(value.month()-1);self.year.blockSignals(False);self.month.blockSignals(False);self.render_calendar()
    def render_calendar(self,*_):
        while self.days.count()>7:
            item=self.days.takeAt(7)
            if item.widget():item.widget().deleteLater()
        year,month=self.year.value(),self.month.currentIndex()+1;offset,total=calendar.monthrange(year,month)
        for day in range(1,total+1):
            date=QDate(year,month,day);available=self.field.day_available(date)
            b=AnimatedButton(str(day)) if available else QPushButton(str(day))
            b.setObjectName('calendarDay' if available else 'calendarUnavailable');b.setProperty('chosen',date==self.chosen);b.setFixedHeight(36)
            b.setToolTip(date.toString('yyyy-MM-dd')+(' · 有记录' if available else ' · 当天无已归档消息'))
            b.clicked.connect(lambda checked=False,d=date,ok=available:self.choose(d,ok))
            i=offset+day-1;self.days.addWidget(b,1+i//7,i%7)
    def choose(self,date,available):
        if not available:self.status.setText('这一天没有已归档消息，无法选择');return
        self.chosen=date;self.status.setText(date.toString('yyyy-MM-dd'));self.render_calendar()
    def finish(self):
        self.field.setDateTime(QDateTime(self.chosen,QTime(*(part.value() for part in self.parts))))
        if self.field.normalize():self.accept()
        else:self.status.setText(self.field.note.text())
