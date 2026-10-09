"""Jev（typesafe.ai System One）判断后端（M1-T1）。

API 事实（2026-10-09 从官方文档提取，见 docs/tasks/M1-T1-judge-layer.md）：
- `POST https://api.typesafe.ai/v1/systemone`，`Authorization: Bearer $TYPESAFE_API_KEY`；
- 请求 `{"state": ..., "model": "jev-1.13.0", "questions": {...}}`；同一次调用可混合
  多个问题并行求值，加问题几乎不增加耗时——所以每条评论一次调用带全部 9 个 Noul；
- 响应 `{"model": ..., "answers": {key: {"type": "noul", "noul": 0~1}}, "usage": {...}}`；
- Noul 答案是「是」的概率（0~1），无 confidence 字段；
- 错误码：401 key 无效（不重试）、422 请求体校验失败（不重试）、
  429 限速 / 529 过载（指数退避重试）。

铁律：
- 模型锁 `jev-1.13.0`（PRD 决策 D3），禁止使用任何滚动版本别名；
- key 只从环境变量 `TYPESAFE_API_KEY` 或仓库根目录 `.env` 读取（自写解析，不引
  python-dotenv），绝不写进任何会被提交的文件；
- `session.trust_env = True`：尊重环境变量代理。**本机 / 被区域封锁的环境**
  （直连 api.typesafe.ai 会返回 HTTP 451）需先设
  `HTTPS_PROXY=http://127.0.0.1:10809`（或 `HTTP_PROXY`）；直连可达的环境什么都
  不用设。注意这与 fetch 层相反：B 站接口国内直连可达，fetch 层用
  `trust_env=False` 绕代理；Jev 需要代理的环境反而要靠环境变量走代理。

可测性：网络全部隔离在本文件；构造函数接受 `session` 参数，测试可注入
fake transport（任何带 `.post(url, json=..., headers=..., timeout=...)` 的对象）。
"""

from __future__ import annotations

import logging
import os
import random
import time
from typing import Any, Dict, Optional

import requests

from .provider import JudgeProvider

logger = logging.getLogger(__name__)

API_URL = "https://api.typesafe.ai/v1/systemone"
MODEL = "jev-1.13.0"  # D3：版本锁死，防止官方升级导致结果漂移

TIMEOUT = 30          # 单次请求超时（秒）
MAX_ATTEMPTS = 5      # 429/529/网络错误 的总尝试次数上限（含首次）
BACKOFF_BASE = 1.0    # 退避基数（秒）：base * 2**attempt + 抖动
BACKOFF_CAP = 30.0    # 单次退避上限（秒）

# 立即抛错、不重试的状态码
NO_RETRY_STATUS = (401, 422)
# 触发指数退避重试的状态码
RETRY_STATUS = (429, 529)


class JevError(Exception):
    """Jev 调用失败基类。"""

    def __init__(self, message: str, status: Optional[int] = None):
        super().__init__(message)
        self.status = status


class JevAuthError(JevError):
    """401 key 无效 / 422 请求体校验失败——请求本身有问题，重试无意义。"""


class JevRateLimitError(JevError):
    """429/529 重试耗尽。"""


def _read_env_file(env_path: str) -> Optional[str]:
    """自写 .env 解析（不引 python-dotenv）：找 TYPESAFE_API_KEY=<value> 行。"""
    try:
        with open(env_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, _, v = line.partition("=")
                if k.strip() == "TYPESAFE_API_KEY":
                    return v.strip().strip('"').strip("'")
    except OSError:
        pass
    return None


def resolve_api_key(
    api_key: Optional[str] = None,
    env_path: Optional[str] = None,
) -> str:
    """按优先级解析 key：显式参数 > 环境变量 > 仓库根目录 .env。

    key 缺失时抛带清晰指引的 JevAuthError。
    """
    if api_key:
        return api_key
    key = os.environ.get("TYPESAFE_API_KEY")
    if key:
        return key
    if env_path is None:
        # 仓库根目录 = 本文件上两级（echolens/judge/jev.py -> repo root）
        env_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
            ".env",
        )
    key = _read_env_file(env_path)
    if key:
        return key
    raise JevAuthError(
        "未找到 TYPESAFE_API_KEY：请先设置环境变量 TYPESAFE_API_KEY，"
        "或在仓库根目录 .env 中写入 TYPESAFE_API_KEY=<key>"
    )


