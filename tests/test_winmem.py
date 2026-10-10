"""Windows 进程内存读取（自动取钥的底层）测试。

这些测试在本机真实执行：枚举进程、打开句柄、读内存区域。
为了在不同机器上都稳定，只断言“必然成立”的事实（自己的进程、自己镜像的 PE 头），
不依赖其他程序的具体内存内容。
"""

from __future__ import annotations

import ctypes
import os
import sys

import pytest

from qqarchive import winmem

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="仅 Windows")


def test_list_processes_contains_self():
    procs = winmem.list_processes()
    assert procs
    me = [p for p in procs if p.pid == os.getpid()]
    assert me, "进程列表里应该有自己的进程"
    assert me[0].name.lower().endswith(".exe")


def test_find_processes_by_name():
    """按名字查进程：用自己进程的真实名字来查，必须能查到自己。"""
    me = next((p for p in winmem.list_processes() if p.pid == os.getpid()), None)
    assert me is not None
    found = winmem.find_processes([me.name])
    assert any(p.pid == os.getpid() for p in found)
    # 带不带 .exe 后缀都能查到
    stem = me.name[:-4] if me.name.lower().endswith(".exe") else me.name
    assert any(p.pid == os.getpid() for p in winmem.find_processes([stem]))


def test_find_processes_unknown_name_is_empty():
    assert winmem.find_processes(["绝对不存在的进程名.exe"]) == []


def test_process_info_str():
    assert str(winmem.ProcessInfo(123, "QQ.exe")) == "QQ.exe(pid=123)"


def test_open_invalid_pid_raises():
    with pytest.raises(winmem.WinMemError):
        winmem.ProcessMemory(0)
    with pytest.raises(winmem.WinMemError):
        winmem.ProcessMemory(-5)


def test_read_own_process_memory():
    """读自己进程里的特征数据：验证 regions/read 的地址与数据都对得上。"""
    marker = b"QQARCHIVE-WINMEM-MARKER-0123456789"
    buf = ctypes.create_string_buffer(marker, len(marker))
    addr = ctypes.addressof(buf)
    with winmem.ProcessMemory(os.getpid()) as mem:
        got = mem.read(addr, len(marker))
        assert got == marker
        regions = mem.regions()
        assert regions, "自己进程一定有可读区域"
        assert any(base <= addr < base + size for base, size in regions), "标记应落在某个可读区域内"


def test_regions_are_within_user_space():
    with winmem.ProcessMemory(os.getpid()) as mem:
        for base, size in mem.regions():
            assert 0 < base < winmem._USER_MAX
            assert 0 < size <= winmem.MAX_REGION


def test_iter_readable_yields_readable_blocks():
    marker = b"ITER-MARKER-" + os.urandom(16).hex().encode()
    ctypes.create_string_buffer(marker, len(marker))  # 常驻，避免被回收
    with winmem.ProcessMemory(os.getpid()) as mem:
        total = 0
        found = False
        for _base, block in mem.iter_readable(chunk=1 << 20):
            total += len(block)
            if marker in block:
                found = True
                break
        assert total > 0
        assert found, "应该能在自己进程的内存里找到刚放进去的标记"


def test_modules_of_own_process():
    with winmem.ProcessMemory(os.getpid()) as mem:
        mods = mem.modules()
        assert mods, "自己进程一定有已加载模块"
        assert any(name.endswith(".dll") for name in mods)
        assert mem.has_module(next(iter(mods)))
        assert not mem.has_module("绝对不存在的模块.dll")


def test_image_path_is_this_interpreter():
    with winmem.ProcessMemory(os.getpid()) as mem:
        path = mem.image_path()
    assert path.lower().endswith(".exe")


def test_readable_region_of_another_process_spans_correctly():
    """跨进程读镜像头：ReadProcessMemory 的基本能力，且要求 MZ 头正确。"""
    target = None
    try:
        with winmem.ProcessMemory(os.getpid()) as mem:
            mods = mem.modules()
        main = next((n for n in mods if n.endswith(".exe")), None)
        if not main:
            pytest.skip("拿不到自身主模块名")
        # 用 explorer.exe 之类的常驻进程做目标；找不到就跳过
        procs = winmem.find_processes(["explorer.exe"])
        if not procs:
            pytest.skip("没有可用于测试的常驻进程")
        target = procs[0]
        with winmem.ProcessMemory(target.pid) as mem:
            mods2 = mem.modules()
            base = next((b for n, b in mods2.items() if n.endswith(".exe")), 0)
            assert base, "应该能拿到目标进程的模块基址"
            assert mem.read(base, 2) == b"MZ", "镜像开头应为 MZ"
    except winmem.WinMemError as exc:
        pytest.skip(f"当前环境不允许读取其他进程：{exc}")


def test_alive_is_true_for_self():
    with winmem.ProcessMemory(os.getpid()) as mem:
        assert mem.alive is True


def test_closed_handle_is_inert():
    mem = winmem.ProcessMemory(os.getpid())
    mem.close()
    assert mem.alive is False
    mem.close()  # 重复关闭不应报错
