#!/usr/bin/env python3
"""
手写 MCP 客户端 —— JSON-RPC 2.0 over stdio，不依赖官方 SDK。

目的：把「Agent 怎么连上一个 MCP server」这件事完全摊开，同时保留向生产级
演进的空间（后台读线程 / id 路由 / 超时 / 通知处理 / 优雅关闭都已具备）。

┌── 你的 Agent 进程（父）────────────┐
│  ManualMCPClient                   │
│    │ 写 stdin ──────────────┐      │
│    │ 读 stdout ◄────────────┼──────┼──► server 子进程（任意语言）
│    │ 读 stderr(日志) ◄──────┘      │
└────────────────────────────────────┘

协议三步：
    initialize      握手 + 能力协商（Request，有 id）
    notifications/initialized   握手收尾（Notification，无 id）
    tools/list      发现工具
    tools/call      调用工具
"""

from __future__ import annotations

import itertools
import json
import os
import queue
import subprocess
import sys
import threading
from typing import Any

# 客户端支持的协议版本（新→旧）。initialize 时报最新的；
# 若 server 回一个不同版本，只要在本列表内即接受——这就是「版本协商」。
#
# 注意只列 handshake 版本：MCP 2.0 起 initialize 仅协商这一段，
# 更新的 modern 版本（2026-07-28+）要走单独的 discover 流程，本 client 未实现。
SUPPORTED_PROTOCOL_VERSIONS = ["2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05"]

DEFAULT_TIMEOUT = 60.0


class MCPError(RuntimeError):
    """server 返回 JSON-RPC error 对象时抛出。"""

    def __init__(self, code: int, message: str, data: Any = None):
        super().__init__(f"[{code}] {message}")
        self.code, self.message, self.data = code, message, data


