"""判断层 Provider 抽象（M1-T1，PRD 决策 D7）。

Jev 是闭源服务（单点依赖，PRD §8 风险表），判断层通过 `JudgeProvider` 接口解耦，
后端可替换（官方 System One Adapter 或其他实现），上层管线只依赖本接口。

统一答案结构（judge 返回值）：
    {
        "model": str,                # 实际打标模型（锁定 jev-1.13.0，见 D3）
        "answers": {key: float},     # 问题 key -> Noul 概率（0~1，「是」的概率）
        "usage": {"input_tokens": int, "output_tokens": int},
    }
"""

from __future__ import annotations

import abc
from typing import Any, Dict


class JudgeProvider(abc.ABC):
    """判断后端抽象基类。

    实现方负责网络 / 重试 / 认证；上层只调 `judge`。
    """

    @abc.abstractmethod
    def judge(self, state: str, questions: Dict[str, Any]) -> Dict[str, Any]:
        """对单个 state 并行求值一组问题。

        参数：
            state:     被评判的文本（已 sanitize 的评论正文）。
            questions: {key: {"type": "noul", "instructions": str, ...}}。

        返回：
            见模块 docstring 的统一答案结构。
        异常：
            实现方抛出的 Provider 错误（如 jev.JevError）。
        """
        raise NotImplementedError


__all__ = ["JudgeProvider"]
