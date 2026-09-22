"""cache.py 单元测试：append/去重、state 读写删、坏行容忍。"""

from __future__ import annotations

import json
import os

from echolens.fetch import cache
from echolens.fetch.models import Comment


def test_append_and_load_seen_dedup(tmp_path):
    p = str(tmp_path / "x.jsonl")
    cache.append_comment(p, Comment(1, 0, 0, 1, 1, "a", 0, False))
    cache.append_comment(p, Comment(2, 0, 0, 1, 1, "b", 0, False))
    cache.append_comment(p, Comment(1, 0, 0, 1, 1, "a", 0, False))  # 重复 rpid
    seen = cache.load_seen_rpids(p)
    assert seen == {1, 2}


def test_load_seen_missing_file(tmp_path):
    assert cache.load_seen_rpids(str(tmp_path / "nope.jsonl")) == set()


def test_load_seen_tolerates_bad_lines(tmp_path):
    p = str(tmp_path / "b.jsonl")
    with open(p, "w", encoding="utf-8") as f:
        f.write('{"rpid":1,"root":0}\n')
        f.write("this is not json\n")
        f.write('{"rpid":2}\n')
        f.write("\n")
        f.write('{"rpid": 3, "garbage": true}\n')  # 合法 JSON 但非评论
    seen = cache.load_seen_rpids(p)
    assert seen == {1, 2, 3}


def test_load_comments_tolerates_bad(tmp_path):
    p = str(tmp_path / "c.jsonl")
    with open(p, "w", encoding="utf-8") as f:
        f.write(Comment(1, 0, 0, 1, 1, "a", 0, False).to_json_line() + "\n")
        f.write("garbage line\n")
        f.write('{"not":"comment"}\n')  # 缺字段，构造 Comment 失败
    cs = cache.load_comments(p)
    assert len(cs) == 1
    assert cs[0].rpid == 1


def test_state_roundtrip(tmp_path):
    p = str(tmp_path / "s.json")
    st = cache.default_state()
    st["phase"] = "sub"
    st["root_cursor"] = 10
    st["sub_done"] = [5, 6]
    cache.write_state(p, st)
    assert os.path.exists(p)
    rd = cache.read_state(p)
    assert rd["phase"] == "sub"
    assert rd["root_cursor"] == 10
    assert rd["sub_done"] == [5, 6]


def test_read_state_missing_returns_default(tmp_path):
    assert cache.read_state(str(tmp_path / "missing.json")) == cache.default_state()


def test_read_state_corrupt_returns_default(tmp_path):
    p = str(tmp_path / "corrupt.json")
    with open(p, "w", encoding="utf-8") as f:
        f.write("{not valid json")
    assert cache.read_state(p) == cache.default_state()


def test_rm_state(tmp_path):
    p = str(tmp_path / "d.json")
    cache.write_state(p, cache.default_state())
    assert os.path.exists(p)
    cache.rm_state(p)
    assert not os.path.exists(p)


def test_meta_write_contains_stats(tmp_path):
    p = str(tmp_path / "m.json")
    from echolens.fetch.models import VideoMeta

    meta = VideoMeta("BV1", 1, "t", 98, 1.0)
    cache.write_meta(
        p,
        meta,
        {
            "root_stored": 50,
            "sub_stored": 48,
            "total_stored": 98,
            "reply_count_api": 98,
            "complete": True,
        },
    )
    obj = json.loads(open(p, encoding="utf-8").read())
    assert obj["bvid"] == "BV1"
    assert obj["stats"]["total_stored"] == 98
    assert obj["stats"]["complete"] is True
