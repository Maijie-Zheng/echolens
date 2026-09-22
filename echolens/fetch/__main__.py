"""命令行入口（M0-T1 数据层）。

用法：
    python -m echolens.fetch <BV号或视频URL> [--out data] [--no-sub] [--fresh]

示例：
    python -m echolens.fetch BV1HHeC62EBs
    python -m echolens.fetch "https://www.bilibili.com/video/BV1HHeC62EBs" --out data
    python -m echolens.fetch BV1HHeC62EBs --no-sub          # 只抓根评论
    python -m echolens.fetch BV1HHeC62EBs --fresh           # 忽略已有断点，从头抓

退出码：
    0   成功
    1   B 站接口错误（已尽量保存断点）
    2   参数错误
    130 用户中断（Ctrl+C，已保存断点，可续抓）
"""

from __future__ import annotations

import argparse
import logging
import re
import sys
import time
from typing import List, Optional, Set

from .bilibili import BilibiliClient, BilibiliError
from .cache import (
    append_comment,
    comment_paths,
    default_state,
    ensure_dir,
    load_seen_rpids,
    read_state,
    rm_state,
    write_meta,
    write_state,
)
from .models import Comment

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("echolens.fetch")

BV_RE = re.compile(r"(BV[0-9A-Za-z]+)")


def parse_bvid(arg: str) -> str:
    """从裸 BV 号或视频 URL 中提取 bvid；拒绝短链 b23.tv。"""
    arg = (arg or "").strip()
    if "b23.tv" in arg:
        raise ValueError("不支持 b23.tv 短链，请提供完整 BV 号或 bilibili.com 视频 URL")
    m = BV_RE.search(arg)
    if not m:
        raise ValueError(f"无法从输入中解析出 BV 号：{arg!r}")
    return m.group(1)


def _count_stored(jsonl_path: str) -> "tuple[int, int]":
    """统计已落盘评论数，返回 (root_count, sub_count)。"""
    root = sub = 0
    import json as _json

    try:
        with open(jsonl_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = _json.loads(line)
                except ValueError:
                    continue
                if obj.get("is_sub"):
                    sub += 1
                else:
                    root += 1
    except OSError:
        pass
    return root, sub


def run(
    bvid: str,
    out: str = "data",
    no_sub: bool = False,
    fresh: bool = False,
    client: Optional[BilibiliClient] = None,
) -> int:
    """执行抓取。返回退出码（0/1/130）。

    过程：
        1. 取 view 元信息；
        2. 读 state（除非 --fresh）确定续抓起点；
        3. root 阶段：按游标翻页，每页落盘并 checkpoint；
        4. sub 阶段：遍历有楼中楼的根评论，逐条抓取子评论，跳过已见 rpid；
        5. 成功后写 meta、删除 state、打印统计。
    中断（Ctrl+C / BilibiliError）时保存 state 以便续抓。
    """
    client = client or BilibiliClient()
    paths = comment_paths(out, bvid)
    ensure_dir(out)

    try:
        meta = client.get_video_meta(bvid)
    except BilibiliError as exc:
        logger.error("获取视频元信息失败：%s", exc)
        return 1

    state = default_state() if fresh else read_state(paths["state"])
    seen: Set[int] = set() if fresh else load_seen_rpids(paths["jsonl"])

    report_root = report_sub = 0

    def _on_page(next_cursor: int, is_end: bool) -> None:
        nonlocal state
        # 只记录游标位置；root 阶段是否结束由翻页循环的退出决定，
        # phase 推进统一在循环后做（否则 is_end 提前置 done 会跳过楼中楼阶段）
        state["root_cursor"] = None if is_end else next_cursor
        write_state(paths["state"], state)

    try:
        # ---- 根评论阶段 ----
        if state["phase"] in ("root", "done"):
            start_next = int(state["root_cursor"] or 0)
            logger.info("开始抓取根评论（起点 next=%d）…", start_next)
            for c in client.iter_root_comments(
                meta.aid, start_next=start_next, on_page=_on_page
            ):
                if c.rpid in seen:
                    continue
                append_comment(paths["jsonl"], c)
                seen.add(c.rpid)
                report_root += 1
            state["phase"] = "sub" if not no_sub else "done"
            write_state(paths["state"], state)

        # ---- 楼中楼阶段 ----
        if state["phase"] == "sub":
            if no_sub:
                state["phase"] = "done"
            else:
                # 重载已落盘根评论，筛出 rcount>0 的根
                from .cache import load_comments

                roots: List[Comment] = [
                    c for c in load_comments(paths["jsonl"]) if not c.is_sub and c.rcount > 0
                ]
                done: List[int] = list(state.get("sub_done") or [])
                done_set = set(done)
                logger.info("开始抓取楼中楼（根评论数=%d）…", len(roots))
                for r in roots:
                    if r.rpid in done_set:
                        continue
                    for c in client.iter_sub_comments(meta.aid, r.rpid):
                        if c.rpid in seen:
                            continue
                        append_comment(paths["jsonl"], c)
                        seen.add(c.rpid)
                        report_sub += 1
                    done.append(r.rpid)
                    done_set.add(r.rpid)
                    state["sub_done"] = done
                    write_state(paths["state"], state)
                state["phase"] = "done"
            write_state(paths["state"], state)

    except KeyboardInterrupt:
        logger.warning("用户中断，已保存断点（%s），可稍后续抓。", paths["state"])
        write_state(paths["state"], state)
        return 130
    except BilibiliError as exc:
        logger.error("抓取中断：%s（已保存断点，可续抓）", exc)
        write_state(paths["state"], state)
        return 1

    # ---- 完成 ----
    stored_root, stored_sub = _count_stored(paths["jsonl"])
    stats = {
        "root_stored": stored_root,
        "sub_stored": stored_sub,
        "total_stored": stored_root + stored_sub,
        "reply_count_api": meta.reply_count,
        "complete": (stored_root + stored_sub) >= meta.reply_count,
    }
    write_meta(paths["meta"], meta, stats)
    rm_state(paths["state"])

    logger.info(
        "完成：根=%d 子=%d 合计=%d / 接口报告=%d（完整=%s）",
        stored_root,
        stored_sub,
        stored_root + stored_sub,
        meta.reply_count,
        stats["complete"],
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m echolens.fetch",
        description="抓取 B 站视频评论（公开接口，匿名化落盘）",
    )
    p.add_argument("target", help="BV 号或 bilibili.com 视频 URL")
    p.add_argument("--out", default="data", help="落盘根目录（默认 data）")
    p.add_argument("--no-sub", action="store_true", help="只抓根评论，不抓楼中楼")
    p.add_argument("--fresh", action="store_true", help="忽略已有断点，从头抓取")
    return p


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        bvid = parse_bvid(args.target)
    except ValueError as exc:
        parser.error(str(exc))
        return 2
    return run(bvid, out=args.out, no_sub=args.no_sub, fresh=args.fresh)


if __name__ == "__main__":
    raise SystemExit(main())
