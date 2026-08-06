#!/usr/bin/env python3
"""
status_report —— 运行状态报告：等待动画 + token 结算 + 上下文占用提示。

调研结论的落地（参照 Claude Code / Codex CLI / aider 的实现）：

    P0  每轮结算行   读每次 API 响应的 usage 字段，轮末打印 ↑输入 ↓输出 · 耗时 · 会话累计
    P1  等待状态行   模型调用期间 \\r 原地重绘动画（思考中… Ns），结束自动擦除
    P2  上下文占用   REPL 提示符显示 estimate_size/CONTEXT_LIMIT 百分比（agent.py 里）
    P3  每次调用实时报告 + 任务预算   record() 时打印单次用量与累计/预算占比；
        累计逼近预算（≥90%）由 agent.py 的预算门拦截并询问用户是否继续

零配置、零依赖。非 TTY（管道/重定向）时动画自动静默——脚本化运行不产生
控制字符垃圾；结算行是真实数据，照常打印。

统计口径：
    主循环（call_llm）     → 轮级 + 会话级（出现在本轮结算行）
    旁路提问（_side_query） → 只记会话级（选记忆/摘要的钱是真实花的，但不进本轮结算）
    子代理（spawn_subagent）→ 只记会话级。它常跑在后台线程（记忆提炼/整理），
                              不挂动画、不计耗时，避免和主线程的显示竞争

线程安全说明：record() 可能被后台线程调用（记忆预取/Stop 钩子的 _side_query）。
计数自增依赖 GIL 够用；实时报告行只有主线程直接打印，后台线程的报告暂存
_pending_reports，由主线程在提示符渲染前 flush_pending() 冲刷，避免与输入行撕扯。
"""

from __future__ import annotations

import os
import sys
import threading
import time

# 单次任务 token 预算（累计 输入+输出）。达到 90% 时由 agent.py 预算门暂停询问。
# 设为 0 = 关闭预算门。可在 .env 用 TASK_TOKEN_BUDGET 覆盖。
TASK_TOKEN_BUDGET = max(0, int(float(os.getenv("TASK_TOKEN_BUDGET") or "200000")))
BUDGET_WARN_RATIO = 0.9


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
        self.session_cached = 0
        self.session_calls = 0
        self.session_elapsed = 0.0
        # 预算：granted = 当前已获授的总额度（用户每确认"继续"追加一个 quota）
        self.budget_quota = TASK_TOKEN_BUDGET
        self.budget_granted = TASK_TOKEN_BUDGET
        self._pending_reports: list[str] = []   # 后台线程产生的待冲刷报告行
        self.reset_turn()

    def reset_turn(self) -> None:
        self.turn_in = 0
        self.turn_out = 0
        self.turn_cached = 0
        self.turn_calls = 0
        self.turn_elapsed = 0.0

    # ── 预算 ──────────────────────────────────────────────────────────────
    def session_total(self) -> int:
        return self.session_in + self.session_out

    def budget_pct(self) -> int:
        """累计用量占当前授权额度的百分比；额度关闭（≤0）时返回 0。"""
        if self.budget_granted <= 0:
            return 0
        return round(100 * self.session_total() / self.budget_granted)

    def budget_exceeded(self) -> bool:
        """是否逼近/超出当前授权额度（≥90%）。额度关闭时恒 False。"""
        return (self.budget_granted > 0
                and self.session_total() >= BUDGET_WARN_RATIO * self.budget_granted)

    def extend_budget(self) -> int:
        """用户确认继续后追加一个等额预算档，返回新的总额度。"""
        self.budget_granted += self.budget_quota
        return self.budget_granted

    def snapshot_tokens(self) -> dict:
        """供 session 存档 meta 持久化（跨 resume 的任务预算才是真预算）。"""
        return {"in": self.session_in, "out": self.session_out,
                "calls": self.session_calls, "granted": self.budget_granted}

    def restore(self, d: dict | None) -> None:
        """从 session 存档 meta 恢复累计与授权额度（resume 时调用）。"""
        if not isinstance(d, dict):
            return
        self.session_in = int(d.get("in") or 0)
        self.session_out = int(d.get("out") or 0)
        self.session_calls = int(d.get("calls") or 0)
        granted = int(d.get("granted") or 0)
        if granted > 0:
            self.budget_granted = granted

    # ── 入账 + 实时报告 ────────────────────────────────────────────────────
    def record(self, usage, elapsed: float, *, to_turn: bool = True) -> None:
        in_tokens = getattr(usage, "input_tokens", 0) or 0
        out_tokens = getattr(usage, "output_tokens", 0) or 0
        cached = (getattr(usage, "cache_read_input_tokens", 0) or 0) + \
                 (getattr(usage, "cache_creation_input_tokens", 0) or 0)
        self.session_in += in_tokens
        self.session_out += out_tokens
        self.session_cached += cached
        self.session_calls += 1
        self.session_elapsed += elapsed
        if to_turn:
            self.turn_in += in_tokens
            self.turn_out += out_tokens
            self.turn_cached += cached
            self.turn_calls += 1
            self.turn_elapsed += elapsed
        self._emit_report(in_tokens, out_tokens, elapsed, to_turn)

    def _emit_report(self, in_tokens: int, out_tokens: int,
                     elapsed: float, to_turn: bool) -> None:
        """每次 API 调用后的一行实时报告。主线程直印，后台线程暂存待冲刷。"""
        tag = "[token]" if to_turn else "[token·旁路]"
        budget = (f" · 预算 {self.budget_pct()}%"
                  if self.budget_granted > 0 else "")
        line = (f"\033[90m{tag} #{self.session_calls} "
                f"↑{fmt_tokens(in_tokens)} ↓{fmt_tokens(out_tokens)}"
                f" · {elapsed:.1f}s"
                f" · 累计 {fmt_tokens(self.session_total())}"
                f"/{fmt_tokens(self.budget_granted)}{budget}\033[0m")
        if threading.current_thread() is threading.main_thread():
            self.flush_pending()
            print(line)
        else:
            self._pending_reports.append(line)

    def flush_pending(self) -> None:
        """把后台线程攒下的报告行冲刷到控制台（主线程调用）。"""
        if self._pending_reports:
            print("\n".join(self._pending_reports))
            self._pending_reports.clear()

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


def reload_budget_config() -> None:
    """重读 TASK_TOKEN_BUDGET。agent.py 在 load_dotenv 之后调用——
    本模块 import 时 .env 尚未加载，模块级常量只吃得到 shell 环境变量。"""
    try:
        quota = max(0, int(float(os.getenv("TASK_TOKEN_BUDGET") or "200000")))
    except ValueError:
        quota = 200_000
    STATS.budget_quota = quota
    STATS.budget_granted = quota


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
