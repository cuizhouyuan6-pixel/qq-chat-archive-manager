"""Native graphite transfer dashboard, with non-modal opt-in detection."""
from __future__ import annotations
from collections import deque
import os
from pathlib import Path
import threading
import time
from PySide6.QtCore import Qt,QThread,Signal,QTimer,QRectF,QPointF,QVariantAnimation,QEasingCurve,QUrl
from PySide6.QtGui import QColor,QPainter,QPainterPath,QPen,QLinearGradient,QFont,QDesktopServices
from PySide6.QtWidgets import QWidget,QVBoxLayout,QHBoxLayout,QLabel,QFrame,QComboBox,QCheckBox,QScrollArea,QSizePolicy,QApplication
from .chrome import AnimatedButton,fade_in
from .design import TOKENS,motion_ms
from .dialogs import MessageBox
from .config import save_config
from .transfer_core import OfferGate,RateHistory,AsyncSessionLog,Snapshot
from .transfer_process import ProcessSampler


def rate(value):
    if value is None:return '—'
    # User-requested decimal units: uppercase B is bytes, lowercase b is bits.
    if value<125000:return f'{value/1000:.2f} KB/s'
    return f'{value*8/1_000_000:.2f} Mb/s'


def size(value):
    if value is None:return '—'
    for unit in ('B','KB','MB','GB','TB'):
        if value<1000 or unit=='TB':return f'{value:.1f} {unit}'
        value/=1000
def text(value,kind='muted'):
    label=QLabel(value);label.setWordWrap(True);label.setObjectName(kind);return label


class TransferWorker(QThread):
    sampleReady=Signal(object)
    def __init__(self,roots=(),parent=None,sampler=None):
        super().__init__(parent);self.stop_event=threading.Event();self.recording=threading.Event();self.enabled=threading.Event();self.enabled.set();self.wake=threading.Event()
        self.roots=roots;self.sampler=sampler
    def run(self):
        sampler=None
        try:
            sampler=self.sampler or ProcessSampler(self.roots,self.stop_event)
            while not self.stop_event.is_set():
                started=time.monotonic()
                if self.enabled.is_set() or self.recording.is_set():
                    try:sample=sampler.sample(self.recording.is_set())
                    except Exception:sample=Snapshot(time.time(),time.monotonic(),errors=['系统采样暂不可用，将自动重试；界面可继续操作'],detection_ok=False)
                    if not self.stop_event.is_set():self.sampleReady.emit(sample)
                interval=2 if self.recording.is_set() else 6
                self.wake.wait(max(.1,interval-(time.monotonic()-started)));self.wake.clear()
        finally:
            if sampler is not None and hasattr(sampler,'close'):sampler.close()
            self.sampler=None
    def stop(self):self.stop_event.set();self.wake.set()


