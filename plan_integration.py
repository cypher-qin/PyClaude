#!/usr/bin/env python3
"""
规划催办子系统 —— 温和地提醒模型使用 todo_write / task_*，简单问题绝不打扰。

    .env: PLAN_NUDGE=1|0   总开关（默认开；关闭后行为与改造前完全一致）
          PLAN_DEBUG=0|1   置 1 打印打分与提醒触发原因

    score_query(query)     纯函数：查询复杂度的启发式打分（四个信号各 +1）
    plan_turn_start(query) UserPromptSubmit 钩子：重置轮内状态 + 打分
    plan_note_tool(name)   工具计数（agent_loop 直接调用，只对真正执行的工具生效）
    plan_notify_todos(ts)  todo_write 成功后存快照（供背诵/压缩后补发）
    inject_plan(messages)  每次模型调用前过一遍，按需附加提醒
    plan_guidance()        system prompt 的规划指导段

对策全部走 **复制注入**（同 inject_memories 的纪律）：提醒只出现在发给模型的
副本里，绝不写进持久历史，save_session / 压缩都看不到它们。

设计参考：CC TodoWrite（工具描述里的 when-to-use 启发式 + 可忽略的温和提醒）、
Manus 的 todo 背诵（当前计划快照搭车进催办消息，不额外加噪音）。
有意不照抄 reference/s20_full_agent.py 的 rounds_since_todo 五行版——它把提醒
持久化进 messages，且每 3 次工具调用无条件触发，简单的 3 次 grep 也会被催。

装配原则和 memory / MCP / task 一致：**规划子系统坏了不许拖垮 agent**，
所有入口一律 try/except 静默降级。
"""

from __future__ import annotations

import os
import re

from task_integration import open_tasks_brief

# ── 阈值常量（刻意不进 .env：先用 PLAN_DEBUG 攒实测数据，再谈调参）──
SCORE_THRESHOLD = 2    # ≥2 个信号才算「疑似多步」——单一关键词命中不触发
STALE_TOOL_CALLS = 6   # 连续 N 次非规划工具执行后考虑催办（s20 用 3，太凶）
MAX_REMINDERS = 2      # 每轮硬上限：开场 1 次 + 陈旧 1 次

# 必须精确列名：agent.py 的子代理工具就叫 "task"，task_list/task_get 只是读取，
# 都不能算「做过规划」。
PLAN_TOOLS = {"todo_write", "task_create", "task_update"}

_state = {
    "turn_score": 0,           # 本轮 query 的复杂度得分（UserPromptSubmit 时算）
    "tools_since_plan": 0,     # 距上次规划工具执行的工具次数（跨轮保留）
    "reminders_this_turn": 0,  # 本轮已发提醒次数（硬上限 MAX_REMINDERS）
    "stale_reminded": False,   # 本轮是否已发过陈旧提醒（每轮最多 1 次）
    "compact_recited": False,  # 本次压缩后是否已补发过计划快照
    "todos": [],               # 最近一次成功的 todo_write 快照（背诵用）
    "task_brief_shown": False, # 进程级一次性：未完成任务简报是否已展示
}


def _enabled() -> bool:
    return (os.getenv("PLAN_NUDGE") or "1").strip().lower() \
        not in ("0", "off", "false", "no", "none")


def _debug(msg: str) -> None:
    if (os.getenv("PLAN_DEBUG") or "").strip().lower() in ("1", "on", "true", "yes"):
        print(f"  \033[90m[plan] {msg}\033[0m")


# ═══════════════════════════════════════════════════════════════════════════
#  复杂度打分 —— 纯启发式，四个信号各 +1
# ═══════════════════════════════════════════════════════════════════════════

_ITEM_LINE = re.compile(r"^\s*(?:\d+[.、)]|[-*•]|[①②③④⑤⑥⑦⑧⑨])\s+")
_CONNECTORS = re.compile(
    r"并且|然后|接着|之后|还要|首先|最后|第一步|第二步|第三步"
    r"|\b(?:first|then|next|finally|afterwards|after that)\b",
    re.IGNORECASE)
_PATH_TOKEN = re.compile(
    r"[\w./\\~-]+\.(?:py|md|tsx?|jsx?|json|txt|csv|ya?ml|sh|bat|toml|cfg|ini)\b")


