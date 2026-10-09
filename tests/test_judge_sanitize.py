"""sanitize.py 单元测试：截断 1000、控制字符清理。"""

from __future__ import annotations

from echolens.judge.sanitize import MAX_COMMENT_CHARS, sanitize_comment


def test_empty_and_none_like():
    assert sanitize_comment("") == ""
    assert sanitize_comment("   ") == ""


def test_normal_text_unchanged():
    assert sanitize_comment("好看！") == "好看！"


def test_truncate_to_1000():
    text = "a" * 1500
    out = sanitize_comment(text)
    assert len(out) == MAX_COMMENT_CHARS == 1000
    assert out == "a" * 1000


def test_truncate_custom_max():
    assert sanitize_comment("abcdef", max_chars=3) == "abc"


def test_exactly_1000_not_truncated():
    text = "b" * 1000
    assert sanitize_comment(text) == text


def test_control_chars_removed():
    # \x00 \x01 \x1f \x7f 等直接被剥掉
    assert sanitize_comment("ab\x00c\x01d\x1fe\x7ff") == "abcdef"


def test_newline_tab_cr_become_space():
    out = sanitize_comment("a\nb\tc\rd")
    assert out == "a b c d"


def test_c1_control_chars_removed():
    # \x80-\x9f 属 Cc 类控制字符
    out = sanitize_comment("x\x85y\x9fz")
    assert "\x85" not in out and "\x9f" not in out


def test_strip_whitespace_ends():
    assert sanitize_comment("  hello  ") == "hello"


def test_strip_happens_before_truncate():
    # 前导空白 strip 后不足上限则不应截断正文
    text = " " * 600 + "c" * 900
    out = sanitize_comment(text)
    assert out == "c" * 900
