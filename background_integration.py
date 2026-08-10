#!/usr/bin/env python3
"""
后台任务集成层 —— 后台子系统对 agent.py 的唯一入口。

    .env: BG_TASKS_ENABLED=1|0   总开关（默认开）
          BG_TIMEOUT=秒          单条后台命令最长运行时间（0=不限，靠 bg_kill）

对标真实 CC（非 s13/s20 教学版）：显式 run_in_background 参数（无关键词启发式）、
完成通知排队进「后续 turn」而非原地等、输出可增量查看/可停止（≈ LocalShellTaskState
的 output/stop/read）。

    bg_bind(decode=)      注入字节解码函数（agent 的 _decode_console），避免反向 import
    bg_enabled()          agent_loop 派发分支用
    bg_tools()            (bg_list/bg_output/bg_kill, handlers)；关闭 → ([], {})
    bg_system_prompt()    system prompt 段；关闭 → ""
    bg_start(command)     起 worker，返回占位 tool_result 文本
    bg_collect()          已完成未通知 → <task_notification> 文本列表（永不抛）
    bg_shutdown()         退出时杀全部运行中任务（不留孤儿子进程）

装配原则和其它子系统一致：**后台子系统坏了不许拖垮 agent**，所有入口 try/except 降级。
"""

from __future__ import annotations

import os
import re
import subprocess
import threading
import time
from pathlib import Path

# ── 常量 ────────────────────────────────────────────────────────────────────
# 每任务原始字节缓冲上限（保尾部，防日志撑爆内存）。bg_output 默认全量返回，
# 上限放宽到 2MB；可用 BG_OUTPUT_CAP 环境变量覆盖。超出仍保尾弃头，
# trimmed 标记会在输出头部提示。
OUTPUT_CAP_BYTES = max(50_000, int(float(os.getenv("BG_OUTPUT_CAP") or "2000000")))
NOTIFY_TAIL_CHARS = max(200, int(float(os.getenv("BG_NOTIFY_TAIL") or "500")))  # 完成通知里携带的输出尾部字符数
DEFAULT_OUTPUT_TAIL = 0      # bg_output 默认返回全量缓冲（0 = 不截断）
_READ_CHUNK = 4096
LOG_CAP_BYTES = 10_000_000   # 单个日志文件上限：超限停写并留标记（文件不做保尾重写，内存缓冲仍保尾，二者互补）
MAX_LOG_FILES = 30           # 日志目录数量上限：起新任务时按 mtime 删最旧

_CWD = os.getcwd()           # 等价 agent.WORKDIR（进程启动目录），避免反向 import
_LOG_DIR = Path(_CWD) / ".tmp" / "bg"   # 任务日志实时落盘处：进程重启后查进度的唯一持久凭证

# ── 全局状态：单锁保护 ──────────────────────────────────────────────────────
# _TASKS: id → {id, command, status, pid, started, ended, exit_code,
#               notified, trimmed, buf(bytearray), error}
# status ∈ running / completed / failed / timeout / killed
# 终态只能从 running 转入；killed/timeout 显式设置后 worker 不再覆盖。
_LOCK = threading.Lock()
_TASKS: dict[str, dict] = {}
_COUNTER = 0

# 解码函数由 agent 通过 bg_bind 注入（默认 utf-8/replace，离线可独立 import）
_decode = lambda raw: raw.decode("utf-8", errors="replace")


# 完成通知里 output 摘要过长时，同样落盘只留头尾预览（与 agent 的
# persist_large_output 同款，这里自带一份避免反向 import agent.py）。
# 落盘目录与 agent 共用 .task_outputs/，由 agent 的 prepare_context 统一清理。
# 阈值保持与 agent.py 的 PERSIST_THRESHOLD / PERSIST_PREVIEW 一致，改动时两边同改。
_PERSIST_THRESHOLD = 5000
_PERSIST_PREVIEW = 500
_RESULT_DIR = Path(_CWD) / ".task_outputs"


