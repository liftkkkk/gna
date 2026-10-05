"""GNA HTML 前端服务层：FastAPI + NDJSON 流式（消费内核事件生成器，内核零改动）。

运行：gna web  （或 python -m gna.server）
前端：gna/static/（零构建原生 SPA）。
"""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel

from .agent import AgentRuntime
from .config import (PROVIDER_PRESETS, Settings, delete_profile, load_profiles,
                     load_settings, save_settings, set_active_profile, upsert_profile)
from .ext import (MEMORY_DIR, add_mcp_server, load_mcp, load_user_skills,
                  pending_memory_files, remove_mcp_server, remove_user_skill,
                  save_memory_file, save_user_skill)

STATIC_DIR = Path(__file__).resolve().parent / "static"

_HOLDER: dict = {"rt": None, "lock": threading.Lock()}


def get_rt() -> AgentRuntime:
    with _HOLDER["lock"]:
        if _HOLDER.get("rt") is None:
            _HOLDER["rt"] = AgentRuntime(settings=load_settings())
        return _HOLDER["rt"]


def reload_runtime() -> None:
    with _HOLDER["lock"]:
        _HOLDER["rt"] = AgentRuntime(settings=load_settings())


app = FastAPI(title="GNA Runtime", docs_url=None, redoc_url=None)


# ---------------------------------------------------------------- 静态页 ----

@app.get("/")
def index():
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/static/{name}")
def static_file(name: str):
    f = STATIC_DIR / Path(name).name
    if not f.exists():
        raise HTTPException(404)
    return FileResponse(f)


# ---------------------------------------------------------------- 干活（流式）----

class ChatBody(BaseModel):
    text: str
    history: list = []
    auto_gate: bool = True
    files: list = []


@app.post("/api/chat")
def api_chat(body: ChatBody):
    rt = get_rt()

    def stream():
        try:
            for ev in rt.chat_turn(body.text, confirm=lambda m: True,
                                   history=body.history or [], allow_write=body.auto_gate):
                yield json.dumps(ev, ensure_ascii=False) + "\n"
            yield json.dumps({"t": "end"}) + "\n"
        except Exception as e:  # noqa: BLE001
            yield json.dumps({"t": "error", "message": str(e)}, ensure_ascii=False) + "\n"

    return StreamingResponse(stream(), media_type="application/x-ndjson")


# ---------------------------------------------------------------- 记忆 / 审计 ----

@app.get("/api/memory")
def api_memory():
    from .views import hero_md, knowledge_md, skills_md

    store = get_rt().store
    return {"hero": hero_md(store), "knowledge": knowledge_md(store), "skills": skills_md(store)}


@app.get("/api/memory/search")
def api_memory_search(q: str = ""):
    from .views import recall_md

    return {"md": recall_md(get_rt().store, q)}


@app.get("/api/timeline")
def api_timeline(raw: int = 0, limit: int = 40):
    from .views import raw_events_md, timeline_md

    store = get_rt().store
    return {"md": (raw_events_md(store, limit) if raw else timeline_md(store, limit))}


@app.post("/api/memory/remember")
def api_memory_remember(body: dict):
    from .ext import mark_memory_ingested
    from .extract import ingest

    title, content = str(body.get("title", "")).strip(), str(body.get("content", "")).strip()
    if not title or not content:
        raise HTTPException(400, "标题与内容必填")
    f = save_memory_file(title, content)
    mark_memory_ingested(f)
    stat = ingest(get_rt().store, get_rt().llm, content, source=f"记忆文件 {f.name}")
    return {"ok": True, "file": f.name, "facts": stat["facts"]}


# ---------------------------------------------------------------- 扩展 ----

@app.get("/api/ext")
def api_ext():
    data = load_mcp()
    skills = load_user_skills()
    n_mem = len(list(MEMORY_DIR.glob("*.md"))) if MEMORY_DIR.exists() else 0
    return {"mcp": [{"name": k, "command": v.get("command"), "args": v.get("args") or [],
                     "enabled": v.get("enabled", True)} for k, v in data["mcpServers"].items()],
            "skills": [{"name": s["name"], "description": s["description"]} for s in skills],
            "memory": {"dir": str(MEMORY_DIR), "count": n_mem, "pending": len(pending_memory_files())}}


@app.post("/api/mcp")
def api_mcp_add(body: dict):
    from .mcp_client import list_server_tools

    name = str(body.get("name", "")).strip()
    command = str(body.get("command", "")).strip()
    if not name or not command:
        raise HTTPException(400, "名称与命令必填")
    env = body.get("env") or {}
    args = body.get("args") or []
    add_mcp_server(name, command, args, env)
    try:
        ts = list_server_tools({"command": command, "args": args, "env": env})
        reload_runtime()
        return {"ok": True, "tools": [t.get("name") for t in ts]}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)}


@app.delete("/api/mcp/{name}")
def api_mcp_del(name: str):
    remove_mcp_server(name)
    reload_runtime()
    return {"ok": True}


@app.post("/api/mcp/test")
def api_mcp_test(body: dict):
    from .mcp_client import list_server_tools

    cfg = (lambda d: d["mcpServers"].get(str(body.get("name", ""))))(load_mcp())
    if not cfg:
        raise HTTPException(404, "未找到该服务器")
    try:
        ts = list_server_tools(cfg)
        return {"ok": True, "tools": [t.get("name") for t in ts]}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)}


