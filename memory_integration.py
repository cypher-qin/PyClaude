#!/usr/bin/env python3
"""
记忆集成层 —— 记忆子系统对 agent.py 的唯一入口。

    .env: MEMORY_ENABLED=1|0        总开关（默认开）
          MEMORY_ROOT=              记忆目录（默认 <项目根>/.memory）
          MEMORY_SELECT=llm|keyword 选记忆的方式（默认 llm，对标 CC）
          MEMORY_EXTRACT=1|0        每轮结束自动提炼记忆
          MEMORY_DREAM=1|0          低频整理
          MEMORY_DEBUG=0|1          打印跳过原因等内部决策

agent.py 只认这里的函数，不直接 import memory.*——
和 mcp_integration.py 同一个模式：子系统内部换实现，主程序一行不动。

    ── 启动期（各调一次）──
    bind_runtime()          注入「怎么调模型」，记忆包因此不反向依赖 agent.py
    init_memory()           启动自检：迁移旧格式 + 重建索引
    memory_tools()          remember / forget 的 (tools, handlers)，并入工具池

    ── 每 turn ──
    start_prefetch()        turn 开始：后台选记忆
    inject_memories()       注入到当前 user turn（CC 加载路径二），冷启动最多等 2.5s
    note_tool_use()         每次工具调用后：记下主 agent 是否动过记忆（重叠保护）
    on_turn_end()           Stop 钩子：提炼 + 整理 + 会话记忆

    ── 按需 ──
    memory_system_prompt()  行为说明 + 索引 → system（CC 加载路径一），每次请求
    session_compact_summary()  压缩时优先用会话记忆，省一次模型调用
    memory_count()          banner 显示用

    可调常量：PREFETCH_WAIT（冷启动等待上限，秒）

装配原则和 MCP 一致：**记忆坏了不许拖垮 agent**，一律降级继续跑。
"""

from __future__ import annotations

import os
import threading
from pathlib import Path

from memory import dream, extract, memdir, relevance, session


# ═══════════════════════════════════════════════════════════════════════════
#  配置与运行时
# ═══════════════════════════════════════════════════════════════════════════

def _flag(name: str, default: str = "1") -> bool:
    return (os.getenv(name) or default).strip().lower() \
        not in ("0", "off", "false", "no", "none")


def _enabled() -> bool:
    return _flag("MEMORY_ENABLED")


def _debug(message: str) -> None:
    if _flag("MEMORY_DEBUG", "0"):
        print(f"\033[90m[memory] {message}\033[0m")


def _warn(where: str, e: Exception) -> None:
    print(f"\033[33m[memory] {where}失败：{e.__class__.__name__}: {e}\033[0m")


_runtime: dict = {"side_query": None, "fork_agent": None}


def bind_runtime(*, side_query, fork_agent) -> None:
    """agent.py 把「怎么调模型」注入进来。

    side_query(prompt, max_tokens) -> str          一次性轻量调用，用于选记忆
    fork_agent(desc, system=, tools=, handlers=, max_turns=) -> str   隔离子代理
    """
    _runtime["side_query"] = side_query
    _runtime["fork_agent"] = fork_agent


def _fork(description: str, *, system: str, max_turns: int) -> str:
    """给记忆子代理的受限工具集：只有 remember / forget，别的一概没有。"""
    tools, handlers = _memory_tool_pool()
    return _runtime["fork_agent"](description, system=system, tools=tools,
                                  handlers=handlers, max_turns=max_turns)


# ═══════════════════════════════════════════════════════════════════════════
#  启动 / system prompt
# ═══════════════════════════════════════════════════════════════════════════

def init_memory(verbose: bool = True) -> None:
    """启动自检。放在拼 system prompt 之前跑，保证索引和磁盘一致。"""
    if not _enabled():
        return
    try:
        memdir.migrate_legacy()
        memdir.rebuild_index()
        if verbose:
            print(f"\033[32m[memory] {len(memdir.scan_memories())} 条记忆 @ "
                  f"{memdir.memory_root()}\033[0m")
    except Exception as e:
        _warn("初始化", e)
        print("\033[31m[memory] 已降级为无记忆继续运行\033[0m")


def memory_system_prompt() -> str:
    """索引常驻 system —— CC 两条加载路径里的第一条。

    只放索引（name + description），正文按需注入是 inject_memories 的事。
    """
    if not _enabled():
        return ""
    try:
        sections = [memdir.behavior_instructions()]
        index = memdir.read_index().strip()
        if index:
            sections.append(f"Memory index:\n{index}")
        return "\n\n".join(sections)
    except Exception as e:
        _warn("读取索引", e)
        return ""


def memory_count() -> int:
    """给 banner 用。读不到就报 0，不让 banner 崩。"""
    if not _enabled():
        return 0
    try:
        return len(memdir.scan_memories())
    except Exception:
        return 0


