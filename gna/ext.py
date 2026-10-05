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
import shutil
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


# ------------------------------------------------- 导入（MCP JSON / 技能包 / 记忆）----

def import_mcp_json(text: str) -> tuple:
    """导入 mcpServers JSON。兼容三种形态：完整 {"mcpServers":{...}} / 单服务器对象
    （含 command）/ {名字:{...}} 映射。text 也可以是本地 json 文件路径。
    返回 (导入数量, 名称列表)。"""
    cand = text.strip()
    head = cand[:2]
    if (head.isalpha() and len(cand) > 2 and cand[1] == ":") or cand.startswith("/"):
        p = Path(cand)
        if p.is_file():
            cand = p.read_text(encoding="utf-8")
    obj = json.loads(cand)
    if isinstance(obj, dict) and "mcpServers" in obj and isinstance(obj["mcpServers"], dict):
        servers = obj["mcpServers"]
    elif isinstance(obj, dict) and "command" in obj:
        nm = obj.get("name") or f"server-{int(time.time())}"
        servers = {nm: {k: v for k, v in obj.items() if k != "name"}}
    elif isinstance(obj, dict):
        servers = {k: v for k, v in obj.items() if isinstance(v, dict) and "command" in v}
    else:
        raise ValueError("未识别出任何 mcpServers 条目")
    if not servers:
        raise ValueError("未识别出任何 mcpServers 条目")
    data = load_mcp()
    for name, cfg in servers.items():
        cfg = dict(cfg or {})
        cfg.setdefault("enabled", True)
        if cfg.get("args") is None:
            cfg["args"] = []
        data["mcpServers"][name] = cfg
    save_mcp(data)
    return len(servers), list(servers.keys())


def _extract_archive_to(path: Path, dest_dir: Path) -> None:
    """zip/tar 解压到指定目录（zip-slip 防护）。"""
    import zipfile
    from .uploads import _safe_target

    if path.suffix.lower() == ".zip":
        with zipfile.ZipFile(path) as z:
            for info in z.infolist():
                target = _safe_target(dest_dir, info.filename)
                if target is None:
                    continue
                if info.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(z.read(info))
    else:
        import tarfile

        with tarfile.open(path) as t:
            for m in t.getmembers():
                target = _safe_target(dest_dir, m.name)
                if target is None or m.issym() or m.islnk():
                    continue
                if not m.isfile():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(t.extractfile(m).read())


def import_skill_package(path: str) -> dict:
    """从本地文件夹或 zip 导入技能包（ZCode 结构：<包>/SKILL.md）。返回 {name, path}。"""
    import tempfile

    p = Path(path).expanduser()
    if not p.exists():
        raise FileNotFoundError(path)
    tmp = None
    if p.is_file():
        tmp = Path(tempfile.mkdtemp(prefix="skill_import_"))
        _extract_archive_to(p, tmp)
        root = tmp
    else:
        root = p
    if (root / "SKILL.md").exists():
        pkg_root = root
    else:
        subs = [d for d in root.iterdir() if d.is_dir() and (d / "SKILL.md").exists()]
        if len(subs) == 1:
            pkg_root = subs[0]
        elif len(subs) > 1:
            raise ValueError(f"压缩包里有 {len(subs)} 个技能包，请拆开分别导入")
        else:
            raise ValueError("未找到 SKILL.md（ZCode 结构：<文件夹>/SKILL.md）")
    meta, _ = _parse_frontmatter((pkg_root / "SKILL.md").read_text(encoding="utf-8"))
    name = meta.get("name") or pkg_root.name
    safe = re.sub(r'[\\/:*?"<>|\s]+', "-", name.strip()) or f"skill-{int(time.time())}"
    dest = SKILLS_DIR / safe
    if dest.exists():
        shutil.rmtree(dest, ignore_errors=True)
    shutil.copytree(pkg_root, dest)
    if tmp:
        shutil.rmtree(tmp, ignore_errors=True)
    fm, body = _parse_frontmatter((dest / "SKILL.md").read_text(encoding="utf-8"))
    (dest / "SKILL.md").write_text(
        "---\nname: {n}\ndescription: {d}\n---\n\n{b}\n".format(
            n=safe, d=meta.get("description", ""), b=body),
        encoding="utf-8")
    return {"name": safe, "path": str(dest / "SKILL.md")}


def list_memory_files() -> List[dict]:
    done = set()
    if INGESTED_FILE.exists():
        try:
            done = set(json.loads(INGESTED_FILE.read_text(encoding="utf-8")))
        except Exception:
            done = set()
    out = []
    if MEMORY_DIR.exists():
        for f in sorted(MEMORY_DIR.glob("*.md")):
            out.append({"name": f.name, "size": f.stat().st_size, "ingested": str(f) in done})
    return out


def import_memory_path(path: str) -> dict:
    """从本地 md 文件或文件夹导入记忆（复制进 MEMORY_DIR；随后启动/刷新时自动入图）。"""
    p = Path(path).expanduser()
    if not p.exists():
        raise FileNotFoundError(path)
    MEMORY_DIR.mkdir(parents=True, exist_ok=True)
    files = [p] if p.is_file() else [q for q in sorted(p.rglob("*.md"))]
    imported = []
    for f in files:
        if f.suffix.lower() != ".md":
            continue
        dest = MEMORY_DIR / f.name
        i = 0
        while dest.exists() and \
                dest.read_text(encoding="utf-8", errors="replace") != f.read_text(encoding="utf-8", errors="replace"):
            i += 1
            dest = MEMORY_DIR / f"{f.stem}-{i}.md"
        if f.resolve() != dest.resolve():
            dest.write_text(f.read_text(encoding="utf-8", errors="replace"), encoding="utf-8")
        imported.append(dest.name)
    if not imported:
        raise ValueError("该路径下没有 .md 文件")
    return {"imported": len(imported), "files": imported}


def delete_memory_file(name: str) -> bool:
    safe = Path(name).name
    f = MEMORY_DIR / safe
    if not f.exists():
        return False
    f.unlink()
    if INGESTED_FILE.exists():
        try:
            done = json.loads(INGESTED_FILE.read_text(encoding="utf-8"))
            INGESTED_FILE.write_text(
                json.dumps([x for x in done if Path(x).name != safe], ensure_ascii=False, indent=1),
                encoding="utf-8")
        except Exception:
            pass
    return True


