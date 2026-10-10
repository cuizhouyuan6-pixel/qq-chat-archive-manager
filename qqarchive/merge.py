"""跨来源合并键、内容哈希与疑似重复指纹（方案 5.4）。"""

from __future__ import annotations

import hashlib
import json
import unicodedata

from .models import GROUP, NormalizedMessage


def _norm_text(s: str) -> str:
    return unicodedata.normalize("NFC", s or "").replace("\r\n", "\n").strip()


def merge_key(m: NormalizedMessage, source_kind: str) -> tuple[str, bool]:
    """返回 (跨来源键, 是否可自动合并)。

    - 群聊：g:<群号>:<msgSeq>（群内 msgSeq 全局唯一递增）
    - 私聊：c:<对方QQ>:<msgSeq>:<秒级时间>
    - 无序号：x:<来源类型>:<来源消息ID>，只在同一类来源内去重（重复导入幂等），
      跨来源只标记疑似重复，不自动合并。
    """
    if source_kind == "package" and m.key_hint:
        return m.key_hint, m.exact_hint
    if m.seq is not None and m.seq > 0:
        if m.kind == GROUP:
            return f"g:{m.peer}:{m.seq}", True
        return f"c:{m.peer}:{m.seq}:{int(m.time)}", True
    if m.source_msg_id:
        return f"x:{source_kind}:{m.source_msg_id}", False
    return f"h:{source_kind}:{fingerprint(m)}", False


def element_signature(e: dict) -> list:
    """元素中与“内容是否相同”有关的部分；本地路径、CDN 地址等因来源而异的字段不参与。"""
    t = e.get("type")
    if t in ("text", "system", "card", "forward", "call", "mface"):
        return [t, _norm_text(e.get("text", ""))]
    if t == "face":
        return [t, e.get("id"), _norm_text(e.get("text", ""))]
    if t in ("image", "file", "voice", "video"):
        md5 = (e.get("md5") or "").lower()
        return [t, md5 or (e.get("name") or "").lower()]
    if t == "reply":
        return [t, e.get("seq")]
    return [t or "unknown", e.get("code")]


def identity_conflicts(row, message: NormalizedMessage) -> bool:
    """A sequence is a lookup hint, never sufficient evidence of identity.

    Real QQ group tables uniquely index (conversation, sequence, random).
    Preserve a separate message when available identity fields disagree, even
    when the rendered text is identical. Missing metadata is not a conflict.
    """
    for field, value in (("msg_random", message.msg_random), ("time", message.time)):
        old = row[field]
        if old is not None and value is not None and old != value:
            return True
    if row['sender_uin'] and message.sender_uin:
        return row['sender_uin'] != message.sender_uin
    if row['sender_uid'] and message.sender_uid:
        return row['sender_uid'] != message.sender_uid
    return False


def collision_key(base: str, message: NormalizedMessage) -> str:
    identity = [message.time, message.msg_random, message.sender_uin or message.sender_uid]
    tag = hashlib.sha256(json.dumps(identity, separators=(',', ':'), ensure_ascii=False).encode()).hexdigest()
    return base + ':identity:' + tag


def content_hash(m: NormalizedMessage) -> str:
    payload = [str(m.sender_uin or ""), [element_signature(e) for e in m.elements]]
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def fingerprint(m: NormalizedMessage) -> str:
    raw = f"{int(m.time)}|{m.sender_uin}|{_norm_text(m.text)}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:20]


def diff_summary(old: dict, new: NormalizedMessage) -> str:
    parts = []
    if str(old.get("sender_uin") or "") != str(new.sender_uin or ""):
        parts.append(f"发送者 {old.get('sender_uin')} → {new.sender_uin}")
    if (old.get("text") or "") != new.text:
        a = (old.get("text") or "")[:40]
        b = new.text[:40]
        parts.append(f"内容「{a}」→「{b}」")
    if not parts:
        parts.append("附件或元素不同")
    return "；".join(parts)
