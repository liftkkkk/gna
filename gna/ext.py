"""用户扩展：MCP 自定义工具（标准 mcpServers 配置）+ 自定义技能（ZCode 标准 SKILL.md）
+ 记忆文件夹（ZCode 式 md 记忆，自动入库）。

约定目录（全部在 ~/.gna 下）：
  mcp.json          —— {"mcpServers": {名字: {command, args, env, enabled}}}
  skills/<名>/SKILL.md —— frontmatter(name/description) + 正文步骤
  memory/*.md       —— 每个文件 = 一条长期记忆，启动时自动入图（幂等）
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Dict, List, Optional

from .config import GNA_HOME

MCP_FILE = GNA_HOME / "mcp.json"
SKILLS_DIR = GNA_HOME / "skills"
MEMORY_DIR = GNA_HOME / "memory"
INGESTED_FILE = MEMORY_DIR / ".ingested.json"


# ------------------------------------------------------------ MCP 配置 ----

def load_mcp() -> dict:
    data = {"mcpServers": {}}
    if MCP_FILE.exists():
        try:
            raw = json.loads(MCP_FILE.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                data["mcpServers"] = raw.get("mcpServers", raw) or {}
        except Exception:
            pass
    return data


def save_mcp(data: dict) -> None:
    GNA_HOME.mkdir(parents=True, exist_ok=True)
    MCP_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def add_mcp_server(name: str, command: str, args: List[str], env: Optional[Dict[str, str]] = None,
                   enabled: bool = True) -> None:
    data = load_mcp()
    data["mcpServers"][name] = {"command": command, "args": args,
                                "env": env or {}, "enabled": bool(enabled)}
    save_mcp(data)


def remove_mcp_server(name: str) -> None:
    data = load_mcp()
    data["mcpServers"].pop(name, None)
    save_mcp(data)


def enabled_mcp_servers() -> Dict[str, dict]:
    return {k: v for k, v in load_mcp()["mcpServers"].items()
            if v.get("enabled", True) and v.get("command")}


def safe_tool_name(server: str, tool: str) -> str:
    base = re.sub(r"[^A-Za-z0-9_\-]", "_", f"mcp__{server}__{tool}")
    return base[:60]


# ------------------------------------------------------------ 用户技能 ----

_FM_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n?", re.S)


def _parse_frontmatter(text: str) -> tuple[dict, str]:
    m = _FM_RE.match(text)
    if not m:
        return {}, text
    meta = {}
    for line in m.group(1).splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            meta[k.strip()] = v.strip().strip("'\"")
    return meta, text[m.end():].strip()


def _skill_files() -> List[Path]:
    if not SKILLS_DIR.exists():
        return []
    return sorted(SKILLS_DIR.glob("*/SKILL.md"))


def load_user_skills() -> List[dict]:
    """读取全部用户技能：[{name, description, body, path}]（ZCode SKILL.md 格式）。"""
    out = []
    for f in _skill_files():
        try:
            meta, body = _parse_frontmatter(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        name = meta.get("name") or f.parent.name
        out.append({"name": name, "description": meta.get("description", ""),
                    "body": body, "path": str(f)})
    return out


def save_user_skill(name: str, description: str, body: str) -> Path:
    safe = re.sub(r'[\\/:*?"<>|\s]+', "-", name.strip()) or f"skill-{int(time.time())}"
    d = SKILLS_DIR / safe
    d.mkdir(parents=True, exist_ok=True)
    fm = f"---\nname: {safe}\ndescription: {description.strip()}\n---\n\n"
    f = d / "SKILL.md"
    f.write_text(fm + body.strip() + "\n", encoding="utf-8")
    return f


def remove_user_skill(name: str) -> bool:
    for f in _skill_files():
        meta, _ = _parse_frontmatter(f.read_text(encoding="utf-8"))
        if meta.get("name") == name or f.parent.name == name:
            import shutil

            shutil.rmtree(f.parent, ignore_errors=True)
            return True
    return False


# ------------------------------------------------------------ 记忆文件夹 ----

def pending_memory_files() -> List[Path]:
    """还没入过图的记忆 md 文件。"""
    if not MEMORY_DIR.exists():
        return []
    done = set()
    if INGESTED_FILE.exists():
        try:
            done = set(json.loads(INGESTED_FILE.read_text(encoding="utf-8")))
        except Exception:
            done = set()
    return [f for f in sorted(MEMORY_DIR.glob("*.md")) if str(f) not in done]


def mark_memory_ingested(path: Path) -> None:
    done = set()
    if INGESTED_FILE.exists():
        try:
            done = set(json.loads(INGESTED_FILE.read_text(encoding="utf-8")))
        except Exception:
            done = set()
    done.add(str(path))
    MEMORY_DIR.mkdir(parents=True, exist_ok=True)
    INGESTED_FILE.write_text(json.dumps(sorted(done), ensure_ascii=False, indent=1), encoding="utf-8")


def save_memory_file(title: str, content: str) -> Path:
    MEMORY_DIR.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r'[\\/:*?"<>|\s]+', "-", title.strip()) or f"memory-{int(time.time())}"
    f = MEMORY_DIR / f"{safe}.md"
    f.write_text(content.strip() + "\n", encoding="utf-8")
    return f


# ---------------------------------------------------- MCP 工具 → 注册表 ----

def build_mcp_tools():
    """把全部启用中的 MCP 服务器工具包装成 Tool 对象（供对话引擎调用）。"""
    from .tools import Tool

    tools = []
    for server, cfg in enabled_mcp_servers().items():
        try:
            from .mcp_client import list_server_tools

            specs = list_server_tools(cfg)
        except Exception as e:  # noqa: BLE001
            tools.append(Tool(f"mcp__{server}__status", f"MCP 服务器 {server} 不可用：{e}", {}, 
                              lambda ctx, _e=str(e), _s=server: (False, f"MCP 服务器 {_s} 不可用：{_e}"),
                              perm="mcp"))
            continue
        for spec in specs:
            tname = spec.get("name", "")
            full = safe_tool_name(server, tname)
            schema = spec.get("inputSchema") or {}
            props = schema.get("properties") or {}
            required = set(schema.get("required") or [])
            params = {k: {"type": str(v.get("type", "string")), "required": k in required}
                      for k, v in props.items()}
            desc = f"[MCP:{server}] {spec.get('description') or tname}"

            def make_fn(srv=server, tname=tname, cfg=cfg):
                def fn(ctx, **kw):
                    from .mcp_client import call_server_tool

                    return True, call_server_tool(cfg, tname, kw)
                return fn

            tools.append(Tool(full, desc, params, make_fn(), perm="mcp"))
    return tools
