#!/usr/bin/env python3
"""
任务存储层 —— 对标 CC 的 utils/tasks.ts：只管 CRUD、依赖图、并发锁，
不含任何 agent/模型逻辑。装配层（task_integration.py）把它包装成工具。

与教学版（s12 / s20）的差异，逐条对齐 CC 真实实现：

    完整 TaskRecord    9 字段：activeForm / blocks / metadata 都补上
    顺序整数 ID        .highwatermark 高水位标，任务删了 ID 也不重用
    双重锁             任务文件锁（{id}.lock，重试+指数退避，锁内重读防 TOCTOU）
                       + 列表级锁（.lock，分配 ID / 全表扫描时原子执行）
    claim/status 分离  claim_task 只解决 owner 竞争；状态流转走 update_task
    release 路径       in_progress → pending 自动清 owner，可被重新认领
    双向依赖           设 blockedBy 时同步对方的 blocks；加边前做环检测
    原子写盘           临时文件 + os.replace，崩溃不留半个 JSON

刻意不做（单进程独占 .tasks/ 时用不上，留 `notify_change` 回调点）：
    fs.watch 反应式监听（CC 的 useTaskListWatcher 是给多 agent 团队用的）

锁的实现：O_CREAT|O_EXCL 创建锁文件，原子性跨平台（proper-lockfile 用 mkdir，
同理）。缺点：进程在持锁期间被强杀会留下锁文件——单人单进程场景接受，
注释在此，真遇到手动删 .tasks/*.lock 即可。
"""

from __future__ import annotations

import json
import os
import random
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

# ── 路径 ──
TASKS_DIR = Path.cwd() / ".tasks"
HIGHWATERMARK = TASKS_DIR / ".highwatermark"
LIST_LOCK = TASKS_DIR / ".lock"

# ── 锁参数（对标 proper-lockfile：指数退避 + 重试上限）──
LOCK_RETRIES = 30
LOCK_BASE_DELAY = 0.005     # 5ms
LOCK_MAX_DELAY = 0.1        # 100ms

STATUSES = ("pending", "in_progress", "completed")


# ═══════════════════════════════════════════════════════════════════════════
#  TaskRecord —— 与 CC 的 9 字段一致
# ═══════════════════════════════════════════════════════════════════════════

@dataclass
class Task:
    id: str                              # 顺序整数（字符串形式）
    subject: str                         # 简短标题
    description: str = ""                # 自由格式描述
    activeForm: str = ""                 # 进行时态，in_progress 时给 spinner 用
    owner: str | None = None             # 认领者（agent 名）
    status: str = "pending"              # pending | in_progress | completed
    blocks: list[str] = field(default_factory=list)       # 下游：被本任务阻塞
    blockedBy: list[str] = field(default_factory=list)    # 上游：阻塞本任务
    metadata: dict = field(default_factory=dict)          # 任意扩展键值


class TaskError(Exception):
    """存储层错误：not_found / already_claimed / already_resolved /
    blocked / cycle / bad_transition / lock_timeout。code 给装配层翻译成文案。"""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


# ═══════════════════════════════════════════════════════════════════════════
#  文件锁 —— O_EXCL 创建锁文件，退避重试
# ═══════════════════════════════════════════════════════════════════════════