# ═══════════════════════════════════════════════════════════════════════════
#  工具：remember / forget
# ═══════════════════════════════════════════════════════════════════════════

_REMEMBER_TOOL = {
    "name": "remember",
    "description": (
        "Save a durable fact to memory for future sessions. Reusing an existing "
        "name overwrites that memory — that is how you update or correct one."),
    "input_schema": {
        "type": "object",
        "properties": {
            "name": {"type": "string",
                     "description": "Short kebab-case id, e.g. 'user-prefers-tabs'"},
            "type": {"type": "string", "enum": list(memdir.MEMORY_TYPES),
                     "description": "user | feedback | project | reference"},
            "description": {"type": "string",
                            "description": "One line, shown in the index"},
            "body": {"type": "string", "description": "Full detail in Markdown"},
        },
        "required": ["name", "type", "description", "body"],
    },
}

_FORGET_TOOL = {
    "name": "forget",
    "description": "Delete a memory that is obsolete or superseded by another one.",
    "input_schema": {
        "type": "object",
        "properties": {"filename": {"type": "string",
                                    "description": "e.g. 'user-prefers-tabs.md'"}},
        "required": ["filename"],
    },
}


# 参数名要和 schema 对齐，所以这里的 `type` 遮蔽了内置名——函数体内不用内置 type。
# 全部给默认值：模型漏传字段时返回错误文本，而不是抛 TypeError 打断整轮。
def _run_remember(name: str = "", type: str = "",
                  description: str = "", body: str = "") -> str:
    if not name.strip() or not body.strip():
        return "Error: remember requires at least 'name' and 'body'."
    try:
        path = memdir.write_memory(name, type, description or name, body)
    except Exception as e:
        return f"Error: could not write memory: {e.__class__.__name__}: {e}"
    return f"Remembered: {path.name}"


def _run_forget(filename: str = "") -> str:
    if not filename.strip():
        return "Error: forget requires 'filename'."
    try:
        removed = memdir.delete_memory(filename)
    except Exception as e:
        return f"Error: could not forget: {e.__class__.__name__}: {e}"
    return f"Forgot: {filename}" if removed else f"No such memory: {filename}"


def _memory_tool_pool() -> tuple[list[dict], dict]:
    return ([_REMEMBER_TOOL, _FORGET_TOOL],
            {"remember": _run_remember, "forget": _run_forget})


def memory_tools() -> tuple[list[dict], dict]:
    """返回 (tools, handlers)，形状与 connect_mcp_servers() 一致，便于并池。"""
    return _memory_tool_pool() if _enabled() else ([], {})


# ═══════════════════════════════════════════════════════════════════════════
#  加载路径二：预取 + 注入
# ═══════════════════════════════════════════════════════════════════════════

class _Turn:
    """一个 user turn 的记忆状态。每轮开始时整体换新，避免跨轮串味。"""

    def __init__(self, carry: str = ""):
        self.block = ""            # 本轮预取到的记忆块
        self.carry = carry         # 上一轮的结果，预取没就绪时先顶上
        self.done = False          # 预取是否已结束
        self.thread = None         # 预取线程
        self.waited = False        # 本轮是否已经等过一次
        self.wrote_memory = False  # 主 agent 本轮是否动过记忆

    def current(self) -> str:
        return self.block if self.done else self.carry


_turn = _Turn()
_last_block = ""       # 最近一次成功的选择结果，用作下一轮的热启动

# 冷启动时最多等预取多久（本轮没有可沿用的结果时才会等）。
# 记忆索引本来就常驻 system，等不到也不至于「完全不知道有这条记忆」。
PREFETCH_WAIT = 2.5


def start_prefetch(messages: list) -> None:
    """turn 开始时后台选记忆（CC query.ts:301-304）。不阻塞主循环。"""
    global _turn
    turn = _turn = _Turn(carry=_last_block)
    if not _enabled():
        turn.done = True
        return

    side_query = _runtime["side_query"]
    if (os.getenv("MEMORY_SELECT") or "llm").strip().lower() == "keyword":
        side_query = None          # 强制走关键词降级，省掉这次调用

    snapshot = list(messages)

    def worker():
        global _last_block
        try:
            picked = relevance.find_relevant(snapshot, side_query)
            turn.block = relevance.build_injection(picked)
            _last_block = turn.block
            _debug(f"预取选中 {len(picked)} 条：{picked or '（无）'}")
        except Exception as e:
            _warn("预取", e)
        finally:
            turn.done = True       # 绑定本轮实例，不受下一轮重置影响

    turn.thread = threading.Thread(target=worker, daemon=True)
    turn.thread.start()


