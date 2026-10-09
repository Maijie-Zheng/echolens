"""label.py 单元测试（fake provider，不联网）。

覆盖：
- 断点续跑去重
- 并发上限校验
- 限速器节奏
- cancel_futures 行为（编排方代改 #2 验证）
- labels 落盘格式（10 维齐全、值域 [0,1]、rpid 无重复）
- LABEL_KEYS 与 questions.json 的 key 集合/顺序一致（v0.2）
"""

from __future__ import annotations

import json
import logging
import threading
import time

import pytest

from echolens.judge import label as label_mod
from echolens.judge.jev import JevError
from echolens.judge.label import (
    LABEL_KEYS,
    MAX_CONCURRENCY,
    RateLimiter,
    load_labeled_rpids,
    load_pending_comments,
    run_label,
)
from echolens.judge.provider import JudgeProvider


class FakeProvider(JudgeProvider):
    """按 rpid 顺序给确定性 Noul；可注入错误/延迟。"""

    def __init__(self, answers=None, fail_on_states=None, delay=0.0):
        self.answers = answers or {k: 0.5 for k in LABEL_KEYS}
        self.fail_on_states = set(fail_on_states or [])
        self.delay = delay
        self.calls = []
        self._lock = threading.Lock()

    def judge(self, state, questions):
        with self._lock:
            self.calls.append(state)
        if self.delay:
            time.sleep(self.delay)
        if state in self.fail_on_states:
            raise RuntimeError(f"boom: {state}")
        return {
            "model": "jev-1.13.0",
            "answers": dict(self.answers),
            "usage": {"input_tokens": 10, "output_tokens": 1},
        }


