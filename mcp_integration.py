#!/usr/bin/env python3
"""
MCP 集成层 —— 一个开关切换两条实现路径，对 agent.py 暴露同一套接口。

    .env: MCP_MODE=off | manual | sdk

    off     不连任何 MCP server（默认）
    manual  手写 client（mcp_manual/client.py）—— 看得见协议全过程
    sdk     官方 mcp SDK（mcp_sdk_client.py）—— 生产级

两条路径连的是同一个 server、返回同样形状的 (tools, handlers)，
所以切换对 agent.py 完全透明。这也顺带证明了一件事：
**只要守同一套协议，client 换谁都能连上同一个 server。**

要连官方 GitHub MCP server（真·生产配置），只改 .env 一行：
    MCP_SERVER_CMD=["docker","run","-i","--rm",
                    "-e","GITHUB_PERSONAL_ACCESS_TOKEN",
                    "ghcr.io/github/github-mcp-server"]
"""

from __future__ import annotations

import json
import os
import re
import sys

_DISALLOWED = re.compile(r"[^a-zA-Z0-9_-]")

# 已连接的客户端，退出时统一关闭
_CLIENTS: list = []


def _normalize(name: str) -> str:
    """工具名只允许 [a-zA-Z0-9_-]，其余字符换成下划线。"""
    return _DISALLOWED.sub("_", name)


def _server_command() -> list[str]:
    """决定要拉起哪个 MCP server 子进程。

    默认用本项目手写的 github server（免 token、开箱即跑）；
    设了 MCP_SERVER_CMD 就用它（JSON 数组，便于带空格/引号的参数）。
    """
    raw = (os.getenv("MCP_SERVER_CMD") or "").strip()
    if not raw:
        return [sys.executable, "-m", "mcp_manual.github_server"]
    try:
        cmd = json.loads(raw)
        if not isinstance(cmd, list) or not all(isinstance(x, str) for x in cmd):
            raise ValueError("MCP_SERVER_CMD must be a JSON array of strings")
        return cmd
    except json.JSONDecodeError:
        return raw.split()          # 退化：按空格切（简单命令够用）


def _server_env() -> dict[str, str]:
    """透传给 server 子进程的环境变量（凭据只进 server，不进模型上下文）。"""
    env = {}
    token = os.getenv("GITHUB_TOKEN")
    if token:
        env["GITHUB_TOKEN"] = token
        # 官方 GitHub MCP server 认的是这个名字
        env["GITHUB_PERSONAL_ACCESS_TOKEN"] = token
    return env


def connect_mcp_servers(verbose: bool = True) -> tuple[list[dict], dict]:
    """按 MCP_MODE 建立连接，返回 (tools, handlers) 供并入工具池。

    tools    —— 给模型看的 schema，名字已加 mcp__{server}__ 前缀
    handlers —— 名字 → 可调用对象，内部转发到对应 server
    """
    mode = (os.getenv("MCP_MODE") or "off").strip().lower()
    if mode in ("off", "", "none", "false", "0"):
        return [], {}

    server_name = os.getenv("MCP_SERVER_NAME", "github")
    command = _server_command()
    env = _server_env()

    try:
        if mode == "manual":
            from mcp_manual.client import ManualMCPClient
            client = ManualMCPClient(server_name, command, env=env).start()
        elif mode == "sdk":
            from mcp_sdk_client import SdkMCPClient
            client = SdkMCPClient(server_name, command[0], command[1:], env=env).start()
        else:
            print(f"\033[33m[mcp] 未知 MCP_MODE={mode!r}，已跳过（可选 off/manual/sdk）\033[0m")
            return [], {}
    except Exception as e:
        # MCP 连不上不应该让整个 agent 起不来——降级为「没有 MCP 工具」继续跑
        print(f"\033[31m[mcp] 连接失败（{mode}）：{type(e).__name__}: {e}\033[0m")
        print(f"\033[31m[mcp] 已降级为无 MCP 工具继续运行\033[0m")
        return [], {}

    _CLIENTS.append(client)

    tools, handlers = [], {}
    safe_server = _normalize(client.name)
    for t in client.tools:
        prefixed = f"mcp__{safe_server}__{_normalize(t['name'])}"
        tools.append({
            "name": prefixed,
            "description": t.get("description", ""),
            "input_schema": t.get("inputSchema") or {"type": "object", "properties": {}},
        })
        # 闭包绑定：c=当前 client，t=原始工具名（去掉前缀后 server 才认识）
        handlers[prefixed] = (
            lambda *, _c=client, _t=t["name"], **kw: _c.call_tool(_t, kw)
        )

    if verbose:
        impl = "手写 client" if mode == "manual" else "官方 SDK"
        print(f"\033[32m[mcp] {impl} 已连接 '{client.name}' "
              f"({client.server_info.get('name')} v{client.server_info.get('version')}, "
              f"协议 {client.protocol_version})\033[0m")
        print(f"\033[32m[mcp] 发现 {len(tools)} 个工具: "
              f"{', '.join(t['name'] for t in tools)}\033[0m")
    return tools, handlers


def shutdown_mcp_servers():
    for client in _CLIENTS:
        try:
            client.stop()
        except Exception:
            pass
    _CLIENTS.clear()