class TransferChart(QWidget):
    """Real timestamps; missing samples stay gaps, never interpolated as zero."""
    def __init__(self,parent=None):
        super().__init__(parent);self.points=[];self.window_seconds=300;self.running=False;self.started=False;self.hover_x=None;self.progress=1.
        self.setMinimumHeight(235);self.setMouseTracking(True);self.setSizePolicy(QSizePolicy.Expanding,QSizePolicy.Expanding)
        self.motion=QVariantAnimation(self);self.motion.setStartValue(0.);self.motion.setEndValue(1.);self.motion.setDuration(motion_ms(170));self.motion.setEasingCurve(QEasingCurve.OutCubic)
        self.motion.valueChanged.connect(self.animate)
        self.setAccessibleName('迁移速率波形图，仅使用 QQ 指标；不可独立获取网络流量时仅显示写入 I/O')
    def animate(self,value):self.progress=float(value);self.update()
    def set_points(self,points,running):
        self.points=list(points);self.running=running;self.started=self.started or running
        self.motion.stop();self.progress=1.
        if self.isVisible() and motion_ms(170):self.motion.start()
        self.update()
    def hideEvent(self,event):
        self.motion.stop();self.progress=1.;super().hideEvent(event)
    def set_window(self,seconds):self.window_seconds=seconds;self.update()
    def mouseMoveEvent(self,event):self.hover_x=event.position().x();self.update()
    def leaveEvent(self,event):self.hover_x=None;self.update()
    def paintEvent(self,event):
        p=QPainter(self);p.setRenderHint(QPainter.Antialiasing);p.fillRect(self.rect(),QColor('#FFFFFF'))
        plot=QRectF(62,24,max(1,self.width()-84),max(1,self.height()-62))
        end=self.points[-1]['mono'] if self.points else time.monotonic()
        if len(self.points)>1 and self.running:end-=(1-self.progress)*min(2,max(0,self.points[-1]['mono']-self.points[-2]['mono']))
        begin=end-self.window_seconds
        points=[v for v in self.points if v['mono']>=begin and v['basis']==self.points[-1]['basis']]
        series=[('value','#24262C')]
        network=bool(points and points[-1]['scope']=='qq')
        if network:series.append(('write','#949BA7'))
        maximum=max([v[k] or 0 for v in points for k,_ in series]+[1024.])*1.15
        font=QFont('Consolas');font.setPixelSize(11);p.setFont(font)
        for i in range(5):
            y=plot.top()+plot.height()*i/4;p.setPen(QColor(TOKENS['line']));p.drawLine(QPointF(plot.left(),y),QPointF(plot.right(),y))
            p.setPen(QColor(TOKENS['secondary']));p.drawText(QRectF(0,y-9,55,18),Qt.AlignRight|Qt.AlignVCenter,rate(maximum*(1-i/4)).replace('/s',''))
        for i in range(6):
            x=plot.left()+plot.width()*i/5;p.setPen(QColor(TOKENS['muted']))
            label='现在' if i==5 and self.running else ('末次' if i==5 else f'-{round(self.window_seconds*(1-i/5))}s')
            p.drawText(QRectF(x-28,plot.bottom()+10,56,20),Qt.AlignCenter,label)
        if not points:
            p.setPen(QColor(TOKENS['secondary']));p.drawText(plot,Qt.AlignCenter,'等待采样…' if self.running else '开始监测后，在这里查看实时速率');p.end();return
        def xy(item,key):
            return QPointF(plot.left()+(item['mono']-begin)/self.window_seconds*plot.width(),plot.bottom()-min(maximum,item[key])/maximum*plot.height())
        p.save();p.setClipRect(plot.adjusted(-1,-1,1,1))
        for key,color in series:
            groups=[];segment=[];previous=None
            for item in points:
                gap=previous and (item['mono']-previous['mono']>6 or key=='value' and item['basis']!=previous['basis'])
                if item[key] is None or gap:
                    if segment:groups.append(segment);segment=[]
                if item[key] is not None:segment.append(xy(item,key))
                previous=item
            if segment:groups.append(segment)
            for segment in groups:
                path=QPainterPath(segment[0])
                for point in segment[1:]:path.lineTo(point)
                if key=='value':
                    area=QPainterPath(path);area.lineTo(segment[-1].x(),plot.bottom());area.lineTo(segment[0].x(),plot.bottom());area.closeSubpath()
                    gradient=QLinearGradient(0,plot.top(),0,plot.bottom());gradient.setColorAt(0,QColor(36,38,44,55));gradient.setColorAt(1,QColor(36,38,44,3));p.fillPath(area,gradient)
                pen=QPen(QColor(color),2 if key=='value' else 1.5);pen.setStyle(Qt.SolidLine if key=='value' else Qt.DashLine);p.setPen(pen);p.drawPath(path)
                if len(segment)==1:p.drawEllipse(segment[0],2,2)
        p.restore()
        if all(v['value'] is None for v in points):
            p.setPen(QColor(TOKENS['secondary']));p.drawText(plot,Qt.AlignCenter,'等待 QQ 指标基线 · 未知速率不显示为零')
        if self.hover_x is not None and plot.contains(QPointF(self.hover_x,plot.center().y())):
            target=begin+(self.hover_x-plot.left())/plot.width()*self.window_seconds
            nearest=min(points,key=lambda v:abs(v['mono']-target));x=plot.left()+(nearest['mono']-begin)/self.window_seconds*plot.width()
            p.setPen(QPen(QColor('#B5BAC4'),1,Qt.DashLine));p.drawLine(QPointF(x,plot.top()),QPointF(x,plot.bottom()))
            box=QRectF(max(plot.left(),min(x+10,plot.right()-230)),plot.top()+12,230,70)
            p.setPen(QColor(TOKENS['line']));p.setBrush(QColor('#FFFFFF'));p.drawRoundedRect(box,9,9)
            p.setPen(QColor(TOKENS['ink']));p.drawText(box.adjusted(12,7,-8,-6),Qt.AlignLeft|Qt.AlignVCenter,
                time.strftime('%H:%M:%S',time.localtime(nearest['at']))+('\nQQ 网络  '+rate(nearest['rx']) if network else '')+'\nQQ 写入 I/O  '+rate(nearest['write']))
        p.end()


