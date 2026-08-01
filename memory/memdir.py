#!/usr/bin/env python3
"""
memdir —— 记忆的物理形态：读写、扫描、索引、注入裁剪。

    <项目根>/.memory/
      MEMORY.md              索引（本模块生成，手改会被覆盖）
      user-prefers-tabs.md   记忆体：YAML frontmatter + Markdown 正文

对标 CC 的 memdir/{memdir,memoryTypes,memoryScan}.ts 与 utils/attachments.ts。

本模块只回答「记忆长什么样、放在哪」。
选哪几条注入、什么时候写、什么时候合并，分别是 relevance / extract / dream 的事。
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import yaml

# ── 四类记忆（CC memoryTypes.ts）──
#   user      你是谁      "用 tab 不用空格"
#   feedback  怎么做事    "别 mock 数据库"
#   project   正在发生什么 "auth 重写是合规驱动"
#   reference 东西在哪找  "pipeline bug 在 Linear INGEST"
MEMORY_TYPES = ("user", "feedback", "project", "reference")
DEFAULT_TYPE = "user"

# ── 预算：取自 CC 的实测值，别随手改 ──
INDEX_MAX_LINES = 200        # memdir.ts:34-38
INDEX_MAX_BYTES = 25_000     # memdir.ts:34-38
SCAN_MAX_FILES = 200         # memoryScan.ts:35-94
FILE_MAX_LINES = 200         # attachments.ts:269-288  单条注入上限
FILE_MAX_BYTES = 4_096       # attachments.ts:269-288  单条注入上限
INJECT_MAX_BYTES = 60_000    # attachments.ts:269-288  单会话注入总预算

INDEX_NAME = "MEMORY.md"


@dataclass(frozen=True)
class Memory:
    filename: str
    name: str
    description: str
    type: str
    body: str
    mtime: float


# ═══════════════════════════════════════════════════════════════════════════
#  位置
# ═══════════════════════════════════════════════════════════════════════════

def _project_root() -> Path:
    """向上找 .git 定位项目根，找不到退回 cwd。

    不调 git 子进程：省一次进程启动，也绕开中文 Windows 的 GBK 解码坑。
    这样从子目录启动 agent 也能读到同一份记忆，不会「换个目录就失忆」。
    """
    start = Path.cwd().resolve()
    for d in (start, *start.parents):
        if (d / ".git").exists():
            return d
    return start


@lru_cache(maxsize=1)
def memory_root() -> Path:
    """记忆目录。默认 <项目根>/.memory，MEMORY_ROOT 可覆盖（.env 是唯一配置入口）。"""
    override = (os.getenv("MEMORY_ROOT") or "").strip()
    if override:
        return Path(override).expanduser().resolve()
    return _project_root() / ".memory"


def index_path() -> Path:
    return memory_root() / INDEX_NAME


# ═══════════════════════════════════════════════════════════════════════════
#  解析与命名
# ═══════════════════════════════════════════════════════════════════════════

def _parse_frontmatter(text: str) -> tuple[dict, str]:
    """与 agent.py 的技能解析同款，只是这里要自带一份保持本包自洽。"""
    if not text.startswith("---"):
        return {}, text.strip()
    parts = text.split("---", 2)
    if len(parts) < 3:
        return {}, text.strip()
    try:
        meta = yaml.safe_load(parts[1]) or {}
    except yaml.YAMLError:
        meta = {}
    return (meta if isinstance(meta, dict) else {}), parts[2].strip()


_SLUG_DISALLOWED = re.compile(r"[^a-z0-9一-鿿]+")


def _slug(name: str) -> str:
    """文件名只保留小写字母 / 数字 / 中文，其余一律折成连字符。

    name 由模型生成，必须挡住路径穿越：'..' 和 '..\\..\\evil' 都会被折平，
    逃不出记忆目录。
    """
    slug = _SLUG_DISALLOWED.sub("-", name.strip().lower()).strip("-")
    return slug[:60] or "memory"


# ═══════════════════════════════════════════════════════════════════════════
#  读
# ═══════════════════════════════════════════════════════════════════════════

def _load(path: Path) -> Memory | None:
    try:
        raw = path.read_text(encoding="utf-8")
        mtime = path.stat().st_mtime
    except OSError as e:
        print(f"\033[33m[memory] 读取失败 {path.name}: {e}\033[0m")
        return None
    meta, body = _parse_frontmatter(raw)
    mem_type = str(meta.get("type") or DEFAULT_TYPE)
    return Memory(
        filename=path.name,
        name=str(meta.get("name") or path.stem),
        description=str(meta.get("description") or body.split("\n")[0][:80]),
        type=mem_type if mem_type in MEMORY_TYPES else DEFAULT_TYPE,
        body=body,
        mtime=mtime,
    )


def scan_memories() -> list[Memory]:
    """扫描全部记忆体：排除索引和点文件，mtime 降序，最多 SCAN_MAX_FILES 条。"""
    root = memory_root()
    if not root.is_dir():
        return []
    files = [f for f in root.glob("*.md")
             if f.name != INDEX_NAME and not f.name.startswith(".")]
    files.sort(key=lambda f: f.stat().st_mtime, reverse=True)
    return [m for m in (_load(f) for f in files[:SCAN_MAX_FILES]) if m]


def read_memory(filename: str) -> Memory | None:
    """按文件名读一条。只取 basename，杜绝越界读取。"""
    path = memory_root() / Path(filename).name
    return _load(path) if path.is_file() else None


def read_index() -> str:
    path = index_path()
    return path.read_text(encoding="utf-8") if path.is_file() else ""


# ═══════════════════════════════════════════════════════════════════════════
#  写
# ═══════════════════════════════════════════════════════════════════════════

def write_memory(name: str, mem_type: str, description: str, body: str) -> Path:
    """写入一条记忆。**同名覆盖**——这就是去重：同一件事只会有一个文件。"""
    root = memory_root()
    root.mkdir(parents=True, exist_ok=True)
    if mem_type not in MEMORY_TYPES:
        mem_type = DEFAULT_TYPE
    # 用 safe_dump 生成 frontmatter：name/description 里带冒号或引号也不会破坏格式
    front = yaml.safe_dump(
        {"name": name, "description": description, "type": mem_type},
        allow_unicode=True, sort_keys=False, default_flow_style=False)
    path = root / f"{_slug(name)}.md"
    path.write_text(f"---\n{front}---\n\n{body.strip()}\n", encoding="utf-8")
    rebuild_index()
    return path


def delete_memory(filename: str) -> bool:
    path = memory_root() / Path(filename).name
    if path.name == INDEX_NAME or not path.is_file():
        return False
    path.unlink()
    rebuild_index()
    return True


_INDEX_HEADER = (
    "# 记忆索引\n"
    "\n"
    "> 本文件由 memory/memdir.py 自动生成，手改会被覆盖。\n"
    "> 要增删改记忆，请编辑 .memory/ 下对应的 .md 文件。\n"
    "\n"
)


def _render_index(lines: list[str]) -> str:
    return _INDEX_HEADER + ("\n".join(lines) + "\n" if lines else "(暂无记忆)\n")


def rebuild_index() -> None:
    """从全部记忆体重建索引。每次写入后必调，启动时也调一次。

    索引常驻 system prompt，所以要封顶：先按 200 行截（scan 已是 mtime 降序，
    留下的是最新的），再按 25KB 逐条砍。
    """
    lines = [f"- [{m.name}]({m.filename}) `{m.type}` — {m.description}"
             for m in scan_memories()[:INDEX_MAX_LINES]]
    while lines and len(_render_index(lines).encode("utf-8")) > INDEX_MAX_BYTES:
        lines.pop()
    root = memory_root()
    root.mkdir(parents=True, exist_ok=True)
    index_path().write_text(_render_index(lines), encoding="utf-8")


# ═══════════════════════════════════════════════════════════════════════════
#  注入
# ═══════════════════════════════════════════════════════════════════════════

def clip_for_injection(memory: Memory) -> str:
    """单条记忆注入前的裁剪：≤200 行 / ≤4096 字节。"""
    lines = memory.body.splitlines()
    body = "\n".join(lines[:FILE_MAX_LINES])
    truncated = len(lines) > FILE_MAX_LINES
    data = body.encode("utf-8")
    if len(data) > FILE_MAX_BYTES:
        body = data[:FILE_MAX_BYTES].decode("utf-8", errors="ignore")
        truncated = True
    if truncated:
        body += "\n…（已截断）"
    return f'<memory name="{memory.name}" type="{memory.type}">\n{body}\n</memory>'


def apply_budget(memories: list[Memory]) -> list[Memory]:
    """按单会话 60KB 总预算截断，超预算的丢掉。"""
    kept, used = [], 0
    for memory in memories:
        size = len(clip_for_injection(memory).encode("utf-8"))
        if used + size > INJECT_MAX_BYTES:
            break
        kept.append(memory)
        used += size
    return kept


def behavior_instructions() -> str:
    """system prompt 里的记忆行为说明（CC memdir.ts:199-266）。

    要讲清三件事：记忆 vs todo 的分工、四类记忆各记什么、怎么写。
    """
    return (
        "Memory: durable knowledge that stays useful ACROSS sessions, kept as "
        f"Markdown files in {memory_root()}. Different from todo_write, which "
        "only tracks steps inside the current task.\n"
        "The index below lists what you already know about this user and project. "
        "**Follow it.** Apply a relevant memory without being asked and without "
        "mentioning it; if only its one-line description is shown and you need the "
        "detail, read the file. Relevant memories may also be injected in full "
        "alongside the user's message.\n"
        "Four types — user: who the user is (\"prefers tabs over spaces\"); "
        "feedback: how to work (\"don't mock the database\"); "
        "project: what is going on (\"the auth rewrite is compliance-driven\"); "
        "reference: where to find things (\"pipeline bugs live in Linear INGEST\").\n"
        "Save one with remember(...); reusing a name overwrites that memory. "
        "You may also manage the files directly with read_file / edit_file / glob / "
        "grep — one fact per file. Never hand-edit MEMORY.md, it is regenerated."
    )


# ═══════════════════════════════════════════════════════════════════════════
#  迁移
# ═══════════════════════════════════════════════════════════════════════════

_INDEX_LINE = re.compile(r"^-\s+\[.+?\]\(.+?\.md\)")
_LEGACY_LINE = re.compile(r"^-\s+(?:\([^)]*\)\s*)?(?P<note>.+?)\s*$")


def migrate_legacy() -> int:
    """旧版 MEMORY.md 是「一行一条笔记」，新版是自动生成的索引。

    首次启动时把旧笔记转存成一个记忆体，避免升级即失忆。
    已经存在记忆体文件就说明迁过了，直接跳过。
    """
    path = index_path()
    if not path.is_file() or scan_memories():
        return 0
    notes = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line.startswith("- ") or _INDEX_LINE.match(line):
            continue
        matched = _LEGACY_LINE.match(line)
        if matched:
            notes.append(matched.group("note").strip())
    if not notes:
        return 0
    write_memory(
        name="legacy-notes",
        mem_type=DEFAULT_TYPE,
        description="迁移自旧版 MEMORY.md 的笔记",
        body="\n".join(f"- {note}" for note in notes),
    )
    print(f"\033[36m[memory] 已迁移旧版 MEMORY.md 的 {len(notes)} 条笔记 "
          f"→ legacy-notes.md\033[0m")
    return len(notes)
