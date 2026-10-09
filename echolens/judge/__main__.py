"""命令行入口（M1-T1 判断层）。

用法：
    python -m echolens.judge <BV号> [--limit N] [--concurrency N] [--fresh]

示例：
    python -m echolens.judge BV1HHeC62EBs                  # 全量打标（断点续跑）
    python -m echolens.judge BV1HHeC62EBs --limit 50       # 只打前 50 条（冒烟）
    python -m echolens.judge BV1HHeC62EBs --concurrency 8  # 调低并发
    python -m echolens.judge BV1HHeC62EBs --fresh          # 清空已有 labels 重打

代理说明：
    直连 api.typesafe.ai 可达的环境无需任何设置；被区域封锁的环境（直连返回
    HTTP 451）需先设环境变量再运行，例如：
    HTTPS_PROXY=http://127.0.0.1:10809 python -m echolens.judge <BV号>
    （与 fetch 层相反：B 站接口国内直连，fetch 层不走代理。）

退出码：
    0   成功
    1   打标中断（API / IO 错误，已落盘结果保留，可续跑）
    2   参数错误
    130 用户中断（Ctrl+C，已落盘结果保留，可续跑）
"""

from __future__ import annotations

import argparse
import logging
import os
from typing import List, Optional

from .jev import JevAuthError, JevError
from .label import (
    DEFAULT_CONCURRENCY,
    DEFAULT_RATE_PER_MIN,
    MAX_CONCURRENCY,
    label_paths,
    run_label,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("echolens.judge")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m echolens.judge",
        description="对 B 站评论做 10 维度打标（每条一次 Jev 调用，断点续跑）",
    )
    p.add_argument("bvid", help="BV 号（需先用 python -m echolens.fetch 抓过评论）")
    p.add_argument("--out", default="data", help="数据根目录（默认 data）")
    p.add_argument("--limit", type=int, default=None, help="只打前 N 条待打标评论（冒烟用）")
    p.add_argument(
        "--concurrency", type=int, default=DEFAULT_CONCURRENCY,
        help=f"并发 worker 数（默认 {DEFAULT_CONCURRENCY}，硬上限 {MAX_CONCURRENCY}）",
    )
    p.add_argument(
        "--rate", type=int, default=DEFAULT_RATE_PER_MIN,
        help=f"全局请求限速（次/分钟，默认 {DEFAULT_RATE_PER_MIN}）",
    )
    p.add_argument("--fresh", action="store_true", help="清空已有 labels，从头打标")
    return p


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    bvid = args.bvid.strip()
    if not bvid:
        parser.error("bvid 不能为空")
        return 2
    if args.limit is not None and args.limit < 1:
        parser.error("--limit 必须为正整数")
        return 2

    paths = label_paths(args.out, bvid)
    if args.fresh and os.path.exists(paths["labels"]):
        os.remove(paths["labels"])
        logger.info("--fresh：已删除旧 labels（%s）", paths["labels"])

    def _progress(done: int, total: int) -> None:
        if done == total or done % 10 == 0:
            logger.info("打标进度 %d/%d", done, total)

    logger.info(
        "开始打标：%s（并发=%d，限速=%d/min%s）",
        bvid, args.concurrency, args.rate,
        f"，limit={args.limit}" if args.limit else "",
    )
    try:
        stats = run_label(
            bvid,
            out=args.out,
            limit=args.limit,
            concurrency=args.concurrency,
            rate_per_min=args.rate,
            progress=_progress,
        )
    except KeyboardInterrupt:
        logger.warning("用户中断，已落盘结果保留，可续跑。")
        return 130
    except (JevAuthError, FileNotFoundError, ValueError) as exc:
        logger.error("%s", exc)
        return 2 if isinstance(exc, ValueError) else 1
    except JevError as exc:
        logger.error("打标中断：%s（已落盘结果保留，可续跑）", exc)
        return 1
    except OSError as exc:
        logger.error("IO 错误：%s（已落盘结果保留，可续跑）", exc)
        return 1

    logger.info(
        "完成：本次打标 %d 条（跳过已打标 %d 条），API 调用 %d 次",
        stats["api_calls"], stats["skipped_labeled"], stats["api_calls"],
    )
    logger.info(
        "token 用量：input=%d output=%d；耗时 %.1fs；估算成本 $%.4f",
        stats["input_tokens"], stats["output_tokens"],
        stats["elapsed_sec"], stats["est_cost_usd"],
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
