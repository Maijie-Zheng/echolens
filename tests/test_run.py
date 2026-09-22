"""__main__.run() 端到端单元测试（全 mock，注入假 client）。

覆盖：根+子全流程落盘、--no-sub、中断后续抓不重复、连续重跑幂等。
"""

from __future__ import annotations

import json

import pytest

from echolens.fetch.__main__ import run
from echolens.fetch.bilibili import BilibiliError
from echolens.fetch.models import Comment, VideoMeta


class FakeClient:
    """模拟 BilibiliClient，内部自管分页游标（next 当作页索引）。

    - roots: 根评论列表
    - subs_by_root: {根rpid: 子评论列表}
    - fail_at: 若设置，在迭代到该 rpid 时抛 BilibiliError（模拟中断）
    - page_size: 每页根评论数（用于模拟多页与续抓起点）
    """

    def __init__(self, roots, subs_by_root, fail_at=None, page_size=2):
        self.roots = roots
        self.subs_by_root = subs_by_root
        self.fail_at = fail_at
        self.page_size = page_size
        total = len(roots) + sum(len(v) for v in subs_by_root.values())
        self.meta = VideoMeta("BV1", 1, "T", total, 0.0)
        self.root_starts: list = []

    def get_video_meta(self, bvid):
        return self.meta

    def iter_root_comments(self, aid, start_next=0, on_page=None):
        self.root_starts.append(int(start_next))
        nxt = int(start_next)
        while True:
            start = nxt
            chunk = self.roots[start : start + self.page_size]
            for c in chunk:
                yield c
                if self.fail_at is not None and c.rpid == self.fail_at:
                    raise BilibiliError("interrupted mid-page")
            is_end = (start + self.page_size) >= len(self.roots)
            nxt = 0 if is_end else start + self.page_size
            if on_page:
                on_page(nxt, is_end)
            if is_end:
                break

    def iter_sub_comments(self, aid, root_rpid):
        for c in self.subs_by_root.get(root_rpid, []):
            yield c


WHITELIST = {
    "rpid",
    "root",
    "parent",
    "like",
    "ctime",
    "message",
    "rcount",
    "is_sub",
}


def _read_jsonl(path):
    out = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                out.append(json.loads(line))
    return out


def test_run_full_root_and_sub(tmp_path):
    roots = [Comment(i, 0, 0, 1, 1, f"r{i}", (2 if i in (1, 3) else 0), False) for i in (1, 2, 3, 4)]
    subs = {
        1: [Comment(101, 1, 1, 1, 1, "s1", 0, True), Comment(102, 1, 1, 1, 1, "s2", 0, True)],
        3: [Comment(103, 3, 3, 1, 1, "s3", 0, True)],
    }
    client = FakeClient(roots, subs, page_size=2)
    rc = run("BV1", out=str(tmp_path), client=client)
    assert rc == 0

    jsonl = tmp_path / "comments" / "BV1.jsonl"
    lines = _read_jsonl(jsonl)
    assert len(lines) == 7  # 4 根 + 3 子
    rpids = [x["rpid"] for x in lines]
    assert len(rpids) == len(set(rpids))  # 无重复
    for x in lines:
        assert set(x.keys()) == WHITELIST

    meta = json.loads((tmp_path / "comments" / "BV1.meta.json").read_text(encoding="utf-8"))
    assert meta["stats"]["total_stored"] == 7
    assert meta["stats"]["root_stored"] == 4
    assert meta["stats"]["sub_stored"] == 3
    assert meta["stats"]["reply_count_api"] == 7
    assert meta["stats"]["complete"] is True
    assert not (tmp_path / "comments" / "BV1.state.json").exists()


def test_run_no_sub(tmp_path):
    roots = [Comment(i, 0, 0, 1, 1, f"r{i}", 2, False) for i in (1, 2, 3)]
    subs = {1: [Comment(101, 1, 1, 1, 1, "s", 0, True)]}
    client = FakeClient(roots, subs)
    rc = run("BV1", out=str(tmp_path), no_sub=True, client=client)
    assert rc == 0

    lines = _read_jsonl(tmp_path / "comments" / "BV1.jsonl")
    assert len(lines) == 3  # 仅根评论
    assert all(not x["is_sub"] for x in lines)


