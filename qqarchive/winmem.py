"""Windows 进程枚举与只读内存读取（自动取钥的底层，FR-03）。

设计约束：

- **只读**：只调用 ``OpenProcess(PROCESS_VM_READ | PROCESS_QUERY_INFORMATION)``
  与 ``ReadProcessMemory``。本模块不提供任何写入进程内存的路径，
  不结束进程、不修改 QQ 的任何文件（NFR-01）。
- **无第三方依赖**：只用 ctypes 调用 kernel32，不引入 pymem / frida / 调试器。
- **权限最小**：请求的访问掩码写死在 ``READ_ACCESS``，调用方无法放宽。
- **不记录敏感数据**：本模块只回传内存字节，是否命中由上层判断；
  本模块自身不做任何日志输出。

只支持 64 位 Windows（本项目其余部分同样只在 x64 上分发）。
"""

from __future__ import annotations

import ctypes
import sys
from ctypes import wintypes
from dataclasses import dataclass

IS_WINDOWS = sys.platform == "win32"
_IS_64 = ctypes.sizeof(ctypes.c_void_p) == 8

# --- 访问权限：只读，不含 PROCESS_VM_WRITE / PROCESS_VM_OPERATION / PROCESS_TERMINATE ---
READ_ACCESS = 0x0010 | 0x0400  # PROCESS_VM_READ | PROCESS_QUERY_INFORMATION

TH32CS_SNAPPROCESS = 0x00000002
TH32CS_SNAPMODULE = 0x00000008
TH32CS_SNAPMODULE32 = 0x00000010
MAX_PATH = 260
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

MEM_COMMIT = 0x1000
PAGE_GUARD = 0x100
# 可读的页保护常量（含只读、读写、写时复制、可执行等）
_READABLE_PROTECT = frozenset({0x02, 0x04, 0x06, 0x08, 0x20, 0x40, 0x60, 0x80})
_USER_MAX = 0x7FFFFFFF0000  # x64 用户空间上界
MAX_REGION = 256 * 1024 * 1024  # 单次 VirtualQuery 区域过大时跳过（保留区或映射文件）


class WinMemError(Exception):
    """进程枚举或内存读取失败。"""


@dataclass(frozen=True)
class ProcessInfo:
    pid: int
    name: str
    parent_pid: int = 0

    def __str__(self) -> str:
        return f"{self.name}(pid={self.pid})"


class _MEMORY_BASIC_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("BaseAddress", ctypes.c_void_p),
        ("AllocationBase", ctypes.c_void_p),
        ("AllocationProtect", wintypes.DWORD),
        ("RegionSize", ctypes.c_size_t),
        ("State", wintypes.DWORD),
        ("Protect", wintypes.DWORD),
        ("Type", wintypes.DWORD),
    ]


class _PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("cntUsage", wintypes.DWORD),
        ("th32ProcessID", wintypes.DWORD),
        ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
        ("th32ModuleID", wintypes.DWORD),
        ("cntThreads", wintypes.DWORD),
        ("th32ParentProcessID", wintypes.DWORD),
        ("pcPriClassBase", ctypes.c_long),
        ("dwFlags", wintypes.DWORD),
        ("szExeFile", ctypes.c_wchar * MAX_PATH),
    ]


class _MODULEENTRY32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("th32ModuleID", wintypes.DWORD),
        ("th32ProcessID", wintypes.DWORD),
        ("GlblcntUsage", wintypes.DWORD),
        ("ProccntUsage", wintypes.DWORD),
        ("modBaseAddr", ctypes.POINTER(ctypes.c_byte)),
        ("modBaseSize", wintypes.DWORD),
        ("hModule", wintypes.HMODULE),
        ("szModule", ctypes.c_wchar * 256),
        ("szExePath", ctypes.c_wchar * MAX_PATH),
    ]


def _require_windows() -> None:
    if not IS_WINDOWS:
        raise WinMemError("自动取钥只在 Windows 上可用")
    if not _IS_64:
        raise WinMemError("自动取钥需要 64 位 Python（QQ 是 64 位进程）")


_k32 = ctypes.WinDLL("kernel32", use_last_error=True) if IS_WINDOWS else None

if IS_WINDOWS:
    _k32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    _k32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    _k32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(_PROCESSENTRY32W)]
    _k32.Process32FirstW.restype = wintypes.BOOL
    _k32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(_PROCESSENTRY32W)]
    _k32.Process32NextW.restype = wintypes.BOOL
    _k32.Module32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(_MODULEENTRY32W)]
    _k32.Module32FirstW.restype = wintypes.BOOL
    _k32.Module32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(_MODULEENTRY32W)]
    _k32.Module32NextW.restype = wintypes.BOOL
    _k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    _k32.OpenProcess.restype = wintypes.HANDLE
    _k32.CloseHandle.argtypes = [wintypes.HANDLE]
    _k32.CloseHandle.restype = wintypes.BOOL
    _k32.ReadProcessMemory.argtypes = [wintypes.HANDLE, ctypes.c_void_p, ctypes.c_void_p,
                                       ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t)]
    _k32.ReadProcessMemory.restype = wintypes.BOOL
    _k32.VirtualQueryEx.argtypes = [wintypes.HANDLE, ctypes.c_void_p,
                                    ctypes.POINTER(_MEMORY_BASIC_INFORMATION), ctypes.c_size_t]
    _k32.VirtualQueryEx.restype = ctypes.c_size_t
    _k32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    _k32.GetExitCodeProcess.restype = wintypes.BOOL
    _k32.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD,
                                                ctypes.c_wchar_p, ctypes.POINTER(wintypes.DWORD)]
    _k32.QueryFullProcessImageNameW.restype = wintypes.BOOL