@app.post("/api/skills")
def api_skill_add(body: dict):
    name, body_text = str(body.get("name", "")).strip(), str(body.get("body", "")).strip()
    if not name or not body_text:
        raise HTTPException(400, "技能名与正文必填")
    f = save_user_skill(name, str(body.get("description", "")), body_text)
    return {"ok": True, "path": str(f)}


@app.delete("/api/skills/{name}")
def api_skill_del(name: str):
    return {"ok": remove_user_skill(name)}


@app.post("/api/skills/import")
def api_skill_import(body: dict):
    from .ext import import_skill_package

    path = str(body.get("path", "")).strip()
    if not path:
        raise HTTPException(400, "请提供技能包路径（文件夹或 zip）")
    try:
        r = import_skill_package(path)
        reload_runtime()
        return {"ok": True, **r}
    except Exception as e:  # noqa: BLE001
        raise HTTPException(400, str(e))


@app.post("/api/mcp/import")
def api_mcp_import(body: dict):
    from .ext import import_mcp_json

    text = str(body.get("json", "")).strip()
    if not text:
        raise HTTPException(400, "请粘贴 mcpServers JSON 或给出文件路径")
    try:
        n, names = import_mcp_json(text)
        reload_runtime()
        return {"ok": True, "count": n, "names": names}
    except Exception as e:  # noqa: BLE001
        raise HTTPException(400, str(e))


@app.get("/api/memory/files")
def api_memory_files():
    from .ext import list_memory_files

    return {"files": list_memory_files(), "dir": str(MEMORY_DIR)}


@app.post("/api/memory/import")
def api_memory_import(body: dict):
    from .ext import import_memory_path

    path = str(body.get("path", "")).strip()
    if not path:
        raise HTTPException(400, "请提供 md 文件或文件夹路径")
    try:
        r = import_memory_path(path)
        reload_runtime()
        return {"ok": True, **r}
    except Exception as e:  # noqa: BLE001
        raise HTTPException(400, str(e))


@app.delete("/api/memory/files/{name}")
def api_memory_delete(name: str):
    from .ext import delete_memory_file

    return {"ok": delete_memory_file(name)}


# ---------------------------------------------------------------- 设置 ----

@app.get("/api/models")
def api_models():
    data = load_profiles()
    s = load_settings()
    return {"active": data["active"], "provider": s.provider, "model": s.model,
            "workspace": s.resolved_workspace().as_posix(), "project_root": s.project_root,
            "profiles": [{"id": p["id"], "name": p.get("name") or p["id"], "model": p.get("model", ""),
                          "base_url": p.get("base_url", ""), "has_key": bool(p.get("api_key"))}
                         for p in data["profiles"]]}


@app.post("/api/models/activate")
def api_models_activate(body: dict):
    set_active_profile(str(body.get("id", "")))
    reload_runtime()
    return {"ok": True, "active": load_profiles()["active"]}


@app.post("/api/models/save")
def api_models_save(body: dict):
    name = str(body.get("name", "")).strip()
    if not name:
        raise HTTPException(400, "方案名必填")
    upsert_profile({"id": name, "name": name, "provider": "mock" if not str(body.get("base_url", "")).strip() else "custom",
                    "model": str(body.get("model", "")).strip(), "base_url": str(body.get("base_url", "")).strip(),
                    "api_key": str(body.get("api_key", "")).strip(),
                    "temperature": float(body.get("temperature") or 0.3)})
    reload_runtime()
    return {"ok": True, "active": name}


@app.post("/api/models/test")
def api_models_test(body: dict):
    from .llm import make_llm

    s = load_settings()
    s.provider = "mock" if not str(body.get("base_url", "")).strip() else "custom"
    s.model, s.base_url = str(body.get("model", "")).strip(), str(body.get("base_url", "")).strip()
    s.api_key, s.temperature = str(body.get("api_key", "")).strip(), float(body.get("temperature") or 0.3)
    try:
        out = make_llm(s).chat([{"role": "system", "content": "[角色:PING]"},
                                {"role": "user", "content": "回复两个字：连通"}], temperature=0.0)
        return {"ok": True, "reply": out[:40]}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)}


@app.post("/api/project")
def api_project(body: dict):
    from pathlib import Path as _P

    s = load_settings()
    path = str(body.get("path", "")).strip()
    if path and not _P(path).is_dir():
        raise HTTPException(400, f"目录不存在：{path}")
    s.project_root = path
    save_settings(s)
    reload_runtime()
    return {"ok": True, "workspace": s.resolved_workspace().as_posix()}


@app.get("/api/status")
def api_status():
    from .backend import autodetect_gx, make_backend

    s = load_settings()
    be = make_backend()
    gx_path = autodetect_gx() or (str(Path(os.environ.get("GX_PATH"))) if os.environ.get("GX_PATH") else "")
    return {"model": s.model, "provider": s.provider, "backend": be.name,
            "backend_path": gx_path if be.name == "gx" else "",
            "workspace": s.resolved_workspace().as_posix(), "project_root": s.project_root,
            "version": "0.1.0"}


def launch(host: str = "127.0.0.1", port: int = 8000):
    import uvicorn

    uvicorn.run(app, host=host, port=port, log_level="warning")


if __name__ == "__main__":
    launch()
