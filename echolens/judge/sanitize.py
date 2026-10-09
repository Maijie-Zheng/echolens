"""评论防注入清洗（M1-T1，PRD 决策 D4 / WORKFLOW 铁律 3）。

**评论永远是不可信输入**：它只作为被评判的 `state` 数据传给 Jev，
绝不作为指令参与 prompt 构造（问题措辞全部来自 questions.json 配置，
评论文本没有机会进入 instructions）。

本模块做两道防线：
1. 控制字符清理：剥掉 \\x00-\\x1f / \\x7f-\\x9f 等（保留换行与制表，转为空格），
   防止异常字节干扰请求体；
2. 长度截断：默认上限 1000 字符（B 站评论上限即 1000），超长直接截断，
   控制 token 消耗与注入面。
"""

from __future__ import annotations

import unicodedata

MAX_COMMENT_CHARS = 1000


def sanitize_comment(text: str, max_chars: int = MAX_COMMENT_CHARS) -> str:
    """清洗单条评论文本：控制字符清理 + 长度截断。

    参数：
        text:      原始评论正文（不可信输入）。
        max_chars: 字符数上限，默认 1000。
    返回：
        可安全作为 state 传给 Jev 的文本。
    """
    if not text:
        return ""
    cleaned_chars = []
    for ch in text:
        # 去掉所有控制类字符（含 \\x00-\\x1f、\\x7f-\\x9f、零宽格式符 Cf 除外）
        if unicodedata.category(ch) == "Cc":
            cleaned_chars.append(" " if ch in ("\n", "\t", "\r") else "")
        else:
            cleaned_chars.append(ch)
    cleaned = "".join(cleaned_chars).strip()
    if len(cleaned) > max_chars:
        cleaned = cleaned[:max_chars]
    return cleaned


__all__ = ["sanitize_comment", "MAX_COMMENT_CHARS"]
