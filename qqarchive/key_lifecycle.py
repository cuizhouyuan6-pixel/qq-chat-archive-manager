"""Local source-bound key validation. No database copies and no secret logging."""
from __future__ import annotations
import time
from pathlib import Path
from .source_guard import signature
from .sqlcipher import EncryptedDatabase, CipherError, PageAuthError, detect_header, PRESETS
from dataclasses import replace

def validate(path, key, params=None):
    """Authenticate the base first page only; active WAL is irrelevant to key expiry."""
    path=Path(path)
    try:
        before=signature(path)
        header=detect_header(path)
        if header['kind']=='sqlite':return 'unavailable',None
        prefixes=(1024,0) if header['kind']=='ntqq' else (0,1024)
        choices=([params] if params else [])+[replace(p,prefix=n) for p in PRESETS for n in prefixes]
        for choice in dict.fromkeys(choices):
            try:
                with EncryptedDatabase(path,key,choice,use_wal=False) as db:
                    if signature(path)!=before:return 'unavailable',None
                    return 'valid',db.params
            except CipherError:continue
        return ('invalid' if signature(path)==before else 'unavailable'),None
    except (OSError,ValueError):return 'unavailable',None

def inspect_saved(store, cancel=None):
    result=[]
    for entry in store.saved_accounts():
        if cancel is not None and cancel.is_set():break
        path=entry.get('source_path')
        if not path:continue
        item=store.get(entry['id'])
        state='unavailable'
        if item:state,_=validate(path,*item)
        result.append({'id':entry['id'],'account':entry['account'],'state':state})
    return result

def close_qq(cancel=None,status=None):
    """Only called after an explicit in-app confirmation. Exact executable identity."""
    import ctypes
    from ctypes import wintypes
    from .keyscan import find_qq_processes
    kernel=ctypes.WinDLL('kernel32',use_last_error=True)
    user=ctypes.WinDLL('user32',use_last_error=True)
    user.GetWindowThreadProcessId.argtypes=[wintypes.HWND,ctypes.POINTER(wintypes.DWORD)]
    user.PostMessageW.argtypes=[wintypes.HWND,wintypes.UINT,wintypes.WPARAM,wintypes.LPARAM]
    kernel.OpenProcess.argtypes=[wintypes.DWORD,wintypes.BOOL,wintypes.DWORD];kernel.OpenProcess.restype=wintypes.HANDLE
    kernel.CloseHandle.argtypes=[wintypes.HANDLE]
    kernel.TerminateProcess.argtypes=[wintypes.HANDLE,wintypes.UINT]
    kernel.WaitForSingleObject.argtypes=[wintypes.HANDLE,wintypes.DWORD]
    handles=[]
    try:
        # Bind handles before requesting shutdown to avoid terminating reused PIDs.
        pids={p.pid if hasattr(p,'pid') else p for p in find_qq_processes()}
        for pid in pids:
            handle=kernel.OpenProcess(0x00100001,False,pid)
            if not handle:raise OSError('无法结束 QQ，请正常退出 QQ 后继续。')
            handles.append(handle)
        callback=ctypes.WINFUNCTYPE(wintypes.BOOL,wintypes.HWND,wintypes.LPARAM)
        def visit(hwnd,_):
            pid=wintypes.DWORD();user.GetWindowThreadProcessId(hwnd,ctypes.byref(pid))
            if pid.value in pids:user.PostMessageW(hwnd,0x0010,0,0)
            return True
        user.EnumWindows.argtypes=[callback,wintypes.LPARAM];user.EnumWindows.restype=wintypes.BOOL
        user.EnumWindows(callback(visit),0)
        deadline=time.monotonic()+5
        while any(kernel.WaitForSingleObject(h,0)==258 for h in handles) and time.monotonic()<deadline:
            if cancel is not None and cancel.is_set():raise InterruptedError('已取消后续导入')
            time.sleep(.1)
        for h in handles:
            if kernel.WaitForSingleObject(h,0)==258:
                if not kernel.TerminateProcess(h,0):raise OSError('QQ 尚未退出，请手动退出后重试。')
                kernel.WaitForSingleObject(h,5000)
        if find_qq_processes():raise OSError('QQ 又启动了，请关闭后再继续。')
        return True
    finally:
        for h in handles:kernel.CloseHandle(h)
