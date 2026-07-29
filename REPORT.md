# learn-claude-code 仓库分析报告 & 个人 Agent 接入 Kimi 指南

> 分析对象：`E:\learn-claude-code`
> 产出目标：`C:\Users\LENOVO\Desktop\T10`（本报告 + 已初始化的个人 Agent 项目）
> 日期：2026-07-30

---

## 0. 一句话结论

这是一个**教学仓库**，用 20 个循序渐进的章节（`s01`~`s20`），从「一个 while 循环 + bash」讲到「一个完整的 Agent harness」。它的核心主张是：**智能来自模型，你要建的是 harness（工具+知识+上下文+权限）**。

- **接入你的 Kimi API 极其简单**：全仓库 20 个 `code.py` 用的都是同一套配置读取方式，只要改 `.env` 三个变量即可（`ANTHROPIC_API_KEY` / `ANTHROPIC_BASE_URL` / `MODEL_ID`），**一行 Python 都不用动**。仓库的 `.env.example` 甚至已经把 Kimi 的配置写好了。
- **最该看的文件**：`s01_agent_loop/code.py`（懂了它就懂了 80%）、`s20_comprehensive/code.py`（所有机制的合集）、`README.md`（设计哲学）。
- **哪些是"真的"**：单机能力（循环、工具、权限、hooks、todo、子代理、技能、上下文压缩、记忆、错误恢复、任务图、后台线程）都是可运行的真实现。
- **哪些只是"示例/占位"**：**MCP 是 mock**（写死了两个假服务器）；**多机协作/定时任务/worktree** 是"教学版"（单进程内的线程+JSONL 信箱，非生产级）。
- 我已经在 `T10` 下把**真正有用的部分**提炼成一个可直接运行的单文件 Agent（`agent.py`），并接好了 Kimi。**离线工具链已全部验证通过**，你只需在 `.env` 填入真实 Key。

---

## 1. 仓库总览

### 1.1 定位与哲学

仓库名副其实——教你做 **harness engineering（脚手架工程）**。它反复强调一个公式：

```
Agent = Model(LLM) + Harness
Harness = Tools + Knowledge + Observation + Action + Permissions
```

核心循环（constant，永不改变）：

```python
while True:
    response = client.messages.create(model, system, messages, tools)
    messages.append(assistant_turn)
    if response.stop_reason != "tool_use":
        return                       # 模型不再调工具 → 结束
    execute tools; append tool_results
```

每一章只在这个循环**外面**加一个机制，循环本身从头到尾不变。

### 1.2 两条并存的教程线（重要，别看混）

| 线 | 位置 | 状态 |
|---|---|---|
| **当前主线（20 章）** | 根目录 `s01_*` ~ `s20_*` | ✅ 新的权威版本，**从这里学** |
| **遗留线（12 课）** | `docs/` + `agents/` + `web/` | ⚠️ 旧版，过渡期保留；`web/` 网站渲染的是旧的 `docs/` |

两条线的章节号**不一一对应**（README 里有映射表）。**你只需关注根目录的 `s01`~`s20`**，`agents/`、`docs/`、`web/` 可以基本忽略（除非你想看网页版）。

---

## 2. 主目录逐个说明（每个目录/文件是干什么的）

### 2.1 顶层文件

| 文件 | 作用 | 要不要看 |
|---|---|---|
| `README.md` / `README-zh.md` / `README-ja.md` | 设计哲学 + 20 章目录 + 快速开始 | ⭐ **必看**（中文版 `README-zh.md`） |
| `.env.example` | 配置模板，**已内置 Kimi/DeepSeek/GLM/MiniMax 的接入参数** | ⭐ **必看**（接 API 的关键） |
| `requirements.txt` | 依赖：`anthropic` / `python-dotenv` / `pyyaml`（就 3 个） | 看一眼 |
| `.gitignore` | 忽略 `.env`、运行时产物等 | 忽略 |
| `CONTRIBUTING.md` / `LICENSE` | 贡献指南 / MIT 协议 | 忽略 |
| `.github/workflows/` | `ci.yml` + `test.yml`，CI 冒烟测试 | 忽略 |

