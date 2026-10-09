"""jev.py 单元测试（mock transport，不联网）。

覆盖：
- 429/529/5xx/网络错误 退避后成功路径
- 401/422 不重试
- 429/529 耗尽 -> JevRateLimitError；5xx 耗尽 -> JevError（编排方代改 #1 验证）
- 网络错误耗尽 -> JevError
- key 解析链（显式 > env > .env > 缺失报错清晰）
- 响应解析（answers/usage 结构）
- model 字段锁 jev-1.13.0
"""

from __future__ import annotations

import os

import pytest
import requests

from echolens.judge import jev
from echolens.judge.jev import (
    API_URL,
    MODEL,
    JevAuthError,
    JevError,
    JevProvider,
    JevRateLimitError,
    resolve_api_key,
)

QUESTIONS = {"praise": {"type": "noul", "instructions": "是夸吗？"}}


class FakeResp:
    def __init__(self, status, payload=None, text=""):
        self.status_code = status
        self._payload = payload
        self.text = text

    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


class FakeSession:
    """按脚本返回响应/抛异常，记录调用次数与请求体。"""

    def __init__(self, script):
        self.script = list(script)
        self.calls = []

    def post(self, url, json=None, headers=None, timeout=None):
        self.calls.append({"url": url, "json": json, "headers": headers})
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


OK_PAYLOAD = {
    "model": MODEL,
    "answers": {"praise": {"type": "noul", "noul": 0.87}},
    "usage": {"input_tokens": 123, "output_tokens": 45},
}


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    """退避 sleep 置零，测试不真等。"""
    monkeypatch.setattr(jev.time, "sleep", lambda s: None)
    monkeypatch.setattr(jev.random, "uniform", lambda *a, **k: 0.0)


@pytest.fixture
def make_provider():
    def _make(script, **kw):
        return JevProvider(api_key="test-key", session=FakeSession(script), **kw)

    return _make


# ---------- 退避成功路径 ----------

@pytest.mark.parametrize("status", [429, 529, 500, 503])
def test_retry_status_then_success(make_provider, status):
    p = make_provider([FakeResp(status), FakeResp(200, OK_PAYLOAD)])
    out = p.judge("state", QUESTIONS)
    assert out["answers"]["praise"] == pytest.approx(0.87)
    assert len(p._session.calls) == 2  # 重试生效


def test_network_error_then_success(make_provider):
    p = make_provider([requests.ConnectionError("boom"), FakeResp(200, OK_PAYLOAD)])
    out = p.judge("state", QUESTIONS)
    assert out["model"] == MODEL
    assert len(p._session.calls) == 2


# ---------- 不重试 ----------

@pytest.mark.parametrize("status", [401, 422])
def test_no_retry_auth_errors(make_provider, status):
    p = make_provider([FakeResp(status, text="bad"), FakeResp(200, OK_PAYLOAD)])
    with pytest.raises(JevAuthError) as ei:
        p.judge("state", QUESTIONS)
    assert ei.value.status == status
    assert len(p._session.calls) == 1  # 未重试


def test_other_4xx_no_retry(make_provider):
    p = make_provider([FakeResp(403, text="forbidden")])
    with pytest.raises(JevError) as ei:
        p.judge("state", QUESTIONS)
    assert ei.value.status == 403
    assert len(p._session.calls) == 1


# ---------- 耗尽分流（编排方代改 #1 验证） ----------

@pytest.mark.parametrize("status", [429, 529])
def test_rate_limit_exhausted_raises_rate_limit_error(make_provider, status):
    p = make_provider([FakeResp(status)] * 5, max_attempts=5)
    with pytest.raises(JevRateLimitError) as ei:
        p.judge("state", QUESTIONS)
    assert str(status) in str(ei.value)
    assert len(p._session.calls) == 5


@pytest.mark.parametrize("status", [500, 502, 503])
def test_5xx_exhausted_raises_jev_error_not_rate_limit(make_provider, status):
    """代改验证：5xx 耗尽必须抛 JevError 而非 JevRateLimitError。"""
    p = make_provider([FakeResp(status)] * 5, max_attempts=5)
    with pytest.raises(JevError) as ei:
        p.judge("state", QUESTIONS)
    assert not isinstance(ei.value, JevRateLimitError)
    assert ei.value.status == status
    assert str(status) in str(ei.value)
    assert len(p._session.calls) == 5