def test_run_interrupt_then_resume_no_dup(tmp_path):
    """中断（BilibiliError）后重跑：最终 rpid 零重复且总量达标，且不重抓已完成的页。"""
    roots = [Comment(i, 0, 0, 1, 1, f"r{i}", 0, False) for i in (1, 2, 3, 4)]
    subs = {}

    # 第一次：在 page1 的根 rpid=3 处中断
    client1 = FakeClient(roots, subs, fail_at=3, page_size=2)
    rc1 = run("BV1", out=str(tmp_path), client=client1)
    assert rc1 == 1  # 中断返回 1
    partial = _read_jsonl(tmp_path / "comments" / "BV1.jsonl")
    assert {x["rpid"] for x in partial} == {1, 2, 3}  # r3 已落盘（中断前产出）
    assert (tmp_path / "comments" / "BV1.state.json").exists()

    # 第二次：从断点续抓，不中断
    client2 = FakeClient(roots, subs, page_size=2)
    rc2 = run("BV1", out=str(tmp_path), client=client2)
    assert rc2 == 0

    final = _read_jsonl(tmp_path / "comments" / "BV1.jsonl")
    rpids = [x["rpid"] for x in final]
    assert len(rpids) == len(set(rpids)) == 4
    # 续抓起点为上一页游标 2，未重抓 page0（起始 0）
    assert client2.root_starts == [2]


def test_run_idempotent_on_rerun(tmp_path):
    """首次完整跑完后再次运行（state 已删除）：靠 jsonl 去重，不增不减、不重复。"""
    roots = [Comment(i, 0, 0, 1, 1, f"r{i}", (2 if i in (1, 3) else 0), False) for i in (1, 2, 3, 4)]
    subs = {
        1: [Comment(101, 1, 1, 1, 1, "s1", 0, True), Comment(102, 1, 1, 1, 1, "s2", 0, True)],
        3: [Comment(103, 3, 3, 1, 1, "s3", 0, True)],
    }
    assert run("BV1", out=str(tmp_path), client=FakeClient(roots, subs, page_size=2)) == 0
    # 再次运行（续抓，无新数据）
    assert run("BV1", out=str(tmp_path), client=FakeClient(roots, subs, page_size=2)) == 0

    lines = _read_jsonl(tmp_path / "comments" / "BV1.jsonl")
    assert len(lines) == 7
    rpids = [x["rpid"] for x in lines]
    assert len(rpids) == len(set(rpids))


def test_run_keyboard_interrupt_saves_state(tmp_path):
    """Ctrl+C（KeyboardInterrupt）应保存断点并返回 130。"""

    class KeyboardClient(FakeClient):
        def iter_root_comments(self, aid, start_next=0, on_page=None):
            for c in self.roots[start_next:]:
                yield c
                raise KeyboardInterrupt()

    roots = [Comment(i, 0, 0, 1, 1, f"r{i}", 0, False) for i in (1, 2, 3, 4)]
    rc = run("BV1", out=str(tmp_path), client=KeyboardClient(roots, {}))
    assert rc == 130
    assert (tmp_path / "comments" / "BV1.state.json").exists()


def test_run_fresh_resets_cursor(tmp_path):
    """--fresh 应忽略已有断点，从根评论第一页重新抓取（不依赖旧 state）。"""
    roots = [Comment(i, 0, 0, 1, 1, f"r{i}", 0, False) for i in (1, 2, 3, 4)]

    # 先写一份假 state（模拟已完成）
    from echolens.fetch import cache

    paths = cache.comment_paths(str(tmp_path), "BV1")
    cache.ensure_dir(str(tmp_path))
    cache.write_state(paths["state"], {"phase": "done", "root_cursor": None, "sub_done": [1]})

    client = FakeClient(roots, {}, page_size=2)
    rc = run("BV1", out=str(tmp_path), fresh=True, client=client)
    assert rc == 0
    # fresh 后从 start_next=0 重新翻页，且最终拿到全部 4 条
    assert client.root_starts == [0]
    lines = _read_jsonl(paths["jsonl"])
    assert {x["rpid"] for x in lines} == {1, 2, 3, 4}


def test_run_fresh_clears_stale_jsonl(tmp_path):
    """回归用例：--fresh 应先清空该视频已有 jsonl/meta/state，再从头抓，避免重复 rpid / 残留旧数据。"""
    roots = [Comment(i, 0, 0, 1, 1, f"r{i}", 0, False) for i in (1, 2, 3, 4)]
    from echolens.fetch import cache

    paths = cache.comment_paths(str(tmp_path), "BV1")
    cache.ensure_dir(str(tmp_path))
    cache.append_comment(paths["jsonl"], Comment(999, 0, 0, 1, 1, "old", 0, False))
    cache.write_state(paths["state"], {"phase": "done", "root_cursor": None, "sub_done": [1]})

    rc = run("BV1", out=str(tmp_path), fresh=True, client=FakeClient(roots, {}))
    assert rc == 0
    lines = _read_jsonl(paths["jsonl"])
    rpids = {x["rpid"] for x in lines}
    assert 999 not in rpids  # 旧数据应被 --fresh 清掉
    assert rpids == {1, 2, 3, 4}