def bg_reload_notify_tail() -> None:
    """重读 BG_NOTIFY_TAIL。agent.py 在 load_dotenv 之后调用——
    本模块 import 时 .env 尚未加载，模块级常量只吃得到 shell 环境变量。"""
    global NOTIFY_TAIL_CHARS
    try:
        NOTIFY_TAIL_CHARS = max(200, int(float(os.getenv("BG_NOTIFY_TAIL") or "500")))
    except (TypeError, ValueError):
        pass


def _persist_output(output: str, name: str) -> str:
    """超长 output → 落盘 .task_outputs/，返回头尾预览 + 路径；失败回退原文。"""
    if len(output) <= _PERSIST_THRESHOLD:
        return output
    try:
        _RESULT_DIR.mkdir(parents=True, exist_ok=True)
        path = _RESULT_DIR / f"{name}-{int(time.time())}.txt"
        tmp = path.with_suffix(".tmp")
        tmp.write_text(output, encoding="utf-8")
        os.replace(tmp, path)
    except Exception:
        return output
    head, tail = output[:_PERSIST_PREVIEW], output[-_PERSIST_PREVIEW:]
    return (f"<persisted-output>\nFull output: {path} "
            f"({len(output)} chars) — read_file to see it all.\n"
            f"Preview (head {_PERSIST_PREVIEW} + tail {_PERSIST_PREVIEW} chars):\n"
            f"{head}\n…[中间省略 {len(output) - 2 * _PERSIST_PREVIEW} 字符]…\n"
            f"{tail}\n</persisted-output>")


def bg_reload_cap() -> None:
    """重读 BG_OUTPUT_CAP。agent.py 在 load_dotenv 之后调用——
    本模块 import 时 .env 尚未加载，常量只吃得到 shell 环境变量。"""
    global OUTPUT_CAP_BYTES
    try:
        OUTPUT_CAP_BYTES = max(50_000,
                               int(float(os.getenv("BG_OUTPUT_CAP") or "2000000")))
    except ValueError:
        pass


def bg_bind(*, decode) -> None:
    """注入字节→文本解码函数（agent 的 _decode_console）。bind 模式避免反向 import。"""
    global _decode
    _decode = decode


def bg_enabled() -> bool:
    try:
        return (os.getenv("BG_TASKS_ENABLED") or "1").strip().lower() \
            not in ("0", "off", "false", "no", "none")
    except Exception:
        return False


def _bg_timeout_seconds() -> float | None:
    """BG_TIMEOUT 秒数；0/未设/非法 → None（不限时，靠 bg_kill）。"""
    try:
        v = float(os.getenv("BG_TIMEOUT") or "0")
        return v if v > 0 else None
    except (TypeError, ValueError):
        return None


# ═══════════════════════════════════════════════════════════════════════════
#  进程树终止 —— Windows 用 taskkill /T，POSIX 用 killpg
# ═══════════════════════════════════════════════════════════════════════════

def _kill_tree(pid: int | None) -> None:
    """杀掉整个进程树。shell=True 会套一层 cmd/sh，必须 /T 或 killpg 才杀得干净。"""
    if not pid:
        return
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)],
                           capture_output=True, timeout=10)
        else:
            import signal
            try:
                os.killpg(os.getpgid(pid), signal.SIGKILL)
            except Exception:
                try:
                    os.kill(pid, signal.SIGKILL)
                except Exception:
                    pass
    except Exception:
        pass


# ═══════════════════════════════════════════════════════════════════════════
#  日志落盘 —— 实时写 .tmp/bg/，进程重启后仍可查进度
# ═══════════════════════════════════════════════════════════════════════════

def _safe_log_name(task_id: str, command: str) -> str:
    """日志文件名带命令语义——让用户只有 ID 也能认出是哪个进程。"""
    frag = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", command.strip())
    frag = re.sub(r"\s+", "_", frag)[:40].strip("_") or "task"
    return f"{task_id}_{frag}.log"


