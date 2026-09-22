"""数据层数据结构定义（M0-T1）。

只描述「落盘需要」的字段，严格遵守 PRD §8.1 与 WORKFLOW 铁律 3 的匿名化要求：
- 评论落盘**只含**下方 Comment 列出的 8 个字段；
- **严禁**任何形式的评论者身份信息（用户 ID、昵称、头像、签名、等级等），
  `Comment.from_reply` 构建时主动丢弃身份相关字段；
- rpid 是评论 ID（去重与断点续抓需要），不是用户身份，允许存储。

见 docs/DATA_FORMAT.md 了解落盘格式。
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any, Dict


@dataclass
class VideoMeta:
    """一个视频的元信息，来自 `x/web-interface/view`。

    reply_count 是 B 站 `stat.reply`，口径为「根评论 + 楼中楼（子评论）的总量」
    （线上实测：某视频根 161 + 子 202 = 363 ≈ 报告数 362）。因此「抓取完整性」
    应以 (根 + 子) 的总量对齐 reply_count，而非仅根评论数。
    """

    bvid: str
    aid: int
    title: str
    reply_count: int
    fetched_at: float

    def to_dict(self) -> Dict[str, Any]:
        return {
            "bvid": self.bvid,
            "aid": self.aid,
            "title": self.title,
            "reply_count": self.reply_count,
            "fetched_at": self.fetched_at,
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "VideoMeta":
        return cls(
            bvid=d["bvid"],
            aid=int(d["aid"]),
            title=d.get("title", ""),
            reply_count=int(d.get("reply_count", 0)),
            fetched_at=float(d.get("fetched_at", time.time())),
        )

    def to_json_line(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False)


@dataclass
class Comment:
    """一条评论（根评论或楼中楼子评论）。

    字段白名单（PRD 任务卡 models.py 规定）：
        rpid   评论 ID（去重用，非身份）
        root   所属根评论 rpid；根评论自身为 0
        parent 父评论 rpid（楼中楼直接回复对象）
        like   点赞数
        ctime  评论发布时间（Unix 秒）
        message 评论正文
        rcount 该评论下的楼中楼数量（仅对根评论有意义）
        is_sub 是否为楼中楼子评论
    """

    rpid: int
    root: int
    parent: int
    like: int
    ctime: int
    message: str
    rcount: int
    is_sub: bool

    @classmethod
    def from_reply(cls, reply: Dict[str, Any], is_sub: bool) -> "Comment":
        """从 B 站单条 reply JSON 构造 Comment。

        只抽取白名单字段；身份、等级、头像等信息一律不读取、不落盘。
        """
        content = reply.get("content") or {}
        return cls(
            rpid=int(reply.get("rpid") or reply.get("rpid_str") or 0),
            root=int(reply.get("root") or 0),
            parent=int(reply.get("parent") or 0),
            like=int(reply.get("like") or 0),
            ctime=int(reply.get("ctime") or 0),
            message=content.get("message") or "",
            rcount=int(reply.get("rcount") or 0),
            is_sub=is_sub,
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "rpid": self.rpid,
            "root": self.root,
            "parent": self.parent,
            "like": self.like,
            "ctime": self.ctime,
            "message": self.message,
            "rcount": self.rcount,
            "is_sub": self.is_sub,
        }

    def to_json_line(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False)


__all__ = ["VideoMeta", "Comment"]
