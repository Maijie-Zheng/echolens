#!/usr/bin/env python
"""构建人工标注界面 data/labeling/label.html（M1-T2）。

数据流：
    tools/labeling/label_template.html  （模板，入库）
    tools/labeling/dims_ui.json         （10 维度 UI 短名/定义，入库）
    data/labeling/sample.jsonl          （本地样本，不入库）
                --> data/labeling/label.html （本地生成物，不入库）

设计：仓库公开（含匿名化要求 PRD §8.1），而 label.html 内嵌真实评论原文，
**不得入库**；故拆分「模板入库、数据本地生成」。维度顺序的唯一来源是
``echolens.judge.label.LABEL_KEYS``，本脚本据其重排 ``dims_ui.json``。

用法：
    python tools/build_label.py
    python tools/build_label.py --sample data/labeling/sample.jsonl \
                                --out data/labeling/label.html

退出码：0 成功；非 0 表示配置/样本有误（中文错误信息打到 stderr）。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import unicodedata
from typing import Dict, List

# `python tools/build_label.py` 时 sys.path[0] 是 tools/，需补上仓库根才能 import echolens
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from echolens.judge.label import LABEL_KEYS, load_questions  # noqa: E402

_HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_TEMPLATE = os.path.join(_HERE, "labeling", "label_template.html")
DEFAULT_DIMS = os.path.join(_HERE, "labeling", "dims_ui.json")
DEFAULT_SAMPLE = os.path.join("data", "labeling", "sample.jsonl")
DEFAULT_OUT = os.path.join("data", "labeling", "label.html")

DATA_PLACEHOLDER = "__DATA_JSON__"
DIMS_PLACEHOLDER = "__DIMS_JSON__"

# name 段在对齐时按显示宽度补到该列宽（>= 此宽度则只留 1 个空格）
_NAME_FIELD_WIDTH = 12
# name 关键字起始列（上面各行 key 段据此对齐）
_NAME_COL = 25


def _disp_width(text: str) -> int:
    """按东亚宽字符（全角）算 2 列、其余 1 列，用于 DIMS 排版对齐。"""
    return sum(
        2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1 for ch in text
    )


def _escape(s: str) -> str:
    """转义 </，避免内联 JSON 文本提前闭合 <script> 标签。"""
    return s.replace("</", "<\\/")


def _load_json(path: str, what: str) -> object:
    if not os.path.exists(path):
        raise SystemExit(f"错误：找不到{what} {path}")
    with open(path, "r", encoding="utf-8") as f:
        try:
            return json.load(f)
        except ValueError as exc:
            raise SystemExit(f"错误：{what} {path} 不是合法 JSON：{exc}")


def _read_sample(path: str) -> List[Dict[str, object]]:
    if not os.path.exists(path):
        raise SystemExit(
            f"错误：找不到样本文件 {path}"
            "（先用 scripts/make_label_sample.py 生成 sample.jsonl）"
        )
    rows: List[Dict[str, object]] = []
    with open(path, "r", encoding="utf-8") as f:
        for lineno, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except ValueError as exc:
                raise SystemExit(f"错误：{path} 第 {lineno} 行不是合法 JSON：{exc}")
    if not rows:
        raise SystemExit(f"错误：样本文件 {path} 为空")
    return rows


def _validate_dims(dims_ui: object, dims_path: str) -> Dict[str, Dict[str, str]]:
    """校验 dims_ui.json 键集合与 LABEL_KEYS 完全相等，且每项形如 {name, def}。"""
    if not isinstance(dims_ui, dict):
        raise SystemExit(f"错误：{dims_path} 顶层必须是 JSON 对象（key -> {{name, def}}）")
    actual = set(dims_ui.keys())
    expected = set(LABEL_KEYS)
    if actual != expected:
        missing = [k for k in LABEL_KEYS if k not in actual]  # dims_ui 缺
        extra = sorted(actual - expected)                     # dims_ui 多
        problems = []
        if missing:
            problems.append(f"缺少 {missing}")
        if extra:
            problems.append(f"多出 {extra}")
        raise SystemExit(
            f"错误：{dims_path} 的维度键与 LABEL_KEYS 不一致（"
            + "；".join(problems)
            + "），请补齐/删除后重试"
        )
    for key in LABEL_KEYS:
        item = dims_ui[key]
        if (
            not isinstance(item, dict)
            or not isinstance(item.get("name"), str)
            or not isinstance(item.get("def"), str)
        ):
            raise SystemExit(
                f"错误：{dims_path} 的 {key} 必须形如 "
                '{"name": "...", "def": "..."}（name/def 均为字符串）'
            )
    return dims_ui  # type: ignore[return-value]


def _render_dims_block(dims_ui: Dict[str, Dict[str, str]], newline: str) -> str:
    """按 LABEL_KEYS 顺序渲染 DIMS 数组文本（多行、逐对象一行）。"""
    obj_lines: List[str] = []
    keys = list(LABEL_KEYS)
    for i, key in enumerate(keys):
        name = _escape(dims_ui[key]["name"])
        defn = _escape(dims_ui[key]["def"])
        prefix_len = 2 + len('{key: "') + len(key) + len('",')
        pad_key = max(1, _NAME_COL - prefix_len)
        pad_name = max(1, _NAME_FIELD_WIDTH - _disp_width(name))
        line = (
            f'  {{key: "{key}",'
            + " " * pad_key
            + f'name: "{name}",'
            + " " * pad_name
            + f'def: "{defn}"}}'
        )
        if i != len(keys) - 1:
            line += ","
        obj_lines.append(line)
    return "[" + newline + newline.join(obj_lines) + newline + "]"


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python tools/build_label.py",
        description="由模板 + 维度表 + 本地样本生成 data/labeling/label.html",
    )
    p.add_argument("--sample", default=DEFAULT_SAMPLE, help="样本 JSONL（默认 data/labeling/sample.jsonl）")
    p.add_argument("--out", default=DEFAULT_OUT, help="输出 HTML（默认 data/labeling/label.html）")
    p.add_argument("--template", default=DEFAULT_TEMPLATE, help="模板 HTML（默认 tools/labeling/label_template.html）")
    p.add_argument("--dims", default=DEFAULT_DIMS, help="维度 UI 表（默认 tools/labeling/dims_ui.json）")
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)

    # 1) 维度 UI 表：键集合必须与 LABEL_KEYS 完全一致
    dims_ui = _validate_dims(_load_json(args.dims, "维度 UI 表"), args.dims)

    # 2) questions.json：键集合必须与 LABEL_KEYS 完全一致（复用必修 4 的强校验）
    try:
        load_questions()
    except ValueError as exc:
        raise SystemExit(f"错误：{exc}")

    # 3) 本地样本
    rows = _read_sample(args.sample)

    # 4) 模板
    if not os.path.exists(args.template):
        raise SystemExit(f"错误：找不到模板 {args.template}")
    with open(args.template, "r", encoding="utf-8", newline="") as f:
        template = f.read()
    for ph, label in ((DATA_PLACEHOLDER, "DATA"), (DIMS_PLACEHOLDER, "DIMS")):
        if ph not in template:
            raise SystemExit(f"错误：模板 {args.template} 缺少占位符 {ph}（{label}）")
    newline = "\r\n" if "\r\n" in template else "\n"

    # 5) 序列化并替换
    data_json = _escape(json.dumps(rows, ensure_ascii=False))
    dims_block = _render_dims_block(dims_ui, newline)
    html = template.replace(DATA_PLACEHOLDER, data_json).replace(DIMS_PLACEHOLDER, dims_block)

    out_dir = os.path.dirname(os.path.abspath(args.out))
    os.makedirs(out_dir, exist_ok=True)
    with open(args.out, "w", encoding="utf-8", newline="") as f:
        f.write(html)

    print(
        f"已生成 {args.out}（{len(rows)} 条评论，{len(LABEL_KEYS)} 个维度）"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
