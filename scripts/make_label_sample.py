"""M1-T2: 从 3 个视频的评论中分层抽样 200 条，供人工标注（金标准）。

分层规则（任务卡 M1-T2）：
- 视频级配额约 20/70/110（BV1HHeC62EBs / BV1uSFHzbEJu / BV1CRez6BENV）；
- 每个视频内按点赞分档 0 / 1-9 / 10-99 / 100+ 按比例分层；
- 每档内根评论与子评论按源数据比例保留；
- 随机种子固定（--seed，默认 42）保证可复现。

输出 data/labeling/sample.jsonl，每行：
{"rpid","bvid","message","like","ctime","is_sub"}
并打印分层分布表。

只依赖标准库。
"""

from __future__ import annotations

import argparse
import json
import random
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
COMMENTS_DIR = ROOT / "data" / "comments"
OUT_PATH = ROOT / "data" / "labeling" / "sample.jsonl"

# 视频级配额（任务卡给定：按比例约 20/70/110）
VIDEO_QUOTA = {
    "BV1HHeC62EBs": 20,
    "BV1uSFHzbEJu": 70,
    "BV1CRez6BENV": 110,
}

TIERS = ["0", "1-9", "10-99", "100+"]


def like_tier(like: int) -> str:
    if like <= 0:
        return "0"
    if like <= 9:
        return "1-9"
    if like <= 99:
        return "10-99"
    return "100+"


def proportional_split(total: int, weights: list[int]) -> list[int]:
    """最大余数法：把 total 按 weights 比例拆成整数份，和严格等于 total。"""
    s = sum(weights)
    if s == 0 or total <= 0:
        return [0] * len(weights)
    raw = [total * w / s for w in weights]
    base = [int(x) for x in raw]
    # 不能超过各自权重（池子大小）
    base = [min(b, w) for b, w in zip(base, weights)]
    remain = total - sum(base)
    # 按小数部分从大到小补，仍受池子大小约束
    order = sorted(range(len(weights)), key=lambda i: raw[i] - int(raw[i]), reverse=True)
    i = 0
    while remain > 0 and i < len(order) * 2:
        idx = order[i % len(order)]
        if base[idx] < weights[idx]:
            base[idx] += 1
            remain -= 1
        i += 1
    return base


def load_comments(bvid: str) -> list[dict]:
    path = COMMENTS_DIR / f"{bvid}.jsonl"
    comments = []
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                comments.append(json.loads(line))
    return comments


def sample_video(bvid: str, quota: int, rng: random.Random) -> tuple[list[dict], dict]:
    """对单个视频分层抽样，返回 (样本, 分层统计)。"""
    comments = load_comments(bvid)
    # 按 点赞档 × 是否子评论 分桶
    buckets: dict[tuple[str, bool], list[dict]] = {}
    for c in comments:
        key = (like_tier(c["like"]), bool(c["is_sub"]))
        buckets.setdefault(key, []).append(c)

    tier_sizes = [sum(len(buckets.get((t, sub), [])) for sub in (False, True)) for t in TIERS]
    tier_quotas = proportional_split(quota, tier_sizes)

    picked: list[dict] = []
    stats: dict[str, dict] = {}
    for tier, tq in zip(TIERS, tier_quotas):
        roots = buckets.get((tier, False), [])
        subs = buckets.get((tier, True), [])
        rq, sq = proportional_split(tq, [len(roots), len(subs)])
        stats[tier] = {
            "pool_root": len(roots), "pool_sub": len(subs),
            "quota_root": rq, "quota_sub": sq,
        }
        picked.extend(rng.sample(roots, rq))
        picked.extend(rng.sample(subs, sq))

    rng.shuffle(picked)
    return picked, stats


def main() -> None:
    ap = argparse.ArgumentParser(description="M1-T2 分层抽样 200 条评论")
    ap.add_argument("--seed", type=int, default=42, help="随机种子（默认 42）")
    ap.add_argument("--total", type=int, default=200, help="总抽样数（默认 200）")
    args = ap.parse_args()

    rng = random.Random(args.seed)
    total_quota = sum(VIDEO_QUOTA.values())
    quotas = VIDEO_QUOTA
    if args.total != total_quota:
        # 用户改了总数：按原配额比例重新分配
        bvids = list(VIDEO_QUOTA)
        parts = proportional_split(args.total, [VIDEO_QUOTA[b] for b in bvids])
        quotas = dict(zip(bvids, parts))

    all_samples: list[dict] = []
    all_stats: dict[str, dict] = {}
    for bvid, quota in quotas.items():
        picked, stats = sample_video(bvid, quota, rng)
        all_stats[bvid] = {"quota": quota, "tiers": stats}
        for c in picked:
            all_samples.append({
                "rpid": c["rpid"],
                "bvid": bvid,
                "message": c["message"],
                "like": c["like"],
                "ctime": c["ctime"],
                "is_sub": bool(c["is_sub"]),
            })

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with OUT_PATH.open("w", encoding="utf-8") as f:
        for s in all_samples:
            f.write(json.dumps(s, ensure_ascii=False) + "\n")

    # 打印分层分布表
    print(f"种子 seed={args.seed}，抽样总数={len(all_samples)}")
    print(f"输出：{OUT_PATH}\n")
    header = f"{'视频':<14}{'配额':>5}{'点赞档':>7}{'池(根/子)':>14}{'抽(根/子)':>12}{'小计':>6}"
    print(header)
    print("-" * len(header))
    for bvid, info in all_stats.items():
        vroot = vsub = 0
        for tier in TIERS:
            st = info["tiers"][tier]
            pool = f"{st['pool_root']}/{st['pool_sub']}"
            pick = f"{st['quota_root']}/{st['quota_sub']}"
            sub = st["quota_root"] + st["quota_sub"]
            vroot += st["quota_root"]
            vsub += st["quota_sub"]
            print(f"{bvid:<14}{info['quota']:>5}{tier:>7}{pool:>14}{pick:>12}{sub:>6}")
        print(f"{'  小计(根/子)':<14}{'':>5}{'':>7}{'':>14}{f'{vroot}/{vsub}':>12}{vroot + vsub:>6}")
        print()
    n_root = sum(1 for s in all_samples if not s["is_sub"])
    print(f"总计：{len(all_samples)} 条（根 {n_root} / 子 {len(all_samples) - n_root}），覆盖 {len(all_stats)} 个视频")


if __name__ == "__main__":
    main()
