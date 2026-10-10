"""最小化的 Protobuf wire 格式编解码（不依赖 .proto 定义）。"""

from __future__ import annotations

from typing import Iterator

VARINT, I64, LEN, SGROUP, EGROUP, I32 = 0, 1, 2, 3, 4, 5


class ProtobufError(ValueError):
    pass


def read_varint(buf: bytes, pos: int) -> tuple[int, int]:
    result = 0
    shift = 0
    while True:
        if pos >= len(buf):
            raise ProtobufError("varint 截断")
        b = buf[pos]
        pos += 1
        result |= (b & 0x7F) << shift
        if not b & 0x80:
            return result, pos
        shift += 7
        if shift > 70:
            raise ProtobufError("varint 过长")


def iter_fields(buf: bytes) -> Iterator[tuple[int, int, object]]:
    """逐个产出 (字段号, wire 类型, 值)。LEN 类型的值为 bytes。"""
    pos = 0
    n = len(buf)
    while pos < n:
        tag, pos = read_varint(buf, pos)
        field, wt = tag >> 3, tag & 7
        if field == 0:
            raise ProtobufError("字段号为 0")
        if wt == VARINT:
            val, pos = read_varint(buf, pos)
        elif wt == I64:
            if pos + 8 > n:
                raise ProtobufError("64 位字段截断")
            val = int.from_bytes(buf[pos:pos + 8], "little")
            pos += 8
        elif wt == LEN:
            ln, pos = read_varint(buf, pos)
            if pos + ln > n:
                raise ProtobufError("长度字段截断")
            val = bytes(buf[pos:pos + ln])
            pos += ln
        elif wt == I32:
            if pos + 4 > n:
                raise ProtobufError("32 位字段截断")
            val = int.from_bytes(buf[pos:pos + 4], "little")
            pos += 4
        elif wt in (SGROUP, EGROUP):
            # 已废弃的 group 类型：不展开，跳过标记本身
            continue
        else:
            raise ProtobufError(f"未知 wire 类型 {wt}")
        yield field, wt, val


def decode(buf: bytes) -> dict[int, list]:
    out: dict[int, list] = {}
    for field, _wt, val in iter_fields(buf):
        out.setdefault(field, []).append(val)
    return out


def looks_like_message(buf: bytes) -> bool:
    if not buf:
        return False
    try:
        for _ in iter_fields(buf):
            pass
        return True
    except ProtobufError:
        return False


# ---- 编码（测试数据与导出往返使用） ----

def _varint(n: int) -> bytes:
    if n < 0:
        n &= (1 << 64) - 1
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


def encode_field(field: int, value) -> bytes:
    if isinstance(value, bool):
        value = int(value)
    if isinstance(value, int):
        return _varint(field << 3 | VARINT) + _varint(value)
    if isinstance(value, str):
        value = value.encode("utf-8")
    if isinstance(value, (bytes, bytearray)):
        return _varint(field << 3 | LEN) + _varint(len(value)) + bytes(value)
    if isinstance(value, dict):
        return encode_field(field, encode(value))
    raise TypeError(f"不支持的值类型 {type(value)!r}")


def encode(fields: dict) -> bytes:
    """fields: {字段号: 值或值列表}；dict 值会递归编码为子消息。"""
    out = bytearray()
    for field, value in fields.items():
        values = value if isinstance(value, list) else [value]
        for v in values:
            out += encode_field(int(field), v)
    return bytes(out)