### 2.2 `s01` ~ `s20/`：20 个教学章节（**仓库主体**）

每个章节是一个独立文件夹，结构一致：

```
sXX_topic/
  README.md      # 中文完整叙述（含内联代码讲解）
  README.en.md   # 英文
  README.ja.md   # 日文
  code.py        # ★ 独立可运行的实现（每章的核心）
  images/        # SVG 图（部分章节有）
```

各章 `code.py` 规模（能直观看出复杂度递增）：

| 章节 | 主题 | 行数 | 一句话 |
|---|---|---:|---|
| s01 | Agent Loop | 137 | 一个循环 + bash = 一个 agent |
| s02 | Tool Use | 190 | 加工具 = 加一个 handler，循环不动 |
| s03 | Permission | 251 | 执行前检查：放行/拦截/问用户 |
| s04 | Hooks | 293 | 循环外加扩展点（Pre/Post/Stop） |
| s05 | TodoWrite | 304 | 先列计划再执行 |
| s06 | Subagent | 383 | 子任务用干净上下文，只带回摘要 |
| s07 | Skill Loading | 426 | 知识按需加载，不塞满上下文 |
| s08 | Context Compact | 524 | 长会话多层压缩，续命 |
| s09 | Memory | 655 | 记忆：筛选/抽取/固化 |
| s10 | System Prompt | 218 | 系统提示词运行时拼装 |
| s11 | Error Recovery | 365 | 重试 / 升配 / 换模型 / 回退 |
| s12 | Task System | 378 | 落盘的任务图 + 依赖 |
| s13 | Background Tasks | 480 | 慢操作丢后台线程，agent 继续想 |
| s14 | Cron Scheduler | 804 | 按时间自动触发 |
| s15 | Agent Teams | 985 | 常驻队友 + 异步信箱 |
| s16 | Team Protocols | 882 | 固定的请求-应答协议 |
| s17 | Autonomous Agents | 813 | 队友自己去任务板认领 |
| s18 | Worktree Isolation | 998 | 每个任务独立 git 目录 |
| s19 | MCP Plugin | 1026 | 外部工具接进同一个工具池 |
| s20 | **Comprehensive** | 2123 | ★ 以上全部机制合到一个循环 |

### 2.3 `skills/`：技能包（被 s07 / s20 加载）

每个子目录一个 `SKILL.md`（带 `name`/`description` 的 YAML frontmatter），agent 用 `load_skill(name)` 按需展开：

| 技能 | 内容 |
|---|---|
| `agent-builder/` | 造 agent 的方法论；**还带 `references/`（minimal-agent.py 等模板）和 `scripts/init_agent.py`（脚手架生成器）** |
| `code-review/` | 代码评审清单 |
| `mcp-builder/` | 造 MCP server 的指南 |
| `pdf/` | PDF 处理技能 |

> `skills/agent-builder/references/minimal-agent.py` 是一个 ~80 行的最小 agent，很适合当"骨架"读。

### 2.4 `agents/`：遗留 12 课的可运行副本（旧线）

`s01_agent_loop.py` ~ `s12_worktree_task_isolation.py` + `s_full.py`（37KB，旧版"完整体"）。**内容与根目录 s01~s20 大量重叠但章节号错位**，属于过渡保留，新学不必看。

### 2.5 `docs/`：遗留 12 课文档（旧线）

`en/` `ja/` `zh/` 三语。`web/` 网站渲染的就是它。新学看根目录章节即可。

### 2.6 `web/`：Next.js 网页版教程（旧线）

`package.json`/`next.config.ts`/`src/`… 一个前端站点，`npm run dev` 起在 3000 端口，渲染的是遗留 `docs/`。**与接入你的 API、做个人工具无关，可忽略。**

### 2.7 `tests/`：少量测试

