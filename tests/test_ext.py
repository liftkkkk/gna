"""用户扩展测试：MCP 客户端（对迷你服务器）、SKILL.md 解析、记忆文件夹幂等入库。"""
import io
import json
import sys
from pathlib import Path

import pytest

from gna import ext
from gna.mcp_client import StdioMCP, with_session

MINI_SERVER = r'''
import json, sys
for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    try:
        msg = json.loads(line)
    except Exception:
        continue
    mid, method = msg.get("id"), msg.get("method")
    if method == "initialize":
        out = {"jsonrpc": "2.0", "id": mid, "result": {"protocolVersion": "2024-11-05",
               "serverInfo": {"name": "mini"}, "capabilities": {"tools": {}}}}
    elif method == "tools/list":
        out = {"jsonrpc": "2.0", "id": mid, "result": {"tools": [
            {"name": "echo", "description": "回显文本",
             "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}},
                             "required": ["text"]}}]}}
    elif method == "tools/call":
        args = msg["params"]["arguments"]
        out = {"jsonrpc": "2.0", "id": mid, "result": {"content": [
            {"type": "text", "text": "echo: " + args.get("text", "")}]}}
    else:
        continue
    sys.stdout.write(json.dumps(out) + "\n")
    sys.stdout.flush()
'''


@pytest.fixture(scope="module")
def mini_server_path(tmp_path_factory):
    p = tmp_path_factory.mktemp("mini") / "mini_mcp_server.py"
    p.write_text(MINI_SERVER, encoding="utf-8")
    return str(p)


def test_mcp_client_handshake_and_call(mini_server_path):
    cfg = {"command": sys.executable, "args": [mini_server_path]}
    result = with_session(cfg, lambda s: s.list_tools())
    assert result[0]["name"] == "echo"
    out = with_session(cfg, lambda s: s.call_tool("echo", {"text": "你好 GNA"}))
    assert "echo: 你好 GNA" in out


def test_mcp_tool_registration_end_to_end(runtime, mini_server_path):
    from gna.ext import add_mcp_server
    from gna.tools import dispatch

    add_mcp_server("mini", sys.executable, [mini_server_path])
    rt = runtime
    rt._load_extensions()   # 重新装载：MCP 工具注册进 chat_tools
    t = rt.chat_tools.get("mcp__mini__echo")
    assert t is not None
    ok, out = dispatch(t, rt.ctx, {"text": "管线通"})
    assert ok and "echo: 管线通" in out


def test_skill_frontmatter_and_crud(runtime):
    f = ext.save_user_skill("代码评审", "PR 审查清单", "1. 先看测试\n2. 再看命名")
    assert f.exists()
    sk = ext.load_user_skills()
    entry = next(s for s in sk if s["name"] == "代码评审")
    assert "PR 审查" in entry["description"] and "先看测试" in entry["body"]
    assert ext.remove_user_skill("代码评审")
    assert not ext.load_user_skills() or all(s["name"] != "代码评审" for s in ext.load_user_skills())


def test_memory_folder_idempotent(runtime, tmp_path, monkeypatch):
    monkeypatch.setattr(ext, "MEMORY_DIR", tmp_path / "memory")
    monkeypatch.setattr(ext, "INGESTED_FILE", tmp_path / "memory" / ".ingested.json")
    f = ext.save_memory_file("探针记忆", "探针：GNA 记忆文件夹幂等入库")
    before = len(ext.pending_memory_files())
    assert before == 1
    ext.mark_memory_ingested(f)
    assert len(ext.pending_memory_files()) == before - 1
    ext.mark_memory_ingested(f)  # 重复标记无害
    assert len(ext.pending_memory_files()) == before - 1