class _FileLock:
    """with _FileLock(path): ... —— 进入时抢锁，退出时释放。

    锁内约定：先重读、再校验、最后写（防 TOCTOU）。
    """

    def __init__(self, lock_path: Path):
        self.lock_path = lock_path

    def __enter__(self):
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        delay = LOCK_BASE_DELAY
        for _ in range(LOCK_RETRIES):
            try:
                fd = os.open(str(self.lock_path),
                             os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.close(fd)
                return self
            except FileExistsError:
                time.sleep(delay + random.uniform(0, delay * 0.25))
                delay = min(delay * 2, LOCK_MAX_DELAY)
        raise TaskError("lock_timeout", f"could not acquire {self.lock_path.name}")

    def __exit__(self, *_):
        try:
            self.lock_path.unlink()
        except OSError:
            pass
        return False


def _task_lock(task_id: str) -> _FileLock:
    return _FileLock(TASKS_DIR / f"{task_id}.lock")


def _list_lock() -> _FileLock:
    return _FileLock(LIST_LOCK)


# ═══════════════════════════════════════════════════════════════════════════
#  CRUD —— 读写一律原子替换
# ═══════════════════════════════════════════════════════════════════════════

def _task_path(task_id: str) -> Path:
    return TASKS_DIR / f"{task_id}.json"


def save_task(task: Task) -> None:
    TASKS_DIR.mkdir(parents=True, exist_ok=True)
    tmp = TASKS_DIR / f".{task.id}.tmp"
    tmp.write_text(json.dumps(asdict(task), ensure_ascii=False, indent=2),
                   encoding="utf-8")
    os.replace(tmp, _task_path(task.id))


def load_task(task_id: str) -> Task:
    path = _task_path(str(task_id))
    if not path.exists():
        raise TaskError("not_found", f"task {task_id} not found")
    data = json.loads(path.read_text(encoding="utf-8"))
    # 容错：旧文件缺字段时用默认值补齐，多余字段忽略
    known = {f for f in Task.__dataclass_fields__}
    return Task(**{k: v for k, v in data.items() if k in known})


def list_tasks() -> list[Task]:
    if not TASKS_DIR.exists():
        return []
    tasks = []
    for p in sorted(TASKS_DIR.glob("*.json"),
                    key=lambda p: int(p.stem) if p.stem.isdigit() else 0):
        try:
            tasks.append(load_task(p.stem))
        except (TaskError, json.JSONDecodeError):
            continue
    return tasks


def _next_id() -> str:
    """高水位标分配 ID：单调递增，任务被删也不重用。调用方须持有列表锁。"""
    high = 0
    if HIGHWATERMARK.exists():
        try:
            high = int(HIGHWATERMARK.read_text(encoding="utf-8").strip() or 0)
        except ValueError:
            high = 0
    # 高水位标丢了就从现有文件重建，宁大勿小
    for p in TASKS_DIR.glob("*.json"):
        if p.stem.isdigit():
            high = max(high, int(p.stem))
    new = high + 1
    HIGHWATERMARK.write_text(str(new), encoding="utf-8")
    return str(new)


def create_task(subject: str, description: str = "", activeForm: str = "",
                metadata: dict | None = None) -> Task:
    """创建任务。依赖关系不在创建时声明（对标 CC TaskCreate），
    由 add_dependency / update_task 的 addBlocks/addBlockedBy 事后维护。"""
    with _list_lock():          # ID 分配是列表级操作
        task = Task(id=_next_id(), subject=subject, description=description,
                    activeForm=activeForm, metadata=metadata or {})
        save_task(task)
    return task


# ═══════════════════════════════════════════════════════════════════════════
#  依赖图 —— 双向维护 + 环检测
# ═══════════════════════════════════════════════════════════════════════════

def can_start(task_id: str) -> bool:
    """blockedBy 全部 completed 才能开始；不存在的依赖视为 blocked。"""
    task = load_task(task_id)
    for dep_id in task.blockedBy:
        try:
            if load_task(dep_id).status != "completed":
                return False
        except TaskError:
            return False
    return True


def unfinished_deps(task: Task) -> list[str]:
    deps = []
    for dep_id in task.blockedBy:
        try:
            if load_task(dep_id).status != "completed":
                deps.append(dep_id)
        except TaskError:
            deps.append(f"{dep_id}(missing)")
    return deps


def _creates_cycle(from_id: str, to_id: str) -> bool:
    """加边 from blockedBy to 之前检查：沿 to 的上游走，能回到 from 就成环。"""
    if from_id == to_id:
        return True
    seen, stack = set(), [to_id]
    while stack:
        current = stack.pop()
        if current == from_id:
            return True
        if current in seen:
            continue
        seen.add(current)
        try:
            stack.extend(load_task(current).blockedBy)
        except TaskError:
            continue
    return False


def add_dependency(task_id: str, dep_id: str) -> None:
    """task_id 被 dep_id 阻塞：写 task.blockedBy，同时同步 dep.blocks。"""
    task_id, dep_id = str(task_id), str(dep_id)
    if _creates_cycle(task_id, dep_id):
        raise TaskError("cycle",
                        f"dependency {task_id} <- {dep_id} would create a cycle")
    with _task_lock(task_id):
        task = load_task(task_id)       # 锁内重读
        if dep_id not in task.blockedBy:
            task.blockedBy.append(dep_id)
            save_task(task)
    with _task_lock(dep_id):
        dep = load_task(dep_id)
        if task_id not in dep.blocks:
            dep.blocks.append(task_id)
            save_task(dep)


# ═══════════════════════════════════════════════════════════════════════════
#  认领 / 状态流转 —— claim 只管 owner 竞争，状态流转走 update_task
# ═══════════════════════════════════════════════════════════════════════════

def claim_task(task_id: str, owner: str) -> Task:
    """设置 owner（CC claimTask 语义：不改 status）。

    锁内重读后的三重校验，错误码与 CC 对齐：
    already_resolved（已完成）/ already_claimed（已被别人认领）/ blocked（上游未完）。
    """
    with _task_lock(task_id):
        task = load_task(task_id)
        if task.status == "completed":
            raise TaskError("already_resolved", f"task {task_id} is completed")
        if task.owner and task.owner != owner:
            raise TaskError("already_claimed",
                            f"task {task_id} is owned by {task.owner}")
        if not can_start(task_id):
            raise TaskError("blocked",
                            f"task {task_id} blocked by: {unfinished_deps(task)}")
        task.owner = owner
        save_task(task)
        return task


# 合法状态机：pending ⇄ in_progress → completed，completed 可 reopen 回 pending
def update_task(task_id: str, *, subject=None, description=None, activeForm=None,
                status=None, owner=None, metadata=None) -> tuple[Task, list[str]]:
    """改字段 + 状态流转。返回 (task, 本次解锁的下游任务 subject 列表)。

    状态流转规则：
      → in_progress   内部走 claim（依赖未完成的进不来）；没 owner 时用传入
                      owner 或默认 "agent" 自动认领
      in_progress → pending   release 路径：自动清 owner，可被重新认领
      → completed     须先 in_progress；完成后扫描 blocks 报告解锁
      completed → pending     允许 reopen
    """
    unblocked: list[str] = []
    with _task_lock(task_id):
        task = load_task(task_id)       # 锁内重读，防 TOCTOU

        if status is not None:
            if status not in STATUSES:
                raise TaskError("bad_transition", f"invalid status '{status}'")
            if status == "in_progress" and task.status == "pending":
                if not can_start(task_id):
                    raise TaskError(
                        "blocked",
                        f"task {task_id} blocked by: {unfinished_deps(task)}")
                if task.owner and owner and task.owner != owner:
                    raise TaskError("already_claimed",
                                    f"task {task_id} is owned by {task.owner}")
                task.owner = task.owner or owner or "agent"
            elif status == "pending" and task.status == "in_progress":
                task.owner = None                        # release
            elif status == "completed" and task.status == "pending":
                raise TaskError("bad_transition",
                                f"task {task_id} is pending; start it first")
            task.status = status

        if owner is not None:
            if owner == "":
                task.owner = None                      # 显式清除
            elif task.owner and task.owner != owner:
                raise TaskError("already_claimed",
                                f"task {task_id} is owned by {task.owner}")
            else:
                task.owner = owner
        if subject is not None:
            task.subject = subject
        if description is not None:
            task.description = description
        if activeForm is not None:
            task.activeForm = activeForm
        if metadata:
            task.metadata.update(metadata)
        save_task(task)

    if task.status == "completed":
        # 解锁通知：下游 pending 且依赖已全部完成的任务
        for other in list_tasks():
            if (other.id in task.blocks and other.status == "pending"
                    and can_start(other.id)):
                unblocked.append(other.subject)
    return task, unblocked
