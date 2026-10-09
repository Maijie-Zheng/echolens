"""打标管线（M1-T1 判断层）。

数据流（层间只用 JSONL 解耦，PRD 决策 D6）：
    data/comments/<bvid>.jsonl  -->  [每条一次 Jev 调用，10 个 Noul 并行]
                                -->  data/labels/<bvid>.jsonl

labels JSONL 每行：
    {"rpid": int, "model": "jev-1.13.0", "labels": {10 个 key: 0.0~1.0}}

特性：
- 并发 worker（默认 16，硬上限 20）+ 全局限速（默认 1000 req/min，
  PRD §6.4 预算 1200，留余量）；
- **断点续跑**：启动时读已有 labels，跳过已打标 rpid（D6 结果缓存，
  重复分析不重复花钱）；结果逐条落盘，中断后重跑不重复打标；
- 汇总统计：耗时、API 调用数、token 用量、估算成本。

估算成本口径：PRD §6.4 给出 1000 条约 $0.023，即 EST_COST_PER_CALL_USD。
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Callable, Dict, List, Optional, Set

from .jev import MODEL, JevProvider
from .provider import JudgeProvider
from .sanitize import sanitize_comment

logger = logging.getLogger(__name__)

QUESTIONS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "questions.json")

DEFAULT_CONCURRENCY = 16
MAX_CONCURRENCY = 20          # 硬上限
DEFAULT_RATE_PER_MIN = 1000   # 全局限速（PRD 预算 1200，留余量）
EST_COST_PER_CALL_USD = 0.000023  # PRD §6.4：1000 条约 $0.023

# labels 必须恰好包含这 10 个 key（顺序与 questions.json 一致，PRD §6.1 v0.2）
LABEL_KEYS = (
    "praise", "criticism", "suggestion", "question", "meme",
    "venting", "spam", "off_topic", "about_creator", "discussion",
)


def label_paths(base_dir: str, bvid: str) -> Dict[str, str]:
    """返回判断层的落盘文件路径。"""
    return {
        "comments": os.path.join(base_dir, "comments", f"{bvid}.jsonl"),
        "labels": os.path.join(base_dir, "labels", f"{bvid}.jsonl"),
    }


def load_questions(path: str = QUESTIONS_PATH) -> Dict[str, Any]:
    """加载 10 个分类问题配置（key -> {type, instructions}）。

    强校验：questions.json 的键集合必须与 LABEL_KEYS 完全相等，否则显式抛
    ValueError。理由：不做校验时两侧不一致是**静默**的——
    questions.json 多一个键会被 {k: ... for k in LABEL_KEYS} 静默丢弃；
    LABEL_KEYS 多一个键则该维度恒为 0.0，导致一致率/占比全错。
    校验放在本函数（而非 run_label 启动处），以保证任何调用方都过同一道关。
    """
    with open(path, "r", encoding="utf-8") as f:
        questions = json.load(f)
    expected = set(LABEL_KEYS)
    actual = set(questions)
    if actual != expected:
        missing = sorted(expected - actual)  # 在 LABEL_KEYS 但 questions.json 缺
        extra = sorted(actual - expected)    # 在 questions.json 但 LABEL_KEYS 缺
        problems = []
        if missing:
            problems.append(f"questions.json 缺少 {missing}")
        if extra:
            problems.append(f"questions.json 多出 {extra}")
        raise ValueError(
            "questions.json 的维度键与 LABEL_KEYS 不一致（"
            + "；".join(problems)
            + "），请同步 echolens/judge/label.py 的 LABEL_KEYS 与 questions.json"
        )
    return questions


def load_labeled_rpids(labels_path: str) -> Set[int]:
    """读已有 labels，返回已打标 rpid 集合（断点续跑去重）。

    若发现已落盘记录缺少当前 LABEL_KEYS 中的维度（例如用 v0.1 的 9 键旧结果
    续跑 v0.2），打一条 WARNING 提示，但不改变续跑语义（仍按 rpid 跳过）。
    """
    seen: Set[int] = set()
    if not os.path.exists(labels_path):
        return seen
    missing_keys: Set[str] = set()
    stale_records = 0
    with open(labels_path, "r", encoding="utf-8") as f:
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
                labels = obj.get("labels")
                if isinstance(labels, dict):
                    lacking = set(LABEL_KEYS) - set(labels)
                    if lacking:
                        stale_records += 1
                        missing_keys |= lacking
    if missing_keys:
        logger.warning(
            "检测到 %d 条旧标注缺少维度 [%s]，建议加 --fresh 重跑",
            stale_records, ", ".join(sorted(missing_keys)),
        )
    return seen


def load_pending_comments(
    comments_path: str, seen: Set[int], limit: Optional[int] = None
) -> List[Dict[str, Any]]:
    """读 comments，跳过已打标 rpid，返回待打标评论（可选截断到 limit 条）。"""
    pending: List[Dict[str, Any]] = []
    with open(comments_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except ValueError:
                continue
            rpid = obj.get("rpid")
            if not isinstance(rpid, int) or rpid in seen:
                continue
            pending.append(obj)
            if limit is not None and len(pending) >= limit:
                break
    return pending


class RateLimiter:
    """全局限速器（线程安全）：匀速放行，rate 单位次/分钟。"""

    def __init__(self, per_minute: int):
        self._interval = 60.0 / max(per_minute, 1)
        self._lock = threading.Lock()
        self._next_at = 0.0

    def acquire(self) -> None:
        with self._lock:
            now = time.monotonic()
            wait = self._next_at - now
            self._next_at = max(now, self._next_at) + self._interval
        if wait > 0:
            time.sleep(wait)


class _ResultWriter:
    """labels JSONL 追加写（线程安全，逐条落盘保证中断不丢结果）。"""

    def __init__(self, labels_path: str):
        os.makedirs(os.path.dirname(labels_path), exist_ok=True)
        self._f = open(labels_path, "a", encoding="utf-8")
        self._lock = threading.Lock()

    def write(self, record: Dict[str, Any]) -> None:
        line = json.dumps(record, ensure_ascii=False)
        with self._lock:
            self._f.write(line)
            self._f.write("\n")
            self._f.flush()

    def close(self) -> None:
        with self._lock:
            self._f.close()


def _judge_one(
    provider: JudgeProvider,
    questions: Dict[str, Any],
    limiter: RateLimiter,
    comment: Dict[str, Any],
) -> Dict[str, Any]:
    """对单条评论做一次调用（10 Noul 并行），返回 labels 记录。"""
    state = sanitize_comment(str(comment.get("message") or ""))
    limiter.acquire()
    result = provider.judge(state, questions)
    labels = {k: float(result["answers"].get(k, 0.0)) for k in LABEL_KEYS}
    return {
        "record": {"rpid": comment["rpid"], "model": result["model"], "labels": labels},
        "usage": result["usage"],
    }


def run_label(
    bvid: str,
    out: str = "data",
    limit: Optional[int] = None,
    concurrency: int = DEFAULT_CONCURRENCY,
    rate_per_min: int = DEFAULT_RATE_PER_MIN,
    provider: Optional[JudgeProvider] = None,
    progress: Optional[Callable[[int, int], None]] = None,
) -> Dict[str, Any]:
    """执行打标。返回统计 dict（不打日志，由 CLI 层输出）。

    流程：
        1. 读已有 labels 的 rpid 集合（断点续跑）；
        2. 从 comments 筛出待打标评论（可选 --limit 截断）；
        3. 并发 worker + 全局限速，每条一次调用（10 Noul）；
        4. 结果逐条追加落盘；汇总耗时 / token / 估算成本。
    异常：任一 worker 抛错即取消剩余任务并向上抛（已落盘结果保留，可续跑）。
    """
    if concurrency < 1 or concurrency > MAX_CONCURRENCY:
        raise ValueError(f"并发数须在 1~{MAX_CONCURRENCY} 之间，收到 {concurrency}")
    paths = label_paths(out, bvid)
    if not os.path.exists(paths["comments"]):
        raise FileNotFoundError(
            f"找不到评论文件 {paths['comments']}（先跑 python -m echolens.fetch {bvid}）"
        )

    questions = load_questions()
    provider = provider or JevProvider()

    seen = load_labeled_rpids(paths["labels"])
    pending = load_pending_comments(paths["comments"], seen, limit=limit)
    total = len(pending)

    stats: Dict[str, Any] = {
        "bvid": bvid,
        "skipped_labeled": len(seen),
        "pending": total,
        "api_calls": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "elapsed_sec": 0.0,
        "est_cost_usd": 0.0,
    }
    if total == 0:
        return stats

    limiter = RateLimiter(rate_per_min)
    writer = _ResultWriter(paths["labels"])
    start = time.monotonic()
    done = 0
    try:
        pool = ThreadPoolExecutor(max_workers=concurrency)
        try:
            futures = {
                pool.submit(_judge_one, provider, questions, limiter, c): c
                for c in pending
            }
            for fut in as_completed(futures):
                out_one = fut.result()  # 任一失败即抛；finally 里取消未开始的剩余任务
                writer.write(out_one["record"])
                stats["api_calls"] += 1
                stats["input_tokens"] += out_one["usage"]["input_tokens"]
                stats["output_tokens"] += out_one["usage"]["output_tokens"]
                done += 1
                if progress is not None:
                    progress(done, total)
        finally:
            # cancel_futures：worker 抛错时取消尚未开始的任务，避免继续烧 API 调用
            # （已开始的任务会跑完；已落盘结果保留，可断点续跑）
            pool.shutdown(wait=True, cancel_futures=True)
    finally:
        writer.close()

    stats["elapsed_sec"] = time.monotonic() - start
    stats["est_cost_usd"] = stats["api_calls"] * EST_COST_PER_CALL_USD
    return stats


__all__ = [
    "run_label",
    "load_questions",
    "load_labeled_rpids",
    "load_pending_comments",
    "label_paths",
    "RateLimiter",
    "LABEL_KEYS",
    "DEFAULT_CONCURRENCY",
    "MAX_CONCURRENCY",
    "DEFAULT_RATE_PER_MIN",
    "EST_COST_PER_CALL_USD",
    "MODEL",
]
