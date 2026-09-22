"""EchoLens 数据层单元测试公共夹具与假对象（不联网）。

本文件仅供 pytest 使用，不会作为测试模块被收集。
通过注入假 `requests.Session` / 假 `BilibiliClient` 把网络隔离在 bilibili.py 之外，
满足任务卡「代码可被 mock session 单测」的验收项。
"""

from __future__ import annotations

import pytest

from echolens.fetch import bilibili


@pytest.fixture
def timing(monkeypatch):
    """冻结时钟并统计 sleep 次数（退避验证用）。

    把 throttle/backoff 的 sleep 改成计数；random.uniform 恒为 0 使限速确定；
    monotonic 恒为常量使 throttle 永不阻塞。
    """
    counter = {"sleep": 0, "total": 0.0}

    def fake_sleep(seconds):
        counter["sleep"] += 1
        counter["total"] += seconds

    monkeypatch.setattr(bilibili.time, "sleep", fake_sleep)
    monkeypatch.setattr(bilibili.random, "uniform", lambda *a, **k: 0.0)
    monkeypatch.setattr(bilibili.time, "monotonic", lambda: 1000.0)
    # 让 sleep 计数只反映退避（_backoff），避免 _throttle 的限速 sleep 干扰断言
    monkeypatch.setattr(bilibili.BilibiliClient, "_throttle", lambda self: None)
    return counter


__all__ = ["timing"]