class TransferPage(QWidget):
    startRequested=Signal();stopRequested=Signal();detectChanged=Signal(bool)
    def __init__(self,parent=None):
        super().__init__(parent);self.setObjectName('transferPage');self.last_point=None;self._latest=None
        outer=QVBoxLayout(self);outer.setContentsMargins(0,0,0,0)
        scroll=QScrollArea();scroll.setFrameShape(QFrame.NoFrame);scroll.setWidgetResizable(True);outer.addWidget(scroll)
        body=QWidget();layout=QVBoxLayout(body);layout.setContentsMargins(28,24,28,24);layout.setSpacing(18);scroll.setWidget(body)
        top=QHBoxLayout();top.addWidget(text('迁移监测','title'));top.addStretch();self.state=text('尚未开始','monitorBadge');top.addWidget(self.state);layout.addLayout(top)
        layout.addWidget(text('观察 QQ 的写入活动；能独立获取 QQ 网络计数时，才显示网络速率。'))
        hero=QFrame();hero.setObjectName('monitorHero');hl=QHBoxLayout(hero);hl.setContentsMargins(24,20,24,20);hl.setSpacing(24)
        left=QVBoxLayout();self.scope=text('QQ 写入 I/O','monitorHeroCaption');left.addWidget(self.scope)
        self.speed=text('—','monitorSpeed');left.addWidget(self.speed);self.source=text('只使用 QQ 指标，不采集整机网卡流量','monitorHeroCaption');left.addWidget(self.source);hl.addLayout(left,2)
        right=QVBoxLayout();self.toggle=AnimatedButton('开始监测');self.toggle.setObjectName('monitorLightButton');self.toggle.clicked.connect(self.clicked);right.addWidget(self.toggle)
        self.elapsed=text('00:00:00','monitorClock');right.addWidget(self.elapsed);hl.addLayout(right);layout.addWidget(hero)
        cards=QHBoxLayout();cards.setSpacing(12);self.metrics={};self.metric_titles={}
        for key,title in [('peak','峰值写入 I/O'),('mean','平均写入 I/O'),('total','累计写入 I/O')]:
            frame=QFrame();frame.setObjectName('card');cl=QVBoxLayout(frame);cl.setContentsMargins(18,16,18,16)
            caption=text(title,'fieldLabel');self.metric_titles[key]=caption;cl.addWidget(caption);value=text('—','monitorMetric');cl.addWidget(value);self.metrics[key]=value;cards.addWidget(frame)
        layout.addLayout(cards)
        graph=QFrame();graph.setObjectName('card');gl=QVBoxLayout(graph);gl.setContentsMargins(18,14,18,12)
        controls=QHBoxLayout();controls.addWidget(text('传输活动','sectionTitle'));controls.addStretch()
        self.window=QComboBox();self.window.setAccessibleName('图表时间范围')
        for title,value in [('最近 2 分钟',120),('最近 5 分钟',300),('最近 15 分钟',900)]:self.window.addItem(title,value)
        self.window.setCurrentIndex(1);controls.addWidget(self.window);gl.addLayout(controls)
        self.chart=TransferChart();self.window.currentIndexChanged.connect(lambda:self.chart.set_window(self.window.currentData()));gl.addWidget(self.chart,1)
        self.legend=text('实线 / 灰色面积：QQ 写入 I/O（包含文件、网络和设备操作）');gl.addWidget(self.legend)
        layout.addWidget(graph)
        settings=QFrame();settings.setObjectName('card');sl=QVBoxLayout(settings);sl.setContentsMargins(20,18,20,18);sl.setSpacing(12)
        self.auto=QCheckBox('发现疑似 QQ 手机迁移时，询问是否开始监测');self.auto.toggled.connect(self.detectChanged);sl.addWidget(self.auto)
        self.health=text('尚未检测到迁移活动','monitorHealth');sl.addWidget(self.health)
        self.detail=text('');sl.addWidget(self.detail)
        bottom=QHBoxLayout();self.folder=AnimatedButton('打开监测日志');self.folder.setEnabled(False);bottom.addWidget(self.folder);bottom.addStretch();self.log_note=text('每 2 秒采样 · 未知速率显示为 —');bottom.addWidget(self.log_note);sl.addLayout(bottom)
        layout.addWidget(settings);layout.addStretch()
        self.setStyleSheet('''
QFrame#monitorHero{background:#24262C;border:0;border-radius:12px}
QLabel#monitorSpeed{font-family:Consolas;font-size:40px;color:#FFFFFF;font-weight:600;background:transparent}
QLabel#monitorHeroCaption{color:#CBD0D9;font-size:12px;background:transparent}
QLabel#monitorClock{font-family:Consolas;color:#FFFFFF;font-size:17px;background:transparent}
QLabel#monitorMetric{font-family:Consolas;font-size:23px;font-weight:600}
QLabel#monitorBadge{padding:6px 12px;background:#E9EAED;color:#4E5560;border-radius:8px;font-size:12px}
QPushButton#monitorLightButton{background:#FFFFFF;color:#202126;border:1px solid #FFFFFF;min-width:112px}
QPushButton#monitorLightButton:hover{background:#E9EAED;color:#202126}
QLabel#monitorHealth{font-size:14px;color:#202126}
''')
        self.running=False
    def clicked(self):self.stopRequested.emit() if self.running else self.startRequested.emit()
    def showEvent(self,event):
        super().showEvent(event)
        if self._latest:self.render(*self._latest)
    def set_running(self,value):
        if value:self._latest=None
        self.running=value;self.chart.running=value;self.toggle.setText('停止监测' if value else ('重新开始' if self.chart.points else '开始监测'))
        self.state.setText('监测中' if value else ('已停止' if self.chart.points else '尚未开始'))
        self.chart.update()
    def render(self,point,points,snapshot):
        self.last_point=point;self._latest=(point,list(points),snapshot)
        if not self.isVisible():return
        network=point['scope']=='qq'
        self.speed.setText(rate(point['value']))
        self.scope.setText('QQ 迁移连接 · 接收速率' if network else 'QQ 写入 I/O · 仅写入监测')
        self.source.setText('已认证的 QQ 迁移连接计数' if network else '未取得 QQ 独立网络计数 · 不采集网卡总流量')
        for key,prefix in [('peak','峰值'),('mean','平均'),('total','累计')]:
            self.metric_titles[key].setText(prefix+('接收' if network else '写入 I/O'))
            self.metrics[key].setText(size(point[key]) if key=='total' else rate(point[key] if point['mean'] is not None else None))
        self.legend.setText('实线：QQ 连接接收     虚线：QQ 写入 I/O' if network else '实线 / 灰色面积：QQ 写入 I/O（包含文件、网络和设备操作）')
        self.chart.set_points(points,self.running)
        if point['write'] is None:message='正在取得 QQ 写入基线，或当前 QQ 指标不可用'
        elif point['files_changed']:message='检测到数据库 / WAL 更新，QQ 仍有写入活动'
        elif point['cpu'] is not None and point['cpu']>50:message='QQ 正在处理数据，暂未观察到数据库更新'
        elif point['connections']:message='迁移连接仍在；短时低速不能单独判定任务卡死'
        else:message='当前未检测到迁移连接；监测保留，等待后续活动'
        self.health.setText(message)
        cpu='—' if point['cpu'] is None else f"{point['cpu']:.0f}% 单核"
        self.detail.setText(f"QQ CPU {cpu}   ·   QQ 写入 I/O {rate(point['write'])}\n"+
            '写入 I/O 包含文件、网络和设备操作，不能当作数据库独占磁盘写入速率。\n'+
            ('当前未找到 QQ 消息库路径。 ' if not point['db_available'] else '')+(point['exact_note'] or '仅记录系统指标，不读取聊天内容。')+
            ('\n'+ '；'.join(point['errors']) if point['errors'] else ''))


