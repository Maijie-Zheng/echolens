# EchoLens 数据层落盘格式（M0-T1）

本文档定义 B 站评论抓取模块（数据层）的落盘文件格式、匿名化规则与断点续抓语义。

## 1. 目录结构

默认落盘根目录为 `data/`（可用 `--out` 覆盖）。每个视频独立成组：

```
data/comments/<bvid>.jsonl      评论逐行 JSON（append-only）
data/comments/<bvid>.meta.json  视频元信息 + 抓取统计
data/comments/<bvid>.state.json 断点续抓状态（完成后删除）
```

## 2. 评论文件 `<bvid>.jsonl`

每行一条评论（UTF-8，不带 BOM），JSON 字段严格限定为 **8 个白名单字段**：

| 字段      | 类型    | 含义                                   |
| --------- | ------- | -------------------------------------- |
| `rpid`    | int     | 评论 ID（去重与断点续抓用，**非身份**）|
| `root`    | int     | 所属根评论 rpid；根评论自身为 `0`      |
| `parent`  | int     | 父评论 rpid（楼中楼直接回复对象）      |
| `like`    | int     | 点赞数                                 |
| `ctime`   | int     | 评论发布时间（Unix 秒）               |
| `message` | str     | 评论正文                               |
| `rcount`  | int     | 该评论下的楼中楼数量（仅根评论有意义）|
| `is_sub`  | bool    | 是否为楼中楼子评论                     |

示例：

```json
{"rpid": 123456, "root": 0, "parent": 0, "like": 12, "ctime": 1690000000, "message": "点赞", "rcount": 3, "is_sub": false}
{"rpid": 123457, "root": 123456, "parent": 123456, "like": 1, "ctime": 1690000060, "message": "回复", "rcount": 0, "is_sub": true}
```

### 匿名化铁律

**严禁**在任何落盘文件中写入评论者身份信息。以下字段 / 对象一律不读取、不落盘：

- 用户 ID（`mid`）、昵称（`uname`）、头像（`avatar`）、签名（`sign`）、等级（`level_info`）；
- `member` 对象及其任何子字段；
- 任何 IP / 地理位置 / 设备 / 登录态信息。

`models.Comment.from_reply` 在构造时**主动丢弃** `member` 等字段，只抽取上述 8 个白名单字段；`meta.json` 也只含视频级公开信息。

## 3. 元信息文件 `<bvid>.meta.json`

```json
{
  "bvid": "BV1HHeC62EBs",
  "aid": 170000000,
  "title": "示例视频标题",
  "reply_count": 98,
  "fetched_at": 1690000100.0,
  "stats": {
    "root_stored": 54,
    "sub_stored": 44,
    "total_stored": 98,
    "reply_count_api": 98,
    "complete": true
  }
}
```

- `reply_count` 来自接口 `stat.reply`，口径为 **根评论 + 楼中楼子评论的总量**。
- `stats.complete` 表示「已落盘总量 (root + sub) >= reply_count」。

> 实测结论：`stat.reply` = 根评论数 + 楼中楼数（例如某视频根 161 + 子 202 = 363 ≈ 接口报告 362）。因此「抓取完整性」以 (根 + 子) 总量对齐 `reply_count`，而非仅根评论数。

## 4. 断点续抓状态文件 `<bvid>.state.json`

```json
{
  "phase": "sub",
  "root_cursor": null,
  "sub_done": [123456, 123458]
}
```

| 字段          | 类型            | 含义                                             |
| ------------- | --------------- | ------------------------------------------------ |
| `phase`       | str             | `root`：抓根评论；`sub`：抓楼中楼；`done`：完成  |
| `root_cursor` | int \| null     | 根评论游标 `next`；`null` 表示第一页 / 已到末尾  |
| `sub_done`    | list[int]       | 已完成楼中楼抓取的根评论 rpid 列表               |

### 续抓语义

