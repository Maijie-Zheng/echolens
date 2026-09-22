"""bilibili.py 单元测试：parse_bvid、get_video_meta、分页、退避（全 mock）。

注意：parse_bvid 定义在 echolens.fetch.__main__ 中，本文件一并覆盖。
"""

from __future__ import annotations

import requests

import pytest

from tests._fakes import FakeResponse, make_client
from echolens.fetch.__main__ import parse_bvid
from echolens.fetch.bilibili import BilibiliClient, BilibiliError


# ---------------- parse_bvid ----------------
def test_parse_bare_bv():
    assert parse_bvid("BV1HHeC62EBs") == "BV1HHeC62EBs"


def test_parse_full_url():
    assert parse_bvid("https://www.bilibili.com/video/BV1HHeC62EBs") == "BV1HHeC62EBs"


def test_parse_url_with_params():
    assert (
        parse_bvid("https://www.bilibili.com/video/BV1HHeC62EBs?t=10&p=2")
        == "BV1HHeC62EBs"
    )


def test_parse_strips_whitespace():
    assert parse_bvid("  BV1HHeC62EBs  ") == "BV1HHeC62EBs"


def test_parse_rejects_b23_shortlink():
    with pytest.raises(ValueError):
        parse_bvid("https://b23.tv/abcd")


def test_parse_rejects_b23_in_text():
    with pytest.raises(ValueError):
        parse_bvid("b23.tv/xyz")


def test_parse_rejects_garbage():
    with pytest.raises(ValueError):
        parse_bvid("not a bvid at all")


# ---------------- get_video_meta ----------------
def test_get_video_meta(timing):
    def responder(url, params):
        assert url.endswith("/x/web-interface/view")
        return FakeResponse(
            200,
            {"code": 0, "data": {"aid": 170000, "title": "T", "stat": {"reply": 98}}},
        )

    m = make_client(responder).get_video_meta("BV1HHeC62EBs")
    assert m.aid == 170000
    assert m.title == "T"
    assert m.reply_count == 98


def test_get_video_meta_no_aid(timing):
    def responder(url, params):
        return FakeResponse(
            200, {"code": 0, "data": {"aid": 0, "title": "", "stat": {"reply": 0}}}
        )

    with pytest.raises(BilibiliError):
        make_client(responder).get_video_meta("BVX")


# ---------------- iter_root_comments 分页 ----------------
def test_iter_root_multi_page(timing):
    pages = [
        {
            "code": 0,
            "data": {
                "replies": [
                    {"rpid": 1, "root": 0, "parent": 0, "ctime": 1, "like": 1, "rcount": 0, "content": {"message": "a"}},
                    {"rpid": 2, "root": 0, "parent": 0, "ctime": 2, "like": 1, "rcount": 0, "content": {"message": "b"}},
                    {"rpid": 99, "root": 5, "parent": 5, "ctime": 9, "like": 0, "rcount": 0, "content": {"message": "sub-mixed"}},
                ],
                "cursor": {"next": 10, "is_end": False},
            },
        },
        {
            "code": 0,
            "data": {
                "replies": [
                    {"rpid": 3, "root": 0, "parent": 0, "ctime": 3, "like": 1, "rcount": 0, "content": {"message": "c"}},
                ],
                "cursor": {"next": 20, "is_end": True},
            },
        },
    ]
    idx = {"i": 0}

    def responder(url, params):
        assert url.endswith("/x/v2/reply/main")
        p = pages[idx["i"]]
        idx["i"] += 1
        return FakeResponse(200, p)

    seen = []
    got = list(
        make_client(responder).iter_root_comments(
            1, start_next=0, on_page=lambda nxt, end: seen.append((nxt, end))
        )
    )
    # 混入的 root=5 子评论被过滤
    assert [g.rpid for g in got] == [1, 2, 3]
    assert all(not g.is_sub for g in got)
    assert seen == [(10, False), (20, True)]


def test_iter_root_stops_on_is_end(timing):
    def responder(url, params):
        return FakeResponse(
            200,
            {
                "code": 0,
                "data": {
                    "replies": [
                        {"rpid": 1, "root": 0, "parent": 0, "ctime": 1, "like": 1, "rcount": 0, "content": {"message": "a"}},
                    ],
                    "cursor": {"next": 999, "is_end": True},
                },
            },
        )

    got = list(make_client(responder).iter_root_comments(1))
    assert [g.rpid for g in got] == [1]


