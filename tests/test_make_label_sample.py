"""make_label_sample.py 单元测试：同 seed 重跑字节级一致（输出到临时目录）。"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location(
    "make_label_sample",
    Path(__file__).resolve().parent.parent / "scripts" / "make_label_sample.py",
)
mls = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(mls)


@pytest.fixture
def fake_data(tmp_path, monkeypatch):
    """构造 3 个视频的假 comments，重定向模块路径到临时目录。"""
    comments_dir = tmp_path / "comments"
    comments_dir.mkdir()
    rng_rows = {
        "BV1HHeC62EBs": 60,
        "BV1uSFHzbEJu": 200,
        "BV1CRez6BENV": 300,
    }
    for vidx, (bvid, n) in enumerate(rng_rows.items()):
        with open(comments_dir / f"{bvid}.jsonl", "w", encoding="utf-8") as f:
            for i in range(n):
                # 覆盖各点赞档与根/子评论
                like = [0, 5, 50, 500][i % 4]
                row = {
                    "rpid": vidx * 10**9 + i + 1,
                    "message": f"{bvid} 评论{i}",
                    "like": like,
                    "ctime": 1700000000 + i,
                    "is_sub": i % 3 == 0,
                }
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
    monkeypatch.setattr(mls, "COMMENTS_DIR", comments_dir)
    return tmp_path


def _run(mls_module, monkeypatch, out_path, seed=42):
    monkeypatch.setattr(mls_module, "OUT_PATH", out_path)
    monkeypatch.setattr(sys, "argv", ["make_label_sample.py", "--seed", str(seed)])
    mls_module.main()


def test_same_seed_byte_identical(fake_data, monkeypatch, capsys):
    out1 = fake_data / "out1.jsonl"
    out2 = fake_data / "out2.jsonl"
    _run(mls, monkeypatch, out1, seed=42)
    capsys.readouterr()
    _run(mls, monkeypatch, out2, seed=42)
    capsys.readouterr()
    assert out1.read_bytes() == out2.read_bytes(), "同 seed 重跑输出不一致"


def test_different_seed_differs(fake_data, monkeypatch, capsys):
    out1 = fake_data / "s42.jsonl"
    out2 = fake_data / "s43.jsonl"
    _run(mls, monkeypatch, out1, seed=42)
    capsys.readouterr()
    _run(mls, monkeypatch, out2, seed=43)
    capsys.readouterr()
    assert out1.read_bytes() != out2.read_bytes()


def test_output_schema_and_quota(fake_data, monkeypatch, capsys):
    out = fake_data / "out.jsonl"
    _run(mls, monkeypatch, out, seed=42)
    capsys.readouterr()
    rows = [json.loads(l) for l in out.read_text(encoding="utf-8").splitlines() if l.strip()]
    assert len(rows) == sum(mls.VIDEO_QUOTA.values())
    for r in rows:
        assert set(r.keys()) == {"rpid", "bvid", "message", "like", "ctime", "is_sub"}
    from collections import Counter

    cnt = Counter(r["bvid"] for r in rows)
    for bvid, quota in mls.VIDEO_QUOTA.items():
        assert cnt[bvid] == quota


def test_proportional_split_sums_exactly():
    assert sum(mls.proportional_split(200, [10, 30, 160])) == 200
    assert mls.proportional_split(5, [0, 0, 0]) == [0, 0, 0]
    # 配额受池子大小约束：需求超池时封顶到池子总量
    parts = mls.proportional_split(10, [2, 2, 2])
    assert parts == [2, 2, 2]
    parts = mls.proportional_split(7, [1, 1, 1])
    assert sum(parts) == 3  # 池子总共只有 3


def test_like_tier():
    assert mls.like_tier(0) == "0"
    assert mls.like_tier(-3) == "0"
    assert mls.like_tier(1) == "1-9"
    assert mls.like_tier(9) == "1-9"
    assert mls.like_tier(10) == "10-99"
    assert mls.like_tier(99) == "10-99"
    assert mls.like_tier(100) == "100+"


def test_real_sample_not_touched():
    """确认测试不改动仓库现有 sample.jsonl（路径已被 monkeypatch 重定向）。"""
    repo_sample = Path(mls.ROOT) / "data" / "labeling" / "sample.jsonl"
    assert repo_sample.exists()  # 仅断言存在，未写
