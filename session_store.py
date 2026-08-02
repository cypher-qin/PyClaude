#!/usr/bin/env python3
"""
session_store —— 会话存档的纯存储层。

不知模型、不知 agent：只负责 messages 的落盘、读回、列目录。
装配逻辑（CLI 参数、picker、回放渲染）在 session_integration.py。

存储：每会话一个文件 <项目根>/.sessions/<时间戳>-<pid>.jsonl

    行 1   {"type":"meta", ...}      id / 标题 / 轮数 / 创建与更新时间 / 恢复次数
    行 2+  {"type":"message", ...}   一条历史消息（role + content，API wire 格式）

每轮结束整文件重写（临时文件 + os.replace 原子替换），而不是 append：
CONTEXT_LIMIT 只有 50KB，全量重写成本可忽略；而压缩会原地替换 history
（messages[:] = ...），append-only 的偏移量记账遇到压缩必然错乱——
快照重写才永远一致。进程崩溃只丢进行中的半轮，文件里永远是完整轮次。
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

FORMAT_VERSION = 1


def project_root() -> Path:
    """向上找 .git 定位项目根，找不到退回 cwd。

    与 memory/memdir.py 同一判定（那边也自带了一份保持自洽，这里同理）。
    从子目录启动 agent 也能找到同一份存档，不会「换个目录就丢会话」。
    """
    start = Path.cwd().resolve()
    for d in (start, *start.parents):
        if (d / ".git").exists():
            return d
    return start


def session_root() -> Path:
    """存档目录。默认 <项目根>/.sessions，SESSION_ROOT 可覆盖。"""
    override = (os.getenv("SESSION_ROOT") or "").strip()
    if override:
        return Path(override).expanduser().resolve()
    return project_root() / ".sessions"


def new_session_path() -> Path:
    """生成不冲突的存档路径：时间戳-pid，撞了（如同进程 /new 后同一秒）加序号。"""
    base = f"{time.strftime('%Y%m%d-%H%M%S')}-{os.getpid()}"
    path = session_root() / f"{base}.jsonl"
    n = 2
    while path.exists():
        path = session_root() / f"{base}-{n}.jsonl"
        n += 1
    return path


def to_wire(obj):
    """把 history 里的内容序列化成可重载的纯 Python 对象。

    assistant 消息的 content 是 SDK 的 Pydantic 块（response.content 原样
    append），json.dumps(default=str) 会写成 "TextBlock(...)" 这样的 repr
    字符串，读不回来——必须走 model_dump 恢复成 API wire 格式（纯 dict）。
    读回后全是 dict：循环里所有 helper（block_type/has_tool_use/...）本来就
    双形态兼容，且 dict 正是发给 API 的线格式。
    """
    if isinstance(obj, dict):
        return {k: to_wire(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [to_wire(v) for v in obj]
    if hasattr(obj, "model_dump"):
        return to_wire(obj.model_dump(mode="json"))
    if isinstance(obj, (str, int, float, bool)) or obj is None:
        return obj
    return str(obj)


def save(path: Path, messages: list, meta: dict) -> None:
    """整文件原子重写：meta 行 + 每条消息一行。updated_at 在这里刷新。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    envelope = {"type": "meta", "version": FORMAT_VERSION,
                "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S"), **meta}
    tmp = path.with_suffix(".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        f.write(json.dumps(envelope, ensure_ascii=False) + "\n")
        for m in messages:
            line = {"type": "message", "role": m.get("role"),
                    "content": to_wire(m.get("content"))}
            f.write(json.dumps(line, ensure_ascii=False) + "\n")
    os.replace(tmp, path)


def load(path: Path) -> tuple[dict, list]:
    """读回 (meta, messages)。坏行跳过并在 meta["_bad_lines"] 里计数。"""
    meta, messages, bad = {}, [], 0
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            bad += 1
            continue
        if record.get("type") == "meta":
            meta = record
        elif (record.get("type") == "message"
                and record.get("role") in ("user", "assistant")
                and "content" in record):
            messages.append({"role": record["role"], "content": record["content"]})
        else:
            bad += 1
    if bad:
        meta["_bad_lines"] = bad
    return meta, messages


def _read_meta(path: Path) -> dict:
    """只读第一行拿 meta——列目录时不必解析整个文件。"""
    try:
        with path.open("r", encoding="utf-8") as f:
            record = json.loads(f.readline())
        return record if record.get("type") == "meta" else {}
    except (OSError, json.JSONDecodeError):
        return {}


def list_sessions(limit: int = 20, prefix: str = "") -> list[dict]:
    """按最后修改时间倒序列出存档摘要（picker / --list 用），可按 id 前缀过滤。"""
    root = session_root()
    if not root.is_dir():
        return []
    # startswith 而不用 glob pattern，避免前缀里的通配符被解释
    files = sorted((p for p in root.glob("*.jsonl") if p.stem.startswith(prefix)),
                   key=lambda p: p.stat().st_mtime, reverse=True)
    rows = []
    for path in files[:limit]:
        meta = _read_meta(path)
        rows.append({"id": path.stem, "path": path,
                     "mtime": path.stat().st_mtime,
                     "title": meta.get("title", ""),
                     "turns": meta.get("turns", "?"),
                     "updated_at": meta.get("updated_at", "")})
    return rows