class ManualMCPClient:
    """一个 client 实例 ←→ 一个 MCP server 子进程（1:1，MCP 规范如此）。"""

    def __init__(self, name: str, command: list[str],
                 env: dict[str, str] | None = None, cwd: str | None = None):
        self.name = name                  # 逻辑名，用于 mcp__{name}__{tool} 前缀
        self.command = command
        self.env = env
        self.cwd = cwd

        self.proc: subprocess.Popen | None = None
        self.server_info: dict = {}
        self.protocol_version: str | None = None
        self.tools: list[dict] = []

        self._ids = itertools.count(1)
        # id -> 放响应的队列。后台线程收到响应后按 id 投递，主线程阻塞取。
        self._pending: dict[int, queue.Queue] = {}
        self._lock = threading.Lock()
        self._alive = threading.Event()

    # ── 生命周期 ───────────────────────────────────────────────────────

    def start(self) -> "ManualMCPClient":
        """起子进程 → 接管管道 → 握手 → 拉工具清单。"""
        child_env = {**os.environ, **(self.env or {})}
        # encoding 必须显式 UTF-8：中文 Windows 默认 GBK 会把 JSON 弄坏
        self.proc = subprocess.Popen(
            self.command,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace",
            bufsize=1,                      # 行缓冲：一条消息一行
            env=child_env, cwd=self.cwd,
        )
        self._alive.set()

        # 两个后台线程：一个读响应，一个抽 stderr。
        # stderr 不抽走，管道写满后子进程会直接卡死——这是 stdio 传输的经典坑。
        threading.Thread(target=self._reader_loop, daemon=True,
                         name=f"mcp-{self.name}-stdout").start()
        threading.Thread(target=self._stderr_loop, daemon=True,
                         name=f"mcp-{self.name}-stderr").start()

        self._handshake()
        self.tools = self._request("tools/list").get("tools", [])
        return self

    def stop(self):
        self._alive.clear()
        if not self.proc:
            return
        try:
            self.proc.stdin.close()          # 关 stdin：server 读到 EOF 应自行退出
        except Exception:
            pass
        try:
            self.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.proc.kill()

    # ── 后台线程 ───────────────────────────────────────────────────────

    def _reader_loop(self):
        """持续读 stdout，按 id 把响应投递给等待方。

        用后台线程而不是「发一条阻塞读一条」，是为了能正确处理：
        乱序响应、server 主动推的通知、以及并发调用。
        """
        stdout = self.proc.stdout
        while self._alive.is_set():
            line = stdout.readline()
            if not line:                     # EOF：子进程退出了
                break
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue                     # 非 JSON 行（有些 server 会打日志到 stdout）
            msg_id = msg.get("id")
            if msg_id is None:
                self._on_notification(msg)   # server → client 的通知
                continue
            with self._lock:
                waiter = self._pending.pop(msg_id, None)
            if waiter:
                waiter.put(msg)

        # 子进程没了：叫醒所有还在等的人，避免永久阻塞
        self._alive.clear()
        with self._lock:
            waiters, self._pending = list(self._pending.values()), {}
        for w in waiters:
            w.put({"error": {"code": -32000, "message": "server closed"}})

    def _stderr_loop(self):
        for line in self.proc.stderr:
            if os.getenv("MCP_DEBUG"):
                print(f"\033[90m[{self.name}:stderr] {line.rstrip()}\033[0m",
                      file=sys.stderr)

    def _on_notification(self, msg: dict):
        if os.getenv("MCP_DEBUG"):
            print(f"\033[90m[{self.name}:notify] {msg.get('method')}\033[0m",
                  file=sys.stderr)

    # ── JSON-RPC 收发 ──────────────────────────────────────────────────

    def _write(self, msg: dict):
        if not self.proc or self.proc.poll() is not None:
            raise MCPError(-32000, f"MCP server '{self.name}' is not running")
        data = json.dumps(msg, ensure_ascii=False)   # 单行、无换行
        if os.getenv("MCP_DEBUG"):
            print(f"\033[36m[{self.name}] → {data[:300]}\033[0m", file=sys.stderr)
        self.proc.stdin.write(data + "\n")
        self.proc.stdin.flush()

    def _request(self, method: str, params: dict | None = None,
                 timeout: float = DEFAULT_TIMEOUT) -> dict:
        """发 Request（带 id），阻塞等待对应 id 的 Response。"""
        msg_id = next(self._ids)
        waiter: queue.Queue = queue.Queue(maxsize=1)
        with self._lock:
            self._pending[msg_id] = waiter

        msg = {"jsonrpc": "2.0", "id": msg_id, "method": method}
        if params is not None:
            msg["params"] = params
        self._write(msg)

        try:
            resp = waiter.get(timeout=timeout)
        except queue.Empty:
            with self._lock:
                self._pending.pop(msg_id, None)
            raise MCPError(-32000, f"'{method}' timed out after {timeout}s")

        if os.getenv("MCP_DEBUG"):
            print(f"\033[35m[{self.name}] ← {json.dumps(resp, ensure_ascii=False)[:300]}\033[0m",
                  file=sys.stderr)
        if "error" in resp:
            e = resp["error"]
            raise MCPError(e.get("code", -1), e.get("message", ""), e.get("data"))
        return resp.get("result", {})

    def _notify(self, method: str, params: dict | None = None):
        """发 Notification（无 id，不等回复）。"""
        msg = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            msg["params"] = params
        self._write(msg)

    # ── 协议流程 ───────────────────────────────────────────────────────

    def _handshake(self):
        result = self._request("initialize", {
            "protocolVersion": SUPPORTED_PROTOCOL_VERSIONS[0],
            "capabilities": {},                       # 本 client 暂不提供 sampling/roots
            "clientInfo": {"name": "t10-manual-client", "version": "0.1.0"},
        })
        self.protocol_version = result.get("protocolVersion")
        self.server_info = result.get("serverInfo", {})
        if self.protocol_version not in SUPPORTED_PROTOCOL_VERSIONS:
            raise MCPError(-32000,
                           f"unsupported protocol version from server: {self.protocol_version}")
        # 握手收尾：告诉 server「我准备好了」，之后才可以发别的请求
        self._notify("notifications/initialized")

    def call_tool(self, tool_name: str, arguments: dict,
                  timeout: float = DEFAULT_TIMEOUT) -> str:
        """调用工具，把 MCP 的 content 数组拍平成给模型看的纯文本。"""
        result = self._request("tools/call",
                               {"name": tool_name, "arguments": arguments},
                               timeout=timeout)
        text = flatten_content(result.get("content", []))
        # isError=true 是「工具执行失败」（业务错误），区别于 JSON-RPC error（协议错误）
        if result.get("isError"):
            return f"Tool error: {text}"
        return text


def flatten_content(content: list) -> str:
    """MCP 的 content 是 [{type:text|image|resource, ...}]，取出可读文本。"""
    parts = []
    for block in content or []:
        if not isinstance(block, dict):
            parts.append(str(block))
        elif block.get("type") == "text":
            parts.append(block.get("text", ""))
        elif block.get("type") == "resource":
            res = block.get("resource", {})
            parts.append(res.get("text") or f"[resource {res.get('uri', '')}]")
        else:
            parts.append(f"[{block.get('type')} content]")
    return "\n".join(p for p in parts if p).strip()