def _valid(handle) -> bool:
    return bool(handle) and handle != INVALID_HANDLE_VALUE


def list_processes() -> list[ProcessInfo]:
    """列出全部进程（名称、PID、父进程 PID）。"""
    _require_windows()
    snap = _k32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if not _valid(snap):
        raise WinMemError(f"无法枚举进程（错误码 {ctypes.get_last_error()}）")
    out: list[ProcessInfo] = []
    try:
        entry = _PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(_PROCESSENTRY32W)
        if not _k32.Process32FirstW(snap, ctypes.byref(entry)):
            return out
        while True:
            out.append(ProcessInfo(entry.th32ProcessID, entry.szExeFile, entry.th32ParentProcessID))
            if not _k32.Process32NextW(snap, ctypes.byref(entry)):
                break
    finally:
        _k32.CloseHandle(snap)
    return out


def find_processes(names: list[str] | tuple[str, ...]) -> list[ProcessInfo]:
    """按可执行文件名查找进程（不区分大小写；可写 "QQ.exe" 或 "QQ"）。"""
    wanted = {n.lower() for n in names}
    wanted |= {n.lower() + ".exe" for n in names}
    return [p for p in list_processes() if p.name.lower() in wanted]


class ProcessMemory:
    """以只读方式打开一个进程，按区域遍历可读内存。

    用法::

        with ProcessMemory(pid) as mem:
            for addr, block in mem.iter_readable():
                ...
    """

    def __init__(self, pid: int, *, chunk: int = 1 << 20):
        _require_windows()
        if pid <= 0:
            raise WinMemError("无效的进程号")
        self.pid = pid
        self.chunk = max(4096, int(chunk))
        self._handle = _k32.OpenProcess(READ_ACCESS, False, pid)
        if not _valid(self._handle):
            err = ctypes.get_last_error()
            if err == 5:
                raise WinMemError(f"没有权限读取进程 {pid}：QQ 可能以管理员身份运行，请以管理员身份启动本程序")
            raise WinMemError(f"无法打开进程 {pid}（错误码 {err}）")

    # -- 生命周期 ---------------------------------------------------------
    def close(self) -> None:
        if getattr(self, "_handle", None):
            _k32.CloseHandle(self._handle)
            self._handle = None

    def __enter__(self) -> "ProcessMemory":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # -- 信息 -------------------------------------------------------------
    @property
    def alive(self) -> bool:
        if not getattr(self, "_handle", None):
            return False
        code = wintypes.DWORD()
        ok = _k32.GetExitCodeProcess(self._handle, ctypes.byref(code))
        return bool(ok) and code.value == 259  # STILL_ACTIVE

    def image_path(self) -> str:
        buf = ctypes.create_unicode_buffer(1024)
        size = wintypes.DWORD(len(buf))
        if _k32.QueryFullProcessImageNameW(self._handle, 0, buf, ctypes.byref(size)):
            return buf.value
        return ""

    def modules(self) -> dict[str, int]:
        """模块名（小写）→ 基址。取不到模块列表时返回空字典。"""
        snap = _k32.CreateToolhelp32Snapshot(TH32CS_SNAPMODULE | TH32CS_SNAPMODULE32, self.pid)
        if not _valid(snap):
            return {}
        out: dict[str, int] = {}
        try:
            entry = _MODULEENTRY32W()
            entry.dwSize = ctypes.sizeof(_MODULEENTRY32W)
            if not _k32.Module32FirstW(snap, ctypes.byref(entry)):
                return out
            while True:
                base = ctypes.cast(entry.modBaseAddr, ctypes.c_void_p).value or 0
                out[entry.szModule.lower()] = base
                if not _k32.Module32NextW(snap, ctypes.byref(entry)):
                    break
        finally:
            _k32.CloseHandle(snap)
        return out

    def has_module(self, name: str) -> bool:
        return name.lower() in self.modules()

    # -- 内存 -------------------------------------------------------------
    def regions(self) -> list[tuple[int, int]]:
        """返回 [(起始地址, 长度)]，只含已提交且可读的区域。"""
        out: list[tuple[int, int]] = []
        info = _MEMORY_BASIC_INFORMATION()
        addr = 0
        while addr < _USER_MAX:
            got = _k32.VirtualQueryEx(self._handle, ctypes.c_void_p(addr), ctypes.byref(info),
                                      ctypes.sizeof(info))
            if not got:
                break
            base = info.BaseAddress or 0
            size = int(info.RegionSize)
            if size <= 0:
                break
            protect = info.Protect or 0
            if (info.State == MEM_COMMIT and not (protect & PAGE_GUARD)
                    and (protect & 0xFF) in _READABLE_PROTECT and size <= MAX_REGION):
                out.append((base, size))
            nxt = base + size
            if nxt <= addr:
                break
            addr = nxt
        return out

    def read(self, address: int, size: int) -> bytes:
        """读一段内存；不可读的部分被截断（返回实际读到的字节）。"""
        if size <= 0:
            return b""
        buf = ctypes.create_string_buffer(size)
        got = ctypes.c_size_t(0)
        ok = _k32.ReadProcessMemory(self._handle, ctypes.c_void_p(address), buf, size,
                                    ctypes.byref(got))
        if not ok and got.value == 0:
            return b""
        return buf.raw[:got.value]

    def iter_readable(self, *, chunk: int | None = None):
        """逐块产出 (地址, 数据)。块内连续可读，块大小有上限，内存占用可控。"""
        step = int(chunk or self.chunk)
        for base, size in self.regions():
            pos = 0
            while pos < size:
                take = min(step, size - pos)
                data = self.read(base + pos, take)
                if data:
                    yield base + pos, data
                pos += take
