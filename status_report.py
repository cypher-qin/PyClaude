#!/usr/bin/env python3
"""
status_report —— 运行状态报告：等待动画 + token 结算 + 上下文占用提示。

调研结论的落地（参照 Claude Code / Codex CLI / aider 的实现）：

    P0  每轮结算行   读每次 API 响应的 usage 字段，轮末打印 ↑输入 ↓输出 · 耗时 · 会话累计
    P1  等待状态行   模型调用期间 \\r 原地重绘动画（思考中… Ns），结束自动擦除
    P2  上下文占用   REPL 提示符显示 estimate_size/CONTEXT_LIMIT 百分比（agent.py 里）

零配置、零依赖。非 TTY（管道/重定向）时动画自动静默——脚本化运行不产生
控制字符垃圾；结算行是真实数据，照常打印。

统计口径：
    主循环（call_llm）     → 轮级 + 会话级（出现在本轮结算行）
    旁路提问（_side_query） → 只记会话级（选记忆/摘要的钱是真实花的，但不进本轮结算）
    子代理（spawn_subagent）→ 只记会话级。它常跑在后台线程（记忆提炼/整理），
                              不挂动画、不计耗时，避免和主线程的显示竞争
"""

from __future__ import annotations

import sys
import threading
import time


def fmt_tokens(n: int) -> str:
    """567 → "567"，1234 → "1,234"，45200 → "45.2k"。"""
    if n >= 10_000:
        return f"{n / 1000:.1f}k"
    return f"{n:,}"


class UsageStats:
    """会话级 + 轮级 token/耗时累计。数据源是 API 响应的 usage 字段。"""

    def __init__(self) -> None:
        self.session_in = 0
        self.session_out = 0
        self.session_calls = 0
        self.reset_turn()

    def reset_turn(self) -> None:
        self.turn_in = 0
        self.turn_out = 0
        self.turn_cached = 0
        self.turn_calls = 0
        self.turn_elapsed = 0.0

    def record(self, usage, elapsed: float, *, to_turn: bool = True) -> None:
        in_tokens = getattr(usage, "input_tokens", 0) or 0
        out_tokens = getattr(usage, "output_tokens", 0) or 0
        cached = (getattr(usage, "cache_read_input_tokens", 0) or 0) + \
                 (getattr(usage, "cache_creation_input_tokens", 0) or 0)
        self.session_in += in_tokens
        self.session_out += out_tokens
        self.session_calls += 1
        if to_turn:
            self.turn_in += in_tokens
            self.turn_out += out_tokens
            self.turn_cached += cached
            self.turn_calls += 1
            self.turn_elapsed += elapsed

    def turn_line(self) -> str:
        """本轮结算行。本轮没有成功的模型调用时返回空串（调用方不打印）。"""
        if not self.turn_calls:
            return ""
        cached = f"（缓存 {fmt_tokens(self.turn_cached)}）" if self.turn_cached else ""
        return (f"↑ {fmt_tokens(self.turn_in)}{cached} "
                f"↓ {fmt_tokens(self.turn_out)}"
                f" · {self.turn_elapsed:.1f}s"
                f" · 会话累计 {fmt_tokens(self.session_in + self.session_out)} tokens")


STATS = UsageStats()


class Spinner:
    """模型等待动画：\\r 原地重绘单行，结束自动擦除。

    只挂在主线程的同步模型调用（call_llm）上；旁路提问和子代理可能跑在
    后台线程，不挂动画。非 TTY 时静默。
    """

    _FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"

    def __init__(self, label: str = "思考中") -> None:
        self.label = label
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._started = 0.0

    def __enter__(self) -> "Spinner":
        if sys.stdout.isatty():
            self._started = time.monotonic()
            self._thread = threading.Thread(target=self._run, daemon=True)
            self._thread.start()
        return self

    def _run(self) -> None:
        i = 0
        while not self._stop.wait(0.1):
            elapsed = time.monotonic() - self._started
            frame = self._FRAMES[i % len(self._FRAMES)]
            sys.stdout.write(
                f"\r\033[90m  {frame} {self.label}… {elapsed:.0f}s\033[0m\033[K")
            sys.stdout.flush()
            i += 1

    def __exit__(self, *exc) -> bool:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=0.3)
            sys.stdout.write("\r\033[K")   # 擦掉状态行
            sys.stdout.flush()
        return False
