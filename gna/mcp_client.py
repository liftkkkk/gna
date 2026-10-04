"""MCP stdio 客户端（纯标准库实现，零新增依赖）。

兼容 ZCode / Claude 标准的 mcpServers 配置格式：
    {"mcpServers": {"名字": {"command": "...", "args": [...], "env": {...}, "enabled": true}}}

协议：newline-delimited JSON-RPC（initialize → notifications/initialized → tools/list / tools/call）。
每次调用独立拉起服务器进程（无状态调用，简单可靠；gx-memory 等服务器自持久化）。
"""
from __future__ import annotations

import json
import os
import queue
import subprocess
import threading
from typing import Callable, Dict, List, Optional

PROTOCOL_VERSION = "2024-11-05"
CLIENT_INFO = {"name": "gna", "version": "0.1.0"}


class MCPError(RuntimeError):
    pass


class StdioMCP:
    """与一个 MCP stdio 服务器的一次会话。"""

    def __init__(self, command: str, args: List[str], env: Optional[Dict[str, str]] = None,
                 cwd: Optional[str] = None):
        full_env = dict(os.environ)
        full_env.update(env or {})
        self.proc = subprocess.Popen(
            [command, *args], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True, encoding="utf-8", errors="replace",
            bufsize=1, cwd=cwd, env=full_env)
        self._q: "queue.Queue[str]" = queue.Queue()
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()
        self._next_id = 1

    # ---- 传输 ----
    def _read_loop(self) -> None:
        for line in self.proc.stdout:  # 跳过服务器误打到 stdout 的非 JSON 行
            line = line.strip()
            if line.startswith("{"):
                self._q.put(line)
        self._q.put("")  # EOF 哨兵

    def _send(self, obj: dict) -> None:
        self.proc.stdin.write(json.dumps(obj, ensure_ascii=False) + "\n")
        self.proc.stdin.flush()

    def _recv(self, msg_id: int, timeout: float) -> dict:
        import time

        deadline = time.time() + timeout
        while True:
            remain = deadline - time.time()
            if remain <= 0:
                raise MCPError(f"MCP 响应超时（{timeout}s）")
            try:
                line = self._q.get(timeout=min(remain, 5))
            except queue.Empty:
                continue
            if line == "":
                raise MCPError("MCP 服务器已退出（stdout 关闭）")
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            if msg.get("id") == msg_id:
                if "error" in msg:
                    raise MCPError(f"MCP 错误：{msg['error'].get('message', msg['error'])}")
                return msg.get("result", {})

    def _request(self, method: str, params: dict, timeout: float) -> dict:
        mid = self._next_id
        self._next_id += 1
        self._send({"jsonrpc": "2.0", "id": mid, "method": method, "params": params})
        return self._recv(mid, timeout)

    # ---- 协议 ----
    def initialize(self, timeout: float = 20.0) -> dict:
        result = self._request("initialize", {
            "protocolVersion": PROTOCOL_VERSION, "capabilities": {}, "clientInfo": CLIENT_INFO,
        }, timeout)
        self._send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        return result

    def list_tools(self, timeout: float = 20.0) -> List[dict]:
        return self._request("tools/list", {}, timeout).get("tools", [])

    def call_tool(self, name: str, arguments: dict, timeout: float = 90.0) -> str:
        result = self._request("tools/call", {"name": name, "arguments": arguments}, timeout)
        if result.get("isError"):
            texts = [c.get("text", "") for c in result.get("content", []) if c.get("type") == "text"]
            raise MCPError(texts[0] if texts else "MCP 工具执行失败")
        parts = [c.get("text", "") for c in result.get("content", []) if c.get("type") == "text"]
        return "\n".join(parts) or "（MCP 工具无文本输出）"

    def close(self) -> None:
        try:
            self.proc.stdin.close()
        except Exception:
            pass
        try:
            self.proc.kill()
        except Exception:
            pass


def with_session(cfg: dict, fn: Callable[[StdioMCP], object], timeout: float = 90.0) -> object:
    """拉起服务器 → initialize → 执行 fn(session) → 关闭。cfg: {command, args, env}。"""
    command = cfg.get("command")
    if not command:
        raise MCPError("mcpServers 配置缺少 command")
    s = StdioMCP(command, list(cfg.get("args") or []), cfg.get("env") or {},
                 cfg.get("cwd") or None)
    try:
        s.initialize(timeout=min(timeout, 30.0))
        return fn(s)
    finally:
        s.close()


def list_server_tools(cfg: dict) -> List[dict]:
    """列出某服务器的工具：[{name, description, inputSchema}]。"""
    return with_session(cfg, lambda s: s.list_tools())


def call_server_tool(cfg: dict, tool_name: str, arguments: dict) -> str:
    return with_session(cfg, lambda s: s.call_tool(tool_name, arguments))
