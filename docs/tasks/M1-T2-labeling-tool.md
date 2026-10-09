# 任务卡：M1-T2 人工标注工具（200 条基线）

> 本卡是 dev agent 的唯一信息来源。先读参考上下文，再开工。
> **本机 .git 有破坏性故障：严禁任何 git 操作，提交由编排方统一做。**

## 背景

M1 要用人工标注的 200 条评论做「金标准」，衡量 Jev 打标的一致率（PRD §9：≥80%；§10 M1 验收）。**铁律 5：标注本身必须用户亲自做**——你的活是准备抽样、标注界面、统计脚本，不是标注。

- PRD §6.1（10 个维度定义）、§9（成功指标）、§10 M1
- WORKFLOW §3 铁律 5

## 数据源（M0 已抓好的真实评论，直接读）

- `data\comments\BV1HHeC62EBs.jsonl`（100 条，根 54 + 子 46）
- `data\comments\BV1uSFHzbEJu.jsonl`（2014 条）
- `data\comments\BV1CRez6BENV.jsonl`（5033 条）

格式见 `docs\DATA_FORMAT.md`（每行 8 字段：rpid/root/parent/like/ctime/message/rcount/is_sub）。

## 交付物（全部绝对路径）

| 文件 | 内容 |
|---|---|
| `scripts\make_label_sample.py` | 分层抽样脚本：从 3 个视频共抽 **200 条**（按比例约 20/70/110；再按点赞分档 0 / 1-9 / 10-99 / 100+ 分层；根评论与子评论按源数据比例保留）。随机种子固定（--seed，默认 42）保证可复现。输出 `data\labeling\sample.jsonl`，每行 `{"rpid","bvid","message","like","ctime","is_sub"}`，并打印分层分布表 |
| `data\labeling\label.html` | **单文件、零依赖、双击即用**的标注界面（sample.jsonl 内容直接内嵌进 HTML，不起本地服务）。功能：一次显示一条评论（显示序号 x/200、正文、点赞数、是否楼中楼）；10 个维度以开关按钮呈现（key + 中文名 + 一行定义，见下）；**多标签**（一条评论可同时勾选多个维度，也可全不勾=「无有效信息」）；键盘快捷键：数字键 1-9 与 0 切换对应维度（0 = 第 10 项 discussion）、→ 下一条、← 上一条；进度条 + 已标/未标计数；自动保存到 localStorage（刷新不丢）；「导出 JSON」按钮下载 `human_labels.json`（结构：`{"<rpid>": ["praise","meme",...], ...}`）；「检查未标」按钮跳到第一个未标条目 |
| `scripts\agreement.py` | 一致率统计：读 human_labels.json + `data\labels\sample_labels.jsonl`（Jev 对同样本的打标，由 M1-T1 的管线后续产出，文件可能还不存在——脚本要对缺失文件报清晰错误）。Jev 的 0~1 概率按阈值 0.5 转布尔。输出：每个维度的一致率、宏平均、按 Cohen's kappa（可选）、分歧最大的 20 条列出（rpid + 正文截断 80 字 + 人工标 vs Jev 标），写 `data\labeling\agreement_report.md` |

10 个维度（key / 中文名 / 一行定义，与 PRD §6.1 v0.2 一致）：
praise 夸、 criticism 批、 suggestion 建议、 question 提问、 meme 玩梗、 venting 情绪宣泄、 spam 水军/广告、 off_topic 跑题、 about_creator 针对 UP 主本人、 discussion 讨论内容

## 验收标准（reviewer 逐条核对）

- [ ] 抽样 200 条整，种子固定可复现；分层分布打印齐全；样本覆盖 3 个视频
- [ ] label.html 双击即用（file:// 协议可用），不依赖任何网络/服务器；内嵌数据与 sample.jsonl 一致
- [ ] 标注可多选、可全不选；快捷键全部生效；localStorage 自动保存（刷新后标注还在）；导出 JSON 结构正确
- [ ] agreement.py 对缺失的 Jev 标注文件给出清晰错误提示；输出报告含每维度一致率 + 宏平均 + 分歧清单
- [ ] 10 个维度 key 与 PRD §6.1 完全一致（拼写逐字母对）
- [ ] 无 git 操作；不改 echolens/ 源码；不引入新 Python 依赖（标准库即可；HTML 内嵌原生 JS）

## 边界

- **不要替用户标注**——sample 的 human_labels 由用户本人产出（铁律 5）
- 不做 Jev 调用（M1-T1 的事）；不做分析层聚合
- label.html 保持单文件零依赖，不引 npm/CDN（file:// 打开 CDN 可能被拦）
- UI 给中文用户用，简洁耐看即可，不追求花哨

## 参考上下文

- PRD：`C:\Users\Y9000\Desktop\EchoLens\PRD.md`（§6.1、§9、§10 M1）
- 数据格式：`C:\Users\Y9000\Desktop\EchoLens\docs\DATA_FORMAT.md`
- 工作流铁律：`C:\Users\Y9000\Desktop\EchoLens\docs\WORKFLOW.md` §3
- 解释器：`C:\Users\Y9000\Desktop\EchoLens\.venv\Scripts\python.exe`
