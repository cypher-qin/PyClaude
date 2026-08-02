#!/usr/bin/env python3
"""
会话存档集成层 —— 存档/恢复子系统对 agent.py 的唯一入口。

    .env: SESSION_ENABLED=1|0   总开关（默认开）
          SESSION_ROOT=         存档目录（默认 <项目根>/.sessions）
          SESSION_MAX_LIST=20   picker 最多列几条
          SESSION_REPLAY=5      恢复/切换时回放最近几轮（0 = 只打一行 recap）

与 memory_integration / mcp_integration 同一模式：agent.py 只 import 这里，
存储细节全在 session_store.py。降级原则也一致：存档坏了不许拖垮 agent，
一律打印警告继续跑。

    ── 启动期 ──
    session_entry()         解析 argv（-c/-r/--list/--full），返回初始 history
    replay_loaded()         启动恢复后：分隔带 + 最近 N 轮回放（无恢复则无输出）
    session_banner_text()   banner 的「会话」行

    ── 每 turn ──
    save_session(history)   REPL 主循环在 agent_loop 返回后调用：快照落盘

    ── REPL 斜杠命令 ──
    handle_slash(cmd, history)   /resume /new /sessions；处理了返回 True
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

import session_store as store

# agent.py 里的合成消息（不算用户轮次，回放时特殊渲染）。值与 agent.py 的
# CONTINUATION_PROMPT 保持一致；不 import 是为了守住装配方向——agent.py
# import 这里，这里绝不反向 import agent.py。
_CONTINUATION = "Continue from the previous response. Do not repeat completed work."
_COMPACT_PREFIX = "[Compacted"


# ── 配置 ──────────────────────────────────────────────────────────────────

def _flag(name: str, default: str = "1") -> bool:
    return (os.getenv(name) or default).strip().lower() \
        not in ("0", "off", "false", "no", "none")


def _enabled() -> bool:
    return _flag("SESSION_ENABLED")


def _int_env(name: str, default: int) -> int:
    try:
        return int((os.getenv(name) or "").strip())
    except ValueError:
        return default


def _max_list() -> int:
    n = _int_env("SESSION_MAX_LIST", 20)
    return n if n > 0 else 20


def _replay_turns() -> int:
    return max(0, _int_env("SESSION_REPLAY", 5))


def _warn(where: str, e: Exception) -> None:
    print(f"\033[33m[session] {where}失败：{e.__class__.__name__}: {e}\033[0m")


# ── 当前存档状态 ───────────────────────────────────────────────────────────
#
# _current 为 None 表示还没写过盘（懒创建：进来就 q 的空会话不留文件）。
# 恢复会话时指向旧文件——同文件续写，不 fork，picker 不会因反复恢复堆积条目。

_current: dict | None = None              # {"path","created_at","title","resume_count"}
_loaded: tuple[dict, list] | None = None  # 启动恢复待回放：(meta, history)
_full_replay = False                      # --full 覆盖 SESSION_REPLAY


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def _is_user_turn(m: dict) -> bool:
    """用户真实输入的轮次边界（排除 tool_result 消息与合成消息）。"""
    return (m.get("role") == "user"
            and isinstance(m.get("content"), str)
            and not m["content"].startswith(_COMPACT_PREFIX)
            and m["content"] != _CONTINUATION)


def _derive_title(history: list) -> str:
    for m in history:
        if _is_user_turn(m):
            return " ".join(m["content"].split())[:60]
    return "(无标题)"


def _count_turns(history: list) -> int:
    return sum(1 for m in history if _is_user_turn(m))


# ── 写入 ──────────────────────────────────────────────────────────────────

def save_session(history: list) -> None:
    """轮边界落盘。REPL 在 agent_loop 返回后调用；崩溃只丢进行中的半轮。"""
    global _current
    if not _enabled() or not history:
        return
    try:
        if _current is None:
            _current = {"path": store.new_session_path(),
                        "created_at": _now(),
                        "title": _derive_title(history),
                        "resume_count": 0}
        meta = {"id": _current["path"].stem,
                "created_at": _current["created_at"],
                "model": os.getenv("MODEL_ID", ""),
                "title": _current["title"],
                "turns": _count_turns(history),
                "resume_count": _current["resume_count"]}
        store.save(_current["path"], history, meta)
    except Exception as e:
        _warn("保存会话", e)


def _adopt(path: Path, meta: dict) -> None:
    """把某个已存在的存档设为当前进程的写入目标（同文件续写）。"""
    global _current
    _current = {"path": path,
                "created_at": meta.get("created_at") or _now(),
                "title": meta.get("title") or "(无标题)",
                "resume_count": int(meta.get("resume_count") or 0) + 1}


# ── 列表与选择 ─────────────────────────────────────────────────────────────

def _fmt_time(row: dict) -> str:
    text = (row.get("updated_at") or "")[5:16].replace("T", " ")
    return text or time.strftime("%m-%d %H:%M", time.localtime(row["mtime"]))


def _print_table(rows: list[dict]) -> None:
    if not rows:
        print("  \033[90m（还没有会话存档）\033[0m")
        return
    for i, row in enumerate(rows, 1):
        print(f"  \033[36m{i:>2}.\033[0m {_fmt_time(row)}  "
              f"{row['turns']:>3} 轮  {row['title'][:40]}")


def _pick(prefix: str | None) -> Path | None:
    """前缀匹配或交互选择，返回 Path 或 None（取消）。"""
    rows = store.list_sessions(limit=_max_list(), prefix=prefix or "")
    if not rows:
        hint = f"以「{prefix}」开头" if prefix else ""
        print(f"  \033[90m没有{hint}的会话存档\033[0m")
        return None
    if prefix and len(rows) == 1:
        return rows[0]["path"]
    _print_table(rows)
    try:
        choice = input("  \033[36m选择序号（回车取消）>> \033[0m").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return None
    if choice.isdigit() and 1 <= int(choice) <= len(rows):
        return rows[int(choice) - 1]["path"]
    if choice:
        print("  \033[90m无效选择，已取消\033[0m")
    return None


# ── 回放渲染 ───────────────────────────────────────────────────────────────
#
# 从 messages 重新渲染，不是终端录像：对话内容保真，但运行时的瞬时打印
# （重试提示、hook 输出、记忆调试行）不在 messages 里，回放不会出现。

def _dim(text: str) -> str:
    return f"\033[90m{text}\033[0m"


def _clip(text: str, limit: int) -> str:
    text = text.strip()
    return text if len(text) <= limit else text[:limit] + " …"


def _block_get(block, key, default=None):
    """dict 与 SDK 对象双形态取值（回放活体 history 时拿到的是后者）。"""
    if isinstance(block, dict):
        return block.get(key, default)
    return getattr(block, key, default)


def _render_tool_use(block) -> None:
    name = _block_get(block, "name", "?")
    args = _block_get(block, "input", {}) or {}
    hint = ""
    if isinstance(args, dict):
        # 挑一个最有辨识度的参数跟上（live 只打工具名，回放需要重新定向）
        for key in ("path", "command", "pattern", "query",
                    "name", "description", "filename"):
            value = args.get(key)
            if isinstance(value, str) and value.strip():
                hint = _dim(f"  {_clip(value, 60)}")
                break
    print(f"\033[36m> {name}\033[0m{hint}")


def _render_tool_result(block) -> None:
    content = _block_get(block, "content", "")
    if isinstance(content, list):   # content blocks 形态：拼出文本
        content = "\n".join(str(_block_get(b, "text", "")) for b in content)
    print(_dim(_clip(str(content), 300)))


def _render_message(m: dict, turn_no: int | None) -> None:
    role, content = m.get("role"), m.get("content")
    if role == "user":
        if isinstance(content, str):
            if content.startswith(_COMPACT_PREFIX):
                print(_dim("── ⚡ 此处发生过压缩，早前上下文折叠为摘要 ──"))
                print(_dim(_clip(content, 400)))
            elif content == _CONTINUATION:
                print(_dim("── （输出截断，自动续写）──"))
            else:
                print(_dim(f"── 第 {turn_no} 轮 " + "─" * 24))
                print(f"\033[36mmyagent >> \033[0m{content}")
        elif isinstance(content, list):
            for block in content:
                _render_tool_result(block)
    elif role == "assistant" and isinstance(content, list):
        for block in content:
            kind = _block_get(block, "type")
            if kind == "text":
                print(f"\033[32m{_block_get(block, 'text', '')}\033[0m")
            elif kind == "tool_use":
                _render_tool_use(block)


def _divider(text: str) -> None:
    print(f"\033[90m{'╌' * 46}\033[0m")
    print(f"  \033[1;36m{text}\033[0m")


def _end_divider() -> None:
    print(f"\033[90m{'╌' * 46}\033[0m")


def replay(history: list, n_turns: int) -> None:
    """回放最近 n_turns 个用户轮次（含中间的助手回复与工具调用）。"""
    if not history:
        return
    if n_turns <= 0:
        print(_dim(f"  （共 {_count_turns(history)} 轮，SESSION_REPLAY=0 不回放）"))
        _end_divider()
        return
    bounds = [i for i, m in enumerate(history) if _is_user_turn(m)]
    start = bounds[-n_turns] if len(bounds) > n_turns else 0
    turn_no_of = {idx: rank + 1 for rank, idx in enumerate(bounds)}
    for i in range(start, len(history)):
        _render_message(history[i], turn_no_of.get(i))
    _end_divider()


def _announce(meta: dict, history: list) -> None:
    sid = meta.get("id", "?")
    active = (meta.get("updated_at") or "").replace("T", " ")
    _divider(f"已恢复会话 {sid}（{_count_turns(history)} 轮 · 最后活跃 {active}）")


# ── 启动入口 ───────────────────────────────────────────────────────────────

def session_entry() -> list:
    """解析 argv，返回初始 history（空 = 新会话）。--list 打印后直接退出进程。"""
    global _loaded, _full_replay
    parser = argparse.ArgumentParser(prog="agent.py",
                                     description="T10 个人编码 agent")
    parser.add_argument("-c", "--continue", dest="cont", action="store_true",
                        help="恢复最近一次会话")
    parser.add_argument("-r", "--resume", nargs="?", const="", default=None,
                        metavar="ID前缀",
                        help="恢复会话：带前缀直接匹配，不带则交互选择")
    parser.add_argument("--list", action="store_true",
                        help="列出会话存档后退出")
    parser.add_argument("--full", action="store_true",
                        help="恢复时全量回放（覆盖 SESSION_REPLAY）")
    args = parser.parse_args()

    if not _enabled():
        return []
    if args.list:
        _print_table(store.list_sessions(_max_list()))
        sys.exit(0)
    _full_replay = args.full

    path = None
    if args.cont:
        rows = store.list_sessions(1)
        if not rows:
            print("  \033[90m没有可恢复的会话存档\033[0m")
            return []
        path = rows[0]["path"]
    elif args.resume is not None:
        path = _pick(args.resume or None)
    if not path:
        return []
    try:
        meta, history = store.load(path)
    except Exception as e:
        _warn(f"读取存档 {path.name}", e)
        return []
    if meta.get("_bad_lines"):
        print(f"\033[33m[session] 存档有 {meta['_bad_lines']} 行损坏，已跳过\033[0m")
    _adopt(path, meta)
    _loaded = (meta, history)
    return history


def replay_loaded() -> None:
    """启动恢复后调用一次：分隔带 + 回放。没恢复则什么都不打印。"""
    global _loaded
    if not _loaded:
        return
    meta, history = _loaded
    _loaded = None
    _announce(meta, history)
    replay(history, 10 ** 9 if _full_replay else _replay_turns())


def session_banner_text() -> str:
    if not _enabled():
        return "off（SESSION_ENABLED=0）"
    return f"{len(store.list_sessions(10 ** 9))} 个存档 · -c 续上 · /resume 切换"


# ── REPL 斜杠命令 ──────────────────────────────────────────────────────────

def handle_slash(cmd: str, history: list) -> bool:
    """处理 /resume /new /sessions。返回 True = 已处理（含取消），False = 未知命令。"""
    name, _, rest = cmd.partition(" ")
    name, rest = name.lower(), rest.strip()
    if name not in ("/resume", "/new", "/sessions"):
        return False
    if not _enabled():
        print("  \033[90m会话存档已禁用（SESSION_ENABLED=0）\033[0m")
        return True

    global _current
    if name == "/sessions":
        _print_table(store.list_sessions(_max_list()))
        return True
    if name == "/new":
        old = _current["path"].stem if _current else None
        history[:] = []
        _current = None   # 下一轮结束时懒创建新文件，旧存档原样保留
        _divider(f"新会话（旧会话已存档：{old}）" if old else "新会话")
        _end_divider()
        return True

    # /resume：旧会话上一轮已落盘，切换零数据丢失（发生在 input 轮边界）
    path = _pick(rest or None)
    if not path:
        return True
    try:
        meta, messages = store.load(path)
    except Exception as e:
        _warn(f"读取存档 {path.name}", e)
        return True
    if meta.get("_bad_lines"):
        print(f"\033[33m[session] 存档有 {meta['_bad_lines']} 行损坏，已跳过\033[0m")
    _adopt(path, meta)
    history[:] = messages   # 原地替换：REPL 其余逻辑零改动
    _announce(meta, messages)
    replay(messages, _replay_turns())
    return True
