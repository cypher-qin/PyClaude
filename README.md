# T10(Version B1)

一个基于 [learn-claude-code](https://github.com/shareAI-lab/learn-claude-code) 提炼的**个人编码 Agent**，接入 Kimi（Moonshot）API。

单文件 `agent.py`（约 500 行），保留了「单人日常使用」真正有用的 harness 机制，去掉了偏演示的多机协作/定时/MCP-mock 部分。可直接使用，也方便你后续迭代。

---

## 快速开始

```bash
# 1. 安装依赖（本仓库已内置 .venv，可跳过；或自建）
python -m venv .venv
.venv\Scripts\activate           # Windows PowerShell
pip install -r requirements.txt

# 2. 配置 Kimi Key
#    编辑 .env，把 ANTHROPIC_API_KEY 换成你自己的 sk-... （见 .env.example 注释）

# 3. 运行
python agent.py
```

> 本仓库已经用 `.venv`（内置好依赖）验证过离线工具链。你只需要在 `.env` 里填入真实的 Kimi Key 即可联网运行。
> 直接用内置 venv 跑：`.venv\Scripts\python.exe agent.py`

启动后进入交互式 REPL，输入自然语言任务，agent 会自己调用工具去完成；输入 `q` 退出。

---

## 会话存档与恢复

每轮对话结束，完整上下文自动快照写入 `.sessions/`（原子重写，进程崩溃只丢进行中的半轮）。
参照 Claude Code / Codex CLI 的恢复三件套：

```bash
python agent.py -c            # 恢复最近一次会话
python agent.py -r [id前缀]   # 带前缀直接匹配；不带则交互选择
python agent.py --list        # 列出存档后退出
python agent.py -c --full     # 全量回放（默认只回放最近 5 轮，SESSION_REPLAY 可配）
```

会话内切换：`/resume`（选择并切换）、`/new`（开新会话）、`/sessions`（列表）。
切换时旧对话留在终端 scrollback 可回翻，以分隔带区分；恢复后续写同一存档文件，不 fork。

与 `.transcripts/` 的分工：transcripts 是压缩前的冷备份（只写不读，审计层）；
`.sessions/` 是恢复层（写了也读）。实现思路见 [docs/SESSION.md](docs/SESSION.md)。

---

## 接入 / 更换模型（唯一改动点：`.env`）

代码用的是 Anthropic 官方 SDK，它原生支持「Anthropic 兼容」的第三方网关。**换厂商不用改一行代码**，只改 `.env`：

| 厂商 | `ANTHROPIC_BASE_URL` | `MODEL_ID`(示例) |
|---|---|---|
| Kimi（国内） | `https://api.moonshot.cn/anthropic` | `kimi-k2-0711-preview` |
| Kimi（海外） | `https://api.moonshot.ai/anthropic` | `kimi-latest` |
| 官方 Anthropic | 留空 | `claude-sonnet-4-5` |
| DeepSeek | `https://api.deepseek.com/anthropic` | `deepseek-chat` |
| GLM（智谱） | `https://open.bigmodel.cn/api/anthropic` | `glm-4.6` |

> 模型 id 仅为示例，**请以各厂商控制台/文档的当前值为准**。

原理：`agent.py` 里只有这三行在读配置——
```python
client = Anthropic(base_url=os.getenv("ANTHROPIC_BASE_URL"))  # SDK 自动读 ANTHROPIC_API_KEY
MODEL  = os.environ["MODEL_ID"]
FALLBACK_MODEL = os.getenv("FALLBACK_MODEL_ID")               # 可选
```

---

## 目录结构

```
T10/
  agent.py               # ★ 主程序：一个循环 + 全部机制（改这里做二次开发）
  .env / .env.example    # ★ 配置（接你的 Kimi Key）
  requirements.txt
  session_store.py       # 会话存档存储层：快照式 JSONL 落盘/读回/列目录
  session_integration.py # 会话恢复装配层：CLI(-c/-r/--list) + 斜杠命令 + 回放渲染
  tasks/store.py         # 任务存储层：TaskRecord + 文件锁 + 依赖图（对齐 CC）
  task_integration.py    # 任务装配层：task_create/update/get/list 四工具
  status_report.py       # 运行状态报告：等待动画 + token/耗时结算 + 上下文占用提示
  skills/                # 按需加载的知识包（每个子目录一个 SKILL.md）
    agent-builder/  code-review/  mcp-builder/  pdf/
  .memory/MEMORY.md      # 跨会话长期记忆（agent 可读写，你也能手改）
  .sessions/             # 会话存档（运行时生成，已 gitignore）
  .tasks/                # 任务图（运行时生成，已 gitignore）
  reference/
    s20_full_agent.py    # 原仓库最全版本（含 teams/cron/worktree/MCP），需要高级功能时参考
  .venv/                 # 内置虚拟环境（已装好依赖）
```

运行后会自动生成（已在 .gitignore 忽略）：`.transcripts/`（压缩前的完整对话备份）。

---

## agent.py 里有什么（对应原仓库的哪一章）

| 模块 | 作用 | 来源 |
|---|---|---|
| agent loop | 核心 while 循环：调模型→执行工具→回灌结果 | s01/s02 |
| tools | bash / read / write / edit / glob / grep | s01/s02/s20 |
| permission + hooks | 危险命令拦截 + Pre/Post/Stop 扩展点 | s03/s04 |
| todo_write | 先列计划再执行 | s05 |
| task（subagent） | 子任务用干净上下文，只带回摘要 | s06 |
| skill loading | 扫描 `skills/`，按需 `load_skill` | s07 |
| context compaction | 长会话自动压缩上下文（保 tool 配对） | s08 |
| memory | 读 `.memory/MEMORY.md` + `remember` 追加 | s09 |
| system prompt | 每轮运行时拼装（技能目录/记忆/时间） | s10 |
| error recovery | 429 退避 / 529 切备用模型 / prompt 过长回退 | s11 |
| task system | 跨会话任务图：依赖/认领/解锁，结构对齐 CC | s12 |

**刻意没有搬进来**（需要时看 `reference/s20_full_agent.py`）：background threads、cron、agent teams、team protocols、autonomous agents、worktree isolation、MCP（原仓库是 mock，非真实实现）。

任务系统与 `todo_write` 的分工：`todo_write` 是本轮执行清单（内存态）；
`task_create / task_update / task_get / task_list` 是跨会话持久任务图，
带依赖（blockedBy/blocks 双向 + 环检测）、认领（owner 冲突保护）、
解锁通知和 release 恢复路径，存在 `.tasks/` 下，`TASKS_ENABLED=0` 可关。
实现取舍见 [docs/TASK.md](docs/TASK.md)。

---

## 怎么二次开发

- **加一个工具**：在 `BUILTIN_TOOLS` 加 schema，在 `BUILTIN_HANDLERS` 加对应函数。循环不用动。
- **加一条安全规则**：改 `DENY_LIST` / `DESTRUCTIVE`，或用 `register_hook("PreToolUse", ...)` 挂新钩子。
- **加一个知识包**：在 `skills/` 下建目录放 `SKILL.md`（带 `name`/`description` frontmatter），重启即被扫描。
- **改人格/规则**：编辑 `IDENTITY` 或 `assemble_system_prompt()`。

详细分析见同目录的 **REPORT.md**。

---

## MCP:两条实现路径,一键切换

同一个 `.env` 开关,切换「手写实现」和「官方 SDK」两条路径——**连的是同一个 server,对模型完全等价**:

```ini
MCP_MODE=off      # 不启用(默认)
MCP_MODE=manual   # 手写 client,纯 stdlib,能看到协议全过程
MCP_MODE=sdk      # 官方 mcp SDK,生产级
```

### 文件

```
mcp_manual/
  client.py          手写 MCP 客户端(JSON-RPC over stdio,后台读线程/id 路由/超时/优雅关闭)
  github_server.py   手写 GitHub MCP server —— 把 tools/call 翻译成 GitHub REST API
  mcp_sdk_client.py    官方 SDK 客户端(异步→同步桥接,长连接)
  mcp_integration.py   统一入口:按 MCP_MODE 选路径,把工具并进工具池
```

### 一次调用的完整链路

```
启动:  起 server 子进程 → initialize(版本协商) → notifications/initialized → tools/list
        → 工具按 mcp__github__* 并进工具池,写进发给模型的 tools[]
运行:  模型输出 tool_use(mcp__github__search_repositories)
        → permission_hook 审核 → 路由到对应 client → tools/call
        → server 翻译成 GET api.github.com/search/repositories → 结果包成 content 回传
        → 作为 tool_result 回灌上下文 → 模型据此回答
退出:  关闭子进程
```

想看**每一条 JSON-RPC 报文**,打开调试开关:
```ini
MCP_DEBUG=1
```

### 切到真·官方 GitHub server(生产配置)

`.env` 改一行即可(需 Docker Desktop 运行 + `GITHUB_TOKEN`):
```ini
MCP_SERVER_CMD=["docker","run","-i","--rm","-e","GITHUB_PERSONAL_ACCESS_TOKEN","ghcr.io/github/github-mcp-server"]
```
客户端代码**一行不用改**——这正是 MCP 的意义:换 server 不换 client。

### 内置的三个 GitHub 工具

`search_repositories`(搜仓库) / `get_repository`(仓库详情) / `get_readme`(读 README)。
不填 `GITHUB_TOKEN` 也能用(公开搜索,限速较低);填了升到 5000 次/时。
**token 只注入 server 子进程,不进模型上下文。**

### 已知边界(教学实现的取舍)

- 手写 client/server 走 **stdio + handshake 协议版本**(`2025-11-25`);MCP 2.0 新增的 modern 版本(`2026-07-28`)需要额外的 `discover` 流程,两条路径都未实现;
- 手写路径**不支持 HTTP/SSE 传输和 OAuth**——要连远程托管 server 请用 `MCP_MODE=sdk`;
- 只实现了 `tools`,未实现 `resources` / `prompts`。

---

## License & 致谢

本项目基于 [shareAI-lab/learn-claude-code](https://github.com/shareAI-lab/learn-claude-code)（MIT License）提炼而来，`skills/` 与 `reference/s20_full_agent.py` 直接复用其代码，特此致谢。本项目自身采用 MIT License，详见 [LICENSE](LICENSE)。