class TransferController(QWidget):
    """GUI-side orchestration; all system calls live on one cancellable worker."""
    def __init__(self,host,page,autostart=True):
        super().__init__(host);self.hide();self.host=host;self.page=page;self.gate=OfferGate();self.history=RateHistory();self.recording=False;self.started=0.;self.log=None;self.latest=None;self.pending=[];self.prompt=None;self.closed=False
        self.worker=TransferWorker(host.cfg.qq_roots,self);self.worker.sampleReady.connect(self.receive)
        if not host.cfg.transfer_detection:self.worker.enabled.clear()
        self.page.auto.setChecked(host.cfg.transfer_detection);self.page.detectChanged.connect(self.preference)
        self.page.startRequested.connect(self.start);self.page.stopRequested.connect(self.stop)
        self.page.folder.clicked.connect(self.open_log)
        self.timer=QTimer(self);self.timer.timeout.connect(self.tick);self.timer.start(500)
        if autostart:self.worker.start()
    def preference(self,value):
        self.host.cfg.transfer_detection=value;save_config(self.host.cfg)
        if value:self.worker.enabled.set()
        else:
            self.worker.enabled.clear();self.pending=[]
            if self.prompt:self.prompt.reject()
        self.worker.wake.set()
    def start(self):
        if self.closed or self.recording:return
        if self.prompt:self.prompt.reject()
        self.history.reset();self.started=time.monotonic();self.recording=True
        self.gate.acknowledge(self.latest.sessions if self.latest else []);self.pending=[]
        self.page.set_running(True);self.page.chart.set_points([],True)
        self.page.speed.setText('—')
        for label in self.page.metrics.values():label.setText('—')
        self.log=AsyncSessionLog(self.host.cfg.data_path/'logs'/'migration')
        try:self.log.start();self.page.folder.setEnabled(True);self.page.log_note.setText('每 2 秒采样 · 日志已开启（单次最多 8 MiB）')
        except OSError:self.log=None;self.page.log_note.setText('无法保存日志，实时监测继续')
        self.worker.recording.set();self.worker.wake.set()
    def stop(self):
        self.recording=False;self.worker.recording.clear();self.page.set_running(False)
        if self.latest:self.gate.acknowledge(self.latest.sessions)
        if self.log:self.log.close()
    def receive(self,snapshot):
        if self.closed:return
        self.latest=snapshot;offers=self.gate.update(snapshot)
        if not self.recording and self.host.cfg.transfer_detection:self.pending=offers
        if self.recording:
            point=self.history.add(snapshot);self.page.render(point,self.history.points,snapshot)
            if self.log:
                try:
                    self.log.append(point)
                    if self.log.capped:self.page.log_note.setText('日志达到 8 MiB 上限 · 实时图表继续')
                    elif self.log.failed:self.page.log_note.setText('日志不可写 · 实时监测继续')
                    elif self.log.dropped:self.page.log_note.setText('日志写入较慢，部分采样未保存 · 实时监测继续')
                except OSError:self.log.close();self.page.log_note.setText('日志写入失败 · 实时图表继续')
        elif not self.page.chart.points:
            self.page.health.setText('发现疑似 QQ 手机迁移连接' if snapshot.connections else ('QQ 正在运行，尚未发现迁移连接' if snapshot.qq else 'QQ 尚未运行'))
        self.consider_offer()
    def consider_offer(self):
        if self.closed or self.recording or self.prompt or not self.pending:return
        if not self.host.isVisible() or self.host.worker or QApplication.activeModalWidget():return
        keys=list(self.pending);self.gate.acknowledge(keys);self.pending=[]
        box=MessageBox(self.host);box.setWindowTitle('发现 QQ 手机迁移活动');box.setIcon(MessageBox.Information)
        box.setText('发现 QQ 与局域网设备的迁移连接。是否开始监测 QQ 写入活动？')
        box.setInformativeText('这是根据连接特征作出的判断，不能确认 QQ 内部的任务进度。仅在能取得 QQ 独立计数时显示网络速率，不采集网卡总流量。窗口关闭后可在“迁移监测”手动开始。')
        yes=box.addButton('开始监测',MessageBox.AcceptRole);box.addButton('暂不监测',MessageBox.RejectRole);self.prompt=box
        def finished(_):
            chosen=box.clickedButton() is yes;self.prompt=None
            if chosen and not self.closed:
                self.start();self.host.navigate(6)
            box.deleteLater()
        box.finished.connect(finished);box.show()
    def tick(self):
        if self.recording:
            seconds=int(time.monotonic()-self.started);self.page.elapsed.setText(f'{seconds//3600:02d}:{seconds//60%60:02d}:{seconds%60:02d}')
        self.consider_offer()
    def open_log(self):QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.host.cfg.data_path/'logs'/'migration')))
    def shutdown(self):
        if not self.closed:
            self.closed=True;self.timer.stop();self.stop();self.worker.stop()
            if self.prompt:self.prompt.reject()
        return not self.worker.isRunning()
