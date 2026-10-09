# 任务卡：M1-T1 判断层（Provider 抽象 + Jev 客户端 + 打标管线）

> 本卡是 dev agent 的唯一信息来源。先读参考上下文，再开工。
> **本机 .git 有破坏性故障：严禁任何 git 操作（不 init/add/commit/branch），提交由编排方统一做。**

## 背景

EchoLens 四层架构：数据层（M0 已完成）→ **判断层（本任务）** → 分析层 → 呈现层。层间只用 JSONL 文件解耦。

- PRD §4 F2（判断层）、§6.1（10 个分类维度）、§6.2（每条评论一次调用）、§6.4（成本性能预算）
- PRD §7 D2（每评论一调用）、D3（锁 `jev-1.13.0`）、D4（评论是不可信输入）、D6（结果缓存）、D7（Provider 抽象）
- WORKFLOW §3 铁律（尤其 3：评论不可信；4：任何地方不得出现 `jev-latest`）

## Jev API 事实（2026-10-09 从官方文档提取，照此实现）

- `POST https://api.typesafe.ai/v1/systemone`，`Authorization: Bearer $TYPESAFE_API_KEY`，JSON body
- 请求：`{"state": <字符串|对象|数组>, "model": "jev-1.13.0", "questions": {"<key>": {"type": "noul", "instructions": "<是非问句>", "criteria": {"true": "...", "false": "..."}（可选）}, ...}}`
- 响应：`{"model": "jev-1.13.0", "answers": {"<key>": {"type": "noul", "noul": <0~1 浮点>}}, "usage": {"input_tokens": int, "output_tokens": int}}`
- **同一次调用可混合多个问题，并行求值，加问题几乎不增加耗时**——所以每条评论一次调用带全部 10 个 Noul
- 错误码：401 key 无效（不重试，直接报错）；422 请求体验证失败（不重试）；429 限速 / 529 过载（指数退避重试）
- Noul 答案是「是」的概率（0~1），无 confidence 字段（Choice/Score 才有）
- 认证 key：环境变量 `TYPESAFE_API_KEY`；本机已备 `C:\Users\Y9000\Desktop\EchoLens\.env`（已 gitignore），可用 python 手动读该文件注入环境（不引 python-dotenv 依赖，自己写 10 行读取即可）

## 交付物（全部绝对路径；在现有代码基础上新增，不改 M0 代码）

| 文件 | 内容 |
|---|---|
| `echolens\judge\provider.py` | `JudgeProvider` 抽象基类（D7）：`judge(state: str, questions: dict) -> dict`，定义统一答案结构 |
| `echolens\judge\jev.py` | `JevProvider`：raw HTTP（用 requests，不引 typesafe-sdk）、model 锁 `jev-1.13.0`、429/529 指数退避（最多 5 次）、401/422 立即抛错、超时 30s、`trust_env=True`（尊重环境变量代理；2026-10-09 实测本机直连 api.typesafe.ai 被 451 区域封锁，需设 `HTTPS_PROXY=http://127.0.0.1:10809`；与 fetch 层 trust_env=False 的差异是「B站国内直连 vs Jev 境外被墙」） |
| `echolens\judge\questions.json` | 10 个分类问题，config 化（key → instructions，基于 PRD §6.1 v0.2 定义改写为是非问句，符合 Noul 语义；`discussion` 附 true/false criteria 编码两条标注边界） |
| `echolens\judge\sanitize.py` | D4 防注入：评论截断（默认 1000 字符上限）+ 控制字符清理；注释里说明「评论永远作为被评判的 state 数据，不作为指令」 |
| `echolens\judge\label.py` | 打标管线：读 `data/comments/<bvid>.jsonl` → 每条一次调用（10 Noul）→ 写 `data/labels/<bvid>.jsonl`；并发 worker（默认 16，硬上限 20）+ 全局限速（默认 1000 req/min，PRD 预算 1200 留余量）；**断点续跑**（读已有 labels 跳过已打标 rpid）；汇总统计（耗时、token 用量、估算成本） |
| `echolens\judge\__main__.py` | CLI：`python -m echolens.judge <bvid> [--limit N] [--concurrency N] [--fresh]`，进度输出风格与 fetch 层一致 |
| `docs\DATA_FORMAT.md` | 追加一节：labels JSONL 格式定义（在现有文件末尾追加，不改前文） |

labels JSONL 每行：`{"rpid": int, "model": "jev-1.13.0", "labels": {"praise": 0.0~1.0, "criticism": ..., "suggestion": ..., "question": ..., "meme": ..., "venting": ..., "spam": ..., "off_topic": ..., "about_creator": ..., "discussion": ...}}`（10 个 key 固定，与 questions.json 对应）

## 验收标准（reviewer 逐条核对）

- [ ] 每条评论恰好 1 次 API 调用，携带全部 10 个 Noul 问题
- [ ] 请求 model 字段 = `jev-1.13.0`；全仓库 grep `jev-latest` 零命中（文档的禁止性说明除外）
- [ ] key 只从环境变量 / .env 读取；全仓库 grep 该 key 值零命中；key 缺失时报错信息清晰
- [ ] 429/529 退避重试、401/422 不重试（mock 测试覆盖）
- [ ] 断点续跑：中断后重跑不重复打标（rpid 去重）
- [ ] 并发 ≤20、限速可配；sanitize 截断生效（超长评论被截到上限）
- [ ] labels JSONL 每行合法 JSON、10 个维度齐全、值在 [0,1]
- [ ] 可 mock 单测（网络隔离在 jev.py，provider 可注入 fake transport）
- [ ] 只做本任务范围：不动 `fetch/`、不写 tests/（tester 的活）、无 git 操作

## 边界

- 依赖只许 requests（已在 venv）；不引 typesafe-sdk / python-dotenv / aiohttp
- 不做分析层的事（不聚合、不出建议）；不做标注工具（那是 M1-T2）
- 真实 API 冒烟只允许 `--limit 50` 以内的小规模（约 $0.003）；大规模性能验收是 tester 的活
- 不改 PRD.md / WORKFLOW.md / M0 代码 / tests 目录

## 参考上下文

- PRD：`C:\Users\Y9000\Desktop\EchoLens\PRD.md`（§4、§6.1、§6.2、§6.4、§7、§10 M1）
- 工作流：`C:\Users\Y9000\Desktop\EchoLens\docs\WORKFLOW.md` §3 铁律
- M0 代码（风格与落盘约定对齐）：`C:\Users\Y9000\Desktop\EchoLens\echolens\fetch\`（重点看 cache.py 的落盘三件套模式和 __main__.py 的 CLI 结构）
- 已有评论数据（冒烟用）：`C:\Users\Y9000\Desktop\EchoLens\data\comments\BV1HHeC62EBs.jsonl`（100 条）
- 解释器：`C:\Users\Y9000\Desktop\EchoLens\.venv\Scripts\python.exe`
- key 位置（禁止复制进任何仓库文件）：`C:\Users\Y9000\.workbuddy\mcp.json` → mcpServers."jev-browser".env.TYPESAFE_API_KEY
