#!/usr/bin/env python3
"""
session —— 单会话内的连续性，供 compact 使用。

对标 CC 的 services/SessionMemory/ 与 services/compact/sessionMemoryCompact.ts：
压缩前先看会话记忆，够用就直接拿它当摘要，**省掉一次摘要模型调用**。

和跨会话记忆的分工（README「Memory 适合保存什么」）：
    memory   —— 以后还会用到的知识，跨会话
    session  —— 当前会话压缩之后还需要保留的上下文，单会话

另外它还是 dream 的会话门控信号源：一次会话一个文件，
「自上次整理以来新增了几个会话」直接数这个目录就有了。
"""

from __future__ import annotations

import os
import time
from functools import lru_cache
from pathlib import Path

from memory import memdir

DIR_NAME = "session"

# CC 的阈值是 token（≥10K token / ≥5 条消息 / ≤40K token），按 200K 上下文定的。
# T10 的 CONTEXT_LIMIT 只有 50KB，等比例缩放；单位换成字符，免得引 tokenizer。
MIN_CHARS = 3_000
MAX_CHARS = 12_000
MIN_TURNS = 5

_TURN_MARK = "## Turn"


@lru_cache(maxsize=1)
def session_id() -> str:
    return f"{time.strftime('%Y%m%d-%H%M%S')}-{os.getpid()}"


def session_dir() -> Path:
    return memdir.memory_root() / DIR_NAME


def session_path() -> Path:
    return session_dir() / f"{session_id()}.md"


def append_turn(user_text: str, assistant_text: str) -> None:
    """每轮结束追加一轮对话。纯文本拼接，不调模型。"""
    if not (user_text or assistant_text):
        return
    path = session_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    index = turn_count() + 1
    with path.open("a", encoding="utf-8") as f:
        f.write(f"\n{_TURN_MARK} {index}\n"
                f"**user:** {user_text.strip()}\n\n"
                f"**assistant:** {assistant_text.strip()}\n")


def read() -> str:
    path = session_path()
    return path.read_text(encoding="utf-8") if path.is_file() else ""


def turn_count() -> int:
    return read().count(_TURN_MARK)


def summary() -> str | None:
    """够用就返回会话记忆本身当摘要，否则 None（调用方回落到模型摘要）。

    三道阈值缺一不可：太短说不清、太长不如让模型压、轮次太少没积累。
    """
    text = read()
    if not text:
        return None
    if turn_count() < MIN_TURNS:
        return None
    if not MIN_CHARS <= len(text) <= MAX_CHARS:
        return None
    return text.strip()


def sessions_since(timestamp: float) -> int:
    """自某个时间点以来有过几个会话——dream 的会话门控用。"""
    directory = session_dir()
    if not directory.is_dir():
        return 0
    return sum(1 for f in directory.glob("*.md") if f.stat().st_mtime > timestamp)