def test_5xx_exhausted_message_mentions_server_error(make_provider):
    """代改验证：5xx 耗尽文案含「服务端错误」与实际状态码。"""
    p = make_provider([FakeResp(500)] * 3, max_attempts=3)
    with pytest.raises(JevError, match="服务端错误"):
        p.judge("state", QUESTIONS)


def test_network_error_exhausted(make_provider):
    p = make_provider([requests.Timeout("t")] * 5, max_attempts=5)
    with pytest.raises(JevError, match="网络错误"):
        p.judge("state", QUESTIONS)
    assert len(p._session.calls) == 5


def test_mixed_429_then_500_exhausted(make_provider):
    """最后一次状态决定异常类型：末尾是 5xx -> JevError。"""
    p = make_provider([FakeResp(429), FakeResp(500), FakeResp(429), FakeResp(500)], max_attempts=4)
    with pytest.raises(JevError) as ei:
        p.judge("state", QUESTIONS)
    assert not isinstance(ei.value, JevRateLimitError)


# ---------- key 解析链 ----------

def test_key_explicit_wins(monkeypatch):
    monkeypatch.setenv("TYPESAFE_API_KEY", "env-key")
    assert resolve_api_key("explicit-key") == "explicit-key"


def test_key_env_beats_env_file(monkeypatch, tmp_path):
    monkeypatch.setenv("TYPESAFE_API_KEY", "env-key")
    env_file = tmp_path / ".env"
    env_file.write_text("TYPESAFE_API_KEY=file-key\n", encoding="utf-8")
    assert resolve_api_key(env_path=str(env_file)) == "env-key"


def test_key_from_env_file(monkeypatch, tmp_path):
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    env_file = tmp_path / ".env"
    env_file.write_text(
        "# comment\n\nOTHER=1\nTYPESAFE_API_KEY=\"file-key\"\n", encoding="utf-8"
    )
    assert resolve_api_key(env_path=str(env_file)) == "file-key"


def test_key_missing_clear_error(monkeypatch, tmp_path):
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    with pytest.raises(JevAuthError, match="TYPESAFE_API_KEY"):
        resolve_api_key(env_path=str(tmp_path / "nonexistent.env"))


# ---------- 响应解析 ----------

def test_parse_answers_and_usage(make_provider):
    payload = {
        "model": MODEL,
        "answers": {
            "praise": {"type": "noul", "noul": 0.5},
            "meme": {"type": "noul", "noul": 0},      # 0 值要保留
            "other": {"type": "choice", "choice": 1},  # 非 noul 应被忽略
        },
        "usage": {"input_tokens": 10, "output_tokens": 2},
    }
    p = make_provider([FakeResp(200, payload)])
    out = p.judge("s", QUESTIONS)
    assert out["answers"] == {"praise": 0.5, "meme": 0.0}
    assert out["usage"] == {"input_tokens": 10, "output_tokens": 2}


def test_parse_missing_usage_defaults_zero(make_provider):
    payload = {"model": MODEL, "answers": {}}
    p = make_provider([FakeResp(200, payload)])
    out = p.judge("s", QUESTIONS)
    assert out["usage"] == {"input_tokens": 0, "output_tokens": 0}
    assert out["answers"] == {}


def test_parse_invalid_json(make_provider):
    p = make_provider([FakeResp(200, None, text="<!DOCTYPE html>")])
    with pytest.raises(JevError, match="JSON"):
        p.judge("s", QUESTIONS)


# ---------- model 锁定 ----------

def test_model_locked_and_sent_in_body(make_provider):
    assert MODEL == "jev-1.13.0"
    p = make_provider([FakeResp(200, OK_PAYLOAD)])
    p.judge("s", QUESTIONS)
    body = p._session.calls[0]["json"]
    assert body["model"] == "jev-1.13.0"
    assert p._session.calls[0]["url"] == API_URL
    assert p._session.calls[0]["headers"]["Authorization"] == "Bearer test-key"


def test_no_rolling_alias_anywhere():
    """代码与配置中禁止 jev-latest（文档的禁止性说明除外）。"""
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    hits = []
    for root, dirs, files in os.walk(repo):
        dirs[:] = [d for d in dirs if d not in (".venv", "__pycache__", ".git", "node_modules", "docs", "tests")]
        for fn in files:
            if fn.endswith((".py", ".json")):
                path = os.path.join(root, fn)
                with open(path, encoding="utf-8", errors="ignore") as f:
                    for i, line in enumerate(f, 1):
                        if "jev-latest" in line:
                            hits.append(f"{path}:{i}")
    assert hits == [], f"发现滚动别名引用：{hits}"
