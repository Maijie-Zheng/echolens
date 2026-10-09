"""M1-T2: 人工标注（金标准）与 Jev 打标的一致率统计。

输入：
- data/labeling/human_labels.json  人工标注，结构 {"<rpid>": ["praise","meme",...], ...}
- data/labels/sample_labels.jsonl  Jev 对同一样本的打标（M1-T1 管线产出），每行
    {"rpid": <int>, "labels": {"praise": 0.87, "criticism": 0.03, ...}}
  其中每个维度为 0~1 概率，本脚本按阈值 0.5 转布尔；缺失维度按 0 处理。
- data/labeling/sample.jsonl       抽样样本（取评论正文用于分歧清单）

输出：
- 控制台 + data/labeling/agreement_report.md：
  每个维度的一致率与 Cohen's kappa、宏平均、分歧最大的 20 条清单。

只依赖标准库。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HUMAN_PATH = ROOT / "data" / "labeling" / "human_labels.json"
JEV_PATH = ROOT / "data" / "labels" / "sample_labels.jsonl"
SAMPLE_PATH = ROOT / "data" / "labeling" / "sample.jsonl"
REPORT_PATH = ROOT / "data" / "labeling" / "agreement_report.md"

DIMS = [
    ("praise", "夸"),
    ("criticism", "批"),
    ("suggestion", "建议"),
    ("question", "提问"),
    ("meme", "玩梗"),
    ("venting", "情绪宣泄"),
    ("spam", "水军/广告"),
    ("off_topic", "跑题"),
    ("about_creator", "针对 UP 主本人"),
    ("discussion", "讨论内容"),
]
DIM_KEYS = [k for k, _ in DIMS]
THRESHOLD = 0.5


def fail(msg: str) -> None:
    print(f"错误：{msg}", file=sys.stderr)
    sys.exit(1)


def load_human(path: Path) -> dict[str, set[str]]:
    if not path.exists():
        fail(f"找不到人工标注文件 {path}\n"
             f"请先用 data/labeling/label.html 标注并点「导出 JSON」，"
             f"把下载的 human_labels.json 放到该路径。")
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        fail(f"{path} 不是合法 JSON：{e}")
    if not isinstance(raw, dict):
        fail(f"{path} 顶层应为对象 {{\"<rpid>\": [维度...]}}")
    out: dict[str, set[str]] = {}
    for rpid, tags in raw.items():
        if not isinstance(tags, list):
            fail(f"{path} 中 rpid={rpid} 的值应为数组，实际 {type(tags).__name__}")
        unknown = set(tags) - set(DIM_KEYS)
        if unknown:
            fail(f"{path} 中 rpid={rpid} 含未知维度 {sorted(unknown)}，合法维度为 {DIM_KEYS}")
        out[str(rpid)] = set(tags)
    return out


def load_jev(path: Path) -> dict[str, set[str]]:
    if not path.exists():
        fail(f"找不到 Jev 标注文件 {path}\n"
             f"该文件由 M1-T1 打标管线产出（对 data/labeling/sample.jsonl 逐条打标），"
             f"请先运行打标管线再执行本脚本。")
    out: dict[str, set[str]] = {}
    with path.open(encoding="utf-8") as f:
        for lineno, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError as e:
                fail(f"{path} 第 {lineno} 行不是合法 JSON：{e}")
            if "rpid" not in rec or "labels" not in rec:
                fail(f"{path} 第 {lineno} 行缺少 rpid/labels 字段")
            probs = rec["labels"]
            if not isinstance(probs, dict):
                fail(f"{path} 第 {lineno} 行 labels 应为对象 {{维度: 概率}}")
            tags = set()
            for k, v in probs.items():
                if k not in DIM_KEYS:
                    fail(f"{path} 第 {lineno} 行含未知维度 {k!r}，合法维度为 {DIM_KEYS}")
                try:
                    if float(v) >= THRESHOLD:
                        tags.add(k)
                except (TypeError, ValueError):
                    fail(f"{path} 第 {lineno} 行维度 {k} 的概率值不是数字：{v!r}")
            out[str(rec["rpid"])] = tags
    return out


def kappa(po: float, a_pos: int, b_pos: int, n: int) -> float | None:
    """Cohen's kappa；po=观察一致率，a_pos/b_pos=双方标为正的条数。"""
    if n == 0:
        return None
    pa = a_pos / n
    pb = b_pos / n
    pe = pa * pb + (1 - pa) * (1 - pb)
    if pe >= 1.0:
        return None  # 双方全同向时 kappa 无定义
    return (po - pe) / (1 - pe)