def test_iter_root_stops_no_cursor_progress(timing):
    """游标未推进时防御性退出，避免死循环。"""
    def responder(url, params):
        return FakeResponse(
            200,
            {
                "code": 0,
                "data": {
                    "replies": [
                        {"rpid": 1, "root": 0, "parent": 0, "ctime": 1, "like": 1, "rcount": 0, "content": {"message": "a"}},
                    ],
                    "cursor": {"next": 0, "is_end": False},
                },
            },
        )

    got = list(make_client(responder).iter_root_comments(1, start_next=0))
    assert [g.rpid for g in got] == [1]


def test_iter_root_empty(timing):
    def responder(url, params):
        return FakeResponse(200, {"code": 0, "data": {"replies": [], "cursor": {"next": 0, "is_end": True}}})

    assert list(make_client(responder).iter_root_comments(1)) == []


# ---------------- iter_sub_comments 分页 ----------------
def test_iter_sub_paging(timing):
    def responder(url, params):
        assert url.endswith("/x/v2/reply/reply")
        pn = params["pn"]
        if pn == 1:
            return FakeResponse(
                200,
                {
                    "code": 0,
                    "data": {
                        "page": {"count": 3, "size": 2},
                        "replies": [
                            {"rpid": 11, "root": 1, "parent": 1, "ctime": 1, "like": 1, "rcount": 0, "content": {"message": "s1"}},
                            {"rpid": 12, "root": 1, "parent": 1, "ctime": 2, "like": 1, "rcount": 0, "content": {"message": "s2"}},
                        ],
                    },
                },
            )
        return FakeResponse(
            200,
            {
                "code": 0,
                "data": {
                    "page": {"count": 3, "size": 2},
                    "replies": [
                        {"rpid": 13, "root": 1, "parent": 1, "ctime": 3, "like": 1, "rcount": 0, "content": {"message": "s3"}},
                    ],
                },
            },
        )

    subs = list(make_client(responder).iter_sub_comments(1, 1))
    assert [s.rpid for s in subs] == [11, 12, 13]
    assert all(s.is_sub for s in subs)
    assert all(s.root == 1 for s in subs)


def test_iter_sub_empty(timing):
    def responder(url, params):
        return FakeResponse(200, {"code": 0, "data": {"page": {"count": 0, "size": 10}, "replies": []}})

    assert list(make_client(responder).iter_sub_comments(1, 99)) == []


# ---------------- 退避 / 重试 ----------------
def test_412_then_success(timing):
    n = {"i": 0}

    def responder(url, params):
        n["i"] += 1
        if n["i"] <= 2:  # 前两次 HTTP 412
            return FakeResponse(412, {})
        return FakeResponse(200, {"code": 0, "data": {"aid": 1, "title": "t", "stat": {"reply": 1}}})

    m = make_client(responder).get_video_meta("BV1")
    assert m.aid == 1
    assert n["i"] == 3  # 2 次 412 + 1 次成功
    assert timing["sleep"] == 2  # 两次退避


def test_risk_code_352_backoff(timing):
    n = {"i": 0}

    def responder(url, params):
        n["i"] += 1
        return FakeResponse(200, {"code": -352, "message": "风控"})

    with pytest.raises(BilibiliError):
        make_client(responder).get_video_meta("BV1")
    # 3 次尝试：第 0、1 次退避，第 2 次直接报错（不再退避）
    assert timing["sleep"] == 2
    assert n["i"] == 3


def test_http_500_raises(timing):
    def responder(url, params):
        return FakeResponse(500, {})

    with pytest.raises(BilibiliError):
        make_client(responder).get_video_meta("BV1")
    assert timing["sleep"] == 0  # 非风控码直接报错，不退避


def test_bad_json_raises(timing):
    class BadSession(requests.Session):
        def get(self, url, params=None, timeout=None):
            r = FakeResponse(200, {})
            original = r.json

            def boom():
                raise ValueError("not json")

            r.json = boom
            return r

    with pytest.raises(BilibiliError):
        BilibiliClient(session=BadSession()).get_video_meta("BV1")


def test_request_exception_retry(timing):
    n = {"i": 0}

    class FlakySession(requests.Session):
        def get(self, url, params=None, timeout=None):
            nonlocal n
            n["i"] += 1
            if n["i"] <= 2:
                raise requests.RequestException("boom")
            return FakeResponse(200, {"code": 0, "data": {"aid": 1, "title": "t", "stat": {"reply": 1}}})

    m = BilibiliClient(session=FlakySession()).get_video_meta("BV1")
    assert m.aid == 1
    assert timing["sleep"] == 2