def _write_comments(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def _comments(n, start=1):
    return [
        {"rpid": start + i, "message": f"评论{i}", "like": i, "ctime": 0, "is_sub": False}
        for i in range(n)
    ]


def _read_labels(path):
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


# ---------- 并发上限 ----------

@pytest.mark.parametrize("bad", [0, -1, 21, 100])
def test_concurrency_out_of_range(tmp_path, bad):
    with pytest.raises(ValueError, match="并发"):
        run_label("BVX", out=str(tmp_path), concurrency=bad, provider=FakeProvider())


def test_concurrency_boundary_ok(tmp_path):
    _write_comments(tmp_path / "comments" / "BVX.jsonl", _comments(1))
    stats = run_label("BVX", out=str(tmp_path), concurrency=MAX_CONCURRENCY,
                      provider=FakeProvider())
    assert stats["api_calls"] == 1


def test_missing_comments_file(tmp_path):
    with pytest.raises(FileNotFoundError, match="评论文件"):
        run_label("BV_NONE", out=str(tmp_path), provider=FakeProvider())


# ---------- 断点续跑去重 ----------

def test_resume_skips_labeled(tmp_path):
    _write_comments(tmp_path / "comments" / "BVX.jsonl", _comments(10))
    # 预置前 4 条已打标
    _write_comments(
        tmp_path / "labels" / "BVX.jsonl",
        [{"rpid": i, "model": "jev-1.13.0", "labels": {k: 0.1 for k in LABEL_KEYS}}
         for i in range(1, 5)],
    )
    provider = FakeProvider()
    stats = run_label("BVX", out=str(tmp_path), provider=provider)
    assert stats["skipped_labeled"] == 4
    assert stats["api_calls"] == 6
    labels = _read_labels(tmp_path / "labels" / "BVX.jsonl")
    rpids = [r["rpid"] for r in labels]
    assert len(rpids) == 10
    assert len(set(rpids)) == 10  # rpid 无重复


def test_rerun_no_duplicate_calls(tmp_path):
    _write_comments(tmp_path / "comments" / "BVX.jsonl", _comments(5))
    provider = FakeProvider()
    run_label("BVX", out=str(tmp_path), provider=provider)
    n_calls = len(provider.calls)
    stats2 = run_label("BVX", out=str(tmp_path), provider=provider)
    assert stats2["api_calls"] == 0  # 全部跳过
    assert len(provider.calls) == n_calls  # 没有重复调用
    assert len(_read_labels(tmp_path / "labels" / "BVX.jsonl")) == 5


def test_limit_respected(tmp_path):
    _write_comments(tmp_path / "comments" / "BVX.jsonl", _comments(10))
    stats = run_label("BVX", out=str(tmp_path), limit=3, provider=FakeProvider())
    assert stats["api_calls"] == 3


# ---------- 落盘格式 ----------

def test_labels_on_disk_format(tmp_path):
    _write_comments(tmp_path / "comments" / "BVX.jsonl", _comments(20))
    run_label("BVX", out=str(tmp_path), provider=FakeProvider())
    labels = _read_labels(tmp_path / "labels" / "BVX.jsonl")
    assert len(labels) == 20
    rpids = set()
    for rec in labels:
        assert set(rec.keys()) == {"rpid", "model", "labels"}
        assert isinstance(rec["rpid"], int)
        assert rec["model"] == "jev-1.13.0"
        assert set(rec["labels"].keys()) == set(LABEL_KEYS)  # 10 维齐全
        for v in rec["labels"].values():
            assert isinstance(v, float)
            assert 0.0 <= v <= 1.0  # 值域
        rpids.add(rec["rpid"])
    assert len(rpids) == 20


def test_missing_answer_dim_raises(tmp_path):
    """provider 少返回一个维度 → 抛 JevError（不静默填 0），消息点名缺失键与 rpid。"""
    _write_comments(tmp_path / "comments" / "BVX.jsonl", _comments(1))
    partial = {k: 0.9 for k in LABEL_KEYS if k != "discussion"}  # 只少 discussion
    provider = FakeProvider(answers=partial)
    with pytest.raises(JevError) as ei:
        run_label("BVX", out=str(tmp_path), provider=provider)
    msg = str(ei.value)
    assert "discussion" in msg   # 点名缺失维度
    assert "rpid" in msg         # 点名 rpid
    # 缺维度时不落盘半成品标签（_ResultWriter 可能已建空文件）
    labels_file = tmp_path / "labels" / "BVX.jsonl"
    assert not labels_file.exists() or _read_labels(labels_file) == []


def test_extra_answer_dim_warns_not_raises(tmp_path, caplog):
    """provider 多返回未知维度 → 不抛错、产生 WARNING，且落盘仍只含 10 维。"""
    _write_comments(tmp_path / "comments" / "BVX.jsonl", _comments(1))
    answers = {k: 0.5 for k in LABEL_KEYS}
    answers["bogus_dim"] = 0.5  # 服务端新增的未知字段
    provider = FakeProvider(answers=answers)
    with caplog.at_level(logging.WARNING, logger="echolens.judge.label"):
        stats = run_label("BVX", out=str(tmp_path), provider=provider)
    assert stats["api_calls"] == 1  # 不中断整轮
    assert "bogus_dim" in caplog.text  # WARNING 列出多出的键
    rec = _read_labels(tmp_path / "labels" / "BVX.jsonl")[0]
    assert set(rec["labels"].keys()) == set(LABEL_KEYS)  # 多出的键被忽略


def test_stats_tokens_and_cost(tmp_path):
    _write_comments(tmp_path / "comments" / "BVX.jsonl", _comments(4))
    stats = run_label("BVX", out=str(tmp_path), provider=FakeProvider())
    assert stats["input_tokens"] == 40
    assert stats["output_tokens"] == 4
    assert stats["est_cost_usd"] == pytest.approx(4 * label_mod.EST_COST_PER_CALL_USD)
    assert stats["elapsed_sec"] >= 0


# ---------- 限速器 ----------

def test_rate_limiter_pacing():
    limiter = RateLimiter(600)  # 0.1s/次
    t0 = time.monotonic()
    for _ in range(4):
        limiter.acquire()
    elapsed = time.monotonic() - t0
    assert elapsed >= 0.3 - 0.05  # 第 2~4 次各等 0.1s


def test_rate_limiter_thread_safe():
    limiter = RateLimiter(6000)  # 0.01s/次
    t0 = time.monotonic()
    threads = [threading.Thread(target=lambda: [limiter.acquire() for _ in range(5)])
               for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    elapsed = time.monotonic() - t0
    assert elapsed >= 20 * 0.01 - 0.05  # 全局 20 次匀速放行，不超速


# ---------- cancel_futures（编排方代改 #2 验证） ----------

def test_worker_failure_cancels_pending(tmp_path):
    """一个 worker 抛错后：异常向上抛；未开始的任务不再调用 API；已落盘可续跑。

    构造：限速器压到极低（60/min → 1/s），worker 1 即失败；若无 cancel_futures，
    后续 19 条会继续调 API 各等 1s 跑完（慢且多调用）；有 cancel_futures 则
    未开始的 future 被取消，API 调用数 << 20 且快速返回。
    """
    n = 20
    _write_comments(tmp_path / "comments" / "BVX.jsonl", _comments(n))
    provider = FakeProvider(fail_on_states={"评论0"})
    t0 = time.monotonic()
    with pytest.raises(RuntimeError, match="boom"):
        run_label("BVX", out=str(tmp_path), provider=provider,
                  concurrency=1, rate_per_min=60)
    elapsed = time.monotonic() - t0
    # 无 cancel 时：剩余 19 条 × 1s ≈ 19s；有 cancel：远小于该值
    assert elapsed < 10, f"未开始的任务未被取消（耗时 {elapsed:.1f}s）"
    assert len(provider.calls) < n, "失败后仍打满了全部 API 调用"
    # 已落盘结果保留，可续跑
    seen = load_labeled_rpids(str(tmp_path / "labels" / "BVX.jsonl"))
    assert seen == {r["rpid"] for r in _read_labels(tmp_path / "labels" / "BVX.jsonl")}


def test_resume_after_failure(tmp_path):
    """失败中断后重跑：不重复打标已落盘的，最终补齐。"""
    n = 6
    _write_comments(tmp_path / "comments" / "BVX.jsonl", _comments(n))
    bad = FakeProvider(fail_on_states={"评论0"})
    with pytest.raises(RuntimeError):
        run_label("BVX", out=str(tmp_path), provider=bad, concurrency=1)
    partial = len(_read_labels(tmp_path / "labels" / "BVX.jsonl"))
    assert partial < n
    good = FakeProvider()
    stats = run_label("BVX", out=str(tmp_path), provider=good, concurrency=1)
    assert stats["api_calls"] == n - partial
    labels = _read_labels(tmp_path / "labels" / "BVX.jsonl")
    assert len(labels) == n
    assert len({r["rpid"] for r in labels}) == n


# ---------- load_* 工具函数 ----------

def test_load_labeled_rpids_tolerates_bad_lines(tmp_path):
    p = tmp_path / "labels" / "BVX.jsonl"
    _write_comments(p, [
        {"rpid": 1, "model": "m", "labels": {}},
        {"rpid": "not-int"},  # rpid 非 int，忽略
    ])
    with open(p, "a", encoding="utf-8") as f:
        f.write("not json\n\n")
    assert load_labeled_rpids(str(p)) == {1}


def test_load_pending_comments_skips_seen_and_bad(tmp_path):
    p = tmp_path / "comments" / "BVX.jsonl"
    _write_comments(p, _comments(5))
    with open(p, "a", encoding="utf-8") as f:
        f.write("broken\n")
    pending = load_pending_comments(str(p), {2, 4})
    assert [c["rpid"] for c in pending] == [1, 3, 5]


# ---------- 维度配置一致性（v0.2：三处必须一致） ----------

def test_label_keys_match_questions_json():
    """LABEL_KEYS 与 questions.json 的 key 集合及顺序必须完全一致。"""
    import os
    qs_path = os.path.join(os.path.dirname(label_mod.__file__), "questions.json")
    with open(qs_path, encoding="utf-8") as f:
        questions = json.load(f)
    assert list(questions.keys()) == list(LABEL_KEYS)
    assert len(LABEL_KEYS) == 10
    assert "discussion" in LABEL_KEYS
    for key, spec in questions.items():
        assert spec["type"] == "noul"
        assert spec["instructions"].strip()


def test_discussion_criteria_encodes_boundaries():
    """discussion 必须带 true/false criteria，且两条边界各有一条**判别性**断言。

    判别性 = 「把该边界描述从 criteria 里删掉 → 该断言必然失败」。
    注意：criteria.true 的**基础句**本身就含「讨论」「内容」，用这些词做断言近乎
    恒真、抓不到回归；真正只在边界半句里出现的词才可用作断言（见变异 A/B 实验）。
    """
    import os
    qs_path = os.path.join(os.path.dirname(label_mod.__file__), "questions.json")
    with open(qs_path, encoding="utf-8") as f:
        spec = json.load(f)["discussion"]
    crit = spec["criteria"]
    assert set(crit.keys()) == {"true", "false"}
    # 边界 1（false 侧）：就内容提问只算 question。基础句（表态/玩梗/闲聊）不含
    # "question"，只有「提问只算 question」那半句含之 → 删该半句则断言失败（变异 B）。
    assert "question" in crit["false"], \
        f"criteria.false 未编码「提问只算 question」边界：{crit['false']!r}"
    # 边界 2（true 侧）：批评 + 讨论「双标」。基础句（讨论/解读内容）不含
    # 「批评/褒贬/吐槽」，只有「即使同时带有褒贬态度…既批评也在讨论内容」那半句含之
    # → 删该半句则断言失败（变异 A）。
    assert any(w in crit["true"] for w in ("批评", "褒贬", "吐槽")), \
        f"criteria.true 未编码「批评/褒贬 + 讨论双标」边界：{crit['true']!r}"


def test_load_questions_rejects_missing_key(tmp_path):
    """questions.json 缺少 LABEL_KEYS 中的键 → 明确报错（不静默）。"""
    import os
    qs_path = os.path.join(os.path.dirname(label_mod.__file__), "questions.json")
    with open(qs_path, encoding="utf-8") as f:
        base = json.load(f)
    missing = {k: v for k, v in base.items() if k != "discussion"}
    p = tmp_path / "questions_missing.json"
    p.write_text(json.dumps(missing, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(ValueError, match="discussion"):
        label_mod.load_questions(str(p))


def test_load_questions_rejects_extra_key(tmp_path):
    """questions.json 多出 LABEL_KEYS 之外的键 → 明确报错（不静默丢弃）。"""
    import os
    qs_path = os.path.join(os.path.dirname(label_mod.__file__), "questions.json")
    with open(qs_path, encoding="utf-8") as f:
        base = json.load(f)
    base["bogus_dim"] = {"type": "noul", "instructions": "x"}
    p = tmp_path / "questions_extra.json"
    p.write_text(json.dumps(base, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(ValueError, match="bogus_dim"):
        label_mod.load_questions(str(p))


def test_load_questions_rejects_bad_type(tmp_path):
    """条目 type 非 'noul' → ValueError，中文点名维度与字段（不等远端 422）。"""
    import os
    qs_path = os.path.join(os.path.dirname(label_mod.__file__), "questions.json")
    with open(qs_path, encoding="utf-8") as f:
        base = json.load(f)
    base["discussion"]["type"] = "choice"  # 非法 type
    p = tmp_path / "questions_bad_type.json"
    p.write_text(json.dumps(base, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(ValueError) as ei:
        label_mod.load_questions(str(p))
    assert "discussion" in str(ei.value)  # 点名维度
    assert "type" in str(ei.value)        # 点名字段


@pytest.mark.parametrize("key,instructions", [
    ("meme", None),      # instructions 缺失
    ("venting", "   "),  # instructions 为空白字符串
])
def test_load_questions_rejects_bad_instructions(tmp_path, key, instructions):
    """instructions 缺失或为空 → ValueError，中文点名维度与字段。"""
    import os
    qs_path = os.path.join(os.path.dirname(label_mod.__file__), "questions.json")
    with open(qs_path, encoding="utf-8") as f:
        base = json.load(f)
    if instructions is None:
        del base[key]["instructions"]
    else:
        base[key]["instructions"] = instructions
    p = tmp_path / f"questions_bad_instr_{key}.json"
    p.write_text(json.dumps(base, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(ValueError) as ei:
        label_mod.load_questions(str(p))
    assert key in str(ei.value)           # 点名维度
    assert "instructions" in str(ei.value)  # 点名字段


def test_load_labeled_rpids_warns_on_missing_dim(tmp_path, caplog):
    """旧标注缺当前维度时打 WARNING，但续跑语义不变（仍按 rpid 跳过）。"""
    p = tmp_path / "labels" / "BVX.jsonl"
    old_keys = [k for k in LABEL_KEYS if k != "discussion"]  # 模拟 v0.1 的 9 键
    _write_comments(p, [
        {"rpid": 1, "model": "m", "labels": {k: 0.1 for k in old_keys}},
        {"rpid": 2, "model": "m", "labels": {k: 0.1 for k in old_keys}},
    ])
    with caplog.at_level(logging.WARNING, logger="echolens.judge.label"):
        seen = load_labeled_rpids(str(p))
    assert seen == {1, 2}                       # 续跑语义不变
    assert "discussion" in caplog.text          # 点名缺失维度
    assert "2" in caplog.text                   # 报出条数
    assert "fresh" in caplog.text               # 给出 --fresh 建议


def test_load_labeled_rpids_warns_on_missing_labels_field(tmp_path, caplog):
    """记录没有 labels 字段 / labels 非 dict 也算需重跑，纳入同一 WARNING。"""
    p = tmp_path / "labels" / "BVX.jsonl"
    _write_comments(p, [
        {"rpid": 1, "model": "m"},                    # 根本没有 labels 字段
        {"rpid": 2, "model": "m", "labels": "oops"},  # labels 不是 dict
    ])
    with caplog.at_level(logging.WARNING, logger="echolens.judge.label"):
        seen = load_labeled_rpids(str(p))
    assert seen == {1, 2}              # 续跑语义不变（仍按 rpid 跳过）
    assert "labels" in caplog.text     # 提示缺 labels 字段
    assert "2" in caplog.text          # 两条都算入计数
    assert "fresh" in caplog.text      # 给出 --fresh 建议


def test_load_labeled_rpids_no_warning_when_complete(tmp_path, caplog):
    """键齐全的旧标注不应产生 WARNING。"""
    p = tmp_path / "labels" / "BVX.jsonl"
    _write_comments(p, [
        {"rpid": 1, "model": "m", "labels": {k: 0.1 for k in LABEL_KEYS}},
    ])
    with caplog.at_level(logging.WARNING, logger="echolens.judge.label"):
        seen = load_labeled_rpids(str(p))
    assert seen == {1}
    assert caplog.text == ""
