"""Bounded, read-only QQ migration telemetry and conservative detection.

QQ has no public 'migration running' API. A QQ process plus an established
private-LAN migration-port connection is a candidate, never proof of import.
No database connection, message payload, process memory, or source copy.
"""
from __future__ import annotations
from collections import deque
from dataclasses import dataclass, field, asdict
import ipaddress
import json
import os
from pathlib import Path
import socket
import time

MIGRATION_PORTS = {8000, 10191}
PRIVATE_NETWORKS = tuple(ipaddress.ip_network(x) for x in ('10.0.0.0/8','172.16.0.0/12','192.168.0.0/16','fc00::/7','fe80::/10'))


def private_peer(address):
    try:
        addr=ipaddress.ip_address(address.split('%')[0])
        addr=getattr(addr,'ipv4_mapped',None) or addr
        return any(addr.version==net.version and addr in net for net in PRIVATE_NETWORKS)
    except ValueError:return False


def delta_rate(current,previous,elapsed):
    if current is None or previous is None or not 0 < elapsed <= 20 or current < previous:return None
    return (current-previous)/elapsed


@dataclass
class Snapshot:
    at: float
    mono: float
    qq: list = field(default_factory=list)
    connections: list = field(default_factory=list)
    interfaces: dict = field(default_factory=dict)
    files: dict = field(default_factory=dict)
    disk_write: int | None = None
    memory_available: int | None = None
    errors: list = field(default_factory=list)
    detection_ok: bool = True
    exact_rx: int | None = None
    exact_key: str = ''
    exact_note: str = ''
    network_mono: float | None = None
    exact_mono: float | None = None
    disk_mono: float | None = None

    @property
    def sessions(self):
        identities={p['pid']:p.get('created',0) for p in self.qq}
        return {f"{c['pid']}:{identities.get(c['pid'],0)}:{c['remote']}" for c in self.connections}


class OfferGate:
    """Two sightings, once per peer/process session; 90 s disconnect re-arms."""
    def __init__(self):self.counts={};self.handled=set();self.absent_since={}
    def update(self,snapshot):
        if not snapshot.detection_ok:return []
        current=snapshot.sessions
        for key in set(self.counts)|self.handled:
            if key not in current:
                self.counts[key]=0
                self.absent_since.setdefault(key,snapshot.mono)
                if snapshot.mono-self.absent_since[key]>=90:
                    self.handled.discard(key);self.counts.pop(key,None);self.absent_since.pop(key,None)
        for key in current:
            self.absent_since.pop(key,None);self.counts[key]=self.counts.get(key,0)+1
        return sorted(k for k in current if self.counts[k]>=2 and k not in self.handled)
    def acknowledge(self,keys):self.handled.update(keys)


class RateHistory:
    """QQ-only counters. Never substitute adapter or whole-machine traffic."""
    def __init__(self,max_samples=1800):self.points=deque(maxlen=max_samples);self.reset()
    def reset(self):
        self.points.clear();self.previous=None;self.total=0.;self.duration=0.;self.peak=0.;self.basis=None
    def add(self,snapshot):
        old=self.previous;self.previous=snapshot
        elapsed=snapshot.mono-old.mono if old else 0
        prior={(p['pid'],p['created']):p for p in old.qq} if old else {}
        writes=[];cpus=[];written=0.
        for p in snapshot.qq:
            prev=prior.get((p['pid'],p['created']),{})
            dt=p.get('measured',snapshot.mono)-prev.get('measured',old.mono if old else snapshot.mono)
            value=delta_rate(p.get('write'),prev.get('write'),dt)
            writes.append(value)
            if value is not None:written+=p['write']-prev['write']
            cpus.append(delta_rate(p.get('cpu'),prev.get('cpu'),dt))
        write=sum(v for v in writes if v is not None) if any(v is not None for v in writes) else None
        cpu=100*sum(v for v in cpus if v is not None) if any(v is not None for v in cpus) else None
        rx=None;rx_elapsed=elapsed
        if snapshot.exact_rx is not None:
            rx_elapsed=(snapshot.exact_mono if snapshot.exact_mono is not None else snapshot.mono)-(old.exact_mono if old and old.exact_mono is not None else old.mono if old else snapshot.mono)
            rx=delta_rate(snapshot.exact_rx,old.exact_rx if old and old.exact_key==snapshot.exact_key else None,rx_elapsed)
            scope='qq';key=('qq',snapshot.exact_key);value=rx;increment=rx*rx_elapsed if rx is not None else 0.;duration=rx_elapsed
        else:
            scope='write';key=('write',tuple(sorted((p['pid'],p['created']) for p in snapshot.qq)))
            value=write;increment=written;duration=elapsed
        changed=key!=self.basis
        if changed:self.basis=key;self.total=0.;self.duration=0.;self.peak=0.;value=None
        if value is not None and 0<duration<=20:
            self.total+=increment;self.duration+=duration;self.peak=max(self.peak,value)
        touched=[path for path,state in snapshot.files.items() if old and path in old.files and state!=old.files[path]]
        point=dict(at=snapshot.at,mono=snapshot.mono,rx=rx,write=write,value=value,cpu=cpu,
                   files_changed=len(touched),db_available=bool(snapshot.files),connections=len(snapshot.connections),
                   scope=scope,basis=str(key),basis_changed=changed,total=self.total,peak=self.peak,
                   mean=self.total/self.duration if self.duration else None,errors=list(snapshot.errors),exact_note=snapshot.exact_note)
        self.points.append(point);return point


