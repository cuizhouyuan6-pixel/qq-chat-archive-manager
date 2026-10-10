import json
from pathlib import Path
import time
import pytest
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from qqarchive.transfer_core import Snapshot,RateHistory,OfferGate,delta_rate,private_peer,SessionLog
from qqarchive.transfer_monitor import TransferWorker
from test_v13_interactions import ui


def sample(t=0,rx=0,connection=True):
    return Snapshot(1700000000+t,t,qq=[dict(pid=7,created=123,cpu=t*.8,write=t*4096)],
        connections=[dict(pid=7,local='10.0.0.2',local_port=45123,remote='10.0.0.1',remote_port=8000)] if connection else [],
        interfaces={'USB':dict(rx=rx,tx=0,up=True,ips=['10.0.0.2'])},
        files={'source/nt_msg.db':(4096,t)},disk_write=t*8192)


@pytest.mark.parametrize('address,expected',[('192.168.1.72',True),('10.0.0.1',True),('172.16.1.2',True),('::ffff:192.168.1.72',True),('fe80::1%4',True),('127.0.0.1',False),('8.8.8.8',False),('::1',False),('not-ip',False)])
def test_only_lan_peers(address,expected):assert private_peer(address)==expected


def test_unknown_reset_and_suspend_are_not_zero_rates():
    assert delta_rate(1024,None,2) is None
    assert delta_rate(0,1024,2) is None
    assert delta_rate(2048,1024,60) is None
    assert delta_rate(1024,1024,2)==0


def test_adapter_bytes_are_never_used_as_qq_network():
    h=RateHistory();assert h.add(sample())['value'] is None
    s=sample(4,4096);s.interfaces['Other']=dict(rx=10**10,tx=0,up=True,ips=['192.168.2.1'])
    p=h.add(s)
    assert p['rx'] is None and p['scope']=='write' and p['value']==4096
    assert p['total']==16384 and p['files_changed']==1 and p['cpu']==80
    assert 'disk' not in p and 'interface' not in p


def test_slow_metadata_queries_do_not_inflate_process_rates():
    h=RateHistory();a=sample(4);a.qq[0].update(cpu=1,write=1000,measured=1);h.add(a)
    b=sample(6);b.qq[0].update(cpu=3,write=3000,measured=5);p=h.add(b)
    assert p['cpu']==50 and p['write']==500 and p['total']==2000


def test_counter_reset_and_qq_restart_never_spike():
    h=RateHistory();h.add(sample());h.add(sample(2))
    s=sample(4);s.qq[0]['write']=0
    assert h.add(s)['value'] is None
    s=sample(6);s.qq[0]['created']=999
    assert h.add(s)['value'] is None and h.total==0


def test_exact_mode_only_with_valid_connection_counters():
    h=RateHistory();a=sample();a.exact_rx=100;a.exact_key='one';h.add(a)
    b=sample(2,9000);b.exact_rx=1100;b.exact_key='one';p=h.add(b)
    assert p['scope']=='qq' and p['rx']==500 and p['value']==500 and p['total']==1000
    c=sample(4,15000);c.exact_note='permission';p=h.add(c)
    assert p['scope']=='write' and p['rx'] is None and p['basis_changed']
    p=h.add(sample(6,999999));assert p['value']==4096 and p['total']==8192


def test_missing_process_is_unknown_not_fake_zero():
    h=RateHistory();h.add(sample());h.add(sample(2));s=sample(4);s.qq=[]
    p=h.add(s);assert p['write'] is None and p['cpu'] is None and p['value'] is None


def test_offer_requires_stable_candidate_and_remembers_decline():
    gate=OfferGate();assert gate.update(sample())==[]
    keys=gate.update(sample(6));assert keys
    gate.acknowledge(keys)
    assert gate.update(sample(12))==[]
    assert gate.update(sample(20,connection=False))==[]
    assert gate.update(sample(40))==[]  # brief connection churn, same session
    gate.update(sample(50,connection=False));gate.update(sample(141,connection=False))
    assert gate.update(sample(147))==[]
    assert gate.update(sample(153))


def test_detector_failure_does_not_rearm_a_declined_session():
    gate=OfferGate();gate.update(sample());keys=gate.update(sample(6));gate.acknowledge(keys)
    error=sample(200,connection=False);error.detection_ok=False;gate.update(error)
    assert gate.update(sample(210))==[]


def test_ordinary_qq_login_never_offers():
    gate=OfferGate()
    for t in range(0,120,6):assert not gate.update(sample(t,connection=False))


def test_history_is_bounded_and_log_cap_preserves_old_file(tmp_path):
    h=RateHistory(max_samples=3)
    for t in range(10):h.add(sample(t,t*100))
    assert len(h.points)==3
    log=SessionLog(tmp_path,limit=50);log.start();log.append({'rx':20});log.append({'too_large':'x'*100})
    assert log.capped and log.path.stat().st_size<=50
    first=log.path;log.close();second=SessionLog(tmp_path);second.start();second.close()
    assert second.path!=first and json.loads(first.read_text())=={'rx':20}


