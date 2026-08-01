#!/usr/bin/env python3
"""
myagent — 个人编码 Agent（基于 learn-claude-code 提炼）

一个 while 循环 + 一批工具 + 一些 harness 机制，接入 Kimi（Moonshot）API。
从 learn-claude-code 的 s01~s20 中提炼出「单人日常使用」真正有用的部分：

    agent loop          —— 核心循环（s01/s02）
    tools               —— bash / read / write / edit / glob / grep（s01/s02/s20）
    permission + hooks  —— 危险命令拦截 + 扩展点（s03/s04）
    todo_write          —— 先规划再执行（s05）
    subagent            —— 子任务隔离上下文（s06）
    skill loading       —— 按需加载 skills/ 下的知识（s07）
    context compaction  —— 长会话自动压缩上下文（s08）
    memory              —— 跨会话记忆：一事一文件 + 自动索引（s09，见 memory/）
    system prompt       —— 每轮运行时拼装（s10）
    error recovery      —— 429/529 重试、max_tokens 升配、prompt 过长回退（s11）

刻意没有搬进来（属于演示/多机协作，需要时看 reference/s20_full_agent.py）：
    task graph / background threads / cron / agent teams / worktree / MCP(mock)

用法:
    pip install -r requirements.txt
    cp .env.example .env   # 填入你的 Kimi Key
    python agent.py
"""

import ast
import json
import os
import platform
import random
import subprocess
import time
from datetime import datetime
from pathlib import Path

# Windows 终端：启用 ANSI 颜色（Win10+）并把输出强制为 UTF-8，避免中文乱码
if os.name == "nt":
    os.system("")
    import sys
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8")
        except Exception:
            pass

try:
    import readline  # noqa: F401  (Unix 下让输入行支持编辑；Windows 无此模块)
    READLINE_AVAILABLE = True
except ImportError:
    READLINE_AVAILABLE = False

import yaml
from anthropic import Anthropic
from dotenv import load_dotenv

# MCP 集成（两条实现路径由 .env 的 MCP_MODE 切换；具体 client 是懒加载的）
from mcp_integration import connect_mcp_servers, shutdown_mcp_servers
from memory_integration import (bind_runtime, init_memory, inject_memories,
                                memory_count, memory_system_prompt, memory_tools,
                                note_tool_use, on_turn_end, session_compact_summary,
                                start_prefetch)

# ═══════════════════════════════════════════════════════════════════════════
#  配置 —— 接入私有 API 的唯一入口（改这里 / 改 .env 即可换模型）
#
#  Anthropic 官方 SDK 原生支持「Anthropic 兼容」的第三方网关，Kimi 就是其一。
#  只要在 .env 里写：
#      ANTHROPIC_API_KEY=<你的 Moonshot Key，sk-...>
#      ANTHROPIC_BASE_URL=https://api.moonshot.cn/anthropic   （国内；海外用 .ai）
#      MODEL_ID=<在 Kimi 控制台确认的模型 id>
#  代码本身一行都不用动。SDK 会自动从环境变量读取 ANTHROPIC_API_KEY。
# ═══════════════════════════════════════════════════════════════════════════
# .env 优先于外部环境变量（override=True），避免 shell 里的旧 key 悄悄生效。
# 例外：MCP 开关允许命令行临时覆盖，方便 `MCP_MODE=sdk python agent.py` 这样 A/B 对比。
_CLI_OVERRIDES = {k: v for k, v in os.environ.items()
                  if k.startswith("MCP_")}
load_dotenv(override=True)
os.environ.update(_CLI_OVERRIDES)

# 用第三方 base_url 时，清掉可能残留的 ANTHROPIC_AUTH_TOKEN，避免鉴权串味
if os.getenv("ANTHROPIC_BASE_URL"):
    os.environ.pop("ANTHROPIC_AUTH_TOKEN", None)

client = Anthropic(base_url=os.getenv("ANTHROPIC_BASE_URL"))  # 未设则走官方
MODEL = os.environ["MODEL_ID"]
PRIMARY_MODEL = MODEL
FALLBACK_MODEL = os.getenv("FALLBACK_MODEL_ID")  # 可选：529 过载时切换的备用模型

WORKDIR = Path.cwd()
SKILLS_DIR = WORKDIR / "skills"
TRANSCRIPT_DIR = WORKDIR / ".transcripts"   # 记忆目录由 memory/memdir.py 自己定位

# —— 调参区 ——
DEFAULT_MAX_TOKENS = 8000
ESCALATED_MAX_TOKENS = 16000
MAX_RETRIES = 3
MAX_CONSECUTIVE_529 = 2
MAX_RECOVERY_RETRIES = 2
BASE_DELAY_MS = 500
CONTEXT_LIMIT = 50000          # messages 序列化字节数超过此值触发摘要压缩
KEEP_RECENT_TOOL_RESULTS = 3   # micro_compact 保留最近几条完整 tool_result
CONTINUATION_PROMPT = "Continue from the previous response. Do not repeat completed work."
PROMPT = "\033[36mmyagent >> \033[0m"