class JevProvider(JudgeProvider):
    """Jev 判断后端：raw HTTP（requests，不引 typesafe-sdk）。

    参数：
        api_key:  显式 key；None 时走 resolve_api_key 的环境变量 / .env 链。
        session:  可注入的 transport（测试用 fake）；None 时自建 requests.Session
                  （trust_env=True 尊重环境变量代理，连接池按并发上限配置）。
        model:    默认锁 jev-1.13.0，正常调用不应覆盖。
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        session: Optional[Any] = None,
        model: str = MODEL,
        timeout: int = TIMEOUT,
        max_attempts: int = MAX_ATTEMPTS,
    ):
        self._api_key = resolve_api_key(api_key)
        self.model = model
        self.timeout = timeout
        self.max_attempts = max_attempts
        if session is None:
            session = requests.Session()
            session.trust_env = True  # 尊重 HTTP_PROXY/HTTPS_PROXY（区域封锁时靠它）
            # 连接池 >= 并发硬上限 20，消除 "Connection pool is full" 告警
            adapter = requests.adapters.HTTPAdapter(pool_maxsize=20)
            session.mount("https://", adapter)
            session.mount("http://", adapter)
        self._session = session

    def judge(self, state: str, questions: Dict[str, Any]) -> Dict[str, Any]:
        """一次调用并行求值全部问题，返回统一答案结构（见 provider.py）。"""
        body = {"state": state, "model": self.model, "questions": questions}
        headers = {"Authorization": f"Bearer {self._api_key}"}

        last_exc: Optional[Exception] = None
        last_status: Optional[int] = None
        for attempt in range(self.max_attempts):
            try:
                resp = self._session.post(
                    API_URL, json=body, headers=headers, timeout=self.timeout
                )
            except requests.RequestException as exc:
                # 网络层错误：按可重试处理，指数退避
                last_exc = exc
                self._sleep_backoff(attempt, None)
                continue

            status = getattr(resp, "status_code", None)
            if status == 200:
                return self._parse_response(resp)
            if status in NO_RETRY_STATUS:
                raise JevAuthError(
                    f"Jev 请求被拒绝（HTTP {status}，不重试）：{self._resp_text(resp)}",
                    status=status,
                )
            if status in RETRY_STATUS or (status is not None and status >= 500):
                last_status = status
                logger.warning(
                    "Jev HTTP %s，第 %d/%d 次尝试后退避重试",
                    status, attempt + 1, self.max_attempts,
                )
                self._sleep_backoff(attempt, status)
                continue
            # 其他 4xx：请求问题，不重试
            raise JevError(
                f"Jev 返回非预期状态码 HTTP {status}：{self._resp_text(resp)}",
                status=status,
            )

        if last_exc is not None:
            raise JevError(f"Jev 网络错误，重试 {self.max_attempts} 次耗尽：{last_exc}")
        if last_status in RETRY_STATUS:
            raise JevRateLimitError(
                f"Jev 限速/过载（HTTP {last_status}），重试 {self.max_attempts} 次耗尽"
            )
        raise JevError(
            f"Jev 服务端错误（HTTP {last_status}），重试 {self.max_attempts} 次耗尽",
            status=last_status,
        )

    # ---- 内部 ----
    @staticmethod
    def _sleep_backoff(attempt: int, status: Optional[int]) -> None:
        delay = min(BACKOFF_BASE * (2 ** attempt) + random.uniform(0, 0.5), BACKOFF_CAP)
        time.sleep(delay)

    @staticmethod
    def _resp_text(resp: Any) -> str:
        try:
            return (resp.text or "")[:500]
        except Exception:
            return "<无法读取响应体>"

    def _parse_response(self, resp: Any) -> Dict[str, Any]:
        try:
            data = resp.json()
        except ValueError as exc:
            raise JevError(f"Jev 响应不是合法 JSON：{exc}")
        raw_answers = data.get("answers") or {}
        answers: Dict[str, float] = {}
        for key, val in raw_answers.items():
            if isinstance(val, dict) and "noul" in val:
                answers[key] = float(val["noul"])
        usage = data.get("usage") or {}
        return {
            "model": data.get("model", self.model),
            "answers": answers,
            "usage": {
                "input_tokens": int(usage.get("input_tokens") or 0),
                "output_tokens": int(usage.get("output_tokens") or 0),
            },
        }


__all__ = [
    "JevProvider",
    "JevError",
    "JevAuthError",
    "JevRateLimitError",
    "resolve_api_key",
    "MODEL",
    "API_URL",
]