def test_nonmodal_prompt_opt_in_duplicate_and_manual_controls(ui):
    app,w,_=ui;c=w.transfer_monitor;p=w.transfer_page
    c.receive(sample());c.receive(sample(6));assert c.prompt and c.prompt.isVisible()
    assert c.prompt.windowModality()==Qt.NonModal and app.activeModalWidget() is None
    c.prompt.buttons()[1].click();QTest.qWait(240);assert c.prompt is None and not c.recording
    c.receive(sample(12));assert c.prompt is None
    w.navigate(6);p.toggle.click();assert c.recording
    c.receive(sample(20));c.receive(sample(22,4096));assert p.last_point['rx'] is None and p.last_point['value']==4096
    assert p.scope.text().startswith('QQ 写入 I/O') and '不采集网卡' in p.source.text()
    p.toggle.click();assert not c.recording and p.chart.points
    c.receive(sample(24,8192));assert len(p.chart.points)==2 and c.prompt is None
    c.shutdown()


def test_accept_prompt_navigates_and_does_not_stop_qq(ui):
    app,w,_=ui;c=w.transfer_monitor
    c.receive(sample());c.receive(sample(6));c.prompt.buttons()[0].click();QTest.qWait(240)
    assert c.recording and w.pages.currentIndex()==6
    c.shutdown()


def test_disable_preference_and_busy_host_delay_prompt(ui):
    app,w,_=ui;c=w.transfer_monitor
    w.worker=object();c.receive(sample());c.receive(sample(6));assert c.pending and not c.prompt
    w.worker=None;c.consider_offer();assert c.prompt
    w.transfer_page.auto.setChecked(False);QTest.qWait(240)
    assert c.prompt is None and not w.cfg.transfer_detection
    c.receive(sample(12));assert c.prompt is None
    c.shutdown()


def test_worker_is_interruptible_without_terminate(ui):
    class Sampler:
        def sample(self,recording):return sample()
    worker=TransferWorker(sampler=Sampler());received=[];worker.sampleReady.connect(received.append);worker.start()
    QTest.qWait(150);worker.stop();assert worker.wait(1000) and received


def test_sampler_never_calls_adapter_or_whole_machine_counters(monkeypatch):
    import psutil
    from qqarchive.transfer_core import WindowsSampler
    def forbidden(*a,**kw):raise AssertionError('whole-machine counters are forbidden')
    for name in ('net_if_stats','net_io_counters','net_if_addrs','disk_io_counters','virtual_memory'):
        monkeypatch.setattr(psutil,name,forbidden)
    monkeypatch.setattr(psutil,'process_iter',lambda *a,**kw:[])
    sampler=WindowsSampler();monkeypatch.setattr(sampler,'discover_files',lambda:None)
    s=sampler.sample(True);assert s.exact_rx is None and not s.errors


def test_slow_log_writer_never_blocks_gui_enqueue(tmp_path):
    import threading
    from qqarchive.transfer_core import AsyncSessionLog
    gate=threading.Event()
    class SlowWriter:
        capped=False
        def __init__(self,*a):pass
        def start(self):gate.wait(2)
        def append(self,p):pass
        def close(self):pass
    log=AsyncSessionLog(tmp_path,writer_factory=SlowWriter)
    start=time.perf_counter();log.start()
    for i in range(1000):log.append({'i':i})
    log.close();assert time.perf_counter()-start<.3 and log.dropped and log.queue.qsize()<=64
    gate.set();log.thread.join(2);assert not log.thread.is_alive()


def test_hidden_page_defers_chart_updates_until_shown(ui):
    app,w,_=ui;c=w.transfer_monitor;p=w.transfer_page
    w.navigate(0);c.start();c.receive(sample(1));c.receive(sample(3))
    assert not p.chart.points and len(c.history.points)==2
    w.navigate(6);assert len(p.chart.points)==2 and p.scope.text().startswith('QQ 写入 I/O')
    c.shutdown()


def test_process_sampler_timeout_does_not_freeze_event_loop(ui):
    from qqarchive.transfer_process import ProcessSampler
    from qqarchive.transfer_checks import stalled_sampler
    app,w,_=ui
    sampler=ProcessSampler(timeout=.6,target=stalled_sampler)
    worker=TransferWorker(sampler=sampler);sampler.stop=worker.stop_event
    from PySide6.QtCore import QEventLoop,QTimer
    loop=QEventLoop();events=[];ticks=[]
    timer=QTimer();timer.timeout.connect(lambda:ticks.append(time.monotonic()));timer.start(20)
    deadline=QTimer();deadline.setSingleShot(True);deadline.timeout.connect(loop.quit);deadline.start(4000)
    worker.sampleReady.connect(lambda s:(events.append(s),loop.quit()));worker.start()
    loop.exec();timer.stop();deadline.stop()
    worker.stop();assert worker.wait(2000)
    assert len(ticks)>20 and events and events[0].detection_ok is False and sampler.process is None


@pytest.mark.parametrize('value,label',[(None,'—'),(0,'0.00 KB/s'),(1000,'1.00 KB/s'),(125000,'1.00 Mb/s'),(1_000_000,'8.00 Mb/s')])
def test_requested_decimal_rate_units(value,label):
    from qqarchive.transfer_monitor import rate
    assert rate(value)==label


def test_cumulative_bytes_are_not_changed_to_bits():
    from qqarchive.transfer_monitor import size
    assert size(1_000_000)=='1.0 MB'
