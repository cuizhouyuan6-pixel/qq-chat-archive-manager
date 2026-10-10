"""Windows per-connection TCP counters; never reads payloads or process memory.

Uses documented IP Helper EStats data/receive counters. If collection cannot
be enabled (e.g. no elevation), records the error, not fabricated zero bytes.
Only enables counters on QQ IPv4 connections to explicitly configured phone IPs.
"""
from __future__ import annotations

import argparse
import ctypes as C
import datetime as dt
import json
import os
from pathlib import Path
import socket
import time

U32, U64, U8 = C.c_uint32, C.c_uint64, C.c_ubyte


class Row(C.Structure):
    _fields_ = [(n, U32) for n in ("state", "local", "local_port", "remote", "remote_port")]


class OwnerRow(C.Structure):
    _fields_ = Row._fields_ + [("pid", U32)]


class Data(C.Structure):
    _fields_ = [(n, U64) for n in (
        "DataBytesOut", "DataSegsOut", "DataBytesIn", "DataSegsIn", "SegsOut", "SegsIn"
    )] + [(n, U32) for n in (
        "SoftErrors", "SoftErrorReason", "SndUna", "SndNxt", "SndMax"
    )] + [("ThruBytesAcked", U64), ("RcvNxt", U32), ("ThruBytesReceived", U64)]


class Receive(C.Structure):
    _fields_ = [(n, U32) for n in (
        "CurRwinSent", "MaxRwinSent", "MinRwinSent", "LimRwin", "DupAckEpisodes",
        "DupAcksOut", "CeRcvd", "EcnSent", "EcnNoncesRcvd", "CurReasmQueue", "MaxReasmQueue"
    )] + [("CurAppRQueue", C.c_size_t), ("MaxAppRQueue", C.c_size_t), ("WinScaleSent", U8)]


def ip(value):
    return socket.inet_ntoa(value.to_bytes(4, "little"))


def bind_api():
    api = C.WinDLL("iphlpapi.dll")
    api.GetExtendedTcpTable.argtypes = [C.c_void_p, C.POINTER(U32), C.c_int, U32, C.c_int, U32]
    api.GetExtendedTcpTable.restype = U32
    api.GetPerTcpConnectionEStats.argtypes = [C.POINTER(Row), C.c_int, C.c_void_p, U32, U32,
                                             C.c_void_p, U32, U32, C.c_void_p, U32, U32]
    api.GetPerTcpConnectionEStats.restype = U32
    api.SetPerTcpConnectionEStats.argtypes = [C.POINTER(Row), C.c_int, C.c_void_p, U32, U32, U32]
    api.SetPerTcpConnectionEStats.restype = U32
    return api


def connections(api):
    size = U32(0)
    code = api.GetExtendedTcpTable(None, C.byref(size), False, 2, 5, 0)
    if code not in (0, 122):
        raise OSError(code, "GetExtendedTcpTable sizing failed")
    for _ in range(3):
        buf = C.create_string_buffer(size.value)
        code = api.GetExtendedTcpTable(buf, C.byref(size), False, 2, 5, 0)
        if code == 122:
            continue
        if code:
            raise OSError(code, "GetExtendedTcpTable failed")
        count = U32.from_buffer_copy(buf).value
        if 4 + count * C.sizeof(OwnerRow) > len(buf):
            raise ValueError("TCP table size mismatch")
        return [OwnerRow.from_buffer_copy(buf, 4 + i * C.sizeof(OwnerRow)) for i in range(count)]
    raise RuntimeError("TCP table changed repeatedly")


def counters(api, row, kind, cls, denied):
    enabled = U8()
    result = cls()
    code = api.GetPerTcpConnectionEStats(C.byref(row), kind, C.byref(enabled), 0, 1,
                                        None, 0, 0, C.byref(result), 0, C.sizeof(result))
    if code:
        return {"error_code": code, "enabled": False}
    if not enabled.value:
        if kind in denied:
            return {"error_code": denied[kind], "enabled": False}
        yes = U8(1)
        code = api.SetPerTcpConnectionEStats(C.byref(row), kind, C.byref(yes), 0, 1, 0)
        if code:
            denied[kind] = code
            return {"error_code": code, "enabled": False}
        code = api.GetPerTcpConnectionEStats(C.byref(row), kind, C.byref(enabled), 0, 1,
                                            None, 0, 0, C.byref(result), 0, C.sizeof(result))
    if code or not enabled.value:
        return {"error_code": code, "enabled": False}
    return {"enabled": True, **{n: getattr(result, n) for n, _ in cls._fields_}}


class TcpCounters:
    def __init__(self):
        self.api=bind_api();self.denied={};self.retry_at=0
    def collect(self,items):
        if time.monotonic()<self.retry_at:return None,'','当前权限无法读取 QQ 单连接速率，仅监测 QQ 写入 I/O'
        total=0;keys=[]
        for item in items:
            try:
                local=int.from_bytes(socket.inet_aton(item['local']),'little')
                remote=int.from_bytes(socket.inet_aton(item['remote']),'little')
            except OSError:return None,'','当前连接暂不支持独立网络计数，仅监测 QQ 写入 I/O'
            row=Row(5,local,socket.htons(item['local_port']),remote,socket.htons(item['remote_port']))
            value=counters(self.api,row,1,Data,self.denied)
            if not value['enabled']:
                code=value.get('error_code')
                if code==5:self.retry_at=float('inf')
                return None,'',('当前权限无法读取 QQ 单连接速率，仅监测 QQ 写入 I/O' if code==5 else 'QQ 单连接计数暂不可用，仅监测 QQ 写入 I/O')
            total+=value['DataBytesIn']
            keys.append(f"{item['pid']}:{item['local']}:{item['local_port']}:{item['remote']}:{item['remote_port']}")
        return total,'|'.join(sorted(keys)),''



