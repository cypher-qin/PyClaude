#!/usr/bin/env python3
"""
生产级 MCP 客户端 —— 基于官方 `mcp` SDK（pip install mcp）。

与 mcp_manual/client.py 的区别：协议细节（JSON-RPC 编解码、握手、版本协商、
帧处理、超时、取消、通知分发）全部由 SDK 负责，这里只做两件事：

  1. 把 SDK 的「异步」接口桥接成 agent.py 需要的「同步」调用；
  2. 维持一个长连接（stdio 子进程在整个会话期间保持存活）。

桥接方式：起一个后台线程跑自己的事件循环，在其中打开并持有 SDK 的
async context；主线程用 run_coroutine_threadsafe 把协程投进去、等结果。
"""

from __future__ import annotations

import asyncio
import os
import sys
import threading
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

DEFAULT_TIMEOUT = 60.0


class SdkMCPClient:
    """一个实例 ←→ 一个 MCP server 子进程（官方 SDK 版）。"""

    def __init__(self, name: str, command: str, args: list[str],
                 env: dict[str, str] | None = None):
        self.name = name
        self.params = StdioServerParameters(
            command=command,
            args=args,
            # 子进程继承当前环境 + 额外注入（GITHUB_TOKEN 等）
            env={**os.environ, **(env or {})},
        )
        self.tools: list[dict] = []
        self.server_info: dict = {}
        self.protocol_version: str | None = None

        self._loop: asyncio.AbstractEventLoop | None = None
        self._session: ClientSession | None = None
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        self._stop = threading.Event()
        self._error: BaseException | None = None

    # ── 生命周期 ───────────────────────────────────────────────────────

    def start(self, timeout: float = 60.0) -> "SdkMCPClient":
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name=f"mcp-sdk-{self.name}")
        self._thread.start()
        if not self._ready.wait(timeout):
            raise RuntimeError(f"MCP server '{self.name}' failed to start in {timeout}s")
        if self._error:
            raise RuntimeError(f"MCP server '{self.name}' failed: {self._error}") from self._error
        return self

    def stop(self):
        self._stop.set()
        if self._loop and self._loop.is_running():
            # 唤醒后台循环，让它退出 async with（SDK 会negotiate 关闭 + 收子进程）
            self._loop.call_soon_threadsafe(lambda: None)
        if self._thread:
            self._thread.join(timeout=10)

    def _run(self):
        """后台线程入口：整个 SDK 会话的生命周期都活在这个事件循环里。"""
        try:
            asyncio.run(self._main())
        except BaseException as e:            # noqa: BLE001 - 传回主线程报错
            self._error = e
            self._ready.set()

    async def _main(self):
        self._loop = asyncio.get_running_loop()
        # server 的 stderr：debug 时透传，否则丢弃（避免 GBK 控制台炸掉）
        errlog = sys.stderr if os.getenv("MCP_DEBUG") else open(os.devnull, "w")
        try:
            async with stdio_client(self.params, errlog=errlog) as (read, write):
                async with ClientSession(read, write) as session:
                    init = await session.initialize()          # 握手由 SDK 完成
                    self._session = session
                    # SDK 2.0 的模型属性是蛇形（wire 上仍是 camelCase）
                    self.protocol_version = init.protocol_version
                    self.server_info = {
                        "name": init.server_info.name,
                        "version": init.server_info.version,
                    }
                    listed = await session.list_tools()
                    self.tools = [
                        {
                            "name": t.name,
                            "description": t.description or "",
                            # SDK 属性是 input_schema，wire 上是 inputSchema
                            "inputSchema": t.input_schema or {"type": "object",
                                                              "properties": {}},
                        }
                        for t in listed.tools
                    ]
                    self._ready.set()

                    # 保持连接：直到 stop() 被调用
                    while not self._stop.is_set():
                        await asyncio.sleep(0.2)
        finally:
            self._session = None
            if errlog is not sys.stderr:
                errlog.close()

    # ── 调用 ───────────────────────────────────────────────────────────

    def call_tool(self, tool_name: str, arguments: dict,
                  timeout: float = DEFAULT_TIMEOUT) -> str:
        if not self._session or not self._loop:
            return f"Error: MCP server '{self.name}' is not connected"

        fut = asyncio.run_coroutine_threadsafe(
            self._session.call_tool(tool_name, arguments or {}), self._loop)
        try:
            result = fut.result(timeout=timeout)
        except Exception as e:                # 超时 / 传输错误 / server 崩溃
            return f"Error calling {tool_name}: {type(e).__name__}: {e}"

        text = _flatten(getattr(result, "content", []) or [])
        if getattr(result, "is_error", False):
            return f"Tool error: {text}"
        return text


def _flatten(content: list[Any]) -> str:
    """把 SDK 的 content 对象列表拍平成给模型看的文本。"""
    parts = []
    for block in content:
        text = getattr(block, "text", None)
        if text:
            parts.append(text)
            continue
        resource = getattr(block, "resource", None)
        if resource is not None and getattr(resource, "text", None):
            parts.append(resource.text)
            continue
        parts.append(f"[{getattr(block, 'type', 'unknown')} content]")
    return "\n".join(p for p in parts if p).strip()