`test_agents_smoke.py`（导入冒烟）、`test_compaction_tool_pairs.py`（压缩时 tool_use/tool_result 配对不被拆——**这是最值得看的一个测试**）、`test_s_full_background.py`、`test_todo_write_string_input.py`。

---

## 3. 你应该着重看哪些文件（带优先级）

**目标 A：理解原理（想学）**
1. `README-zh.md` —— 设计哲学（30 分钟）
2. `s01_agent_loop/code.py` —— ⭐ 137 行，agent 的全部秘密
3. `s02_tool_use/code.py` —— 工具分发表怎么加工具
4. `s06_subagent/code.py` —— 上下文隔离
5. `s07_skill_loading/code.py` —— 知识按需加载
6. `s08_context_compact/code.py` —— 长会话压缩（有点难，但很关键）
7. `s20_comprehensive/code.py` —— ⭐ 所有机制的合集（读之前先读完上面）

**目标 B：做个人工具（想用/想改）**
- 直接看我在 `T10/agent.py`（已提炼好），配合本报告第 6 节。
- 想加高级功能时，回头翻 `s20_comprehensive/code.py` 对应段落，或 `T10/reference/s20_full_agent.py`。

---

## 4. Agent 示例的核心功能与模块（已真正实现的）

以下都是 s20 里**可运行的真实现**（单机场景生产可用或接近可用）：

| 模块 | 说明 | 章节 |
|---|---|---|
| **Agent Loop** | `while` 循环，靠 `stop_reason=="tool_use"` 决定是否继续 | s01 |
| **工具分发** | `BUILTIN_TOOLS`(给模型的 schema) + `BUILTIN_HANDLERS`(Python 执行) 两张表 | s02 |
| **工具集** | bash / read_file / write_file / edit_file / glob（文件类有 `safe_path` 沙箱） | s02/s20 |
| **权限管线** | 黑名单直接拒、危险操作弹确认、写文件越界拦截 | s03 |
| **Hooks** | `UserPromptSubmit`/`PreToolUse`/`PostToolUse`/`Stop` 四类扩展点 | s04 |
| **TodoWrite** | 会话内任务清单，先规划再执行 | s05 |
| **Subagent** | 子任务开全新 `messages[]`，只把最终文字摘要带回 | s06 |
| **Skill 加载** | 扫描 `skills/*/SKILL.md`，`load_skill` 按需注入 | s07 |
| **上下文压缩** | 4 层：`tool_result_budget`→`snip_compact`→`micro_compact`→模型摘要，**且保证 tool 配对不被拆** | s08 |
| **Memory** | 读 `.memory/MEMORY.md` 注入 prompt | s09 |
| **System Prompt 拼装** | 每轮用当前上下文（技能目录/记忆/MCP 状态/时间）重新拼 | s10 |
| **Error Recovery** | 429 指数退避、529 过载切 `FALLBACK_MODEL`、`max_tokens` 升配+续写、prompt 过长时 reactive 压缩 | s11 |
| **Task 图** | 落盘 JSON 任务（`blockedBy` 依赖、认领、完成） | s12 |
| **后台任务** | 慢命令丢线程跑，完成后把通知注入回对话 | s13 |

---

## 5. 哪些只是"示例/占位"，没有真正实现（⚠️ 重点）

README 的 "Scope" 一节自己也坦白了简化点。结合读码，分三档：

### 5.1 ❌ Mock / 假实现（最需要注意）

- **MCP（s19 / s20）是彻头彻尾的 mock**。代码里只有两个**写死的假服务器**：
  ```python
  MOCK_SERVERS = {"docs": _mock_server_docs, "deploy": _mock_server_deploy}
  # search → 永远返回 "[docs] Found 3 results for '{query}'"
  # trigger → 永远返回 "[deploy] Triggered: {service}"
  ```
  **没有任何真实的 MCP 传输层**（stdio/SSE/HTTP）、没有 OAuth、没有 resource 订阅。它只演示了"如何把外部工具用 `mcp__server__tool` 前缀并进工具池"这个**装配思路**。要接真实 MCP，得自己实现 client 传输。

