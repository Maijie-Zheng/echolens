# 任务卡：M0-T1 B站评论抓取模块（数据层）

> 本卡是 dev agent 的唯一信息来源。开工前先读参考上下文里的三份文件。

## 背景

EchoLens 把视频评论区变成一张「成分表」。四层架构（数据/判断/分析/呈现）之间只用 JSONL 文件解耦。本任务是 M0 的唯一交付：**数据层**——输入 BV 号或 URL，抓全量评论，匿名化落盘，支持断点续抓。

- PRD §4 F1（数据获取）、§6.2（四层架构与数据流）
- PRD §7 D6（JSONL 解耦）、D8 + D8 附注（接口实测结论与分页怪癖）
- PRD §8.1（合规边界，写死在代码里）
- PRD §10 M0（验收标准）

## 交付物

在分支 `feature/m0-bilibili-fetch`（从 main 切出）上开发，全部使用绝对路径：

| 文件 | 内容 |
|---|---|
| `C:\Users\Y9000\Desktop\EchoLens\echolens\fetch\models.py` | 数据结构：`VideoMeta`（bvid, aid, title, reply_count, fetched_at）、`Comment`（rpid, root, parent, like, ctime, message, rcount, is_sub） |
| `C:\Users\Y9000\Desktop\EchoLens\echolens\fetch\bilibili.py` | `BilibiliClient`：视频信息 + 评论分页抓取，限速、重试、防风控退避。网络只许出在这一层 |
| `C:\Users\Y9000\Desktop\EchoLens\echolens\fetch\cache.py` | JSONL 追加写、按 rpid 去重、checkpoint 读写（断点续抓） |
| `C:\Users\Y9000\Desktop\EchoLens\echolens\fetch\__main__.py` | CLI：`python -m echolens.fetch <BV号或URL> [--out data] [--no-sub] [--fresh]` |
| `C:\Users\Y9000\Desktop\EchoLens\docs\DATA_FORMAT.md` | JSONL 缓存格式定义（字段、类型、示例、meta/state 文件说明） |
| `C:\Users\Y9000\Desktop\EchoLens\requirements.txt` | 新增一行 `requests` |

运行入口：`C:\Users\Y9000\Desktop\EchoLens\.venv\Scripts\python.exe -m echolens.fetch <BV或URL>`

## 关键设计约束（已拍板，照做）

1. **接口只用 PRD D8 实测通过的两个**：`x/web-interface/view?bvid=...`（拿 aid/标题/评论数）和评论接口（`x/v2/reply` 系）。**禁止搜索接口**（412 风控），禁止 cookie/登录态。请求头带常见浏览器 UA 和 `Referer: https://www.bilibili.com`。
2. **分页怪癖**：`sort=2`（热度）第 1 页只返回 3 条热评；翻页用响应里的游标/`next` 字段。样本 `data\samples\reply_sample.json` 是真实响应但它没有 cursor 字段——**先用小评论区探测线上真实行为，以线上为准**，把结论写进代码注释和 commit message。完整性的判据是「抓到的根评论数 ≈ view 接口报告的评论数」（允许少量被删评论的损耗）。
3. **楼中楼（子评论）**：默认也抓（`x/v2/reply/reply`，root=根评论 rpid，仅当 rcount>0），`--no-sub` 可关闭。
4. **限速**：并发硬上限 ≤ 3；默认串行 + 每次请求间隔 ≥0.8s（带抖动）。HTTP 412 或 code=-352 → 指数退避（30s/60s），重试 ≤3 次仍失败则报错退出并提示稍后用断点续抓。
5. **断点续抓**：每个视频三个文件——`data/comments/<bvid>.jsonl`（一行一条评论）、`data/comments/<bvid>.meta.json`（视频元信息+抓取统计）、`data/comments/<bvid>.state.json`（checkpoint：阶段 root/sub/done、游标位置；完成后删除）。重跑默认续抓：从已有 JSONL 加载已见 rpid 集合去重；`--fresh` 从头再来。
6. **匿名化（铁律3 + PRD §8.1）**：落盘只含任务卡 models.py 里列的字段。**严禁落盘** mid/uname/avatar/sign/level 等任何评论者身份信息——API 响应里的 `member` 字段直接丢弃。rpid 是评论 ID（去重需要），不是用户身份，可以存。
7. **进度输出**：每 20 页或每 500 条打一行进度（已抓/总数/当前阶段），最终打印汇总（根评论数、子评论数、耗时、落盘路径）。

## 验收标准（reviewer 按此逐条核对）

- [ ] CLI 输入 BV 号或完整 URL 均能解析并跑通
- [ ] 抓 1 个真实小评论区（<100 条）端到端成功，根评论数与 view 接口报告数一致（允许被删损耗 ≤5%）
- [ ] JSONL 每行是合法 JSON，字段与 `docs/DATA_FORMAT.md` 完全一致；全文件无任何评论者身份字段（grep mid/uname/avatar 应为零命中，除文档说明外）
- [ ] 中断后重跑不重复、不丢数据（续抓后 rpid 无重复）
- [ ] 限速逻辑存在且并发 ≤3；412/网络错误有退避重试
- [ ] 子评论抓取可开关（--no-sub）
- [ ] commit 在 `feature/m0-bilibili-fetch` 分支，conventional commits，一个 commit 只做一件事；**未合并 main、未 push**
- [ ] 代码可被 mock session 单测（网络隔离在 bilibili.py，client 可注入 session）

## 边界（明确不要做的事）

- 不要动 `echolens/judge/`、`echolens/analyze/`、`echolens/report/`、`tests/`（tests 归 tester）
- 不要引入 requests 以外的依赖
- 不要实现「按关键词找视频」；不处理 b23.tv 短链（README 注释说明即可）
- 不要在代码或文档里出现 `jev-latest`（判断层是 M1 的事）
- 不要 git push、不要合并 main、不要改 PRD.md / WORKFLOW.md
- 抓真实视频验证时只挑 <500 条评论的（实测三档视频是 tester 的活）

## 参考上下文

- PRD：`C:\Users\Y9000\Desktop\EchoLens\PRD.md`（重点 §4、§6.2、§7 D6/D8、§8.1、§10 M0）
- 工作流与铁律：`C:\Users\Y9000\Desktop\EchoLens\docs\WORKFLOW.md`
- 真实接口样本：`C:\Users\Y9000\Desktop\EchoLens\data\samples\reply_sample.json`
- Python 解释器（venv 已装好 requests/pytest）：`C:\Users\Y9000\Desktop\EchoLens\.venv\Scripts\python.exe`
- B站是国内站点，直连即可；若 shell 里有 HTTP(S)_PROXY 环境变量，对本项目请忽略/绕过代理。
