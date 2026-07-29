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
    memory              —— 读取/追加 .memory/MEMORY.md（s09）
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
load_dotenv(override=True)

# 用第三方 base_url 时，清掉可能残留的 ANTHROPIC_AUTH_TOKEN，避免鉴权串味
if os.getenv("ANTHROPIC_BASE_URL"):
    os.environ.pop("ANTHROPIC_AUTH_TOKEN", None)

client = Anthropic(base_url=os.getenv("ANTHROPIC_BASE_URL"))  # 未设则走官方
MODEL = os.environ["MODEL_ID"]
PRIMARY_MODEL = MODEL
FALLBACK_MODEL = os.getenv("FALLBACK_MODEL_ID")  # 可选：529 过载时切换的备用模型

WORKDIR = Path.cwd()
SKILLS_DIR = WORKDIR / "skills"
MEMORY_DIR = WORKDIR / ".memory"
MEMORY_INDEX = MEMORY_DIR / "MEMORY.md"
TRANSCRIPT_DIR = WORKDIR / ".transcripts"

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

def safe_path(p: str) -> Path:
    """文件类工具限制在工作目录内；bash 不受此限，交给 permission hook 管控。"""
    path = (WORKDIR / p).resolve()
    if not path.is_relative_to(WORKDIR):
        raise ValueError(f"Path escapes workspace: {p}")
    return path


def run_bash(command: str) -> str:
    try:
        r = subprocess.run(command, shell=True, cwd=WORKDIR,
                           capture_output=True, text=True, timeout=120)
        out = (r.stdout + r.stderr).strip()
        return out[:50000] if out else "(no output)"
    except subprocess.TimeoutExpired:
        return "Error: Timeout (120s)"
    except (FileNotFoundError, OSError) as e:
        return f"Error: {e}"


def run_read(path: str, limit: int | None = None, offset: int = 0) -> str:
    try:
        lines = safe_path(path).read_text(encoding="utf-8", errors="replace").splitlines()
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
        fp = safe_path(path)
        fp.parent.mkdir(parents=True, exist_ok=True)
        fp.write_text(content, encoding="utf-8")
        return f"Wrote {len(content)} bytes to {path}"
    except Exception as e:
        return f"Error: {e}"


def run_edit(path: str, old_text: str, new_text: str) -> str:
    try:
        fp = safe_path(path)
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
        results = [m for m in g.glob(pattern, root_dir=WORKDIR)
                   if (WORKDIR / m).resolve().is_relative_to(WORKDIR)]
        return "\n".join(results) if results else "(no matches)"
    except Exception as e:
        return f"Error: {e}"


def run_grep(pattern: str, path: str = ".", glob: str = "*") -> str:
    """在工作目录内递归搜索（纯 Python，跨平台，不依赖 rg/grep）。"""
    import fnmatch
    import re
    try:
        regex = re.compile(pattern)
    except re.error as e:
        return f"Error: bad regex: {e}"
    base = safe_path(path)
    hits = []
    files = [base] if base.is_file() else base.rglob("*")
    for fp in files:
        if not fp.is_file() or not fnmatch.fnmatch(fp.name, glob):
            continue
        try:
            for i, line in enumerate(fp.read_text(encoding="utf-8",
                                                  errors="replace").splitlines(), 1):
                if regex.search(line):
                    rel = fp.relative_to(WORKDIR)
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
#  Memory —— 跨会话记忆（读 .memory/MEMORY.md，remember 工具追加）
# ═══════════════════════════════════════════════════════════════════════════

def read_memory() -> str:
    if MEMORY_INDEX.exists():
        return MEMORY_INDEX.read_text(encoding="utf-8")[:2000]
    return ""


def run_remember(note: str) -> str:
    MEMORY_DIR.mkdir(exist_ok=True)
    stamp = datetime.now().strftime("%Y-%m-%d")
    with MEMORY_INDEX.open("a", encoding="utf-8") as f:
        f.write(f"- ({stamp}) {note.strip()}\n")
    return "Remembered."


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
    memories = read_memory()
    if memories:
        sections.append(f"Relevant memories:\n{memories}")
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
        try:
            safe_path(block.input.get("path", ""))
        except Exception:
            return f"Permission denied: path escapes workspace: {block.input.get('path')}"
    return None


def log_hook(block):
    print(f"\033[90m[hook] {block.name}\033[0m")
    return None


register_hook("PreToolUse", permission_hook)
register_hook("PreToolUse", log_hook)


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


def spawn_subagent(description: str) -> str:
    sub_tools = [t for t in BUILTIN_TOOLS if t["name"] in SUB_TOOL_NAMES]
    messages = [{"role": "user", "content": description}]
    for _ in range(30):
        response = client.messages.create(
            model=MODEL, system=SUB_SYSTEM, messages=messages,
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
                output = call_tool_handler(BUILTIN_HANDLERS.get(block.name),
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
    {"name": "remember",
     "description": "Persist a durable note to memory for future sessions.",
     "input_schema": {"type": "object",
                      "properties": {"note": {"type": "string"}},
                      "required": ["note"]}},
    {"name": "compact",
     "description": "Summarize earlier conversation and continue with less context.",
     "input_schema": {"type": "object",
                      "properties": {"focus": {"type": "string"}}, "required": []}},
]

BUILTIN_HANDLERS = {
    "bash": run_bash, "read_file": run_read, "write_file": run_write,
    "edit_file": run_edit, "glob": run_glob, "grep": run_grep,
    "todo_write": run_todo_write, "task": spawn_subagent,
    "load_skill": load_skill, "remember": run_remember,
}


def call_tool_handler(handler, args: dict, name: str) -> str:
    if not handler:
        return f"Unknown tool: {name}"
    try:
        return handler(**(args or {}))
    except TypeError as e:
        return f"Error: {e}"


# ═══════════════════════════════════════════════════════════════════════════
#  Agent Loop —— 全部机制都围着这一个 while 循环转
# ═══════════════════════════════════════════════════════════════════════════

def call_llm(messages: list, state: RecoveryState, max_tokens: int):
    return with_retry(
        lambda: client.messages.create(
            model=state.current_model, system=assemble_system_prompt(),
            messages=messages, tools=BUILTIN_TOOLS, max_tokens=max_tokens),
        state)


def agent_loop(messages: list):
    state = RecoveryState()
    max_tokens = DEFAULT_MAX_TOKENS

    while True:
        prepare_context(messages)

        try:
            response = call_llm(messages, state, max_tokens)
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
            trigger_hooks("Stop", messages)
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

            output = call_tool_handler(BUILTIN_HANDLERS.get(block.name),
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
if __name__ == "__main__":
    print(f"\033[1mmyagent\033[0m  ({MODEL} @ {os.getenv('ANTHROPIC_BASE_URL') or 'api.anthropic.com'})")
    print(f"工作目录: {WORKDIR}")
    print("输入任务，回车发送。输入 q 退出。\n")

    history: list = []
    while True:
        try:
            query = input(PROMPT)
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