### 5.2 ⚠️ "教学版"机制：能跑，但非生产级

- **多机协作（s15/s16/s17）**：队友是**同一进程内的线程**，靠**追加式 JSONL 信箱**通信。README 明说"JSONL 信箱协议是教学实现，不代表任何生产内部实现"。真实的多 agent 需要独立进程/机器、消息队列、故障恢复等。
- **Cron（s14）**：调度器是**进程内的一个后台线程**（`cron_scheduler_loop` + `time.sleep(1)` 轮询），会话结束就没了；虽有 durable jobs 落盘，但**不是** OS 级 crontab / systemd timer。
- **Worktree（s18）**：真的调 `git worktree`，但生命周期管理（清理、冲突、并发）是简化的；且**需要工作目录是一个 git 仓库**才能用（s18/s19/s20 都依赖 git）。
- **权限治理（s03）**：只是关键字黑名单 + 简单确认，README 说"基于规则的完整权限治理与信任工作流"是简化的。
- **Hooks（s04）**：只有最小生命周期事件；README 说完整事件总线（如 `SessionStart/End`、`ConfigChange`）**未实现**。
- **会话生命周期**：resume / fork 等未实现。

### 5.3 📦 遗留/重复代码（不是"没实现"，而是"旧版"）

- `agents/`、`docs/`、`web/` 是**旧的 12 课线**，与根目录 20 章重叠，处于过渡保留状态。做新东西别在这上面改。

> **一句话**：把它当**学习脚手架**——单机 agent 的部分照着用没问题；MCP / 多机 / cron / worktree 这几块，理解思路即可，要上生产得自己补真实现。

---

## 6. 接入你私有 Kimi API 应该改哪部分代码（核心问题）

### 6.1 好消息：不用改代码，只改 `.env`

全仓库 20 个 `code.py` 的配置读取**完全一致**（我逐个核对过，20 个文件都引用 `ANTHROPIC_BASE_URL`）：

```python
load_dotenv(override=True)
if os.getenv("ANTHROPIC_BASE_URL"):
    os.environ.pop("ANTHROPIC_AUTH_TOKEN", None)   # 避免鉴权变量打架
client = Anthropic(base_url=os.getenv("ANTHROPIC_BASE_URL"))  # SDK 自动读 ANTHROPIC_API_KEY
MODEL = os.environ["MODEL_ID"]
```

**原理**：Anthropic 官方 SDK 支持自定义 `base_url`，而 Kimi/Moonshot 提供了**「Anthropic 兼容」端点**。于是把 base_url 指向 Kimi，SDK 就照常发 Anthropic 格式的请求，Kimi 那边解析。API Key 由 SDK 从环境变量 `ANTHROPIC_API_KEY` 自动读取。

### 6.2 具体操作（对原仓库）

```bash
cd E:\learn-claude-code
cp .env.example .env
```

编辑 `.env`，填三行（国内用 `.cn`，海外用 `.ai`）：

```ini
ANTHROPIC_API_KEY=sk-你的Moonshot密钥
ANTHROPIC_BASE_URL=https://api.moonshot.cn/anthropic
MODEL_ID=kimi-k2-0711-preview        # ← 以 Kimi 控制台当前可用的模型名为准
```

然后 `pip install -r requirements.txt && python s01_agent_loop/code.py` 即可。

> **模型 id 一定要以你 Kimi 控制台/文档为准**。仓库 `.env.example` 里写的 `kimi-k2.7-code` 等是"未来占位名"，不一定是当前真名。常见稳妥值：`kimi-k2-0711-preview`、`kimi-latest`。
>
> **可选**：`FALLBACK_MODEL_ID=...`（s11/s20 在 529 过载时会切到它）。

### 6.3 唯一的例外

`skills/agent-builder/references/minimal-agent.py` 是个独立模板，它写的是
```python
client = Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))   # 没读 base_url！
MODEL = os.getenv("MODEL_NAME", "claude-sonnet-4-20250514")  # 默认硬编码
```
如果你要用**这个**模板接 Kimi，得自己补上 `base_url=os.getenv("ANTHROPIC_BASE_URL")` 并改 `MODEL` 的来源。**（我在 T10/agent.py 里已经用的是正确的 base_url 写法，无此问题。）**

