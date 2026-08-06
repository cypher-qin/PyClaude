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
import subprocess
import threading
import time

# ── 常量 ────────────────────────────────────────────────────────────────────
# 每任务原始字节缓冲上限（保尾部，防日志撑爆内存）。bg_output 默认全量返回，
# 上限放宽到 2MB；可用 BG_OUTPUT_CAP 环境变量覆盖。超出仍保尾弃头，
# trimmed 标记会在输出头部提示。
OUTPUT_CAP_BYTES = max(50_000, int(float(os.getenv("BG_OUTPUT_CAP") or "2000000")))
NOTIFY_TAIL_CHARS = 500      # 完成通知里携带的输出尾部字符数
DEFAULT_OUTPUT_TAIL = 0      # bg_output 默认返回全量缓冲（0 = 不截断）
_READ_CHUNK = 4096

_CWD = os.getcwd()           # 等价 agent.WORKDIR（进程启动目录），避免反向 import

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
#  执行：worker 等进程退出定终态，drain 持续抽干输出（两线程，均 daemon）
# ═══════════════════════════════════════════════════════════════════════════

def _drain(task: dict, proc) -> None:
    """持续读输出进缓冲（保尾部）。stdout/stderr 已合流，单管道无死锁。"""
    try:
        stream = proc.stdout
        while True:
            # read1：一次底层读、有多少返回多少 → bg_output 能看到实时增量
            chunk = stream.read1(_READ_CHUNK)
            if not chunk:
                break
            with _LOCK:
                task["buf"] += chunk
                overflow = len(task["buf"]) - OUTPUT_CAP_BYTES
                if overflow > 0:
                    del task["buf"][:overflow]
                    task["trimmed"] = True
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
        with _LOCK:
            _COUNTER += 1
            task_id = f"bg_{_COUNTER:04d}"
            _TASKS[task_id] = {
                "id": task_id, "command": command, "status": "running",
                "pid": None, "started": time.time(), "ended": None,
                "exit_code": None, "notified": False, "trimmed": False,
                "buf": bytearray(), "error": None,
            }
        threading.Thread(target=_worker, args=(_TASKS[task_id],),
                         daemon=True).start()
        return (f"[Background task {task_id} started] Running detached: "
                f"{command}. Continue with other work — a <task_notification> "
                f"arrives when it finishes. Use bg_output(task_id=\"{task_id}\") "
                f"to peek at live output, bg_kill(task_id=\"{task_id}\") to stop it.")
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
    xml = ("<task_notification>\n"
           f"  <task_id>{s['id']}</task_id>\n"
           f"  <status>{_status_text(s)}</status>\n"
           f"  <command>{s['command']}</command>\n"
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
                    "error": t.get("error"),
                    "output": _decode(bytes(t["buf"])),
                })
        for s in snapshots:
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
                lines.append(f"{t['id']} [{t['status']}] {elapsed:.0f}s "
                             f"{t['command'][:60]}")
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
        "genuinely long commands (installs, builds, long test runs). You get a "
        "placeholder with a task id immediately and keep working; a "
        "<task_notification> is injected into the conversation when the command "
        "finishes. bg_list shows all tasks, bg_output(task_id) reads live output, "
        "bg_kill(task_id) stops one. Do NOT background quick commands (under ~30s) "
        "— foreground returns faster.")
