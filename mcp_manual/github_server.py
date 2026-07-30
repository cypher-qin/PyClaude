#!/usr/bin/env python3
"""
手写 GitHub MCP Server —— 不依赖官方 SDK，纯 stdlib。

它就是那层「翻译」：
    MCP 世界                          GitHub 世界
    tools/call search_repositories ──► GET https://api.github.com/search/repositories?q=...
    ←── {"content":[{"type":"text",...}]} ── JSON 响应

运行方式：由客户端作为子进程拉起，通过 stdin/stdout 收发 JSON-RPC。
    python -m mcp_manual.github_server

⚠ 铁律：stdout 只能出现 JSON-RPC 消息，任何日志都必须走 stderr，
   否则会污染协议流、把客户端解析弄崩。

鉴权：可选。设了 GITHUB_TOKEN 就带上（限速 5000 次/时），
     不设也能用公开搜索（限速较低，足够 demo）。
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

SERVER_NAME = "github-manual"
SERVER_VERSION = "0.1.0"

# 协议版本：MCP 2.0 起把协商拆成两段——
#   · handshake 版本集：走经典的 initialize 请求协商（本 server 实现的就是这套）
#   · modern 版本集（2026-07-28+）：需另外实现 discover 流程，教学 server 不涉及
# 所以这里只能报 handshake 版本，否则官方 SDK 客户端会以
# "Unsupported protocol version from the server" 拒绝握手。
LATEST_PROTOCOL_VERSION = "2025-11-25"
SUPPORTED_PROTOCOL_VERSIONS = {"2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05"}

GITHUB_API = "https://api.github.com"
TIMEOUT = 30

# JSON-RPC 标准错误码
PARSE_ERROR, INVALID_REQUEST = -32700, -32600
METHOD_NOT_FOUND, INVALID_PARAMS, INTERNAL_ERROR = -32601, -32602, -32603


def log(msg: str):
    """日志一律走 stderr —— stdout 是协议专用通道。"""
    print(f"[{SERVER_NAME}] {msg}", file=sys.stderr, flush=True)


# ═══════════════════════════════════════════════════════════════════
#  GitHub REST 调用（真正干活的地方）
# ═══════════════════════════════════════════════════════════════════

def github_get(path: str, params: dict | None = None) -> dict:
    url = f"{GITHUB_API}{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": f"{SERVER_NAME}/{SERVER_VERSION}",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    token = os.getenv("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"

    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        return json.loads(resp.read().decode("utf-8"))


# ═══════════════════════════════════════════════════════════════════
#  工具实现 —— 每个函数 = 一个 MCP 工具的 handler
# ═══════════════════════════════════════════════════════════════════

def tool_search_repositories(query: str, sort: str = "stars",
                             order: str = "desc", limit: int = 10) -> str:
    limit = max(1, min(int(limit), 25))
    data = github_get("/search/repositories", {
        "q": query, "sort": sort, "order": order, "per_page": limit,
    })
    items = data.get("items", [])
    if not items:
        return f"No repositories found for query: {query!r}"

    lines = [f"Found {data.get('total_count', 0)} repositories "
             f"(showing top {len(items)}) for {query!r}:\n"]
    for i, r in enumerate(items, 1):
        lines.append(
            f"{i}. {r['full_name']}  ⭐{r['stargazers_count']:,}  "
            f"🍴{r['forks_count']:,}  [{r.get('language') or 'n/a'}]\n"
            f"   {(r.get('description') or '(no description)')[:160]}\n"
            f"   {r['html_url']}"
        )
    return "\n".join(lines)


def tool_get_repository(owner: str, repo: str) -> str:
    r = github_get(f"/repos/{owner}/{repo}")
    topics = ", ".join(r.get("topics", [])) or "(none)"
    return (
        f"{r['full_name']}\n"
        f"  description : {r.get('description') or '(none)'}\n"
        f"  stars/forks : {r['stargazers_count']:,} / {r['forks_count']:,}\n"
        f"  language    : {r.get('language') or 'n/a'}\n"
        f"  license     : {(r.get('license') or {}).get('spdx_id') or 'n/a'}\n"
        f"  topics      : {topics}\n"
        f"  open issues : {r.get('open_issues_count', 0)}\n"
        f"  updated     : {r.get('updated_at')}\n"
        f"  url         : {r['html_url']}"
    )


def tool_get_readme(owner: str, repo: str, max_chars: int = 4000) -> str:
    import base64
    r = github_get(f"/repos/{owner}/{repo}/readme")
    content = base64.b64decode(r.get("content", "")).decode("utf-8", errors="replace")
    if len(content) > max_chars:
        content = content[:max_chars] + f"\n... (truncated, {len(content)} chars total)"
    return f"README of {owner}/{repo}:\n\n{content}"


# 两张表：给模型看的 schema（注意 wire 格式是 camelCase 的 inputSchema）+ 实际 handler
TOOLS = [
    {
        "name": "search_repositories",
        "description": "Search public GitHub repositories by keyword. "
                       "Use for questions like 'find open-source projects about X'.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string",
                          "description": "Search keywords, e.g. 'autonomous driving'"},
                "sort": {"type": "string", "enum": ["stars", "forks", "updated"],
                         "description": "Sort field (default: stars)"},
                "order": {"type": "string", "enum": ["desc", "asc"]},
                "limit": {"type": "integer",
                          "description": "How many results, 1-25 (default 10)"},
            },
            "required": ["query"],
        },
    },
    {
        "name": "get_repository",
        "description": "Get details (stars, language, license, topics) of one GitHub repository.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "owner": {"type": "string", "description": "Repo owner, e.g. 'ApolloAuto'"},
                "repo": {"type": "string", "description": "Repo name, e.g. 'apollo'"},
            },
            "required": ["owner", "repo"],
        },
    },
    {
        "name": "get_readme",
        "description": "Fetch the README content of a GitHub repository.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "owner": {"type": "string"},
                "repo": {"type": "string"},
                "max_chars": {"type": "integer",
                              "description": "Truncate length (default 4000)"},
            },
            "required": ["owner", "repo"],
        },
    },
]

HANDLERS = {
    "search_repositories": tool_search_repositories,
    "get_repository": tool_get_repository,
    "get_readme": tool_get_readme,
}


# ═══════════════════════════════════════════════════════════════════
#  MCP 协议方法
# ═══════════════════════════════════════════════════════════════════

def handle_initialize(params: dict) -> dict:
    # 版本协商：客户端报的版本我们支持就原样回，否则回我们最新的，让客户端决定
    requested = params.get("protocolVersion")
    agreed = requested if requested in SUPPORTED_PROTOCOL_VERSIONS else LATEST_PROTOCOL_VERSION
    client = params.get("clientInfo", {})
    log(f"initialize from {client.get('name')} v{client.get('version')} "
        f"(requested={requested}, agreed={agreed})")
    return {
        "protocolVersion": agreed,
        "capabilities": {"tools": {}},        # 本 server 只提供 tools（无 resources/prompts）
        "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
    }


def handle_tools_list(_params: dict) -> dict:
    return {"tools": TOOLS}


def handle_tools_call(params: dict) -> dict:
    name = params.get("name")
    args = params.get("arguments") or {}
    handler = HANDLERS.get(name)
    if handler is None:
        raise JsonRpcError(INVALID_PARAMS, f"Unknown tool: {name}")

    log(f"tools/call {name} args={json.dumps(args, ensure_ascii=False)[:200]}")
    try:
        text = handler(**args)
        is_error = False
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")[:300]
        # 业务错误用 isError 返回，让模型能看到并自行改策略（而不是抛协议级错误）
        text, is_error = f"GitHub API error {e.code}: {body}", True
    except urllib.error.URLError as e:
        text, is_error = f"Network error reaching GitHub: {e.reason}", True
    except TypeError as e:
        text, is_error = f"Bad arguments for {name}: {e}", True
    except Exception as e:
        text, is_error = f"{type(e).__name__}: {e}", True

    return {"content": [{"type": "text", "text": text}], "isError": is_error}


METHODS = {
    "initialize": handle_initialize,
    "tools/list": handle_tools_list,
    "tools/call": handle_tools_call,
    "ping": lambda _p: {},
}


class JsonRpcError(Exception):
    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code, self.message = code, message


# ═══════════════════════════════════════════════════════════════════
#  主循环：一行一条 JSON-RPC 消息
# ═══════════════════════════════════════════════════════════════════

def main():
    # Windows 上必须锁死 UTF-8，否则中文/emoji 会把协议流写坏
    sys.stdin.reconfigure(encoding="utf-8")
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

    log(f"started (auth={'token' if os.getenv('GITHUB_TOKEN') else 'anonymous'})")

    for line in sys.stdin:                     # 读到 EOF 自然退出
        line = line.strip()
        if not line:
            continue

        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            send({"jsonrpc": "2.0", "id": None,
                  "error": {"code": PARSE_ERROR, "message": "Parse error"}})
            continue

        msg_id = msg.get("id")
        method = msg.get("method")
        params = msg.get("params") or {}

        # 无 id = Notification：处理但绝不回复（回了就违反规范）
        if msg_id is None:
            log(f"notification: {method}")
            continue

        handler = METHODS.get(method)
        if handler is None:
            send({"jsonrpc": "2.0", "id": msg_id,
                  "error": {"code": METHOD_NOT_FOUND, "message": f"Method not found: {method}"}})
            continue

        try:
            result = handler(params)
            send({"jsonrpc": "2.0", "id": msg_id, "result": result})
        except JsonRpcError as e:
            send({"jsonrpc": "2.0", "id": msg_id,
                  "error": {"code": e.code, "message": e.message}})
        except Exception as e:
            log(f"internal error: {type(e).__name__}: {e}")
            send({"jsonrpc": "2.0", "id": msg_id,
                  "error": {"code": INTERNAL_ERROR, "message": f"{type(e).__name__}: {e}"}})

    log("stdin closed, exiting")


def send(msg: dict):
    sys.stdout.write(json.dumps(msg, ensure_ascii=False) + "\n")
    sys.stdout.flush()


if __name__ == "__main__":
    main()