### 6.4 Kimi 端注意事项

- 端点：国内 `https://api.moonshot.cn/anthropic`，海外 `https://api.moonshot.ai/anthropic`。
- Key 从 Moonshot 平台（platform.moonshot.cn / .ai）申请，形如 `sk-...`。
- 该仓库全程用**非流式** `messages.create`（没有 streaming），Kimi 的 Anthropic 端点对此支持良好。
- 工具调用（tool use）：确认你选的模型支持 function/tool calling（k2 系列支持）。

---

## 7. 我已为你初始化的项目（T10 目录）

基于 learn-claude-code，把**单人日常使用真正有用的部分**提炼成一个**可直接运行的单文件 Agent**，并接好 Kimi。

### 7.1 目录

```
T10/
  REPORT.md              # 本报告
  README.md              # 项目说明 & 快速开始
  agent.py               # ★ 主程序（~500 行，一个循环+全部有用机制）
  .env / .env.example    # ★ 配置（已写好 Kimi 模板，填 Key 即用）
  requirements.txt
  skills/                # 从原仓库搬来的 4 个技能包
  .memory/MEMORY.md      # 跨会话记忆（已放一条示例）
  reference/
    s20_full_agent.py    # 原仓库最全版本（要 teams/cron/worktree/MCP 时参考）
  .venv/                 # 已装好依赖的虚拟环境
```

### 7.2 `agent.py` 保留了什么 / 去掉了什么

**保留**（对应章节）：agent loop(s01/s02)、工具 bash/read/write/edit/glob**/grep**(新增)、permission+hooks(s03/s04)、todo_write(s05)、subagent(s06)、skill loading(s07)、上下文压缩(s08)、memory 读+`remember`写(s09)、system prompt 拼装(s10)、error recovery(s11)。

**去掉**（偏演示/多机，需要时看 `reference/`）：task 图(s12)、后台线程(s13)、cron(s14)、teams/protocols/autonomous(s15-17)、worktree(s18)、MCP-mock(s19)。

> 去掉这些的理由：它们要么是"教学版非生产级"，要么对**单人本地编码工具**价值不高，留着反而增加维护面。骨架清爽了，你后续想加哪个，从 `reference/s20_full_agent.py` 对应段落抄回来即可。

### 7.3 验证状态

- ✅ `python -m py_compile agent.py` 通过（Python 3.11.7）
- ✅ 离线冒烟测试全通过：导入、11 个工具注册、write/read/grep/edit/glob/todo、skill 加载、memory 追加、system prompt 拼装、`safe_path` 越界拦截。
- ⏳ **联网调用未测**——因为需要你自己的真实 Kimi Key。填好 `.env` 后 `python agent.py` 即可。

### 7.4 你的下一步

1. 打开 `T10\.env`，把 `ANTHROPIC_API_KEY` 换成你的真实 Kimi Key，确认 `MODEL_ID` 是控制台当前可用模型名。
2. 运行：
   ```
   .venv\Scripts\python.exe agent.py
   ```
   （或先 `.venv\Scripts\activate` 再 `python agent.py`）
3. 迭代方向见 `README.md` 的"怎么二次开发"。

---

## 附录：关键设计速查

- **加工具** = `BUILTIN_TOOLS` 加 schema + `BUILTIN_HANDLERS` 加函数，循环不动。
- **加安全规则** = 改 `DENY_LIST`/`DESTRUCTIVE` 或 `register_hook("PreToolUse", fn)`。
- **换模型/厂商** = 只改 `.env` 三个变量。
- **上下文压缩铁律** = 裁消息时绝不能拆散 `tool_use`↔`tool_result` 配对（否则 API 报错）——这是 s08 和 `tests/test_compaction_tool_pairs.py` 的重点，`agent.py` 已内置这套保护。