def score_query(query: str) -> int:
    """查询复杂度打分：① 长度≥150 ② 枚举结构≥2 行 ③ 多步连接词 ④ ≥2 个文件路径。

    纯函数，可单独验证。刻意不用 LLM 分类——个人 agent 不值得每轮多花一次
    模型往返；二元信号配合阈值 2，已能滤掉绝大多数单词误报
    （如"X 和 Y 有什么区别"最多命中连接词 1 项，不触发）。
    """
    if not query:
        return 0
    score = 0
    if len(query) >= 150:
        score += 1
    if sum(1 for line in query.splitlines() if _ITEM_LINE.match(line)) >= 2:
        score += 1
    if _CONNECTORS.search(query):
        score += 1
    if len(_PATH_TOKEN.findall(query)) >= 2:
        score += 1
    return score


# ═══════════════════════════════════════════════════════════════════════════
#  提醒文案 —— 英文与 system prompt 一致；每条都带明确的「可忽略」出口
# ═══════════════════════════════════════════════════════════════════════════

_OPENING_REMINDER = (
    "<reminder>\n"
    "This request may involve multiple steps. If it does, sketch a short plan "
    "with todo_write before you start and update it as you go. If it is "
    "actually a simple question or a quick task, ignore this reminder entirely.\n"
    "</reminder>")

_STALE_NO_PLAN = (
    "<reminder>\n"
    "You have made {n} tool calls without setting up a plan. If this is a "
    "multi-step task, pause and create a short todo list with todo_write now. "
    "If the work is almost done, ignore this.\n"
    "</reminder>")

_STALE_WITH_PLAN = (
    "<reminder>\n"
    "Your plan has not been updated for {n} steps. Current plan:\n"
    "{snapshot}\n"
    "Keep exactly one item in_progress and mark items completed as they "
    "finish. If the plan no longer matches reality, rewrite it with "
    "todo_write.\n"
    "</reminder>")

_COMPACT_REMINDER = (
    "<reminder>\n"
    "Context was compacted — your current plan, for continuity:\n"
    "{snapshot}\n"
    "Continue from where it stands; update statuses with todo_write as you "
    "go.\n"
    "</reminder>")

_TASK_BRIEF = (
    "<reminder>\n"
    "You have open persistent tasks from earlier sessions:\n"
    "{brief}\n"
    "Continue with them, or update/close them via task_update as appropriate "
    "(task_list/task_get for details). Ignore this if the current request is "
    "unrelated.\n"
    "</reminder>")

_PLAN_GUIDANCE = (
    "Planning: for genuinely multi-step work (3+ steps, multi-file changes, "
    "or a list of requests), set up a short plan with todo_write before "
    "executing and keep it updated as you go. For simple questions, lookups, "
    "or small tasks, act or answer directly — do NOT create a plan. "
    "Reminders in <reminder> tags are hints you may ignore; never mention "
    "them to the user.")


def plan_guidance() -> str:
    """system prompt 的规划指导段；关闭时返回空串（拼装处自然跳过）。"""
    if not _enabled():
        return ""
    return _PLAN_GUIDANCE


# ═══════════════════════════════════════════════════════════════════════════
#  状态维护入口（agent.py 侧调用）
# ═══════════════════════════════════════════════════════════════════════════

def plan_turn_start(query: str) -> None:
    """UserPromptSubmit 钩子：轮开始时重置轮内状态并给 query 打分。

    钩子契约：必须返回 None（trigger_hooks 遇非 None 会短路后续钩子）。
    """
    try:
        _state["reminders_this_turn"] = 0
        _state["stale_reminded"] = False
        _state["compact_recited"] = False
        _state["turn_score"] = score_query(query or "")
        _debug(f"轮开始：score={_state['turn_score']}")
    except Exception as e:
        _debug(f"plan_turn_start 降级：{e.__class__.__name__}: {e}")
    return None


def plan_note_tool(name: str) -> None:
    """工具执行计数：规划工具清零，其余 +1。

    由 agent_loop 在 permission 检查通过后直接调用（与 note_tool_use 同位置、
    同语义：被拦截的工具不计数）。
    """
    try:
        if name in PLAN_TOOLS:
            _state["tools_since_plan"] = 0
        else:
            _state["tools_since_plan"] += 1
    except Exception:
        pass