def _cleanup_old_logs() -> None:
    """日志文件超上限时删最旧（按 mtime）。吞异常：清理失败不影响起任务。"""
    try:
        logs = sorted(_LOG_DIR.glob("*.log"), key=lambda p: p.stat().st_mtime)
        for old in logs[:max(0, len(logs) - MAX_LOG_FILES)]:
            try:
                old.unlink()
            except Exception:
                pass
    except Exception:
        pass


def _fmt_elapsed(seconds: float) -> str:
    s = max(0, int(seconds))
    return f"{s // 60}m{s % 60:02d}s" if s >= 60 else f"{s}s"


# ═══════════════════════════════════════════════════════════════════════════
#  执行：worker 等进程退出定终态，drain 持续抽干输出（两线程，均 daemon）
# ═══════════════════════════════════════════════════════════════════════════

def _drain(task: dict, proc) -> None:
    """持续读输出：进内存缓冲（保尾部）+ 实时落盘日志文件。
    stdout/stderr 已合流，单管道无死锁。写盘失败只吞掉，不阻塞读取。"""
    log_file, log_written = None, 0
    try:
        if task.get("log_path"):
            log_file = open(task["log_path"], "ab")
            log_written = log_file.tell()
    except Exception:
        log_file = None
    try:
        stream = proc.stdout
        while True:
            # read1：一次底层读、有多少返回多少 → bg_output 能看到实时增量
            chunk = stream.read1(_READ_CHUNK)
            if not chunk:
                break
            if log_file is not None and not task.get("log_truncated"):
                try:
                    if log_written + len(chunk) <= LOG_CAP_BYTES:
                        log_file.write(chunk)
                        log_file.flush()
                        log_written += len(chunk)
                    else:
                        log_file.write(b"\n[log truncated]\n")
                        log_file.flush()
                        with _LOCK:
                            task["log_truncated"] = True
                except Exception:
                    pass
            with _LOCK:
                task["buf"] += chunk
                overflow = len(task["buf"]) - OUTPUT_CAP_BYTES
                if overflow > 0:
                    del task["buf"][:overflow]
                    task["trimmed"] = True
    except Exception:
        pass
    finally:
        if log_file is not None:
            try:
                log_file.close()
            except Exception:
                pass


