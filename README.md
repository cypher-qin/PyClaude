# PyClaude — 个人编码 Agent

单进程、交互式 REPL 的**个人编码 Agent**。启动后进入命令行，输入自然语言任务，Agent 自己调用工具、完成多步工作。接任意 **Anthropic 兼容**的模型网关（Kimi / DeepSeek / GLM / 官方 Claude…），换厂商只改 `.env`，一行代码不动。

源自 [learn-claude-code](https://github.com/shareAI-lab/learn-claude-code)（一个逐章教你写 Agent 的教学仓库），但**不是照抄**：只保留「单人日常使用」真正有用的机制，并有两个方向超越教学版——**记忆系统**对标 Claude Code 的真实结构分层重写、**MCP 集成**是真实的 JSON-RPC over stdio 实现（教学版只是写死的 Mock）。

---

# 第一部分 · 如何使用

## 1. 快速开始

```bash
# 1. 依赖（本仓库已内置 .venv，可直接跳过；或自建）
python -m venv .venv
.venv\Scripts\activate           # Windows PowerShell
pip install -r requirements.txt

# 2. 配置：编辑 .env，填入你的 API Key（见 .env.example 注释）

# 3. 运行
python agent.py
```

> 仓库已内置 `.venv`（依赖装好）。你只需在 `.env` 填真实 Key 即可联网运行；直接用内置 venv 跑：`.venv\Scripts\python.exe agent.py`。

启动后进入交互式 REPL，输入自然语言任务，Agent 自己调工具完成；输入 `q` 退出。

## 2. 换模型：唯一改动点 `.env`

代码用 Anthropic 官方 SDK，它原生支持「Anthropic 兼容」的第三方网关，所以换厂商不用改代码，只改 `.env` 三行（Key / Base URL / 模型 ID）：

| 厂商 | `ANTHROPIC_BASE_URL` | `MODEL_ID`（示例） |
|---|---|---|
| Kimi（国内） | `https://api.moonshot.cn/anthropic` | `kimi-k2-0711-preview` |
| Kimi（海外） | `https://api.moonshot.ai/anthropic` | `kimi-latest` |
| 官方 Anthropic | 留空 | `claude-sonnet-4-5` |
| DeepSeek | `https://api.deepseek.com/anthropic` | `deepseek-chat` |
| GLM（智谱） | `https://open.bigmodel.cn/api/anthropic` | `glm-4.6` |

> 模型 id 仅为示例，以各厂商控制台当前值为准。完整配置项见 [.env.example](.env.example)（含各子系统开关）。

## 3. 会话存档与恢复

每轮对话结束，完整上下文自动快照写入 `.sessions/`（原子重写，崩溃最多丢进行中的半轮）。参照 Claude Code / Codex CLI 的恢复三件套：

```bash
python agent.py -c            # 恢复最近一次会话
python agent.py -r [id前缀]   # 带前缀直接匹配；不带则交互选择
python agent.py --list        # 列出存档后退出
python agent.py -c --full     # 全量回放（默认只回放最近 5 轮）
```

会话内切换用斜杠命令：`/resume`（选择并切换）、`/new`（开新会话）、`/sessions`（只列不切）。

## 4. 常用能力速览

| 想干什么 | 怎么做 |
|---|---|
| 慢命令不阻塞（安装/构建/长测试） | `bash` 工具带 `run_in_background: true` 丢后台，完成后自动 `<task_notification>` 送回；`bg_output` 看输出、`bg_kill` 停任务、`/bg` 查看列表 |
| 跨会话的任务清单 | `task_create` / `task_update` / `task_get` / `task_list` 四个工具，带依赖图、认领、状态流转 |
| 长期记忆 | Agent 用 `remember` / `forget` 写 `.memory/`，跨会话存活；也可直接 `read_file .memory/MEMORY.md` |
| 接外部工具 | 设 `MCP_MODE=manual`（手写 client）或 `sdk`（官方 SDK），默认 `off`；两条路径连同一个 server，对模型等价 |
| 无人值守 / 全自主 | `.env` 设 `AUTONOMOUS_MODE=1`（危险命令确认、工作区外写审核会跳过；毁灭性命令硬底线仍保留） |

---

# 第二部分 · 总体架构

## 1. Agent循环

T10 的一切机制都围绕**一个循环**转：

```
取模型 → 拿到文本 / 工具调用 → 执行工具 → 把结果回灌上下文 → 再取模型
```

`agent.py`（约 1100 行）是这个循环的汇聚点，其他所有文件都是往这个骨架里「插槽子」。理解 T10，先记住这个循环，再看各子系统怎么挂进去。

## 2. 三个贯穿全局的设计思想

| 概念 | 含义 |
|---|---|
| **工具池（Tool Pool）** | 一张「名称 → 函数」的表 + 一张同名的 schema 表。模型只能看到 schema（名字、参数、说明），实际执行落在 Python 函数上。**加一个子系统 = 往这两张表各加一行。** |
| **装配层（Integration Layer）** | 每个子系统对 `agent.py` 只暴露一个入口文件（如 `memory_integration.py`）。`agent.py` 只 import 这里，子系统内部随便重构，主程序一行不动。 |
| **复制注入（Copy Injection）** | 有些内容（记忆、规划提醒）不该写进持久历史——调用模型前在 `messages` 的**副本**上临时拼接，用完即弃，不污染对话。 |

## 3. 可扩展性模型：主循环永远不懂子系统

所有子系统（记忆 / 任务 / 后台 / MCP）都以同一种形状汇入主循环：暴露 `xxx_tools() -> (tools, handlers)`，再由 `assemble_tool_pool()` 机械地并池。并池之后，内置工具和子系统工具对模型、对循环、对权限钩子**完全一视同仁**。

**一个新子系统 = 一个 `xxx_integration.py` 文件 + 在主循环的几个固定点各调一次它的入口函数。** 主循环本身永远不用懂这个子系统在干什么。禁用某个子系统（如 `MEMORY_ENABLED=0`）时它返回空，等于那个子系统不存在——「配置即删减」。

## 4. Hooks 与权限：策略不侵入循环

主循环在执行工具前后预留了四个「插槽」，让外部逻辑**不侵入循环**就能加策略：

```
UserPromptSubmit → PreToolUse → PostToolUse → Stop
```

权限系统挂在 `PreToolUse` 上，分三层：**黑名单直接拒绝**（`rm -rf /`、`shutdown` 等毁灭性命令，任何模式下都不放行）→ **危险操作弹确认**（`rm`、`git push`、`git reset --hard`…）→ **工作区外写审核**（改动预览请用户批准）。

关键设计：权限拦截**不是报错，而是给模型的一条工具结果**（`Permission denied: ...` 回灌进上下文），模型会看到它并自行调整策略。

## 5. 各子系统一览

| 子系统 | 一句话 | 装配层 |
|---|---|---|
| **记忆 Memory** | 跨会话长期记忆，分层存储：索引常驻 system + 正文按需注入，压缩也不丢 | `memory_integration.py` |
| **任务 Task** | 跨会话持久任务图：拆任务、连依赖、认领、逐个执行 | `task_integration.py` |
| **规划催办 Plan** | 启发式打分，温和可忽略地提醒「该继续推进了」 | `plan_integration.py` |
| **后台任务 BG** | 慢命令丢后台执行，完成后通知送回，可查可停 | `background_integration.py` |
| **会话 Session** | 每轮快照存档 + `-c/-r` 恢复，重启不失忆 | `session_integration.py` |
| **MCP** | 统一工具协议接外部 server，一个开关切手写/官方 SDK 两条实现 | `mcp_integration.py` |

## 6. 上下文与成本：两道防线

模型的上下文窗口是有限资源，T10 用**源头收窄 + 事后压缩**两道防线应对：

- **源头**：超大工具输出/入参在产生那一刻就落盘 `.task_outputs/`，上下文只留头尾预览；正常收尾轮次的 thinking 块剥掉。
- **事后**：每次调用模型前检查——消息太多裁中间段（保证 tool 配对不拆散）、太大的旧输出替换成占位、超限就触发模型摘要。

成本上对应一套「按端点真实能力决策」的机制：token 预算门（逼近额度暂停询问）、prompt cache 探针（启动时实测端点是否真打折，再决定挂不挂缓存断点）等。这些是「能省则省」的优化，不影响使用。

## 7. 目录地图

```
T10/
  agent.py                    ★ 主程序：一个循环 + 全部机制
  .env / .env.example         ★ 配置（Key + 各子系统开关）
  session_store.py            会话存档存储层
  session_integration.py      会话装配层（-c/-r/--list + 斜杠命令 + 回放）
  memory_integration.py       记忆装配层（remember/forget + 预取 + 提炼）
  memory/                     记忆内部实现
  task_integration.py         任务装配层（四个 task_* 工具）
  tasks/store.py              任务存储层
  plan_integration.py         规划催办装配层
  background_integration.py   后台任务装配层
  status_report.py            运行状态报告（动画 + token/耗时结算）
  mcp_integration.py          MCP 装配层（一个开关切两条路径）
  mcp_manual/                 手写 MCP client + GitHub server
  mcp_sdk_client.py           官方 mcp SDK 客户端
  skills/                     按需加载的知识包（每目录一个 SKILL.md）
  reference/s20_full_agent.py 教学版完整源码，需要高级功能时参考
```

运行时自动生成（均已 gitignore）：`.sessions/`（会话存档）、`.tasks/`（任务图）、`.memory/`（长期记忆）、`.transcripts/`（压缩前冷备份）、`.tmp/bg/`（后台任务日志）。

---

## License

本项目基于 [shareAI-lab/learn-claude-code](https://github.com/shareAI-lab/learn-claude-code)（MIT License）提炼而来，`skills/` 与 `reference/s20_full_agent.py` 直接复用其代码，特此致谢。本项目自身采用 MIT License，详见 [LICENSE](LICENSE)。
