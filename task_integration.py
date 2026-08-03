#!/usr/bin/env python3
"""
任务系统集成层 —— 任务子系统对 agent.py 的唯一入口。

    .env: TASKS_ENABLED=1|0     总开关（默认开）

对标 CC 的四个工具（不是教学版的五个）：依赖关系不在创建时声明，
由 task_update 的 addBlocks / addBlockedBy 事后维护。

    task_create   建任务：subject / description / activeForm / metadata
    task_update   改任意字段 + addBlocks/addBlockedBy 维护依赖 + 状态流转
                  （内部走 store 里带锁的 claim / release）
    task_get      完整 JSON，跨会话恢复用
    task_list     一行摘要：状态 / owner / 阻塞标记

    task_tools()           (tools, handlers)，形状同 memory_tools()
    task_system_prompt()   与 todo_write 的分工说明 → system，每次请求
    task_count()           banner 显示用

装配原则和 memory / MCP 一致：**任务系统坏了不许拖垮 agent**，
工具内一律把异常翻译成错误文本返回，不抛进主循环。
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict

from tasks import store
from tasks.store import TaskError


def _enabled() -> bool:
    return (os.getenv("TASKS_ENABLED") or "1").strip().lower() \
        not in ("0", "off", "false", "no", "none")


# ═══════════════════════════════════════════════════════════════════════════
#  system prompt / banner
# ═══════════════════════════════════════════════════════════════════════════

def task_system_prompt() -> str:
    """todo_write 与 task_* 的分工——两套系统共存，不互相替代。"""
    if not _enabled():
        return ""
    return (
        "Task system: task_* tools manage a PERSISTENT, cross-session task "
        "graph in .tasks/ — use it for goals bigger than one turn: break the "
        "goal into tasks, wire dependencies with task_update "
        "(addBlockedBy/addBlocks), then work through them "
        "(pending → in_progress → completed). Blocked tasks cannot start "
        "until their dependencies complete. todo_write is different: an "
        "in-memory checklist for the current turn's steps — keep using it "
        "for immediate execution plans. Tasks survive session resume; "
        "after a resume, task_list to see where things stand.")


def task_count() -> int:
    """banner 用：未完成任务数。读不到就报 0，不让 banner 崩。"""
    if not _enabled():
        return 0
    try:
        return sum(1 for t in store.list_tasks() if t.status != "completed")
    except Exception:
        return 0


def open_tasks_brief(limit: int = 5) -> str:
    """未完成任务一行一条（规划催办的任务简报用）。读不到/没有 → 空串，绝不抛。"""
    if not _enabled():
        return ""
    try:
        tasks = [t for t in store.list_tasks() if t.status != "completed"]
    except Exception:
        return ""
    if not tasks:
        return ""
    marks = {"pending": " ", "in_progress": ">"}
    lines = [f"[{marks.get(t.status, ' ')}] #{t.id} {t.subject}"
             for t in tasks[:limit]]
    if len(tasks) > limit:
        lines.append(f"... ({len(tasks) - limit} more — use task_list)")
    return "\n".join(lines)


# ═══════════════════════════════════════════════════════════════════════════
#  四个工具
# ═══════════════════════════════════════════════════════════════════════════

_TASK_CREATE = {
    "name": "task_create",
    "description": (
        "Create a persistent task (survives across sessions). Dependencies "
        "are NOT set here — wire them afterwards with task_update "
        "addBlockedBy/addBlocks."),
    "input_schema": {
        "type": "object",
        "properties": {
            "subject": {"type": "string", "description": "Short title"},
            "description": {"type": "string",
                            "description": "Free-form detail; include enough "
                                           "context to resume work cold"},
            "activeForm": {"type": "string",
                           "description": "Present-continuous form shown while "
                                          "in_progress, e.g. 'Writing tests'"},
            "metadata": {"type": "object",
                         "description": "Arbitrary key-value extensions"},
        },
        "required": ["subject"],
    },
}

_TASK_UPDATE = {
    "name": "task_update",
    "description": (
        "Update a task's fields, wire dependencies (addBlockedBy/addBlocks), "
        "or move its status: pending → in_progress (claims it; blocked tasks "
        "refuse), in_progress → completed (unblocks downstream), "
        "in_progress → pending (releases ownership for re-claim)."),
    "input_schema": {
        "type": "object",
        "properties": {
            "task_id": {"type": "string"},
            "subject": {"type": "string"},
            "description": {"type": "string"},
            "activeForm": {"type": "string"},
            "status": {"type": "string",
                       "enum": ["pending", "in_progress", "completed"]},
            "owner": {"type": "string",
                      "description": "Claim for this owner; empty string "
                                     "clears ownership"},
            "metadata": {"type": "object",
                         "description": "Merged into existing metadata"},
            "addBlockedBy": {"type": "array", "items": {"type": "string"},
                             "description": "Task IDs that must complete "
                                            "before this one"},
            "addBlocks": {"type": "array", "items": {"type": "string"},
                          "description": "Task IDs that this one blocks"},
        },
        "required": ["task_id"],
    },
}

_TASK_GET = {
    "name": "task_get",
    "description": "Get a task's full JSON (description, deps, metadata).",
    "input_schema": {
        "type": "object",
        "properties": {"task_id": {"type": "string"}},
        "required": ["task_id"],
    },
}

_TASK_LIST = {
    "name": "task_list",
    "description": (
        "List all tasks, one line each: status, owner, blocked marker. "
        "Use task_get for full details."),
    "input_schema": {"type": "object", "properties": {}, "required": []},
}


# 参数全部给默认值：模型漏传字段时返回错误文本，而不是抛 TypeError 打断整轮。
def _run_create(subject: str = "", description: str = "",
                activeForm: str = "", metadata: dict | None = None) -> str:
    if not subject.strip():
        return "Error: task_create requires 'subject'."
    try:
        task = store.create_task(subject, description, activeForm,
                                 metadata if isinstance(metadata, dict) else None)
    except Exception as e:
        return f"Error: could not create task: {e.__class__.__name__}: {e}"
    print(f"  \033[34m[task #{task.id}] created: {task.subject}\033[0m")
    return f"Created task {task.id}: {task.subject}"


def _run_update(task_id: str = "", subject: str | None = None,
                description: str | None = None, activeForm: str | None = None,
                status: str | None = None, owner: str | None = None,
                metadata: dict | None = None,
                addBlockedBy: list | None = None,
                addBlocks: list | None = None) -> str:
    if not str(task_id).strip():
        return "Error: task_update requires 'task_id'."
    try:
        for dep_id in addBlockedBy or []:
            store.add_dependency(task_id, dep_id)
        for down_id in addBlocks or []:
            store.add_dependency(down_id, task_id)
        task, unblocked = store.update_task(
            str(task_id), subject=subject, description=description,
            activeForm=activeForm, status=status, owner=owner,
            metadata=metadata if isinstance(metadata, dict) else None)
    except TaskError as e:
        return f"Error ({e.code}): {e}"
    except Exception as e:
        return f"Error: could not update task: {e.__class__.__name__}: {e}"

    marks = {"in_progress": (">", "36"), "completed": ("x", "32"),
             "pending": (" ", "33")}
    mark, color = marks[task.status]
    label = task.activeForm if task.status == "in_progress" and task.activeForm \
        else task.subject
    print(f"  \033[{color}m[task #{task.id}] [{mark}] {label}\033[0m")

    msg = f"Task {task.id} is now {task.status}"
    if task.owner:
        msg += f" (owner: {task.owner})"
    if unblocked:
        msg += f"\nUnblocked: {', '.join(unblocked)}"
    return msg


def _run_get(task_id: str = "") -> str:
    if not str(task_id).strip():
        return "Error: task_get requires 'task_id'."
    try:
        return json.dumps(asdict(store.load_task(str(task_id))),
                          ensure_ascii=False, indent=2)
    except TaskError as e:
        return f"Error ({e.code}): {e}"
    except Exception as e:
        return f"Error: {e.__class__.__name__}: {e}"


def _run_list() -> str:
    try:
        tasks = store.list_tasks()
    except Exception as e:
        return f"Error: could not list tasks: {e.__class__.__name__}: {e}"
    if not tasks:
        return "No tasks."
    marks = {"pending": " ", "in_progress": ">", "completed": "x"}
    lines = []
    for t in tasks:
        owner = f" @{t.owner}" if t.owner else ""
        blocked = ""
        if t.status == "pending" and t.blockedBy:
            try:
                blocked = " [blocked]" if not store.can_start(t.id) else ""
            except Exception:
                blocked = ""
        lines.append(f"[{marks[t.status]}] #{t.id} {t.subject}{owner}{blocked}")
    return "\n".join(lines)


def task_tools() -> tuple[list[dict], dict]:
    """返回 (tools, handlers)，形状与 memory_tools() 一致，便于并池。"""
    if not _enabled():
        return [], {}
    return ([_TASK_CREATE, _TASK_UPDATE, _TASK_GET, _TASK_LIST],
            {"task_create": _run_create, "task_update": _run_update,
             "task_get": _run_get, "task_list": _run_list})
