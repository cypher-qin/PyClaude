#!/usr/bin/env python3
"""
relevance —— 这一轮该注入哪几条记忆。

对标 CC 的 memdir/findRelevantMemories.ts：让模型自己做一次轻量 side-query，
按 name + description 挑，**不是** embedding 相似度。几十上百条记忆的规模下，
一次 200 token 的 side-query 比一整套向量基础设施划算得多。

本模块只回答「选哪几条」：不管什么时候调（那是集成层的事），也不写文件。
"""

from __future__ import annotations

import json
import re

from memory import memdir

MAX_ITEMS = 5              # findRelevantMemories.ts：最多 5 条
RECENT_CHARS = 2000        # 喂给 side-query 的最近对话上限
FALLBACK_MIN_SCORE = 0.2   # 关键词降级的最低重合度

# CC 给 side-query 的预算是 200 token，但那是按**非推理**模型定的。
# kimi-k2.5 会先出 thinking 块：实测 200 token 全被思考吃光，stop_reason=max_tokens、
# 一个 text 块都没有 → extract_text 返回空 → 每次都白白降级到关键词匹配。
# 实测 419 token 才走完「思考 + 输出 JSON」，这里给 1000 留足余量。
SIDE_QUERY_TOKENS = 1000

_SELECT_PROMPT = (
    "You pick which stored memories are relevant to a conversation.\n"
    f"Return the ones that are genuinely useful, at most {MAX_ITEMS}. "
    "If you are not sure, do not pick it.\n"
    'Reply with JSON only: {"selected_memories": ["a.md", "b.md"]}. '
    'Pick nothing by replying {"selected_memories": []}.'
)


def recent_user_text(messages: list, limit: int = 3) -> str:
    """最近几条用户输入。工具结果的 content 是 list，不算用户说的话。"""
    texts = []
    for msg in reversed(messages):
        if msg.get("role") != "user":
            continue
        content = msg.get("content")
        if isinstance(content, str) and content.strip():
            texts.append(content)
        if len(texts) >= limit:
            break
    return "\n".join(reversed(texts))[-RECENT_CHARS:]


def find_relevant(messages: list, side_query=None,
                  *, max_items: int = MAX_ITEMS) -> list[str]:
    """选出相关记忆的文件名。

    side_query 为 None（MEMORY_SELECT=keyword 或运行时未绑定）时直接走关键词降级。
    """
    memories = memdir.scan_memories()
    if not memories:
        return []
    recent = recent_user_text(messages)
    if not recent.strip():
        return []

    picked = None
    if side_query is not None:
        catalog = "\n".join(f"- {m.filename} — {m.name}: {m.description}"
                            for m in memories)
        prompt = (f"{_SELECT_PROMPT}\n\nRecent conversation:\n{recent}\n\n"
                  f"Memory catalog:\n{catalog}")
        try:
            picked = _parse_selection(side_query(prompt, SIDE_QUERY_TOKENS))
        except Exception as e:
            print(f"\033[33m[memory] side-query 失败，降级关键词匹配："
                  f"{e.__class__.__name__}: {e}\033[0m")

    if picked is None:                       # None = 没问成或没解析出来；[] = 模型说都不相关
        picked = _keyword_fallback(recent, memories, max_items)

    known = {m.filename for m in memories}   # 模型可能编出不存在的文件名
    return [f for f in picked if f in known][:max_items]


def build_injection(filenames: list[str]) -> str:
    """把选中的记忆读出来、裁剪、按预算截断，拼成注入块。"""
    memories = [m for m in (memdir.read_memory(f) for f in filenames) if m]
    memories = memdir.apply_budget(memories)
    if not memories:
        return ""
    blocks = "\n\n".join(memdir.clip_for_injection(m) for m in memories)
    return f"<relevant_memories>\n{blocks}\n</relevant_memories>"


# ═══════════════════════════════════════════════════════════════════════════
#  解析与降级
# ═══════════════════════════════════════════════════════════════════════════

def _parse_selection(raw: str) -> list[str] | None:
    """抠出模型回复里的选择结果。

    返回 None 表示「没解析出来」→ 调用方降级；返回 [] 表示「模型说都不相关」。
    这两者必须分开，否则模型每说一次「都不相关」就会白跑一次关键词匹配。
    """
    for pattern, extract in ((r"\{.*\}", _from_object), (r"\[.*\]", _from_array)):
        match = re.search(pattern, raw or "", re.DOTALL)
        if not match:
            continue
        try:
            return extract(json.loads(match.group()))
        except (json.JSONDecodeError, TypeError, AttributeError):
            return None
    return None


def _from_object(data) -> list[str] | None:
    picked = data.get("selected_memories")
    return [str(x) for x in picked] if isinstance(picked, list) else None


def _from_array(data) -> list[str] | None:
    return [str(x) for x in data] if isinstance(data, list) else None


def _bigrams(text: str) -> set[str]:
    """字符 2-gram。中文不按空格分词，按空格 split 的写法对中文完全失效。"""
    compact = re.sub(r"\s+", "", text.lower())
    return {compact[i:i + 2] for i in range(len(compact) - 1)}


def _keyword_fallback(recent: str, memories: list, max_items: int) -> list[str]:
    query = _bigrams(recent)
    if not query:
        return []
    scored = []
    for memory in memories:
        target = _bigrams(f"{memory.name} {memory.description}")
        if not target:
            continue
        score = len(query & target) / len(target)
        if score >= FALLBACK_MIN_SCORE:
            scored.append((score, memory.filename))
    scored.sort(reverse=True)
    return [filename for _, filename in scored[:max_items]]