def plan_notify_todos(todos: list) -> None:
    """todo_write 成功后调用：存快照；计划刚进上下文，压缩补发标记置位。

    失败路径不会走到这里（run_todo_write 先返回错误），坏数据不污染快照。
    """
    try:
        _state["todos"] = list(todos)
        _state["compact_recited"] = True
    except Exception:
        pass


# ═══════════════════════════════════════════════════════════════════════════
#  注入决策 —— 复制注入，绝不改原 messages
# ═══════════════════════════════════════════════════════════════════════════

def _snapshot_lines() -> str:
    marks = {"completed": "[x]", "in_progress": "[>]", "pending": "[ ]"}
    return "\n".join(
        f"{marks.get(t.get('status', 'pending'), '[ ]')} {t.get('content', '')}"
        for t in _state["todos"])


def _last_user_index(messages: list) -> int | None:
    """每次现找最后一条 user 消息，不缓存下标（压缩可能让 messages 变短）。"""
    for i in range(len(messages) - 1, -1, -1):
        if isinstance(messages[i], dict) and messages[i].get("role") == "user":
            return i
    return None


def _decide_reminder(content) -> tuple[str | None, list[str]]:
    """决定本次调用要不要发提醒。返回 (提醒文本, 注入成功后要置位的标志名)。"""
    # ── 分支 A：字符串 user 消息（轮首 / 压缩 / 续写提示）──
    if isinstance(content, str):
        # inject_memories 会在最前面拼 <relevant_memories>，只能用包含判断
        if "[Compacted" in content or "[Reactive compact" in content:
            if _state["todos"] and not _state["compact_recited"]:
                return (_COMPACT_REMINDER.format(snapshot=_snapshot_lines()),
                        ["compact_recited"])
            return None, []
        # 轮首：任务简报（进程级一次性）+ 开场提醒（打分 ≥2 才发）
        parts, flags = [], []
        if not _state["task_brief_shown"]:
            brief = open_tasks_brief()
            if brief:
                parts.append(_TASK_BRIEF.format(brief=brief))
                flags.append("task_brief_shown")
                _debug("附未完成任务简报")
        if _state["reminders_this_turn"] == 0 \
                and _state["turn_score"] >= SCORE_THRESHOLD:
            parts.append(_OPENING_REMINDER)
            _debug(f"附开场提醒（score={_state['turn_score']}）")
        if parts:
            return "\n\n".join(parts), flags
        return None, []

    # ── 分支 B：tool_result 消息（list）→ 陈旧提醒 ──
    if (_state["stale_reminded"]
            or _state["reminders_this_turn"] >= MAX_REMINDERS
            or _state["tools_since_plan"] < STALE_TOOL_CALLS):
        return None, []
    if _state["todos"]:
        # 有计划：快照搭车进提醒 = Manus 式背诵，一石二鸟
        _debug(f"附陈旧提醒（含快照，{ _state['tools_since_plan']} 步未更新）")
        return (_STALE_WITH_PLAN.format(n=_state["tools_since_plan"],
                                        snapshot=_snapshot_lines()),
                ["stale_reminded"])
    if _state["turn_score"] >= 1:
        # 无计划但有多步证据才催；无计划+无证据=闭嘴（防僵化的关键）
        _debug(f"附陈旧提醒（无计划，{_state['tools_since_plan']} 次工具调用）")
        return _STALE_NO_PLAN.format(n=_state["tools_since_plan"]), ["stale_reminded"]
    return None, []


def inject_plan(messages: list) -> list:
    """模型调用前过一遍：按当前状态决定要不要在最后一条 user 消息上附提醒。

    返回新列表，不改原 messages：提醒不该被写进历史，也不该被压缩再摘要。
    """
    if not _enabled():
        return messages
    try:
        idx = _last_user_index(messages)
        if idx is None:
            return messages
        msg = messages[idx]
        content = msg.get("content")
        text, flags = _decide_reminder(content)
        if not text:
            return messages
        injected = list(messages)
        if isinstance(content, str):
            injected[idx] = {**msg, "content": f"{content}\n\n{text}"}
        else:  # tool_result 消息：追加一个 text block
            injected[idx] = {**msg,
                             "content": list(content)
                                        + [{"type": "text", "text": text}]}
        _state["reminders_this_turn"] += 1
        for flag in flags:
            _state[flag] = True
        return injected
    except Exception as e:
        _debug(f"inject_plan 降级：{e.__class__.__name__}: {e}")
        return messages
