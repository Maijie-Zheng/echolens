"""B 站公开接口客户端（M0-T1 数据层）。

**接口白名单（PRD 任务卡铁律 1）**：只允许访问以下公开接口，绝不触碰搜索 / 登录
/cookie / 个人中心等接口：

    x/web-interface/view      视频元信息（bvid -> aid / title / stat.reply）
    x/v2/reply/main           根评论游标分页（mode=2 按时间，可抓全量）
    x/v2/reply/reply          楼中楼子评论分页（pn 翻页）

**线上分页实测结论（probe 阶段验证）**：
1. 根评论必须用 `x/v2/reply/main`，旧 `x/v2/reply` 已废弃：只返回 3 条热评，
   `pn>=2` 直接失败。
2. `x/v2/reply/main?type=1&oid=<aid>&mode=2&next=<int>`：
   - 第一页 `next=0`；之后用响应 `data.cursor.next`（整数）继续；
   - 停止条件为 `data.cursor.is_end == true`。
   - `mode=2` 为时间序（全量），`mode=3` 为热度序（仅部分，不能用）。
3. 楼中楼用 `x/v2/reply/reply?type=1&oid=<aid>&root=<root_rpid>&pn=<int>`（`pn>=1`），
   按 `data.replies` 是否为空 / `page.count` 判停。
4. `stat.reply` 口径 = 根评论数 + 楼中楼子评论数（总量），因此「抓取完整性」
   以 (根 + 子) 总量对齐 `reply_count`，而非仅根评论数。

**限速与容错（铁律 4/5）**：
- 串行为主，单连接最小间隔 MIN_INTERVAL 秒 + 随机抖动 JITTER 秒；
- 遇到 HTTP 412 或 JSON `code` 命中风险码（RISK_CODES）时按 BACKOFF 退避重试；
- 最大并发 MAX_CONCURRENCY（预留，本实现默认串行），最大重试 MAX_RETRIES。

见 docs/DATA_FORMAT.md 了解落盘格式与匿名化规则。
"""

from __future__ import annotations

import json
import logging
import random
import time
from typing import Any, Dict, Iterator, List, Optional

import requests

from .models import Comment, VideoMeta

logger = logging.getLogger(__name__)

BASE = "https://api.bilibili.com"
VIEW_URL = BASE + "/x/web-interface/view"
REPLY_MAIN_URL = BASE + "/x/v2/reply/main"
REPLY_SUB_URL = BASE + "/x/v2/reply/reply"

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
HEADERS = {
    "User-Agent": UA,
    "Referer": "https://www.bilibili.com",
    "Accept": "application/json, text/plain, */*",
}

# 限速参数
MIN_INTERVAL = 0.8
JITTER = 0.4
MAX_CONCURRENCY = 3
MAX_RETRIES = 3
# 触发退避的 HTTP 状态码
RISK_HTTP = (412,)
# 触发退避的 JSON 业务码（-352 / -412 为风控，-404 等不在此列）
RISK_CODES = (-352, -412, 412)
# 退避秒数（按重试次数递增）
BACKOFF = (30, 60, 120)


class BilibiliError(Exception):
    """B 站接口调用失败（HTTP 异常 / 业务码非 0 / 风控）。"""

    def __init__(self, message: str, code: Optional[int] = None, status: Optional[int] = None):
        super().__init__(message)
        self.code = code
        self.status = status