class SessionLog:
    """Bounded log per manual session; no source contents or key material."""
    def __init__(self,directory,limit=8*1024*1024):
        self.directory=Path(directory);self.limit=limit;self.stream=None;self.written=0;self.capped=False
    def start(self):
        import uuid
        self.directory.mkdir(parents=True,exist_ok=True)
        self.path=self.directory/(time.strftime('migration-%Y%m%d-%H%M%S-')+uuid.uuid4().hex[:6]+'.jsonl')
        self.stream=self.path.open('x',encoding='utf-8')
    def append(self,point):
        if not self.stream or self.capped:return
        data=json.dumps(point,ensure_ascii=False,separators=(',',':'))+'\n';size=len(data.encode('utf-8'))
        if self.written+size>self.limit:self.capped=True;return
        self.stream.write(data);self.stream.flush();self.written+=size
    def close(self):
        if self.stream:self.stream.close();self.stream=None


class AsyncSessionLog:
    def __init__(self,directory,limit=8*1024*1024,writer_factory=SessionLog):
        import queue,threading
        self.queue=queue.Queue(maxsize=64);self.closed=threading.Event();self.directory=directory;self.limit=limit
        self.factory=writer_factory;self.thread=None;self.capped=False;self.failed=False;self.dropped=False
    def start(self):
        import threading
        self.thread=threading.Thread(target=self.run,name='migration-log',daemon=True);self.thread.start()
    def run(self):
        import queue
        writer=None
        try:
            writer=self.factory(self.directory,self.limit);writer.start()
            while not self.closed.is_set() or not self.queue.empty():
                try:point=self.queue.get(timeout=.1)
                except queue.Empty:continue
                writer.append(point);self.capped=writer.capped
        except OSError:self.failed=True
        finally:
            if writer:
                try:writer.close()
                except OSError:self.failed=True
    def append(self,point):
        import queue
        if self.closed.is_set() or self.failed or self.capped:return
        try:self.queue.put_nowait(dict(point))
        except queue.Full:self.dropped=True
    def close(self):self.closed.set()


class WindowsSampler:
    def __init__(self,roots=()):
        import psutil
        self.ps=psutil;self.roots=list(roots);self.paths=[];self.last_discovery=-1e9;self.exact=None
    def discover_files(self):
        from .discovery import default_qq_roots
        roots=list(dict.fromkeys([Path(p) for p in self.roots]+default_qq_roots()))
        found=[]
        for root in roots[:16]:
            try:
                if root.is_file():candidates=[root]
                else:
                    dirs=[root]+[p for p in root.iterdir() if p.is_dir() and p.name.isdigit()][:100]
                    candidates=[p/'nt_qq'/'nt_db'/'nt_msg.db' for p in dirs]
                    if root.name=='nt_db':candidates.append(root/'nt_msg.db')
                for path in candidates:
                    if path.is_file():found.extend([path,Path(str(path)+'-wal')])
            except OSError:continue
        self.paths=list(dict.fromkeys(found))[:64];self.last_discovery=time.monotonic()
    def sample(self,recording=False):
        ps=self.ps;s=Snapshot(time.time(),time.monotonic())
        if s.mono-self.last_discovery>60:self.discover_files()
        try:
            for p in ps.process_iter(['pid','name']):
                if (p.info.get('name') or '').lower()!='qq.exe':continue
                try:created=p.create_time()
                except (ps.Error,OSError):continue
                item={'pid':p.pid,'created':created,'cpu':None,'write':None}
                if recording:
                    try:
                        with p.oneshot():
                            c=p.cpu_times();item['cpu']=c.user+c.system;item['write']=p.io_counters().write_bytes
                            item['measured']=time.monotonic()
                    except (ps.Error,OSError):s.errors.append('QQ 进程 I/O 暂不可读')
                s.qq.append(item)
            qqids={p['pid'] for p in s.qq}
            if qqids:
                for c in ps.net_connections(kind='tcp'):
                    if c.pid not in qqids or c.status!='ESTABLISHED' or not c.raddr:continue
                    if c.raddr.port not in MIGRATION_PORTS or not private_peer(c.raddr.ip):continue
                    s.connections.append(dict(pid=c.pid,local=c.laddr.ip,local_port=c.laddr.port,
                                              remote=c.raddr.ip,remote_port=c.raddr.port))
        except (ps.Error,OSError) as e:s.detection_ok=False;s.errors.append('暂时无法检查 QQ 迁移连接')
        if recording:
            for path in self.paths:
                try:
                    st=path.stat();s.files[str(path)]=(st.st_size,st.st_mtime_ns)
                except OSError:continue
            if os.name=='nt' and s.connections:
                if self.exact is None:
                    from .transfer_tcp import TcpCounters
                    self.exact=TcpCounters()
                s.exact_rx,s.exact_key,s.exact_note=self.exact.collect(s.connections)
                s.exact_mono=time.monotonic()
        # Process and authenticated connection counters have their own timestamps.
        s.at=time.time();s.mono=time.monotonic()
        return s
