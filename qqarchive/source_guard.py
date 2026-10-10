"""Hold Windows source files read-only against writers; never copy a database."""
from __future__ import annotations
import os
from pathlib import Path

class SourceBusyError(OSError):
    pass

class SourceChangedError(OSError):
    pass

BUSY_MESSAGE = ('QQ 仍在使用这个数据库，无法保证完整读取。\n'
                '请保留 QQ聊天记录管理器，先从系统托盘正常退出电脑 QQ，再点击“读取会话列表”。\n'
                '本次已获取的密钥会留在 QQ聊天记录管理器内存中，无需重新取钥。')
CHANGED_MESSAGE = ('读取期间来源数据库或日志发生变化，本次结果已作废。\n'
                   '请保留 QQ聊天记录管理器，正常退出电脑 QQ 后重试。')

def signature(path):
    result=[]
    for suffix in ('','-wal','-journal'):
        p=Path(str(path)+suffix)
        try:
            stat=p.stat()
            result.append((suffix,stat.st_size,stat.st_mtime_ns,stat.st_ctime_ns,stat.st_ino))
        except FileNotFoundError:result.append((suffix,None))
    return tuple(result)

def _deny_writers(path):
    import ctypes
    from ctypes import wintypes
    create=ctypes.WinDLL('kernel32',use_last_error=True).CreateFileW
    create.argtypes=[wintypes.LPCWSTR,wintypes.DWORD,wintypes.DWORD,wintypes.LPVOID,wintypes.DWORD,wintypes.DWORD,wintypes.HANDLE]
    create.restype=wintypes.HANDLE
    handle=create(str(path),0x80000000,1,None,3,0x80,None) # GENERIC_READ, SHARE_READ, OPEN_EXISTING
    if handle==ctypes.c_void_p(-1).value:
        code=ctypes.get_last_error()
        if code in (32,33):raise SourceBusyError(BUSY_MESSAGE)
        raise ctypes.WinError(code)
    return handle

def _release(handle):
    import ctypes
    from ctypes import wintypes
    close=ctypes.WinDLL('kernel32',use_last_error=True).CloseHandle
    close.argtypes=[wintypes.HANDLE];close.restype=wintypes.BOOL;close(handle)

class StableSource:
    def __init__(self,path):self.path=Path(path).resolve();self.handles=[];self.before=None
    def __enter__(self):
        self.before=signature(self.path)
        try:
            if os.name=='nt':
                for suffix in ('','-wal','-journal'):
                    p=Path(str(self.path)+suffix)
                    if not suffix or p.exists():self.handles.append(_deny_writers(p))
            self.check()
            journal=Path(str(self.path)+'-journal')
            if journal.exists() and journal.stat().st_size:
                with journal.open('rb') as f:header=f.read(28)
                if any(header):raise SourceBusyError('来源存在未处理的回滚日志。请让 QQ 正常打开并退出后重试；不要删除日志文件。')
            return self
        except BaseException:
            self.close();raise
    def check(self):
        if signature(self.path)!=self.before:raise SourceChangedError(CHANGED_MESSAGE)
    def close(self):
        for handle in reversed(self.handles):_release(handle)
        self.handles.clear()
    def __exit__(self,exc_type,exc,tb):
        try:
            # An exception halfway through a query must still be classified as a moving source.
            self.check()
        finally:self.close()