# ═══════════════════════════════════════════════════════════════════════════
#  基础工具（给模型的「手」）
# ═══════════════════════════════════════════════════════════════════════════

def resolve_path(p: str) -> Path:
    """解析为绝对路径（相对路径相对工作目录）；不在此处做范围限制。"""
    return (WORKDIR / p).resolve()


def is_inside_workspace(path: Path) -> bool:
    """路径是否在工作目录内。工作区外的写/改会走 permission_hook 的用户审核。"""
    return path.is_relative_to(WORKDIR)


def _decode_console(raw: bytes) -> str:
    """中文 Windows 上命令输出编码不统一：先按 UTF-8，失败回退 GBK，最后兜底替换。"""
    for enc in ("utf-8", "gbk"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def run_bash(command: str) -> str:
    # 注意：capture_output 拿字节再手动解码，避免 text=True 在中文 Windows 用 GBK
    # 解码非 GBK 输出时崩溃（会让 stdout 变 None 触发 None+str）。
    try:
        r = subprocess.run(command, shell=True, cwd=WORKDIR,
                           capture_output=True, timeout=120)
        raw = (r.stdout or b"") + (r.stderr or b"")
        out = _decode_console(raw).strip()
        return out[:50000] if out else "(no output)"
    except subprocess.TimeoutExpired:
        return "Error: Timeout (120s)"
    except (FileNotFoundError, OSError) as e:
        return f"Error: {e}"


def run_read(path: str, limit: int | None = None, offset: int = 0) -> str:
    try:
        lines = resolve_path(path).read_text(encoding="utf-8", errors="replace").splitlines()
        offset = max(int(offset or 0), 0)
        limit = int(limit) if limit is not None else None
        lines = lines[offset:]
        if limit is not None and limit < len(lines):
            lines = lines[:limit] + [f"... ({len(lines) - limit} more lines)"]
        return "\n".join(lines)
    except Exception as e:
        return f"Error: {e}"


def run_write(path: str, content: str) -> str:
    try:
        fp = resolve_path(path)
        fp.parent.mkdir(parents=True, exist_ok=True)
        fp.write_text(content, encoding="utf-8")
        return f"Wrote {len(content)} bytes to {path}"
    except Exception as e:
        return f"Error: {e}"


def run_edit(path: str, old_text: str, new_text: str) -> str:
    try:
        fp = resolve_path(path)
        text = fp.read_text(encoding="utf-8")
        if old_text not in text:
            return f"Error: text not found in {path}"
        fp.write_text(text.replace(old_text, new_text, 1), encoding="utf-8")
        return f"Edited {path}"
    except Exception as e:
        return f"Error: {e}"


def run_glob(pattern: str) -> str:
    import glob as g
    try:
        # 绝对模式直接匹配；相对模式相对工作目录。读类工具不再限制在工作区内。
        if os.path.isabs(pattern):
            results = g.glob(pattern, recursive=True)
        else:
            results = g.glob(pattern, root_dir=WORKDIR, recursive=True)
        return "\n".join(results) if results else "(no matches)"
    except Exception as e:
        return f"Error: {e}"


def run_grep(pattern: str, path: str = ".", glob: str = "*") -> str:
    """递归正则搜索（纯 Python，跨平台）。path 可为整机任意路径。"""
    import fnmatch
    import re
    try:
        regex = re.compile(pattern)
    except re.error as e:
        return f"Error: bad regex: {e}"
    base = resolve_path(path)
    hits = []
    files = [base] if base.is_file() else base.rglob("*")
    for fp in files:
        if not fp.is_file() or not fnmatch.fnmatch(fp.name, glob):
            continue
        try:
            for i, line in enumerate(fp.read_text(encoding="utf-8",
                                                  errors="replace").splitlines(), 1):
                if regex.search(line):
                    try:
                        rel = fp.relative_to(WORKDIR)
                    except ValueError:
                        rel = fp
                    hits.append(f"{rel}:{i}:{line.strip()[:200]}")
                    if len(hits) >= 200:
                        return "\n".join(hits) + "\n... (truncated at 200 hits)"
        except Exception:
            continue
    return "\n".join(hits) if hits else "(no matches)"


# ── TodoWrite：先规划再执行 ──
CURRENT_TODOS: list[dict] = []


def _normalize_todos(todos):
    if isinstance(todos, str):
        try:
            todos = json.loads(todos)
        except json.JSONDecodeError:
            try:
                todos = ast.literal_eval(todos)
            except (SyntaxError, ValueError):
                return None, "Error: todos must be a list or JSON array string"
    if not isinstance(todos, list):
        return None, "Error: todos must be a list"
    for i, todo in enumerate(todos):
        if not isinstance(todo, dict):
            return None, f"Error: todos[{i}] must be an object"
        if "content" not in todo or "status" not in todo:
            return None, f"Error: todos[{i}] missing 'content' or 'status'"
        if todo["status"] not in ("pending", "in_progress", "completed"):
            return None, f"Error: todos[{i}] has invalid status '{todo['status']}'"
    return todos, None


def run_todo_write(todos: list) -> str:
    global CURRENT_TODOS
    todos, error = _normalize_todos(todos)
    if error:
        return error
    CURRENT_TODOS = todos
    for t in CURRENT_TODOS:
        mark = {"completed": "x", "in_progress": ">", "pending": " "}[t["status"]]
        print(f"  \033[33m[{mark}] {t['content']}\033[0m")
    return f"Updated {len(CURRENT_TODOS)} todos"


# ═══════════════════════════════════════════════════════════════════════════
#  Skill 加载 —— 按需注入知识（扫描 skills/*/SKILL.md）
# ═══════════════════════════════════════════════════════════════════════════
SKILL_REGISTRY: dict[str, dict] = {}


def _parse_frontmatter(text: str) -> tuple[dict, str]:
    if not text.startswith("---"):
        return {}, text
    parts = text.split("---", 2)
    if len(parts) < 3:
        return {}, text
    try:
        meta = yaml.safe_load(parts[1]) or {}
    except yaml.YAMLError:
        meta = {}
    return meta, parts[2].strip()


def scan_skills():
    SKILL_REGISTRY.clear()
    if not SKILLS_DIR.exists():
        return
    for directory in sorted(SKILLS_DIR.iterdir()):
        manifest = directory / "SKILL.md"
        if not directory.is_dir() or not manifest.exists():
            continue
        raw = manifest.read_text(encoding="utf-8")
        meta, _ = _parse_frontmatter(raw)
        name = meta.get("name", directory.name)
        desc = meta.get("description", raw.split("\n")[0].lstrip("#").strip())
        SKILL_REGISTRY[name] = {"name": name, "description": desc, "content": raw}


def list_skills() -> str:
    if not SKILL_REGISTRY:
        return "(no skills found)"
    return "\n".join(f"- {s['name']}: {str(s['description']).splitlines()[0]}"
                     for s in SKILL_REGISTRY.values())


def load_skill(name: str) -> str:
    skill = SKILL_REGISTRY.get(name)
    if not skill:
        available = ", ".join(SKILL_REGISTRY.keys()) or "(none)"
        return f"Skill not found: {name}. Available: {available}"
    return skill["content"]


scan_skills()


# ═══════════════════════════════════════════════════════════════════════════
#  System Prompt —— 每轮运行时拼装（把技能目录 / 记忆 / 时间注入进去）
# ═══════════════════════════════════════════════════════════════════════════
IDENTITY = ("You are a coding agent. Act, don't explain. Use tools to make "
            "changes directly. When done, give a short summary of what you did.")


def assemble_system_prompt() -> str:
    sections = [
        IDENTITY,
        f"Working directory: {WORKDIR}",
        f"Current time: {datetime.now().isoformat(timespec='seconds')}",
        "Skills catalog:\n" + list_skills()
        + "\nUse load_skill(name) to expand a skill when it is relevant.",
    ]
    mcp_tools = [t["name"] for t in TOOLS if t["name"].startswith("mcp__")]
    if mcp_tools:
        sections.append(
            "MCP tools: tools named mcp__{server}__{tool} come from external MCP servers "
            "and are as real as the built-in ones — call them the same way. "
            f"Available now: {', '.join(mcp_tools)}. "
            "Prefer them over `bash curl` for the systems they cover.")
    sections.append(
        "File tools: read_file / glob / grep can access ANY path on the machine. "
        "write_file / edit_file default to the working directory; writing OUTSIDE "
        "it is allowed but will PAUSE for the user's confirmation — so only do it "
        "when the user clearly wants a file changed outside this project.")
    if os.name == "nt":
        sections.append(
            "Platform: Windows. The `bash` tool runs via cmd.exe, NOT a Unix shell — "
            "use dir / type / findstr or `powershell -Command ...`, never cat/ls/grep. "
            "Prefer the dedicated file tools over the shell for file work.")
    memory = memory_system_prompt()
    if memory:
        sections.append(memory)
    return "\n\n".join(sections)


# ═══════════════════════════════════════════════════════════════════════════
#  Hooks + Permission —— 循环外的扩展点，不改主循环就能加策略
# ═══════════════════════════════════════════════════════════════════════════
HOOKS = {"UserPromptSubmit": [], "PreToolUse": [], "PostToolUse": [], "Stop": []}


def register_hook(event: str, callback):
    HOOKS[event].append(callback)


def trigger_hooks(event: str, *args):
    for callback in HOOKS[event]:
        result = callback(*args)
        if result is not None:
            return result
    return None


DENY_LIST = ["rm -rf /", "sudo", "shutdown", "reboot", "mkfs", "dd if=", "> /dev/"]
DESTRUCTIVE = ["rm ", "rmdir", "del ", "format ", "> /etc/", "chmod 777",
               "git push", "git reset --hard"]


def confirm_out_of_workspace_write(tool_name: str, target: Path, args: dict) -> bool:
    """工作区外的写/改：展示改动预览并请用户当场批准。批准返回 True。"""
    action = "写入(覆盖)" if tool_name == "write_file" else "修改"
    bar = "\033[33m│\033[0m"
    print(f"\n\033[33m╭─ 审核请求：Agent 想{action}『工作区外』的文件\033[0m")
    print(f"{bar}  路径  {target}")
    if tool_name == "write_file":
        content = str(args.get("content", ""))
        state = "已存在，将被覆盖" if target.exists() else "新建文件"
        print(f"{bar}  目标  {state} · {len(content)} 字符")
        preview = content[:200]
        if preview:
            indented = preview.replace("\n", f"\n{bar}        ")
            print(f"{bar}  预览  {indented}")
    else:
        old = str(args.get("old_text", ""))
        new = str(args.get("new_text", ""))
        print(f"{bar}  \033[31m- {old[:120]!r}\033[0m")
        print(f"{bar}  \033[32m+ {new[:120]!r}\033[0m")
    print("\033[33m╰─\033[0m", end="")
    try:
        answer = input(" 允许? [y/N] ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        return False
    return answer in ("y", "yes")


def permission_hook(block):
    """在工具真正执行前拦截：黑名单直接拒绝，危险操作弹确认。"""
    if block.name == "bash":
        command = block.input.get("command", "")
        for pattern in DENY_LIST:
            if pattern in command:
                return f"Permission denied: '{pattern}' is on the deny list"
        if any(token in command for token in DESTRUCTIVE):
            print(f"\n\033[33m[permission] 潜在危险命令:\033[0m\n  {command}")
            if input("  允许执行? [y/N] ").strip().lower() not in ("y", "yes"):
                return "Permission denied by user"
    if block.name in ("write_file", "edit_file"):
        raw = block.input.get("path", "")
        try:
            target = resolve_path(raw)
        except Exception as e:
            return f"Permission denied: invalid path {raw!r}: {e}"
        # 工作区内直接放行；工作区外的写/改需要用户当场审核
        if not is_inside_workspace(target):
            if not confirm_out_of_workspace_write(block.name, target, block.input):
                return ("Permission denied by user: refused to modify "
                        f"out-of-workspace file {target}")
    return None


def log_hook(block):
    print(f"\033[90m[hook] {block.name}\033[0m")
    return None


register_hook("PreToolUse", permission_hook)
register_hook("PreToolUse", log_hook)
# turn 结束：后台提炼记忆 + 低频整理 + 记一笔会话记忆。
# 主线程紧接着阻塞在 input() 等用户打字，这段空闲正好给后台用完。
register_hook("Stop", on_turn_end)


# ═══════════════════════════════════════════════════════════════════════════
#  Subagent —— 子任务用全新 messages[]，只把最终摘要带回，隔离上下文噪音
# ═══════════════════════════════════════════════════════════════════════════
SUB_SYSTEM = (f"You are a coding subagent at {WORKDIR}. "
              "Complete the task, then return a concise final summary. "
              "Do not spawn more agents.")
SUB_TOOL_NAMES = ("bash", "read_file", "write_file", "edit_file", "glob", "grep")


def extract_text(content) -> str:
    if not isinstance(content, list):
        return str(content)
    return "\n".join(getattr(b, "text", "") for b in content
                     if getattr(b, "type", None) == "text").strip()


def has_tool_use(content) -> bool:
    return any(getattr(b, "type", None) == "tool_use" for b in content)


def spawn_subagent(description: str, *, system: str = SUB_SYSTEM,
                   tools: list | None = None, handlers: dict | None = None,
                   max_turns: int = 30) -> str:
    """隔离上下文跑一个子任务。

    默认仍是「编码子代理」，工具从 BUILTIN_* 里按 SUB_TOOL_NAMES 收窄——
    约定不变：**子代理拿不到 MCP 工具**。
    记忆子系统会显式传 tools/handlers 进来，换成一套只有 remember/forget 的
    受限工具集（对标 CC 的 forked agent + 受限权限）。
    """
    sub_tools = tools if tools is not None else [
        t for t in BUILTIN_TOOLS if t["name"] in SUB_TOOL_NAMES]
    pool = handlers if handlers is not None else BUILTIN_HANDLERS
    messages = [{"role": "user", "content": description}]
    for _ in range(max_turns):
        response = client.messages.create(
            model=MODEL, system=system, messages=messages,
            tools=sub_tools, max_tokens=8000)
        messages.append({"role": "assistant", "content": response.content})
        if not has_tool_use(response.content):
            break
        results = []
        for block in response.content:
            if block.type != "tool_use":
                continue
            blocked = trigger_hooks("PreToolUse", block)
            if blocked:
                output = str(blocked)
            else:
                output = call_tool_handler(pool.get(block.name),
                                           block.input, block.name)
            results.append({"type": "tool_result", "tool_use_id": block.id,
                            "content": str(output)})
        messages.append({"role": "user", "content": results})
    for msg in reversed(messages):
        if msg["role"] == "assistant":
            text = extract_text(msg["content"])
            if text:
                return text
    return "Subagent finished without a text summary."


_side_query_no_thinking = True   # 端点若不认这个参数，首次失败后自动关掉


def _side_query(prompt: str, max_tokens: int = 1000) -> str:
    """一次性轻量模型调用（不带工具、不进历史），记忆子系统用它选记忆。

    关掉 thinking——选记忆是分类，不是推理。kimi-k2.5 默认会先思考：实测
    8.8~20.6s / 466~1000 输出 token，还常把预算耗光，连一个 text 块都出不来；
    关掉后 1.3s / 16 token，选得一样准。
    """
    global _side_query_no_thinking
    body = {"model": MODEL, "max_tokens": max_tokens,
            "messages": [{"role": "user", "content": prompt}]}
    try:
        extra = {"thinking": {"type": "disabled"}} if _side_query_no_thinking else {}
        response = client.messages.create(**body, **extra)
    except Exception:
        if not _side_query_no_thinking:
            raise
        _side_query_no_thinking = False      # 换了厂商可能不认，降级重来一次
        response = client.messages.create(**body)
    return extract_text(response.content)


# 把「怎么调模型」注入给记忆子系统：记忆包因此不反向 import agent.py，
# 测试时也能换成假的，不打网络。
bind_runtime(side_query=_side_query, fork_agent=spawn_subagent)


# ═══════════════════════════════════════════════════════════════════════════
#  Context Compaction —— 长会话保命：先压 tool_result，再裁旧消息，最后摘要
# ═══════════════════════════════════════════════════════════════════════════

def estimate_size(messages: list) -> int:
    return len(json.dumps(messages, default=str))


def block_type(block):
    return block.get("type") if isinstance(block, dict) else getattr(block, "type", None)


def message_has_tool_use(message: dict) -> bool:
    content = message.get("content")
    return (message.get("role") == "assistant" and isinstance(content, list)
            and any(block_type(b) == "tool_use" for b in content))


def is_tool_result_message(message: dict) -> bool:
    content = message.get("content")
    return (message.get("role") == "user" and isinstance(content, list)
            and any(isinstance(b, dict) and b.get("type") == "tool_result"
                    for b in content))


def collect_tool_results(messages: list):
    found = []
    for msg in messages:
        content = msg.get("content")
        if msg.get("role") != "user" or not isinstance(content, list):
            continue
        for block in content:
            if isinstance(block, dict) and block.get("type") == "tool_result":
                found.append(block)
    return found


def micro_compact(messages: list) -> list:
    """把较早的大 tool_result 替换成占位符，只保留最近几条完整结果。"""
    tool_results = collect_tool_results(messages)
    if len(tool_results) <= KEEP_RECENT_TOOL_RESULTS:
        return messages
    for block in tool_results[:-KEEP_RECENT_TOOL_RESULTS]:
        if len(str(block.get("content", ""))) > 120:
            block["content"] = "[Earlier tool result compacted. Re-run if needed.]"
    return messages


def snip_compact(messages: list, max_messages: int = 50) -> list:
    """消息过多时裁掉中间段，注意保持 tool_use / tool_result 配对不被拆散。"""
    if len(messages) <= max_messages:
        return messages
    head_end, tail_start = 3, len(messages) - (max_messages - 3)
    if head_end > 0 and message_has_tool_use(messages[head_end - 1]):
        while head_end < len(messages) and is_tool_result_message(messages[head_end]):
            head_end += 1
    if (0 < tail_start < len(messages)
            and is_tool_result_message(messages[tail_start])
            and message_has_tool_use(messages[tail_start - 1])):
        tail_start -= 1
    if head_end >= tail_start:
        return messages
    snipped = tail_start - head_end
    return (messages[:head_end]
            + [{"role": "user", "content": f"[snipped {snipped} messages]"}]
            + messages[tail_start:])


def write_transcript(messages: list) -> Path:
    TRANSCRIPT_DIR.mkdir(parents=True, exist_ok=True)
    path = TRANSCRIPT_DIR / f"transcript_{int(time.time())}.jsonl"
    with path.open("w", encoding="utf-8") as f:
        for msg in messages:
            f.write(json.dumps(msg, default=str, ensure_ascii=False) + "\n")
    return path


def summarize_history(messages: list) -> str:
    conversation = json.dumps(messages, default=str, ensure_ascii=False)[:80000]
    prompt = ("Summarize this coding-agent conversation so work can continue. "
              "Preserve current goal, key findings, changed files, remaining work, "
              "and user constraints.\n\n" + conversation)
    response = client.messages.create(
        model=MODEL, messages=[{"role": "user", "content": prompt}], max_tokens=2000)
    return extract_text(response.content) or "(empty summary)"


def compact_history(messages: list) -> list:
    transcript = write_transcript(messages)
    print(f"  \033[36m[compact] 完整记录已存: {transcript}\033[0m")
    # 会话记忆够用就直接拿它当摘要，省掉一次模型调用（CC sessionMemoryCompact）
    summary = session_compact_summary()
    if summary:
        print("  \033[36m[compact] 用会话记忆做摘要（省一次模型调用）\033[0m")
    else:
        summary = summarize_history(messages)
    return [{"role": "user", "content": f"[Compacted]\n\n{summary}"}]


def reactive_compact(messages: list) -> list:
    """遇到 prompt-too-long 时的兜底：保留末尾几条，前面全部摘要。"""
    write_transcript(messages)
    tail_start = max(0, len(messages) - 5)
    if (0 < tail_start < len(messages)
            and is_tool_result_message(messages[tail_start])
            and message_has_tool_use(messages[tail_start - 1])):
        tail_start -= 1
    try:
        summary = summarize_history(messages[:tail_start])
    except Exception:
        summary = "Earlier conversation was trimmed after a prompt-too-long error."
    return [{"role": "user", "content": f"[Reactive compact]\n\n{summary}"},
            *messages[tail_start:]]


def prepare_context(messages: list) -> list:
    """每次调用模型前，都过一遍上下文预算管线。"""
    messages[:] = snip_compact(messages)
    messages[:] = micro_compact(messages)
    if estimate_size(messages) > CONTEXT_LIMIT:
        messages[:] = compact_history(messages)
    return messages


# ═══════════════════════════════════════════════════════════════════════════
#  Error Recovery —— 429 退避重试 / 529 过载切备用模型 / prompt 过长回退
# ═══════════════════════════════════════════════════════════════════════════

class RecoveryState:
    def __init__(self):
        self.has_escalated = False
        self.recovery_count = 0
        self.consecutive_529 = 0
        self.has_attempted_reactive_compact = False
        self.current_model = PRIMARY_MODEL


def retry_delay(attempt: int) -> float:
    base = min(BASE_DELAY_MS * (2 ** attempt), 32000) / 1000
    return base + random.uniform(0, base * 0.25)


def with_retry(fn, state: RecoveryState):
    for attempt in range(MAX_RETRIES):
        try:
            result = fn()
            state.consecutive_529 = 0
            return result
        except Exception as e:
            name = type(e).__name__.lower()
            msg = str(e).lower()
            if "ratelimit" in name or "429" in msg:
                delay = retry_delay(attempt)
                print(f"  \033[33m[429] retry {attempt + 1}/{MAX_RETRIES} after {delay:.1f}s\033[0m")
                time.sleep(delay)
                continue
            if "overloaded" in name or "529" in msg or "overloaded" in msg:
                state.consecutive_529 += 1
                if state.consecutive_529 >= MAX_CONSECUTIVE_529 and FALLBACK_MODEL:
                    state.current_model = FALLBACK_MODEL
                    state.consecutive_529 = 0
                    print(f"  \033[31m[529] switching to {FALLBACK_MODEL}\033[0m")
                delay = retry_delay(attempt)
                print(f"  \033[33m[529] retry {attempt + 1}/{MAX_RETRIES} after {delay:.1f}s\033[0m")
                time.sleep(delay)
                continue
            raise
    raise RuntimeError(f"Max retries ({MAX_RETRIES}) exceeded")


def is_prompt_too_long_error(e: Exception) -> bool:
    msg = str(e).lower()
    return (("prompt" in msg and "long" in msg)
            or "context_length_exceeded" in msg
            or "max_context_window" in msg)


# ═══════════════════════════════════════════════════════════════════════════
#  工具表 —— 模型看到 schema，Python 执行 handler，两张表一一对应
# ═══════════════════════════════════════════════════════════════════════════
BUILTIN_TOOLS = [
    {"name": "bash", "description": "Run a shell command in the working directory.",
     "input_schema": {"type": "object",
                      "properties": {"command": {"type": "string"}},
                      "required": ["command"]}},
    {"name": "read_file", "description": "Read file contents (optionally a slice).",
     "input_schema": {"type": "object",
                      "properties": {"path": {"type": "string"},
                                     "limit": {"type": "integer"},
                                     "offset": {"type": "integer"}},
                      "required": ["path"]}},
    {"name": "write_file", "description": "Write content to a file (creates dirs).",
     "input_schema": {"type": "object",
                      "properties": {"path": {"type": "string"},
                                     "content": {"type": "string"}},
                      "required": ["path", "content"]}},
    {"name": "edit_file", "description": "Replace exact text in a file once.",
     "input_schema": {"type": "object",
                      "properties": {"path": {"type": "string"},
                                     "old_text": {"type": "string"},
                                     "new_text": {"type": "string"}},
                      "required": ["path", "old_text", "new_text"]}},
    {"name": "glob", "description": "Find files matching a glob pattern.",
     "input_schema": {"type": "object",
                      "properties": {"pattern": {"type": "string"}},
                      "required": ["pattern"]}},
    {"name": "grep", "description": "Regex-search file contents recursively.",
     "input_schema": {"type": "object",
                      "properties": {"pattern": {"type": "string"},
                                     "path": {"type": "string"},
                                     "glob": {"type": "string"}},
                      "required": ["pattern"]}},
    {"name": "todo_write",
     "description": "Create/replace the session task list (plan before executing).",
     "input_schema": {"type": "object",
                      "properties": {"todos": {"type": "array", "items": {
                          "type": "object",
                          "properties": {"content": {"type": "string"},
                                         "status": {"type": "string",
                                                    "enum": ["pending", "in_progress", "completed"]}},
                          "required": ["content", "status"]}}},
                      "required": ["todos"]}},
    {"name": "task",
     "description": "Launch a focused subagent for a subtask. Returns only its summary.",
     "input_schema": {"type": "object",
                      "properties": {"description": {"type": "string"}},
                      "required": ["description"]}},
    {"name": "load_skill", "description": "Load the full text of a skill by name.",
     "input_schema": {"type": "object",
                      "properties": {"name": {"type": "string"}},
                      "required": ["name"]}},
    {"name": "compact",
     "description": "Summarize earlier conversation and continue with less context.",
     "input_schema": {"type": "object",
                      "properties": {"focus": {"type": "string"}}, "required": []}},
]

BUILTIN_HANDLERS = {
    "bash": run_bash, "read_file": run_read, "write_file": run_write,
    "edit_file": run_edit, "glob": run_glob, "grep": run_grep,
    "todo_write": run_todo_write, "task": spawn_subagent,
    "load_skill": load_skill,
}


def call_tool_handler(handler, args: dict, name: str) -> str:
    if not handler:
        return f"Unknown tool: {name}"
    try:
        return handler(**(args or {}))
    except TypeError as e:
        return f"Error: {e}"


# ── 工具池装配：内置工具 + 记忆工具 + MCP 工具 ──
# MCP 工具在启动时通过 tools/list 动态发现，按 mcp__{server}__{tool} 并进来。
# 装配后三者对模型、对 agent_loop、对权限钩子完全一视同仁。
TOOLS: list[dict] = list(BUILTIN_TOOLS)
HANDLERS: dict = dict(BUILTIN_HANDLERS)


def assemble_tool_pool():
    """把记忆工具和 MCP 工具（若 .env 里开了）并入全局工具池。"""
    global TOOLS, HANDLERS
    mem_tools, mem_handlers = memory_tools()
    mcp_tools, mcp_handlers = connect_mcp_servers()
    TOOLS = list(BUILTIN_TOOLS) + mem_tools + mcp_tools
    HANDLERS = {**BUILTIN_HANDLERS, **mem_handlers, **mcp_handlers}
    return TOOLS, HANDLERS


def mcp_tool_count() -> int:
    """按前缀数 MCP 工具——工具池里还有记忆工具，不能再用总数减内置数。"""
    return sum(1 for t in TOOLS if t["name"].startswith("mcp__"))


# ═══════════════════════════════════════════════════════════════════════════
#  Agent Loop —— 全部机制都围着这一个 while 循环转
# ═══════════════════════════════════════════════════════════════════════════

def call_llm(messages: list, state: RecoveryState, max_tokens: int):
    return with_retry(
        lambda: client.messages.create(
            model=state.current_model, system=assemble_system_prompt(),
            messages=messages, tools=TOOLS, max_tokens=max_tokens),
        state)


def agent_loop(messages: list):
    state = RecoveryState()
    max_tokens = DEFAULT_MAX_TOKENS
    start_prefetch(messages)        # 后台选记忆，不阻塞下面的循环
    snapshot = list(messages)

    while True:
        snapshot = list(messages)   # 压缩前快照：提炼记忆要看没被打码的对话
        prepare_context(messages)

        try:
            response = call_llm(inject_memories(messages), state, max_tokens)
        except Exception as e:
            if is_prompt_too_long_error(e) and not state.has_attempted_reactive_compact:
                messages[:] = reactive_compact(messages)
                state.has_attempted_reactive_compact = True
                continue
            messages.append({"role": "assistant", "content": [
                {"type": "text", "text": f"[Error] {type(e).__name__}: {e}"}]})
            return

        # 输出被 max_tokens 截断：先升配重试一次，再尝试续写
        if response.stop_reason == "max_tokens":
            if not state.has_escalated:
                max_tokens = ESCALATED_MAX_TOKENS
                state.has_escalated = True
                print(f"  \033[33m[max_tokens] retry with {max_tokens}\033[0m")
                continue
            messages.append({"role": "assistant", "content": response.content})
            if state.recovery_count < MAX_RECOVERY_RETRIES:
                messages.append({"role": "user", "content": CONTINUATION_PROMPT})
                state.recovery_count += 1
                continue
            return

        max_tokens = DEFAULT_MAX_TOKENS
        state.has_escalated = False
        messages.append({"role": "assistant", "content": response.content})

        # 没有 tool_use → 本轮结束
        if not has_tool_use(response.content):
            # Stop 钩子拿压缩前快照 + 本轮最后一条回复，提炼记忆才看得到原话
            trigger_hooks("Stop", snapshot + [messages[-1]])
            return

        results = []
        compacted_now = False
        for block in response.content:
            if block.type != "tool_use":
                continue
            print(f"\033[36m> {block.name}\033[0m")

            if block.name == "compact":
                messages[:] = compact_history(messages)
                messages.append({"role": "user",
                                 "content": "[Compacted. Continue with summarized context.]"})
                compacted_now = True
                break

            blocked = trigger_hooks("PreToolUse", block)
            if blocked:
                results.append({"type": "tool_result", "tool_use_id": block.id,
                                "content": str(blocked)})
                continue

            note_tool_use(block.name, block.input)   # 动过记忆就别再自动提炼
            output = call_tool_handler(HANDLERS.get(block.name),
                                       block.input, block.name)
            trigger_hooks("PostToolUse", block, output)
            print(str(output)[:300])
            results.append({"type": "tool_result", "tool_use_id": block.id,
                            "content": output})

        if compacted_now:
            continue
        messages.append({"role": "user", "content": results})


# ═══════════════════════════════════════════════════════════════════════════
#  入口 —— 交互式 REPL
# ═══════════════════════════════════════════════════════════════════════════
LOGO = r"""
        █████████╗  ██╗ ██████╗
        ╚══██╔═══╝  ██║██╔═══██╗
           ██║      ██║██║   ██║
           ██║      ██║██║   ██║
           ██║      ██║╚██████╔╝
           ╚═╝      ╚═╝ ╚═════╝

              T10 AGENT
"""

def _os_label() -> str:
    # platform.release() 在 Win11 上会误报 "10"，用 build 号(>=22000 即 Win11)纠正
    if platform.system() == "Windows":
        try:
            build = int(platform.version().split(".")[-1])
            return "Windows 11" if build >= 22000 else "Windows 10"
        except (ValueError, IndexError):
            return f"Windows {platform.release()}"
    return f"{platform.system()} {platform.release()}"


def _mcp_label() -> str:
    mode = (os.getenv("MCP_MODE") or "off").strip().lower()
    n = mcp_tool_count()
    if mode in ("off", "", "none", "false", "0"):
        return "off  \033[90m(.env 里设 MCP_MODE=manual 或 sdk 开启)\033[0m"
    impl = {"manual": "手写 client(mcp_manual/)", "sdk": "官方 SDK(mcp)"}.get(mode, mode)
    status = f"已连接 · {n} 个远端工具" if n else "\033[31m连接失败(已降级)\033[0m"
    return f"{mode}  \033[90m{impl}\033[0m  {status}"


def print_banner():
    now = datetime.now()
    week = "一二三四五六日"[now.weekday()]
    endpoint = os.getenv("ANTHROPIC_BASE_URL") or "api.anthropic.com (官方)"
    model_line = MODEL + (f"  · fallback: {FALLBACK_MODEL}" if FALLBACK_MODEL else "")
    rows = [
        ("时间", f"{now:%Y-%m-%d %H:%M:%S}  周{week}"),
        ("模型", model_line),
        ("端点", endpoint),
        ("目录", str(WORKDIR)),
        ("环境", f"{_os_label()} · Python {platform.python_version()}"),
        ("能力", f"{len(TOOLS)} tools ({mcp_tool_count()} via MCP)"
                 f" · {len(SKILL_REGISTRY)} skills"
                 f" · {memory_count()} memories"),
        ("MCP", _mcp_label()),
    ]
    print(f"\033[1;36m{LOGO}\033[0m")
    for label, value in rows:
        print(f"  \033[1;36m{label}\033[0m  \033[37m{value}\033[0m")
    print(f"\n  \033[90m回车发送 · q 退出 · 让我 compact 可压缩上下文 · "
          f"read/glob/grep 可读整机 · write/edit 出工作区会请你确认\033[0m\n")


if __name__ == "__main__":
    # 先做记忆自检（迁移旧格式 + 重建索引），再连 MCP（子进程 + 握手 + tools/list），
    # 最后打印 banner——这样 banner 里的能力数和记忆数才是准的
    init_memory()
    assemble_tool_pool()
    print_banner()

    try:
        history: list = []
        while True:
            try:
                query = input(f"\033[36m[{datetime.now():%H:%M}] myagent >> \033[0m")
            except (EOFError, KeyboardInterrupt):
                break
            if query.strip().lower() in ("q", "exit", "quit", ""):
                break
            trigger_hooks("UserPromptSubmit", query)
            turn_start = len(history)
            history.append({"role": "user", "content": query})
            agent_loop(history)
            # 打印本轮模型的文字回复
            for msg in history[turn_start:]:
                if msg.get("role") == "assistant":
                    for block in msg.get("content", []):
                        if block_type(block) == "text":
                            text = block["text"] if isinstance(block, dict) else block.text
                            print(f"\033[32m{text}\033[0m")
            print()
    finally:
        # 退出时关掉 MCP server 子进程，别留孤儿进程
        shutdown_mcp_servers()
