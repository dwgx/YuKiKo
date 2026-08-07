"""Phase 2：平台无关消息组件 + MessageChain（AstrBot 风格子集）。

OneBot V11 段 → 组件（入站）、组件 → OneBot 段（出站）两侧封闭在转换函数里，
内核只见组件。只移植 YuKiKo 用到的子集（Plain/Image/At/AtAll/Reply/Record/Video/
Face/Node），对应 docs/zh-CN/RECONSTRUCTION-BLUEPRINT.md §4.4（3）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class BaseComponent:
    """所有消息组件的基类（类型标记）。"""


@dataclass(frozen=True)
class Plain(BaseComponent):
    text: str = ""


@dataclass(frozen=True)
class Image(BaseComponent):
    file: str = ""
    url: str = ""
    base64: str = ""


@dataclass(frozen=True)
class At(BaseComponent):
    qq: str = ""


@dataclass(frozen=True)
class AtAll(BaseComponent):
    pass


@dataclass(frozen=True)
class Reply(BaseComponent):
    message_id: str = ""


@dataclass(frozen=True)
class Record(BaseComponent):
    file: str = ""
    url: str = ""


@dataclass(frozen=True)
class Video(BaseComponent):
    file: str = ""
    url: str = ""


@dataclass(frozen=True)
class Face(BaseComponent):
    id: str = ""


@dataclass(frozen=True)
class Node(BaseComponent):
    user_id: str = ""
    nickname: str = ""
    content: list[BaseComponent] = field(default_factory=list)


class MessageChain:
    """平台无关的消息链。"""

    def __init__(self, components: list[BaseComponent] | None = None) -> None:
        self.components: list[BaseComponent] = list(components or [])

    def get_plain_text(self) -> str:
        return "".join(c.text for c in self.components if isinstance(c, Plain))

    def squash_plain(self) -> None:
        """合并连续 Plain 为一个 Plain。"""
        merged: list[BaseComponent] = []
        buf: list[str] = []
        for component in self.components:
            if isinstance(component, Plain):
                buf.append(component.text)
            else:
                if buf:
                    merged.append(Plain(text="".join(buf)))
                    buf = []
                merged.append(component)
        if buf:
            merged.append(Plain(text="".join(buf)))
        self.components = merged

    def to_onebot_segments(self) -> list[dict[str, Any]]:
        """出站：MessageChain → OneBot V11 段 JSON 列表。"""
        segments: list[dict[str, Any]] = []
        for component in self.components:
            if isinstance(component, Plain):
                segments.append({"type": "text", "data": {"text": component.text}})
            elif isinstance(component, Image):
                data: dict[str, str] = {}
                if component.base64:
                    data["file"] = f"base64://{component.base64}"
                elif component.file:
                    data["file"] = component.file
                elif component.url:
                    data["url"] = component.url
                segments.append({"type": "image", "data": data})
            elif isinstance(component, At):
                segments.append({"type": "at", "data": {"qq": component.qq}})
            elif isinstance(component, AtAll):
                segments.append({"type": "at", "data": {"qq": "all"}})
            elif isinstance(component, Reply):
                segments.append({"type": "reply", "data": {"id": component.message_id}})
            elif isinstance(component, Record):
                data = {"file": component.file or component.url} if (component.file or component.url) else {}
                segments.append({"type": "record", "data": data})
            elif isinstance(component, Video):
                data = {"file": component.file or component.url} if (component.file or component.url) else {}
                segments.append({"type": "video", "data": data})
            elif isinstance(component, Face):
                segments.append({"type": "face", "data": {"id": component.id}})
            elif isinstance(component, Node):
                segments.append(
                    {
                        "type": "node",
                        "data": {
                            "user_id": component.user_id,
                            "nickname": component.nickname,
                            "content": component.content,
                        },
                    }
                )
        return segments

    @classmethod
    def from_onebot_segments(cls, segments: list[dict[str, Any]] | None) -> MessageChain:
        """入站：OneBot V11 段 JSON 列表 → MessageChain。"""
        chain = cls()
        for segment in segments or []:
            if not isinstance(segment, dict):
                continue
            seg_type = segment.get("type")
            data = segment.get("data") if isinstance(segment.get("data"), dict) else {}
            if seg_type == "text":
                chain.components.append(Plain(text=str(data.get("text", ""))))
            elif seg_type == "image":
                chain.components.append(
                    Image(file=str(data.get("file", "")), url=str(data.get("url", "")))
                )
            elif seg_type == "at":
                qq = str(data.get("qq", ""))
                if qq == "all":
                    chain.components.append(AtAll())
                else:
                    chain.components.append(At(qq=qq))
            elif seg_type == "reply":
                chain.components.append(Reply(message_id=str(data.get("id", ""))))
            elif seg_type == "record":
                chain.components.append(
                    Record(file=str(data.get("file", "")), url=str(data.get("url", "")))
                )
            elif seg_type == "video":
                chain.components.append(
                    Video(file=str(data.get("file", "")), url=str(data.get("url", "")))
                )
            elif seg_type == "face":
                chain.components.append(Face(id=str(data.get("id", ""))))
        return chain
