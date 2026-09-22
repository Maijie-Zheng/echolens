"""落盘缓存与断点续抓状态管理（M0-T1 数据层）。

落盘目录结构（base_dir 默认 `data`）：
    <base>/comments/<bvid>.jsonl      评论逐行 JSON（append-only）
    <base>/comments/<bvid>.meta.json  视频元信息 + 抓取统计
    <base>/comments/<bvid>.state.json 断点续抓状态（完成后删除）

**匿名化铁律**：`.jsonl` 中每条评论严格只有 models.Comment 的 8 个白名单字段，
绝不写入任何成员身份信息（见 models.py / docs/DATA_FORMAT.md）。

**断点续抓**：
- 通过 `.jsonl` 中已存 rpid 集合去重，避免重复抓取；
- `.state.json` 记录当前阶段（root / sub / done）、根评论游标、已完成楼中楼的
  根 rpid 列表；程序中断（Ctrl+C / 异常）时落盘 state，下次从断点继续。
"""

from __future__ import annotations

import json
import logging
import os
import random
import time
from typing import Any, Dict, List, Set

from .models import Comment, VideoMeta

_logger = logging.getLogger(__name__)


def comment_paths(base_dir: str, bvid: str) -> Dict[str, str]:
    """返回某个视频的三种落盘文件路径。"""
    d = os.path.join(base_dir, "comments")
    return {
        "jsonl": os.path.join(d, f"{bvid}.jsonl"),
        "meta": os.path.join(d, f"{bvid}.meta.json"),
        "state": os.path.join(d, f"{bvid}.state.json"),
    }


def ensure_dir(base_dir: str) -> str:
    """确保 `base_dir/comments` 目录存在，返回该目录路径。"""
    d = os.path.join(base_dir, "comments")
    os.makedirs(d, exist_ok=True)
    return d


def append_comment(jsonl_path: str, comment: Comment) -> None:
    """追加一条评论到 jsonl（append-only，已存在由调用方去重）。"""
    with open(jsonl_path, "a", encoding="utf-8") as f:
        f.write(comment.to_json_line())
        f.write("\n")


def load_seen_rpids(jsonl_path: str) -> Set[int]:
    """读取 jsonl，返回已落盘的 rpid 集合（用于去重 / 续抓跳过）。"""
    seen: Set[int] = set()
    if not os.path.exists(jsonl_path):
        return seen
    with open(jsonl_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except ValueError:
                continue
            rpid = obj.get("rpid")
            if isinstance(rpid, int):
                seen.add(rpid)
    return seen


def load_comments(jsonl_path: str) -> List[Comment]:
    """读取 jsonl，返回 Comment 列表（调试 / 校验用）。"""
    out: List[Comment] = []
    if not os.path.exists(jsonl_path):
        return out
    with open(jsonl_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except ValueError:
                continue
            # Comment 字段与 dict 键一一对应，直接构造（不读取任何身份信息）
            try:
                out.append(Comment(**obj))
            except TypeError:
                continue
    return out


def write_meta(meta_path: str, meta: VideoMeta, stats: Dict[str, Any]) -> None:
    """写入 meta.json：视频元信息 + 抓取统计。"""
    payload: Dict[str, Any] = meta.to_dict()
    payload["stats"] = stats
    with open(meta_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)


# ---- 断点续抓状态 ----
def default_state() -> Dict[str, Any]:
    return {
        "phase": "root",       # root -> 抓根评论；sub -> 抓楼中楼；done -> 完成
        "root_cursor": None,   # 根评论游标 next（None 表示从头）
        "sub_done": [],        # 已完成楼中楼的根 rpid 列表
    }


def read_state(state_path: str) -> Dict[str, Any]:
    if not os.path.exists(state_path):
        return default_state()
    try:
        with open(state_path, "r", encoding="utf-8") as f:
            obj = json.load(f)
        base = default_state()
        base.update(obj)
        return base
    except (ValueError, OSError):
        return default_state()


def write_state(state_path: str, state: Dict[str, Any]) -> None:
    """原子写 state.json，对 Windows 文件占用（WinError 32）做有限重试 + 降级。

    策略：
    1. 先写 `<state>.tmp` 再 `os.replace` 落盘；
    2. 若 `os.replace` 因被杀软 / 文件监视器占用而抛 OSError（PermissionError
       [WinError 32]），重试最多 3 次，间隔 0.2~0.5s；
    3. 仍失败则降级为直接 `open(state_path, "w")` 覆写；
    4. 再失败仅记 warning 不抛异常——断点写失败不该杀死一次跑了很久的抓取。
    """
    payload = json.dumps(state, ensure_ascii=False, indent=2)
    tmp = state_path + ".tmp"
    for attempt in range(3):
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(payload)
            os.replace(tmp, state_path)
            return
        except OSError:
            if attempt < 2:
                time.sleep(random.uniform(0.2, 0.5))
                continue
    # 重试耗尽：降级为直接覆写
    try:
        with open(state_path, "w", encoding="utf-8") as f:
            f.write(payload)
        return
    except OSError as exc:
        _logger.warning("写断点状态失败，已跳过（下次可续抓）：%s", exc)
    finally:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except OSError:
            pass


def rm_state(state_path: str) -> None:
    try:
        os.remove(state_path)
    except OSError:
        pass


def clear_video_files(base_dir: str, bvid: str) -> None:
    """删除某视频已落盘的 jsonl / meta / state 三文件（--fresh 用）。

    任一文件不存在或删除失败均静默忽略，不抛异常。
    """
    for p in comment_paths(base_dir, bvid).values():
        try:
            if os.path.exists(p):
                os.remove(p)
        except OSError:
            pass


__all__ = [
    "comment_paths",
    "ensure_dir",
    "append_comment",
    "load_seen_rpids",
    "load_comments",
    "write_meta",
    "default_state",
    "read_state",
    "write_state",
    "rm_state",
    "clear_video_files",
]
