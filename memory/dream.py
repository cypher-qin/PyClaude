#!/usr/bin/env python3
"""
dream —— 低频整理：合并重复、淘汰过时。

对标 CC 的 services/autoDream/autoDream.ts：四层门控 + forked agent 增量剪枝。
锁文件的 mtime 就是 lastConsolidatedAt；1 小时后自动过期，用来做崩溃恢复。

**增量剪枝，不是先删后写**：forked agent 拿到目录后，用 remember 写合并结果、
用 forget 删掉已被取代的，一条一条来。全程不存在「记忆被清空」的窗口——
所以这里不需要暂存区和事务替换。真要加备份，位置就在 _take_lock() 之后。
"""

from __future__ import annotations

import time
from pathlib import Path

from memory import memdir, session

LOCK_NAME = ".consolidate-lock"
LOCK_TTL = 3600             # 1h：锁过期时间，兼做崩溃恢复
DREAM_INTERVAL = 24 * 3600  # 24h：距上次整理的最小间隔
MIN_MEMORIES = 10           # 记忆太少不值得整理
MIN_SESSIONS = 5            # 自上次整理以来的新会话数
MAX_TURNS = 20
TARGET_MAX = 30             # 整理后记忆总数上限

_SYSTEM = (
    "You tidy up a memory store, and do nothing else.\n"
    "1. Merge duplicates into a single memory\n"
    "2. Remove outdated or contradicted memories\n"
    f"3. Keep the total under {TARGET_MAX}\n"
    "4. Preserve important user preferences above all\n"
    "Work incrementally with the tools: remember(...) writes a merged memory "
    "(reusing a name overwrites it), forget(...) removes one that is now superseded. "
    "Never forget something unless its content already lives in a memory you kept. "
    "If the store is already tidy, change nothing and reply 'already tidy'."
)


def lock_path() -> Path:
    return memdir.memory_root() / LOCK_NAME


def last_consolidated_at() -> float:
    """锁文件的 mtime 就是上次整理时间（CC autoDream.ts）。"""
    lock = lock_path()
    return lock.stat().st_mtime if lock.is_file() else 0.0


def should_dream() -> tuple[bool, str]:
    """四层门控。返回 (要不要整理, 原因) —— 原因用于 MEMORY_DEBUG 打印。"""
    last = last_consolidated_at()
    age = time.time() - last

    # 门控 1 锁：别人正在整理，或刚崩溃还没过期
    if last and age < LOCK_TTL:
        return False, f"锁未过期（{age / 60:.0f} 分钟前）"
    # 门控 2 时间：距上次整理 ≥ 24h
    if last and age < DREAM_INTERVAL:
        return False, f"距上次整理 {age / 3600:.1f}h < {DREAM_INTERVAL / 3600:.0f}h"
    # 门控 3 数量：记忆够多才值得
    count = len(memdir.scan_memories())
    if count < MIN_MEMORIES:
        return False, f"记忆 {count} 条 < {MIN_MEMORIES}"
    # 门控 4 会话：自上次整理以来攒够新会话
    sessions = session.sessions_since(last)
    if sessions < MIN_SESSIONS:
        return False, f"新会话 {sessions} 个 < {MIN_SESSIONS}"

    return True, f"{count} 条记忆 · {sessions} 个新会话"


def dream(fork_agent) -> str:
    """整理一次。fork_agent(description, system=, max_turns=) -> str。"""
    _touch_lock()                       # 取锁：把 mtime 刷成现在，挡住并发的第二个进程
    try:
        return fork_agent(_catalog(), system=_SYSTEM, max_turns=MAX_TURNS)
    finally:
        _touch_lock()                   # 完成时间即下一次的 lastConsolidatedAt


def _touch_lock() -> None:
    lock = lock_path()
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_text(str(time.time()), encoding="utf-8")


def _catalog() -> str:
    """整理要看全文，不能只看 description——判重靠的是内容。"""
    return "\n\n".join(
        f"## {m.filename}\nname: {m.name}\ntype: {m.type}\n"
        f"description: {m.description}\n\n{m.body}"
        for m in memdir.scan_memories())
