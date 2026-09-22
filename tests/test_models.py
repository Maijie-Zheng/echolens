"""models.py 单元测试：Comment.from_reply 匿名化、VideoMeta 往返。"""

from __future__ import annotations

from echolens.fetch.models import Comment, VideoMeta

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


def test_from_reply_strips_identity():
    """完整 reply（含 member 等身份信息）只产出 8 个白名单字段。"""
    reply = {
        "rpid": 111,
        "oid": 1,
        "type": 1,
        "mid": 999,
        "root": 0,
        "parent": 0,
        "ctime": 1600000000,
        "like": 42,
        "rcount": 5,
        "content": {"message": "hello"},
        "member": {
            "mid": 999,
            "uname": "张三",
            "avatar": "http://x/face.jpg",
            "sign": "签名",
            "level_info": {"current_level": 5},
            "vip": {"vipType": 1},
        },
        "rpid_str": "111",
        "mid_str": "999",
    }
    c = Comment.from_reply(reply, is_sub=False)
    d = c.to_dict()
    assert set(d.keys()) == WHITELIST
    for bad in ("member", "mid", "uname", "avatar", "sign", "level_info", "vip"):
        assert bad not in d, f"泄露了身份字段 {bad}"
    assert c.rpid == 111
    assert c.message == "hello"
    assert c.is_sub is False
    assert c.rcount == 5


def test_from_reply_sub_flag():
    reply = {
        "rpid": 222,
        "root": 111,
        "parent": 111,
        "ctime": 1,
        "like": 0,
        "rcount": 0,
        "content": {"message": "x"},
    }
    c = Comment.from_reply(reply, is_sub=True)
    assert c.is_sub is True
    assert c.root == 111
    assert c.parent == 111


def test_from_reply_handles_rpid_str():
    """rpid 可能以字符串形式返回（rpid_str），应正确转 int。"""
    reply = {
        "rpid_str": "333",
        "root": 0,
        "parent": 0,
        "ctime": 1,
        "like": 1,
        "rcount": 0,
        "content": {"message": "m"},
    }
    c = Comment.from_reply(reply, is_sub=False)
    assert c.rpid == 333


def test_from_reply_missing_fields_default():
    """缺字段时不应抛错，缺失项给 0 / 空串。"""
    c = Comment.from_reply({"rpid": 7}, is_sub=False)
    assert c.rpid == 7
    assert c.like == 0
    assert c.message == ""
    assert c.root == 0


def test_comment_roundtrip_json():
    c = Comment(1, 0, 0, 5, 1690000000, "hi", 0, False)
    c2 = Comment(**c.to_dict())
    assert c == c2


def test_video_meta_roundtrip():
    m = VideoMeta("BV1HHeC62EBs", 170000000, "示例标题", 98, 1690000100.0)
    m2 = VideoMeta.from_dict(m.to_dict())
    assert m == m2
    assert m2.title == "示例标题"
    assert m2.reply_count == 98