class BilibiliClient:
    """B 站公开接口客户端，内置限速、退避与分页迭代。

    默认串行抓取（安全、合规）。`session` 可注入自定义 requests.Session；
    内部强制 `session.trust_env = False` 以绕过 shell 代理环境变量。
    """

    def __init__(
        self,
        session: Optional[requests.Session] = None,
        min_interval: float = MIN_INTERVAL,
        max_retries: int = MAX_RETRIES,
        logger: Optional[logging.Logger] = None,
    ) -> None:
        self.session = session or requests.Session()
        # 不读取系统代理环境变量，避免被公司代理拦截
        self.session.trust_env = False
        self.session.headers.update(HEADERS)
        self.min_interval = min_interval
        self.max_retries = max_retries
        self._log = logger or logging.getLogger(__name__)
        self._last_request = 0.0

    # ---- 限速 ----
    def _throttle(self) -> None:
        """保证两次请求之间至少间隔 min_interval + 随机抖动秒。"""
        elapsed = time.monotonic() - self._last_request
        wait = self.min_interval + random.uniform(0, JITTER)
        if elapsed < wait:
            time.sleep(wait - elapsed)

    def _backoff(self, attempt: int, reason: str) -> None:
        if attempt < len(BACKOFF):
            sleep_for = BACKOFF[attempt]
        else:
            sleep_for = BACKOFF[-1]
        self._log.warning("退避 %ss（第 %d 次）：%s", sleep_for, attempt + 1, reason)
        time.sleep(sleep_for)

    # ---- 底层请求 ----
    def _request(self, url: str, params: Dict[str, Any]) -> Dict[str, Any]:
        """带限速、重试、退避的 GET 请求，返回 `data` 字段字典。

        异常：
            - HTTP 非 200 / 412：BilibiliError
            - JSON 解析失败：BilibiliError
            - 业务 `code != 0`：BilibiliError（命中 RISK_CODES 时已在重试中退避）
        """
        for attempt in range(self.max_retries):
            self._throttle()
            try:
                resp = self.session.get(url, params=params, timeout=15)
            except requests.RequestException as exc:
                if attempt < self.max_retries - 1:
                    self._backoff(attempt, f"请求异常 {exc!r}")
                    continue
                raise BilibiliError(f"请求失败: {exc!r}") from exc

            self._last_request = time.monotonic()

            if resp.status_code in RISK_HTTP:
                if attempt < self.max_retries - 1:
                    self._backoff(attempt, f"HTTP {resp.status_code} 风控")
                    continue
                raise BilibiliError(f"HTTP {resp.status_code} 风控超限", status=resp.status_code)

            if resp.status_code != 200:
                raise BilibiliError(
                    f"HTTP {resp.status_code}", status=resp.status_code
                )

            try:
                payload = resp.json()
            except ValueError as exc:
                raise BilibiliError("响应不是合法 JSON") from exc

            code = payload.get("code")
            if code in RISK_CODES and attempt < self.max_retries - 1:
                self._backoff(attempt, f"业务码 {code} 风控")
                continue
            if code != 0:
                raise BilibiliError(
                    payload.get("message") or f"业务码 {code}", code=code
                )

            return payload.get("data", {})

        raise BilibiliError("重试次数耗尽仍未成功")

    # ---- 业务方法 ----
    def get_video_meta(self, bvid: str) -> VideoMeta:
        """获取视频元信息。"""
        data = self._request(VIEW_URL, {"bvid": bvid})
        aid = int(data.get("aid") or 0)
        if aid == 0:
            raise BilibiliError(f"view 接口未返回 aid（bvid={bvid}）")
        stat = data.get("stat") or {}
        return VideoMeta(
            bvid=bvid,
            aid=aid,
            title=data.get("title") or "",
            reply_count=int(stat.get("reply") or 0),
            fetched_at=time.time(),
        )

    def iter_root_comments(
        self,
        aid: int,
        start_next: int = 0,
        on_page: Optional[callable] = None,
    ) -> Iterator[Comment]:
        """迭代某个视频的**根评论**（游标分页，mode=2 时间序全量）。

        :param aid: 视频 aid
        :param start_next: 续抓起点 `next` 游标（断点续抓）；0 表示从第一页开始。
        :param on_page: 每完成一页后回调 `on_page(next_cursor, is_end)`，
                        用于落盘断点 checkpoint。
        :yield: 根评论 Comment（is_sub=False）
        """
        nxt = int(start_next)
        while True:
            data = self._request(
                REPLY_MAIN_URL,
                {"type": 1, "oid": aid, "mode": 2, "next": nxt},
            )
            replies: List[Dict[str, Any]] = data.get("replies") or []
            cursor = data.get("cursor") or {}
            is_end = bool(cursor.get("is_end", False))
            next_cursor = int(cursor.get("next", 0))

            for r in replies:
                root = int(r.get("root") or 0)
                # 只产出根评论（root==0）；个别接口会把楼中楼混入 replies
                if root == 0:
                    yield Comment.from_reply(r, is_sub=False)

            if on_page is not None:
                on_page(next_cursor, is_end)

            if is_end:
                break
            if not next_cursor or next_cursor == nxt:
                # 防御：游标未推进，避免死循环
                break
            nxt = next_cursor

    def iter_sub_comments(
        self,
        aid: int,
        root_rpid: int,
    ) -> Iterator[Comment]:
        """迭代某条根评论下的**楼中楼子评论**（pn 分页）。

        :param aid: 视频 aid
        :param root_rpid: 根评论 rpid
        :yield: 子评论 Comment（is_sub=True）
        """
        pn = 1
        while True:
            data = self._request(
                REPLY_SUB_URL,
                {"type": 1, "oid": aid, "root": root_rpid, "pn": pn},
            )
            replies: List[Dict[str, Any]] = data.get("replies") or []
            if not replies:
                break

            for r in replies:
                yield Comment.from_reply(r, is_sub=True)

            page = data.get("page") or {}
            count = int(page.get("count", 0))
            size = int(page.get("size", len(replies)))
            # 已取数量达到总数，或不足一页 -> 结束
            if count == 0 or len(replies) < size:
                break
            pn += 1


__all__ = ["BilibiliClient", "BilibiliError", "MAX_CONCURRENCY"]
