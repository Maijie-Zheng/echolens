"""从全量打标结果中提取 200 条样本的 Jev 标注（agreement.py 的输入）。

样本（data/labeling/sample.jsonl）来自 3 个已全量打标的视频，
因此无需再调 API——直接按 rpid 过滤 data/labels/<bvid>.jsonl 即可。

用法：
    python scripts/make_sample_labels.py

输出：data/labels/sample_labels.jsonl（每行与全量 labels 同构）
"""

from __future__ import annotations

import json
import os
import sys

DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
SAMPLE = os.path.join(DATA_DIR, "labeling", "sample.jsonl")
LABELS_DIR = os.path.join(DATA_DIR, "labels")
OUT = os.path.join(LABELS_DIR, "sample_labels.jsonl")


def main() -> int:
    if not os.path.exists(SAMPLE):
        print(f"找不到样本文件 {SAMPLE}（先跑 scripts/make_label_sample.py）", file=sys.stderr)
        return 1

    wanted = {}  # rpid -> bvid
    with open(SAMPLE, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                obj = json.loads(line)
                wanted[obj["rpid"]] = obj["bvid"]

    by_bvid: dict[str, set[int]] = {}
    for rpid, bvid in wanted.items():
        by_bvid.setdefault(bvid, set()).add(rpid)

    found: dict[int, dict] = {}
    for bvid, rpids in by_bvid.items():
        path = os.path.join(LABELS_DIR, f"{bvid}.jsonl")
        if not os.path.exists(path):
            print(f"缺少打标文件 {path}（先跑 python -m echolens.judge {bvid}）", file=sys.stderr)
            return 1
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                obj = json.loads(line)
                if obj.get("rpid") in rpids:
                    found[obj["rpid"]] = obj

    missing = [r for r in wanted if r not in found]
    with open(OUT, "w", encoding="utf-8") as f:
        for rpid in wanted:  # 保持样本顺序
            if rpid in found:
                f.write(json.dumps(found[rpid], ensure_ascii=False) + "\n")

    print(f"样本 {len(wanted)} 条，命中 {len(found)} 条，缺失 {len(missing)} 条 -> {OUT}")
    if missing:
        print(f"缺失 rpid 示例：{missing[:5]}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
