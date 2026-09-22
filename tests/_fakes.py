"""EchoLens 数据层单元测试公共假对象（不联网）。

把假 `requests.Session` / 假 client 构造抽出来，供各测试模块以
`from tests._fakes import ...` 导入，避免依赖 conftest 的导入模式。
网络隔离在 bilibili.py 之外，满足「代码可被 mock session 单测」验收项。
"""

from __future__ import annotations

from echolens.fetch import bilibili
from echolens.fetch.bilibili import BilibiliError


class FakeResponse:
    """模仿 requests.Response：持有 status_code 与 JSON 载荷。"""

    def __init__(self, status_code: int = 200, payload: dict | None = None):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}

    def json(self):
        return self._payload


class FakeSession:
    """可注入 BilibiliClient 的假 session。

    `responder(url, params)` 由测试用例提供，按 URL/参数返回 FakeResponse。
    """

    def __init__(self, responder):
        self.responder = responder
        self.headers: dict = {}
        self.trust_env = True  # client.__init__ 会强制改为 False，可验证该副作用
        self.calls: list = []

    def get(self, url, params=None, timeout=None):
        self.calls.append((url, params))
        return self.responder(url, params)


def make_client(responder) -> "bilibili.BilibiliClient":
    """用假 responder 构造注入了 FakeSession 的 BilibiliClient。"""
    return bilibili.BilibiliClient(session=FakeSession(responder))


__all__ = ["FakeResponse", "FakeSession", "make_client", "BilibiliError"]
