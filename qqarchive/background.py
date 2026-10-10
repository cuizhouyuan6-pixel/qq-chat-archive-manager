"""Latest-request-wins read jobs. Workers only own paths, callbacks and plain data."""
import threading,time
from concurrent.futures import ThreadPoolExecutor
from PySide6.QtCore import QObject,QTimer,Signal
from .store import Archive

class ReadJob(QObject):
    result=Signal(object);error=Signal(str);busy=Signal(bool);elapsed=Signal(float)
    def __init__(self,path,parent):
        super().__init__(parent);self.path=path;self.pool=ThreadPoolExecutor(max_workers=1,thread_name_prefix='archive-read')
        self.future=None;self.cancel=threading.Event();self.pending=None;self.closed=False;self.started=0.
        self.timer=QTimer(self);self.timer.timeout.connect(self.poll);self.timer.start(40)
    def submit(self,fn):
        self.cancel.set();self.pending=fn;self.started=time.monotonic();self.busy.emit(True)
    def poll(self):
        if self.closed:return
        if self.future and self.future.done():
            future=self.future;self.future=None
            if not self.cancel.is_set():
                self.busy.emit(False)
                try:self.result.emit(future.result())
                except Exception as exc:self.error.emit(str(exc))
        if self.pending and self.future is None:
            fn=self.pending;self.pending=None;self.cancel=threading.Event()
            self.future=self.pool.submit(_read,self.path,self.cancel,fn)
        if self.future or self.pending:self.elapsed.emit(time.monotonic()-self.started)
    def stop(self):
        self.closed=True;self.cancel.set();self.pending=None;self.timer.stop();self.pool.shutdown(wait=False,cancel_futures=True)

def _read(path,cancel,fn):
    a=Archive(path,read_only=True)
    try:
        a.con.set_progress_handler(lambda:int(cancel.is_set()),1000)
        if cancel.is_set():raise InterruptedError()
        return fn(a)
    finally:a.close()
