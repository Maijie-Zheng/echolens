"""agreement.py 单元测试：合成数据端到端、阈值 0.5、一致率、缺失文件报错。"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location(
    "agreement",
    Path(__file__).resolve().parent.parent / "scripts" / "agreement.py",
)
agr = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(agr)

DIM_KEYS = agr.DIM_KEYS


def _write_json(path, obj):
    path.write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")


def _write_jsonl(path, rows):
    with open(path, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def _run(monkeypatch, human, jev, sample, out):
    monkeypatch.setattr(
        sys, "argv",
        ["agreement.py", "--human", str(human), "--jev", str(jev),
         "--sample", str(sample), "--out", str(out)],
    )
    agr.main()


@pytest.fixture
def synth(tmp_path):
    """合成 4 条数据：rpid 1 完全一致，2 完全不一致，3 部分一致，4 只有人工。"""
    human = {
        "1": ["praise"],
        "2": ["praise"],
        "3": ["praise", "meme"],
        "4": ["criticism"],
    }
    jev_rows = [
        {"rpid": 1, "labels": {**{k: 0.0 for k in DIM_KEYS}, "praise": 0.9}},
        {"rpid": 2, "labels": {k: 0.0 for k in DIM_KEYS}},                     # praise 漏标
        {"rpid": 3, "labels": {**{k: 0.0 for k in DIM_KEYS}, "praise": 0.6}},  # meme 漏标
    ]
    sample_rows = [
        {"rpid": 1, "message": "好看"}, {"rpid": 2, "message": "一般"},
        {"rpid": 3, "message": "哈哈哈"}, {"rpid": 4, "message": "差"},
    ]
    h = tmp_path / "human.json"
    j = tmp_path / "jev.jsonl"
    s = tmp_path / "sample.jsonl"
    o = tmp_path / "report.md"
    _write_json(h, human)
    _write_jsonl(j, jev_rows)
    _write_jsonl(s, sample_rows)
    return h, j, s, o


def test_end_to_end_report(synth, monkeypatch, capsys):
    h, j, s, o = synth
    _run(monkeypatch, h, j, s, o)
    assert o.exists()
    report = o.read_text(encoding="utf-8")
    # 共同覆盖 3 条（rpid 4 只有人工，未计入）
    assert "共同覆盖：3 条" in report
    assert "1 条只有人工标注" in report
    # praise：rpid1 一致，rpid2 不一致，rpid3 一致 → 2/3
    assert "| `praise` | 夸 | 66.7%" in report
    # meme：rpid3 不一致，其余一致 → 2/3；其余 7 维全一致 100%
    # 宏平均 = (7*1 + 2/3 + 2/3) / 9 ≈ 92.6%
    assert "宏平均一致率：92.6%" in report
    # 分歧清单含 rpid 2 和 3
    assert "rpid `2`" in report and "rpid `3`" in report


def test_threshold_boundary(tmp_path, monkeypatch):
    """0.5 恰好算正（>= 阈值），0.499 算负。"""
    h = tmp_path / "h.json"
    j = tmp_path / "j.jsonl"
    o = tmp_path / "r.md"
    _write_json(h, {"1": ["praise"], "2": ["praise"]})
    _write_jsonl(j, [
        {"rpid": 1, "labels": {k: 0.0 for k in DIM_KEYS} | {"praise": 0.5}},
        {"rpid": 2, "labels": {k: 0.0 for k in DIM_KEYS} | {"praise": 0.499}},
    ])
    _run(monkeypatch, h, j, tmp_path / "none.jsonl", o)
    report = o.read_text(encoding="utf-8")
    # rpid1 一致，rpid2 不一致 → praise 一致率 50%
    assert "| `praise` | 夸 | 50.0%" in report


def test_missing_human_file(tmp_path, monkeypatch, capsys):
    j = tmp_path / "j.jsonl"
    _write_jsonl(j, [])
    with pytest.raises(SystemExit) as ei:
        _run(monkeypatch, tmp_path / "nope.json", j, tmp_path / "s", tmp_path / "o")
    assert ei.value.code == 1
    assert "找不到人工标注文件" in capsys.readouterr().err


def test_missing_jev_file(tmp_path, monkeypatch, capsys):
    h = tmp_path / "h.json"
    _write_json(h, {"1": []})
    with pytest.raises(SystemExit) as ei:
        _run(monkeypatch, h, tmp_path / "nope.jsonl", tmp_path / "s", tmp_path / "o")
    assert ei.value.code == 1
    assert "找不到 Jev 标注文件" in capsys.readouterr().err


def test_unknown_dim_rejected(tmp_path, monkeypatch, capsys):
    h = tmp_path / "h.json"
    _write_json(h, {"1": ["not_a_dim"]})
    with pytest.raises(SystemExit):
        _run(monkeypatch, h, tmp_path / "j", tmp_path / "s", tmp_path / "o")
    assert "未知维度" in capsys.readouterr().err


def test_no_common_rpid(tmp_path, monkeypatch, capsys):
    h = tmp_path / "h.json"
    j = tmp_path / "j.jsonl"
    _write_json(h, {"1": ["praise"]})
    _write_jsonl(j, [{"rpid": 2, "labels": {k: 0.0 for k in DIM_KEYS}}])
    with pytest.raises(SystemExit):
        _run(monkeypatch, h, j, tmp_path / "s", tmp_path / "o")
    assert "没有任何共同 rpid" in capsys.readouterr().err


def test_kappa_math():
    # 双方各一半标正且完全一致：po=1, pe=0.5 → kappa=1
    assert agr.kappa(1.0, 5, 5, 10) == pytest.approx(1.0)
    # 全同向（都 0 正）→ 无定义
    assert agr.kappa(1.0, 0, 0, 10) is None
    assert agr.kappa(0.5, 0, 0, 0) is None


def test_truncate():
    assert agr.truncate("短", 80) == "短"
    long = "x" * 100
    out = agr.truncate(long, 80)
    assert len(out) == 80 and out.endswith("…")
    assert agr.truncate("a\n  b\tc", 80) == "a b c"
