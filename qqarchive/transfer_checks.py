"""Packaged, isolated visual/interaction acceptance for migration monitoring."""
import json,math,os,time
from pathlib import Path
from PySide6.QtCore import Qt,QPoint
from PySide6.QtWidgets import QApplication,QScrollArea
from .dialog_checks import wait
from .transfer_core import Snapshot


def run(destination):
    from .desktop import MainWindow
    from .config import Config
    root=Path(destination).resolve();root.mkdir(parents=True,exist_ok=True)
    os.environ['QQARCHIVE_CONFIG_DIR']=str(root/'config')
    host=MainWindow(Config(data_dir=str(root/'data')));app=QApplication.instance();app.setQuitOnLastWindowClosed(False)
    c=host.transfer_monitor;p=host.transfer_page
    report={'passed':False,'scale':host.devicePixelRatioF()}
    def snapshot(mono,rx=0,connection=True):
        return Snapshot(time.time()-240+mono,mono,
            qq=[dict(pid=123,created=1,cpu=mono*.84,write=mono*64000)],
            connections=[dict(pid=123,local='192.168.1.20',local_port=44000,remote='192.168.1.30',remote_port=8000)] if connection else [],
            interfaces={'USB 共享网络':dict(up=True,ips=['192.168.1.20'],rx=rx,tx=0)},
            files={'synthetic/nt_msg.db':(123456789,int(mono))},disk_write=mono*70000)
    try:
        host.resize(1260,940);host.show();host.navigate(6);wait(250)
        host.grab().save(str(root/'empty.png'))
        c.receive(snapshot(1));c.receive(snapshot(7));wait(250)
        assert c.prompt and c.prompt.windowModality()==Qt.NonModal
        assert app.activeModalWidget() is None
        c.prompt.grab().save(str(root/'detected.png'));c.prompt.buttons()[0].click();wait(250)
        assert c.recording and host.pages.currentIndex()==6
        received=0
        for i in range(121):
            throughput=0 if 53<=i<=61 else max(0,13+9*math.sin(i*.13)+5*math.sin(i*.47))
            received+=int(throughput*1024**2*2)
            c.receive(snapshot(i*2+10,received))
        c.started=time.monotonic()-245;c.tick();wait(200)
        assert p.scope.text().startswith('QQ 写入 I/O') and '不采集网卡' in p.source.text() and len(p.chart.points)==121
        host.grab().save(str(root/'monitoring.png'))
        scroll=p.findChild(QScrollArea);assert scroll.horizontalScrollBar().maximum()==0
        p.window.setCurrentIndex(0);assert p.chart.window_seconds==120
        p.window.setCurrentIndex(2);assert p.chart.window_seconds==900
        p.window.setCurrentIndex(1)
        c.receive(snapshot(254,received+1024));wait(180)
        p.toggle.click();assert not c.recording
        frozen=len(p.chart.points);c.receive(snapshot(256,received+2048));assert len(p.chart.points)==frozen
        host.grab().save(str(root/'stopped.png'))
        # Exact QQ counters still render when authenticated, without adapter fallback.
        p.toggle.click();a=snapshot(257);a.exact_rx=0;a.exact_key='synthetic';c.receive(a)
        b=snapshot(259);b.exact_rx=2048;b.exact_key='synthetic';c.receive(b);wait(200)
        assert p.last_point['value']==1024 and p.scope.text().startswith('QQ 迁移连接')
        host.grab().save(str(root/'qq-network.png'));p.toggle.click()
        p.toggle.click();assert c.recording and len(c.history.points)==0
        c.receive(snapshot(260,received));c.receive(snapshot(262,received+1024));p.toggle.click()
        assert c.history.points[-1]['rx'] is None and c.history.points[-1]['value']==64000 and c.history.total==128000
        report.update(passed=True,nonmodal_prompt=True,chart_points_bounded=True,scope_label_correct=True,write_only_without_exact_counter=True,
                      time_range_switch=True,pause_retains_chart=True,restart_resets_baseline=True,
                      no_horizontal_clipping=True,independent_from_import_lock=True)
    finally:
        c.shutdown();host.request_exit();wait(120)
    (root/'result.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    return 0


def stalled_sampler(channel,roots):
    """Only used by isolated regression runs: simulate a blocked driver."""
    import time
    channel.recv();time.sleep(30)
