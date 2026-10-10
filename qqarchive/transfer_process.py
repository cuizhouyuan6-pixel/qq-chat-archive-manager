"""Bounded telemetry IPC. Driver calls cannot hold the desktop Python GIL."""
from __future__ import annotations
import multiprocessing as mp
import os
from pathlib import Path
import sys
import time


def sampler_main(channel, roots):
    from .transfer_core import WindowsSampler, Snapshot
    sampler=WindowsSampler(roots)
    try:
        while True:
            recording=channel.recv()
            if recording is None:break
            try:sample=sampler.sample(bool(recording))
            except Exception:
                sample=Snapshot(time.time(),time.monotonic(),detection_ok=False,
                                errors=['采样暂不可用，下个周期会重试'])
            channel.send(sample)
    except (EOFError,BrokenPipeError,OSError):pass
    finally:channel.close()


class ProcessSampler:
    def __init__(self,roots=(),stop=None,timeout=8,target=sampler_main):
        self.roots=tuple(roots);self.stop=stop;self.timeout=timeout;self.target=target
        self.process=None;self.channel=None;self.retry_at=0.
    def open(self):
        # Frozen releases use the same windowless executable. Source probes also
        # use pythonw so a sampling helper never creates a console window.
        if os.name=='nt' and not getattr(sys,'frozen',False):
            hidden=Path(sys.executable).with_name('pythonw.exe')
            if hidden.exists():mp.set_executable(str(hidden))
        ctx=mp.get_context('spawn');parent,child=ctx.Pipe()
        process=ctx.Process(target=self.target,args=(child,self.roots),daemon=True,name='QQArchive-telemetry')
        try:process.start()
        except BaseException:parent.close();child.close();raise
        child.close();self.channel=parent;self.process=process
    def sample(self,recording=False):
        if time.monotonic()<self.retry_at:raise TimeoutError('采样恢复等待中')
        if self.process is None:self.open()
        try:
            self.channel.send(bool(recording));deadline=time.monotonic()+self.timeout
            while not self.channel.poll(.05):
                if self.stop is not None and self.stop.is_set():raise InterruptedError('采样已停止')
                if not self.process.is_alive():raise OSError('采样进程退出')
                if time.monotonic()>deadline:raise TimeoutError('系统查询超时')
            return self.channel.recv()
        except (OSError,EOFError,TimeoutError):
            self.close();self.retry_at=time.monotonic()+10;raise
    def close(self):
        process,self.process=self.process,None;channel,self.channel=self.channel,None
        if channel is not None:
            try:channel.send(None)
            except (OSError,EOFError):pass
            channel.close()
        if process is not None:
            process.join(.15)
            # Only this disposable read-only helper is terminated, never QQ,
            # ADB, an importer, or a QThread. No archive handles live here.
            if process.is_alive():process.terminate();process.join(1)
            if not process.is_alive():process.close()