def _worker(task: dict) -> None:
    task_id = task["id"]
    proc = None
    try:
        kwargs = dict(shell=True, cwd=_CWD,
                      stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        if os.name == "nt":
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
        else:
            kwargs["start_new_session"] = True
        proc = subprocess.Popen(task["command"], **kwargs)
    except Exception as e:
        with _LOCK:
            task["status"] = "failed"
            task["ended"] = time.time()
            task["error"] = str(e)
        print(f"  \033[31m[bg] {task_id} failed to start: {e}\033[0m")
        return

    # 置 pid；若 bg_kill 抢在 Popen 前已置 killed，立刻杀掉刚起的进程
    with _LOCK:
        if task["status"] != "running":
            preempted = proc.pid
        else:
            task["pid"] = proc.pid
            preempted = None
    if preempted:
        _kill_tree(preempted)
        try:
            proc.wait(timeout=5)
        except Exception:
            pass
        return

    drain = threading.Thread(target=_drain, args=(task, proc), daemon=True)
    drain.start()

    timed_out = False
    try:
        proc.wait(timeout=_bg_timeout_seconds())   # None = 一直等
    except subprocess.TimeoutExpired:
        timed_out = True
        _kill_tree(proc.pid)
        try:
            proc.wait(timeout=5)
        except Exception:
            pass

    drain.join(timeout=2)

    # 终态只从 running 转；bg_kill/bg_timeout 已显式置位则不覆盖
    with _LOCK:
        if task["status"] == "running":
            task["ended"] = time.time()
            if timed_out:
                task["status"] = "timeout"
            elif proc.returncode == 0:
                task["status"] = "completed"
                task["exit_code"] = 0
            else:
                task["status"] = "failed"
                task["exit_code"] = proc.returncode
        status = task["status"]
        exit_code = task.get("exit_code")
    tail = f" (exit {exit_code})" if exit_code is not None else ""
    print(f"  \033[32m[bg] {task_id} {status}{tail}\033[0m")


# ═══════════════════════════════════════════════════════════════════════════
#  对外入口：start / collect / shutdown
# ═══════════════════════════════════════════════════════════════════════════

def bg_start(command: str = "") -> str:
    """起后台任务，立即返回占位文本（模型据此先干别的）。"""
    global _COUNTER
    command = (command or "").strip()
    if not command:
        return "Error: empty command."
    try:
        _LOG_DIR.mkdir(parents=True, exist_ok=True)
        _cleanup_old_logs()
        with _LOCK:
            _COUNTER += 1
            task_id = f"bg_{_COUNTER:04d}"
            log_path = _LOG_DIR / _safe_log_name(task_id, command)
            _TASKS[task_id] = {
                "id": task_id, "command": command, "status": "running",
                "pid": None, "started": time.time(), "ended": None,
                "exit_code": None, "notified": False, "trimmed": False,
                "buf": bytearray(), "error": None,
                "log_path": str(log_path), "log_truncated": False,
            }
        threading.Thread(target=_worker, args=(_TASKS[task_id],),
                         daemon=True).start()
        return (f"[Background task {task_id} started] Running detached: "
                f"{command}. Continue with other work — a <task_notification> "
                f"arrives when it finishes. Live log: {log_path}. "
                f"Use bg_output(task_id=\"{task_id}\") to peek at live output, "
                f"bg_kill(task_id=\"{task_id}\") to stop it.")
    except Exception as e:
        return f"Error: could not start background task: {e}"


def _status_text(s: dict) -> str:
    status = s["status"]
    if status == "completed":
        return "completed (exit code 0)"
    if status == "failed":
        code = s.get("exit_code")
        if code is None:
            return f"failed to start ({s.get('error') or 'unknown'})"
        return f"failed (exit code {code})"
    if status == "timeout":
        dur = int((s.get("ended") or time.time()) - s["started"])
        return f"timeout after {dur}s"
    return status   # killed


def _format_notification(s: dict) -> str:
    output = s["output"].strip()
    summary = output[-NOTIFY_TAIL_CHARS:] if output else "(no output)"
    log = s.get("log_path") or ""
    log_line = f"\n  <log>{log}</log>" if log else ""
    xml = ("<task_notification>\n"
           f"  <task_id>{s['id']}</task_id>\n"
           f"  <status>{_status_text(s)}</status>\n"
           f"  <command>{s['command']}</command>{log_line}\n"
           f"  <summary>{summary}</summary>\n"
           "</task_notification>")
    if len(output) > NOTIFY_TAIL_CHARS:
        xml += (f"\n(use bg_output(task_id=\"{s['id']}\") for the full output)")
    return xml


def bg_collect() -> list[str]:
    """弹出「已完成且未通知」的任务 → <task_notification> 文本列表。永不抛。"""
    notifications = []
    try:
        with _LOCK:
            done = [t for t in _TASKS.values()
                    if t["status"] != "running" and not t["notified"]]
            snapshots = []
            for t in done:
                t["notified"] = True
                snapshots.append({
                    "id": t["id"], "status": t["status"],
                    "command": t["command"], "exit_code": t.get("exit_code"),
                    "started": t["started"], "ended": t.get("ended"),
                    "error": t.get("error"), "log_path": t.get("log_path"),
                    "output": _decode(bytes(t["buf"])),
                })
        for s in snapshots:
            # 通知内容同样过一道大输出落盘：完成通知是发给模型的（进上下文），
            # 尾部 500 字的 output 摘要被 persist 收窄成头尾预览 + 全文路径
            s["output"] = _persist_output(s["output"], f"bg_{s['id']}")
            notifications.append(_format_notification(s))
    except Exception:
        return []
    return notifications


def bg_shutdown() -> None:
    """退出时杀掉所有运行中的后台任务，不留孤儿子进程。吞异常。"""
    try:
        with _LOCK:
            running = [(t["id"], t["pid"]) for t in _TASKS.values()
                       if t["status"] == "running"]
            for tid, _ in running:
                _TASKS[tid]["status"] = "killed"
                _TASKS[tid]["ended"] = time.time()
        for tid, pid in running:
            _kill_tree(pid)
            print(f"  \033[33m[bg] killed {tid} on exit\033[0m")
    except Exception:
        pass


# ═══════════════════════════════════════════════════════════════════════════
#  三个工具：bg_list / bg_output / bg_kill
# ═══════════════════════════════════════════════════════════════════════════

def _run_bg_list() -> str:
    try:
        with _LOCK:
            if not _TASKS:
                return "No background tasks."
            now = time.time()
            lines = []
            for t in sorted(_TASKS.values(), key=lambda x: x["id"]):
                elapsed = (t.get("ended") or now) - t["started"]
                log = t.get("log_path") or ""
                lines.append(f"{t['id']} [{t['status']}] {elapsed:.0f}s "
                             f"{t['command'][:60]}"
                             + (f" · log: {log}" if log else ""))
        return "\n".join(lines)
    except Exception as e:
        return f"Error: {e}"


def _run_bg_output(task_id: str = "", tail: int = DEFAULT_OUTPUT_TAIL) -> str:
    try:
        task_id = (task_id or "").strip()
        with _LOCK:
            t = _TASKS.get(task_id)
            if t is None:
                return (f"Error: no such task: '{task_id}'. "
                        "Use bg_list to see tasks.")
            status = t["status"]
            exit_code = t.get("exit_code")
            started = t["started"]
            ended = t.get("ended")
            trimmed = t["trimmed"]
            raw = bytes(t["buf"])          # 锁内拷贝，锁外解码
        try:
            tail = int(tail)
        except (TypeError, ValueError):
            tail = DEFAULT_OUTPUT_TAIL
        text = _decode(raw)
        elapsed = (ended or time.time()) - started
        header = f"[{task_id}] {status}"
        if exit_code is not None:
            header += f" (exit {exit_code})"
        header += f" · elapsed {elapsed:.0f}s"
        if status == "running":
            header += " · (still running)"
        if trimmed:
            header += f" · [output trimmed to last {OUTPUT_CAP_BYTES // 1024}KB]"
        # tail ≤ 0 = 全量返回（默认）；显式正数才按尾部截断（最少 200 字符）。
        # 注意：全量输出会整体进模型上下文，超长日志由 CONTEXT_LIMIT 压缩管线
        # 与任务预算门兜底。
        shown = text if tail <= 0 else text[-max(200, tail):]
        return header + "\n" + (shown if shown.strip() else "(no output)")
    except Exception as e:
        return f"Error: {e}"


def _run_bg_kill(task_id: str = "") -> str:
    try:
        task_id = (task_id or "").strip()
        with _LOCK:
            t = _TASKS.get(task_id)
            if t is None:
                return (f"Error: no such task: '{task_id}'. "
                        "Use bg_list to see tasks.")
            if t["status"] != "running":
                return f"Task {task_id} already finished ({t['status']})."
            pid = t["pid"]
            t["status"] = "killed"
            t["ended"] = time.time()
        _kill_tree(pid)                    # pid 可能为 None（未及 Popen），_kill_tree 自处理
        return f"Killed {task_id}."
    except Exception as e:
        return f"Error: {e}"


_BG_LIST = {"name": "bg_list",
            "description": "List background tasks: id, status, elapsed, command.",
            "input_schema": {"type": "object", "properties": {}, "required": []}}
_BG_OUTPUT = {"name": "bg_output",
              "description": "Read output of a background task — the FULL "
                             "buffered output by default (works while it is "
                             "still running). Pass tail>0 to limit to the "
                             "last N chars.",
              "input_schema": {"type": "object",
                               "properties": {"task_id": {"type": "string"},
                                              "tail": {"type": "integer",
                                                       "description": "omit or "
                                                       "0 = full output; "
                                                       "positive = chars of "
                                                       "tail to return"}},
                               "required": ["task_id"]}}
_BG_KILL = {"name": "bg_kill",
            "description": "Stop a running background task (kills the whole "
                           "process tree).",
            "input_schema": {"type": "object",
                             "properties": {"task_id": {"type": "string"}},
                             "required": ["task_id"]}}


def bg_tools() -> tuple[list[dict], dict]:
    """返回 (tools, handlers)，形状同 memory_tools()。关闭 → ([], {})。"""
    if not bg_enabled():
        return [], {}
    return ([_BG_LIST, _BG_OUTPUT, _BG_KILL],
            {"bg_list": _run_bg_list, "bg_output": _run_bg_output,
             "bg_kill": _run_bg_kill})


def bg_system_prompt() -> str:
    if not bg_enabled():
        return ""
    return (
        "Background bash: the `bash` tool accepts run_in_background=true for "
        "genuinely long commands (installs, builds, long test runs). Decide at "
        "launch:\n"
        "- If the task does NOT block other work: leave yields_turn unset and "
        "CONTINUE with other tasks. NEVER busy-wait (no sleep/bg_output polling "
        "loops) — the <task_notification> arrives on its own.\n"
        "- If later work DEPENDS on its result: set yields_turn=true AND put a "
        "short progress summary in the text block of the SAME response (text can "
        "coexist with tool_use). The harness then ends the turn immediately, "
        "hands control back to the user, and the notification is delivered with "
        "the user's next input.\n"
        "bg_list shows all tasks, bg_output(task_id) reads live output (use it "
        "only when the user asks to check, never as a waiting mechanism), "
        "bg_kill(task_id) stops one. Every task streams its log to .tmp/bg/ in "
        "real time — after a restart, read that file to recover progress. Do "
        "NOT background quick commands (under ~30s) — foreground returns faster.")


# ═══════════════════════════════════════════════════════════════════════════
#  REPL /bg 命令 + 交接状态卡 —— 面向「人」的任务管理入口
# ═══════════════════════════════════════════════════════════════════════════

def _ordered_tasks() -> list[dict]:
    """按启动顺序返回任务引用（dict 插入序）。列表与 kill 共用，编号才一致。"""
    with _LOCK:
        return list(_TASKS.values())


def _resolve_task(ref: str):
    """编号（/bg 列表里的序号）或 id 解析成任务；找不到返回 None。"""
    ref = (ref or "").strip()
    tasks = _ordered_tasks()
    if ref.isdigit() and 1 <= int(ref) <= len(tasks):
        return tasks[int(ref) - 1]
    with _LOCK:
        return _TASKS.get(ref)


def _last_output_line(t: dict) -> str:
    """任务输出的最后一行（最新进展）。缓冲空时兜底读日志文件尾部。"""
    try:
        text = _decode(bytes(t["buf"])).strip()
    except Exception:
        text = ""
    if not text:
        lp = t.get("log_path")
        if lp and os.path.exists(lp):
            try:
                with open(lp, "rb") as f:
                    text = _decode(f.read()[-8192:]).strip()
            except Exception:
                text = ""
    return text.splitlines()[-1] if text else "(no output)"


def bg_status_card() -> str:
    """当前运行中任务的状态卡（run_turn 让出控制权时的交接卡用）。无则空串。"""
    try:
        with _LOCK:
            running = [dict(t) for t in _TASKS.values()
                       if t["status"] == "running"]
        if not running:
            return ""
        lines = []
        for t in running:
            elapsed = _fmt_elapsed(time.time() - t["started"])
            line = f"  · {t['id']}（{elapsed}）{t['command']}"
            lp = t.get("log_path") or ""
            if lp:
                # 无条件显示日志路径：交接卡打印时文件可能还没被 drain 线程创建
                size = (f"（{os.path.getsize(lp)} B）" if os.path.exists(lp)
                        else "（写入中）")
                line += f"\n    日志: {lp} {size}"
            lines.append(line)
        return "\n".join(lines)
    except Exception:
        return ""


def _slash_list() -> None:
    tasks = _ordered_tasks()
    if not tasks:
        print("  \033[90m还没有后台任务（bash 工具 run_in_background=true 启动）\033[0m")
        return
    running = sum(1 for t in tasks if t["status"] == "running")
    print(f"  后台任务（{running} 运行中 · {len(tasks) - running} 已结束）：")
    for i, t in enumerate(tasks, 1):
        elapsed = _fmt_elapsed((t.get("ended") or time.time()) - t["started"])
        exit_tail = (f" (exit {t['exit_code']})"
                     if t.get("exit_code") is not None else "")
        print(f"  \033[36m{i:>2}.\033[0m {t['id']}  "
              f"\033[33m{t['status']:<9}\033[0m {elapsed:>6}  "
              f"{t['command']}{exit_tail}")
        lp = t.get("log_path") or ""
        if lp:
            size = os.path.getsize(lp) if os.path.exists(lp) else 0
            print(f"      └ 日志: {lp}（{size} B）")
        print(f"        最新: {_last_output_line(t)[:120]}")
    print("  \033[90m命令: /bg log <编号|id> [行数] 查看输出 · "
          "/bg kill <编号|id> 终止 · /bg kill all 终止全部\033[0m")


def bg_handle_slash(cmd: str) -> bool:
    """REPL 的 /bg 命令族：列表 / 看日志 / 手动终止。返回 True = 已处理。

    编号 = /bg 列表里的序号（与列表同一排序函数，稳定）；id 永远可用。
    """
    if not bg_enabled():
        print("  \033[90m后台任务已禁用（BG_TASKS_ENABLED=0）\033[0m")
        return True
    parts = cmd.split()
    sub = parts[1].lower() if len(parts) > 1 else ""

    if not sub:
        _slash_list()
        return True

    if sub == "log":
        if len(parts) < 3:
            print("  \033[90m用法: /bg log <编号|id> [行数]\033[0m")
            return True
        t = _resolve_task(parts[2])
        if t is None:
            print(f"  \033[90m没有这个任务: {parts[2]}（/bg 查看列表）\033[0m")
            return True
        n = 20
        if len(parts) >= 4 and parts[3].isdigit():
            n = max(1, int(parts[3]))
        lp = t.get("log_path") or ""
        if not lp or not os.path.exists(lp):
            print(f"  \033[90m{t['id']} 还没有日志文件\033[0m")
            return True
        try:
            with open(lp, "rb") as f:
                lines = _decode(f.read()).splitlines()
        except Exception as e:
            print(f"  \033[90m读日志失败: {e}\033[0m")
            return True
        print(f"  ── {t['id']} 日志尾部 {n} 行（{t['status']}）──")
        for line in lines[-n:]:
            print(f"  {line}")
        return True

    if sub == "kill":
        if len(parts) < 3:
            print("  \033[90m用法: /bg kill <编号|id> · /bg kill all\033[0m")
            return True
        ref = parts[2]
        if ref.lower() == "all":
            with _LOCK:
                running = [t["id"] for t in _TASKS.values()
                           if t["status"] == "running"]
            if not running:
                print("  \033[90m没有运行中的后台任务\033[0m")
                return True
            try:
                ans = input(f"  确认终止全部 {len(running)} 个运行中任务? [y/N] ")
            except (EOFError, KeyboardInterrupt):
                ans = ""
            if ans.strip().lower() not in ("y", "yes", "是"):
                print("  \033[90m已取消\033[0m")
                return True
            for tid in running:
                print("  " + _run_bg_kill(tid))
            return True
        t = _resolve_task(ref)
        if t is None:
            print(f"  \033[90m没有这个任务: {ref}（/bg 查看列表）\033[0m")
            return True
        print("  " + _run_bg_kill(t["id"]))
        return True

    print("  \033[90m未知 /bg 子命令，可用: /bg · /bg log <编号|id> [行数] · "
          "/bg kill <编号|id> · /bg kill all\033[0m")
    return True
