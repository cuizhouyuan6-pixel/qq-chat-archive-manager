"""标准化消息模型与元素文本化。

所有来源适配器都输出 NormalizedMessage；元素（elements）是普通 dict，
type 取值：text / face / image / file / voice / video / reply / system / card /
forward / mface / call / unknown。
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field

GROUP = "group"
PRIVATE = "private"
KIND_LABEL = {GROUP: "群聊", PRIVATE: "私聊"}
MEDIA_TYPES = ("image", "file", "voice", "video")


@dataclass
class NormalizedMessage:
    owner: str
    kind: str
    peer: str
    time: int
    sender_uin: str = ""
    sender_uid: str = ""
    sender_name: str = ""
    sender_card: str = ""
    is_self: bool = False
    seq: int | None = None
    msg_random: int | None = None
    source_msg_id: str | None = None
    elements: list[dict] = field(default_factory=list)
    reply_seq: int | None = None
    peer_name: str = ""
    peer_uid: str = ""
    key_hint: str = ""  # Only archive packages carry an already established merge identity.
    exact_hint: bool = False

    @property
    def text(self) -> str:
        return render_text(self.elements)

    @property
    def msg_type(self) -> str:
        return main_type(self.elements)

    @property
    def display_name(self) -> str:
        return self.sender_card or self.sender_name or self.sender_uin

    def to_dict(self) -> dict:
        d = asdict(self)
        d["text"] = self.text
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "NormalizedMessage":
        keys = cls.__dataclass_fields__.keys()
        return cls(**{k: d[k] for k in keys if k in d})


def _face_text(e: dict) -> str:
    t = (e.get("text") or "").strip()
    if t.startswith("/"):
        t = t[1:]
    return f"[{t}]" if t else "[表情]"


def element_text(e: dict) -> str:
    t = e.get("type")
    if t == "text":
        return e.get("text", "")
    if t == "face":
        return _face_text(e)
    if t == "image":
        return "[图片]"
    if t == "file":
        return f"[文件] {e.get('name', '')}".rstrip()
    if t == "voice":
        return "[语音]"
    if t == "video":
        return "[视频]"
    if t == "reply":
        return ""
    if t == "system":
        return e.get("text", "")
    if t == "card":
        return f"[卡片] {e.get('text', '')}".rstrip()
    if t == "forward":
        return f"[聊天记录] {e.get('text', '')}".rstrip()
    if t == "mface":
        return f"[{e.get('text') or '动画表情'}]"
    if t == "call":
        return f"[通话] {e.get('text', '')}".rstrip()
    if e.get("text"):
        return e["text"]
    return "[暂不支持的消息]"


def render_text(elements: list[dict]) -> str:
    return "".join(element_text(e) for e in elements).strip()


def main_type(elements: list[dict]) -> str:
    for e in elements:
        if e.get("type") != "reply":
            return e.get("type", "unknown")
    return "text" if elements else "unknown"


def dumps_elements(elements: list[dict]) -> str:
    return json.dumps(elements, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def loads_elements(s: str | None) -> list[dict]:
    if not s:
        return []
    return json.loads(s)
