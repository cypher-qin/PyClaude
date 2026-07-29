# myagent

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
  skills/                # 按需加载的知识包（每个子目录一个 SKILL.md）
    agent-builder/  code-review/  mcp-builder/  pdf/
  .memory/MEMORY.md      # 跨会话长期记忆（agent 可读写，你也能手改）
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

**刻意没有搬进来**（需要时看 `reference/s20_full_agent.py`）：task graph、background threads、cron、agent teams、team protocols、autonomous agents、worktree isolation、MCP（原仓库是 mock，非真实实现）。

---

## 怎么二次开发

- **加一个工具**：在 `BUILTIN_TOOLS` 加 schema，在 `BUILTIN_HANDLERS` 加对应函数。循环不用动。
- **加一条安全规则**：改 `DENY_LIST` / `DESTRUCTIVE`，或用 `register_hook("PreToolUse", ...)` 挂新钩子。
- **加一个知识包**：在 `skills/` 下建目录放 `SKILL.md`（带 `name`/`description` frontmatter），重启即被扫描。
- **改人格/规则**：编辑 `IDENTITY` 或 `assemble_system_prompt()`。

详细分析见同目录的 **REPORT.md**。

---

## License & 致谢

本项目基于 [shareAI-lab/learn-claude-code](https://github.com/shareAI-lab/learn-claude-code)（MIT License）提炼而来，`skills/` 与 `reference/s20_full_agent.py` 直接复用其代码，特此致谢。本项目自身采用 MIT License，详见 [LICENSE](LICENSE)。
