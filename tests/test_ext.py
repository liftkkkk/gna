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


def test_skill_frontmatter_and_crud(runtime, tmp_path, monkeypatch):
    monkeypatch.setattr(ext, "SKILLS_DIR", tmp_path / "skills")
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


def test_import_mcp_json_three_shapes(monkeypatch, tmp_path):
    monkeypatch.setattr(ext, "MCP_FILE", tmp_path / "mcp.json")
    n1, k1 = ext.import_mcp_json('{"mcpServers": {"s1": {"command": "c1", "args": []}}}')
    n2, k2 = ext.import_mcp_json('{"command": "c2", "name": "s2"}')
    n3, k3 = ext.import_mcp_json('{"s3": {"command": "c3"}, "ignored": {"x": 1}}')
    assert (n1, n2, n3) == (1, 1, 1)
    data = ext.load_mcp()["mcpServers"]
    assert {"s1", "s2", "s3"} <= set(data)


def test_import_skill_package_dir_and_zip(runtime, tmp_path, monkeypatch):
    monkeypatch.setattr(ext, "SKILLS_DIR", tmp_path / "skills")
    import io
    import zipfile

    pkg = tmp_path / "评审技能"
    pkg.mkdir()
    (pkg / "SKILL.md").write_text("---\nname: 评审技能\ndescription: 审查\n---\n步骤", encoding="utf-8")
    r1 = ext.import_skill_package(str(pkg))
    assert r1["name"] == "评审技能"
    assert (ext.SKILLS_DIR / "评审技能" / "SKILL.md").exists()

    zbuf = io.BytesIO()
    with zipfile.ZipFile(zbuf, "w") as z:
        z.writestr("zipskill/SKILL.md", "---\nname: zipskill\ndescription: 压缩\n---\n内容")
    zpath = tmp_path / "pack.zip"
    zpath.write_bytes(zbuf.getvalue())
    r2 = ext.import_skill_package(str(zpath))
    assert r2["name"] == "zipskill"


def test_memory_import_and_delete(runtime, tmp_path, monkeypatch):
    monkeypatch.setattr(ext, "MEMORY_DIR", tmp_path / "mem")
    monkeypatch.setattr(ext, "INGESTED_FILE", tmp_path / "mem" / ".ing.json")
    folder = tmp_path / "memories"
    folder.mkdir()
    (folder / "x.md").write_text("记忆X", encoding="utf-8")
    (folder / "y.md").write_text("记忆Y", encoding="utf-8")
    r = ext.import_memory_path(str(folder))
    assert r["imported"] == 2
    names = [f["name"] for f in ext.list_memory_files()]
    assert {"x.md", "y.md"} <= set(names)
    assert ext.delete_memory_file("x.md")
    assert "x.md" not in [f["name"] for f in ext.list_memory_files()]