def inject_memories(messages: list) -> list:
    """把记忆注入到当前 user turn（CC 加载路径二）。

    side-query 是一次完整的模型往返，短 turn（只有一次模型调用）等不到它。三级兜底：

        1. 预取已就绪   → 用本轮结果
        2. 有上一轮结果 → 先顶上，不等；本轮内预取回来后自动换成新的
        3. 都没有（冷启动）→ 等一次，上限 PREFETCH_WAIT

    等不到也不要紧：记忆索引本来就常驻 system prompt，模型至少知道有哪些记忆。

    返回新列表，不改原 messages：注入内容不该被写进历史，也不该被压缩再摘要。
    """
    if not _enabled():
        return messages
    if not _turn.done and not _turn.carry and not _turn.waited and _turn.thread:
        _turn.waited = True
        _turn.thread.join(timeout=PREFETCH_WAIT)
        if not _turn.done:
            _debug(f"预取超过 {PREFETCH_WAIT}s 未就绪，本次只靠 system 里的索引")
    block = _turn.current()
    if not block:
        return messages
    index = _last_user_text_index(messages)
    if index is None:
        return messages
    injected = list(messages)
    injected[index] = {**messages[index],
                       "content": f"{block}\n\n{messages[index]['content']}"}
    return injected


def _last_user_text_index(messages: list) -> int | None:
    """每次现找最后一条用户文本，不缓存下标。

    教学版把下标在 turn 开头就存死了，中途一触发压缩 messages 变短，
    `index < len(messages)` 不成立，注入就此静默失效。现找不会有这问题。
    """
    for i in range(len(messages) - 1, -1, -1):
        message = messages[i]
        if message.get("role") == "user" and isinstance(message.get("content"), str):
            return i
    return None


# ═══════════════════════════════════════════════════════════════════════════
#  写入路径：重叠保护 + turn 结束
# ═══════════════════════════════════════════════════════════════════════════

def note_tool_use(name: str, args: dict | None) -> None:
    """记下主 agent 本轮是否已经动过记忆（CC 的重叠保护）。

    动过就不再自动提炼——否则同一件事会被写两遍。
    """
    if name in ("remember", "forget"):
        _turn.wrote_memory = True
    elif name in ("write_file", "edit_file") and _under_memory_root(
            str((args or {}).get("path", ""))):
        _turn.wrote_memory = True


def _under_memory_root(path: str) -> bool:
    if not path:
        return False
    try:
        return Path(path).resolve().is_relative_to(memdir.memory_root())
    except (OSError, ValueError):
        return False


def on_turn_end(messages: list) -> None:
    """Stop 钩子：提炼记忆 + 低频整理 + 记一笔会话记忆。

    全部丢后台线程。主线程紧接着就阻塞在 input() 等用户打字，
    这段空闲正好给后台用完——**用户感知延迟为 0**。
    """
    if not _enabled():
        return
    threading.Thread(target=_turn_end_worker,
                     args=(list(messages), _turn.wrote_memory), daemon=True).start()


def _turn_end_worker(snapshot: list, wrote_memory: bool) -> None:
    try:
        session.append_turn(relevance.recent_user_text(snapshot, limit=1),
                            extract.text_of(snapshot[-1].get("content")
                                            if snapshot else ""))
    except Exception as e:
        _warn("写会话记忆", e)

    if not _flag("MEMORY_EXTRACT"):
        _debug("提炼已关闭（MEMORY_EXTRACT=0）")
    elif wrote_memory:
        _debug("主 agent 本轮已写过记忆，跳过提炼（重叠保护）")
    elif not _runtime["fork_agent"]:
        _debug("运行时未绑定，跳过提炼")
    else:
        try:
            _debug(f"提炼结果：{extract.extract(snapshot, _fork)[:120]}")
        except Exception as e:
            _warn("提炼记忆", e)

    if not _flag("MEMORY_DREAM"):
        _debug("整理已关闭（MEMORY_DREAM=0）")
    elif not _runtime["fork_agent"]:
        _debug("运行时未绑定，跳过整理")
    else:
        try:
            go, why = dream.should_dream()
            if not go:
                _debug(f"跳过整理：{why}")
            else:
                print(f"\033[36m[memory] 整理记忆（{why}）\033[0m")
                _debug(f"整理结果：{dream.dream(_fork)[:120]}")
        except Exception as e:
            _warn("整理记忆", e)


# ═══════════════════════════════════════════════════════════════════════════
#  压缩：优先用会话记忆
# ═══════════════════════════════════════════════════════════════════════════

def session_compact_summary() -> str | None:
    """会话记忆够用就拿它当摘要，省掉一次摘要模型调用；否则 None。"""
    if not _enabled():
        return None
    try:
        return session.summary()
    except Exception as e:
        _warn("读会话记忆", e)
        return None