def truncate(s: str, n: int) -> str:
    s = " ".join(s.split())
    return s if len(s) <= n else s[: n - 1] + "…"


def main() -> None:
    ap = argparse.ArgumentParser(description="人工标注 vs Jev 打标 一致率统计")
    ap.add_argument("--human", type=Path, default=HUMAN_PATH, help="human_labels.json 路径")
    ap.add_argument("--jev", type=Path, default=JEV_PATH, help="Jev sample_labels.jsonl 路径")
    ap.add_argument("--sample", type=Path, default=SAMPLE_PATH, help="sample.jsonl 路径（取正文用）")
    ap.add_argument("--out", type=Path, default=REPORT_PATH, help="报告输出路径")
    args = ap.parse_args()

    human = load_human(args.human)
    jev = load_jev(args.jev)

    messages: dict[str, str] = {}
    if args.sample.exists():
        with args.sample.open(encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    rec = json.loads(line)
                    messages[str(rec["rpid"])] = rec.get("message", "")

    common = sorted(set(human) & set(jev), key=int)
    if not common:
        fail("人工标注与 Jev 标注没有任何共同 rpid，请确认两份文件对应同一样本。")
    only_human = set(human) - set(jev)
    only_jev = set(jev) - set(human)
    n = len(common)

    # 每维度统计
    lines = []
    lines.append("# EchoLens M1 一致率报告（人工标注 vs Jev）")
    lines.append("")
    lines.append(f"- 人工标注：{args.human}（{len(human)} 条）")
    lines.append(f"- Jev 标注：{args.jev}（{len(jev)} 条，阈值 {THRESHOLD} 转布尔）")
    lines.append(f"- 共同覆盖：{n} 条")
    if only_human:
        lines.append(f"- 注意：{len(only_human)} 条只有人工标注（Jev 缺失），未计入")
    if only_jev:
        lines.append(f"- 注意：{len(only_jev)} 条只有 Jev 标注（人工缺失），未计入")
    lines.append("")
    lines.append("| 维度 | 中文名 | 一致率 | Cohen's κ | 人工标正 | Jev 标正 |")
    lines.append("|---|---|---|---|---|---|")

    agree_sum = 0.0
    kappas = []
    for key, cn in DIMS:
        agree = sum(1 for r in common if (key in human[r]) == (key in jev[r]))
        h_pos = sum(1 for r in common if key in human[r])
        j_pos = sum(1 for r in common if key in jev[r])
        po = agree / n
        kp = kappa(po, h_pos, j_pos, n)
        agree_sum += po
        if kp is not None:
            kappas.append(kp)
        lines.append(f"| `{key}` | {cn} | {po:.1%} | {('%.3f' % kp) if kp is not None else '—'} | {h_pos} | {j_pos} |")

    macro = agree_sum / len(DIMS)
    macro_k = sum(kappas) / len(kappas) if kappas else None
    lines.append("")
    lines.append(f"**宏平均一致率：{macro:.1%}**（目标 ≥ 80%，PRD §9）"
                 + (f"；宏平均 κ：{macro_k:.3f}" if macro_k is not None else ""))
    lines.append("")

    # 分歧清单：按不一致维度数降序取前 20
    scored = []
    for r in common:
        diff = [k for k in DIM_KEYS if (k in human[r]) != (k in jev[r])]
        if diff:
            scored.append((len(diff), r, diff))
    scored.sort(key=lambda x: (-x[0], int(x[1])))
    lines.append(f"## 分歧清单（共 {len(scored)} 条存在分歧，下列前 20）")
    lines.append("")
    for cnt, r, diff in scored[:20]:
        msg = truncate(messages.get(r, "（正文缺失）"), 80)
        h = sorted(human[r]) or ["（无）"]
        j = sorted(jev[r]) or ["（无）"]
        lines.append(f"- rpid `{r}`（分歧 {cnt} 维）：{msg}")
        lines.append(f"  - 人工：{', '.join(h)}")
        lines.append(f"  - Jev：{', '.join(j)}")
    lines.append("")

    report = "\n".join(lines)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(report, encoding="utf-8")
    print(report)
    print(f"报告已写入 {args.out}")


if __name__ == "__main__":
    main()
