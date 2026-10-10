"""Isolated event-loop acceptance; never uses the user's archive/config."""
from pathlib import Path
import json,time


def slow_driver(channel,roots):
    import ctypes
    # Deliberately hold the child interpreter's GIL, as slow Windows extensions
    # can do. This disposable helper must not block the desktop interpreter.
    channel.recv();api=ctypes.PyDLL('kernel32');api.Sleep(5000)


def run(destination,mode='live',seconds=40):
    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QWidget,QVBoxLayout,QApplication
    from .config import Config
    from .transfer_monitor import TransferPage,TransferController
    from .transfer_process import ProcessSampler
    import os
    root=Path(destination).resolve();root.mkdir(parents=True,exist_ok=True)
    os.environ['QQARCHIVE_CONFIG_DIR']=str(root/'config')
    class Host(QWidget):
        def navigate(self,n):pass
    host=Host();host.cfg=Config(data_dir=str(root/'data'));host.worker=None
    page=TransferPage();QVBoxLayout(host).addWidget(page);host.resize(1140,900);host.show()
    controller=TransferController(host,page,autostart=False)
    if mode=='stalled':
        controller.worker.sampler=ProcessSampler(timeout=1,target=slow_driver,stop=controller.worker.stop_event)
    beats=[];received=[];errors=[];previous=[time.perf_counter()]
    def tick():
        now=time.perf_counter();beats.append((now-previous[0])*1000);previous[0]=now
    timer=QTimer(host);timer.timeout.connect(tick);timer.start(20)
    controller.worker.sampleReady.connect(lambda s:received.append(s))
    controller.worker.sampleReady.connect(lambda s:errors.extend(s.errors))
    controller.start();controller.worker.start();interaction_count=[0]
    def interact():
        interaction_count[0]+=1
        page.window.setCurrentIndex(interaction_count[0]%3)
        page.chart.hover_x=100+(interaction_count[0]*13)%600;page.chart.update()
    clicks=QTimer(host);clicks.timeout.connect(interact);clicks.start(100)
    app=QApplication.instance();app.setQuitOnLastWindowClosed(False)
    def finish():
        if not controller.shutdown():QTimer.singleShot(50,finish);return
        clicks.stop();timer.stop();host.grab().save(str(root/'monitor.png'));host.close();app.quit()
    QTimer.singleShot(seconds*1000,finish);app.exec()
    result={'mode':mode,'duration_seconds':seconds,'heartbeat_max_ms':round(max(beats,default=0),2),
            'heartbeat_p95_ms':round(sorted(beats)[int(len(beats)*.95)],2) if beats else None,
            'over_100ms':sum(x>100 for x in beats),'samples':len(received),'interactions':interaction_count[0],
            'errors':len(errors),'scope':page.last_point['scope'] if page.last_point else None,
            'worker_stopped':not controller.worker.isRunning()}
    result['passed']=len(beats)>seconds*15 and result['heartbeat_max_ms']<250 and len(received)>0 and result['worker_stopped']
    if mode=='stalled':result['passed']=result['passed'] and bool(errors)
    (root/'result.json').write_text(json.dumps(result,indent=2),encoding='utf-8')
    return 0 if result['passed'] else 1
