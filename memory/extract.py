#!/usr/bin/env python3
"""
extract —— 每轮结束后从对话里提炼新记忆。

对标 CC 的 services/extractMemories/extractMemories.ts：forked agent、受限工具
（只给 remember）、maxTurns=5。

用 forked agent 而不是「让模型吐 JSON 再解析」有个实打实的好处：
写入是靠调用 remember 工具完成的，**整条链路没有 JSON 解析这一环**，
也就没有「解析失败 → 这轮提炼全丢」这类故障。

重叠保护（主 agent 本轮已自己写过记忆就别重复提）由集成层判断，不在这里。
"""

from __future__ import annotations

from memory import memdir

MAX_TURNS = 5              # extractMemories.ts:371-427
RECENT_MESSAGES = 10
DIALOGUE_CHARS = 4000

_SYSTEM = (
    "You extract durable memories from a coding session, and do nothing else.\n"
    "Save what will still matter in a FUTURE session: user preferences, standing "
    "feedback about how to work, project facts, useful pointers. Skip one-off task "
    "details, file contents, and anything already covered by the existing memories.\n"
    "Save each fact with the remember tool — one call per fact, kebab-case name. "
    "Reuse an existing name to correct or extend that memory.\n"
    "If there is nothing new worth keeping, save nothing and reply 'nothing to save'."
)


def extract(snapshot: list, fork_agent) -> str:
    """从压缩前快照提炼记忆。fork_agent(description, system=, max_turns=) -> str。"""
    dialogue = format_dialogue(snapshot)
    if not dialogue.strip():
        return "nothing to save (empty dialogue)"
    existing = "\n".join(f"- {m.name}: {m.description}"
                         for m in memdir.scan_memories()) or "(none)"
    description = (f"Existing memories:\n{existing}\n\n"
                   f"Dialogue:\n{dialogue}")
    return fork_agent(description, system=_SYSTEM, max_turns=MAX_TURNS)


def format_dialogue(messages: list) -> str:
    """只保留文字，丢掉工具调用与工具结果——提炼靠的是对话，不是执行细节。"""
    parts = []
    for msg in messages[-RECENT_MESSAGES:]:
        text = text_of(msg.get("content"))
        if text:
            parts.append(f"{msg.get('role', '?')}: {text}")
    return "\n".join(parts)[-DIALOGUE_CHARS:]


def text_of(content) -> str:
    """取出 content 里的纯文字。content 可能是 str、dict 列表，或 SDK 的块对象。"""
    if isinstance(content, str):
        return content.strip()
    if not isinstance(content, list):
        return ""
    chunks = []
    for block in content:
        if isinstance(block, dict):
            if block.get("type") == "text":
                chunks.append(str(block.get("text", "")))
        elif getattr(block, "type", None) == "text":
            chunks.append(str(getattr(block, "text", "")))
    return " ".join(chunk for chunk in chunks if chunk).strip()