- 程序启动先读 `state.json`（除非 `--fresh`）；`--fresh` 忽略已有断点从头抓，**并清空该视频已落盘的 jsonl 评论与 meta 统计**（删除旧 jsonl / meta / state 三文件），保证重新抓取后落盘内容恰为本次结果，不残留旧数据、不产生重复 rpid。
- 每完成一页根评论 / 每条根评论的楼中楼，都会更新并写回 `state.json`。
- 中断（Ctrl+C / 接口错误）时保存 `state.json`，下次从断点继续，**不会重复抓取**（配合 jsonl 中已存 rpid 去重）。
- 全部完成后删除 `state.json`。

## 5. 线上接口与分页实测结论

| 接口                       | 用途            | 分页方式                              | 备注                                   |
| -------------------------- | --------------- | ------------------------------------- | -------------------------------------- |
| `x/web-interface/view`     | 视频元信息      | 单条                                  | 返回 `aid` / `title` / `stat.reply`    |
| `x/v2/reply/main`          | 根评论          | 游标：`next` + `cursor.is_end`        | `mode=2` 时间序全量；`mode=3` 仅热评   |
| `x/v2/reply/reply`         | 楼中楼          | `pn` 递增，按 `replies` 是否为空判停  | `root=<root_rpid>`                     |

- 旧 `x/v2/reply` 已废弃：只返回 3 条热评，`pn>=2` 直接失败，停止使用。
- 根评论第一页 `next=0`，之后用响应 `data.cursor.next` 继续，直到 `data.cursor.is_end == true`。
- 楼中楼用 `x/v2/reply/reply?type=1&oid=<aid>&root=<root_rpid>&pn=<int>`，`pn` 从 1 开始。

## 6. 限速与容错

- 串行为主，单连接最小间隔 0.8s + 随机抖动 0.4s；预留最大并发 3（默认不启用）。
- 遇到 HTTP 412 或业务码命中风险码（`-352` / `-412` / `412`）时，按 30/60/120s 退避重试，最多 3 次。
- 强制 `session.trust_env = False` 绕过 shell 代理。

## 7. 接口白名单

数据层**只允许**访问以下公开接口，绝不触碰搜索 / 登录 / cookie / 个人中心等接口：

- `x/web-interface/view`
- `x/v2/reply/main`
- `x/v2/reply/reply`

---

# 判断层落盘格式（M1-T1）

以下定义判断层（`echolens/judge/`）的输出文件格式。

## 8. 打标结果文件 `data/labels/<bvid>.jsonl`

每行一条打标结果（UTF-8，不带 BOM，append-only），字段严格限定为 3 个：

| 字段     | 类型   | 含义                                                       |
| -------- | ------ | ---------------------------------------------------------- |
| `rpid`   | int    | 评论 ID，与 `data/comments/<bvid>.jsonl` 一一对应          |
| `model`  | str    | 打标模型版本，锁 `jev-1.13.0`（PRD 决策 D3）               |
| `labels` | object | 9 个分类维度的 Noul 概率（「是」的概率，值域 [0,1]）       |

`labels` 固定包含 9 个 key（与 `echolens/judge/questions.json` 一一对应，见 PRD §6.1）：
`praise` / `criticism` / `suggestion` / `question` / `meme` / `venting` / `spam` / `off_topic` / `about_creator`。

示例：

```json
{"rpid": 318263056080, "model": "jev-1.13.0", "labels": {"praise": 0.02, "criticism": 0.91, "suggestion": 0.35, "question": 0.78, "meme": 0.04, "venting": 0.11, "spam": 0.0, "off_topic": 0.06, "about_creator": 0.03}}
```

### 打标语义与断点续跑

- **每条评论恰好一次 Jev 调用**，一次调用携带全部 9 个 Noul 问题并行求值（PRD §6.2 D2）。
- 评论正文先经 `sanitize.py` 清洗（控制字符清理 + 1000 字符截断，D4 防注入），仅作为被评判的 `state` 传入，不作为指令。
- 结果逐条追加落盘；重跑时读已有 labels 的 `rpid` 集合去重，**已打标评论不重复调用**（D6 结果缓存）。
- `--fresh` 删除该视频的 labels 文件后从头打标。
- 行序不保证与 comments 文件一致（并发写入）；消费方按 `rpid` join。
