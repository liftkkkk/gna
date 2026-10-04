"""GNA Web 界面（Gradio）——同一 RuntimeGraph 之上的视图层。

运行：python -m gna web   （或 gna web）
默认 http://127.0.0.1:7860，数据落在 ~/.gna/runtime.json（与 CLI 共享同一张图）。
"""
from __future__ import annotations

import json
import threading
from pathlib import Path

import gradio as gr

from .agent import AgentRuntime
from .config import (PROVIDER_PRESETS, Settings, delete_profile, load_profiles,
                     load_settings, save_settings, set_active_profile, upsert_profile)
from .demo import DEMO_FACTS, DEMO_NOTES
from .graph import now

_VIZ_DIR = Path.home() / ".gna" / "viz"
_VIZ_DIR.mkdir(parents=True, exist_ok=True)

_HOLDER: dict = {"rt": None, "lock": threading.Lock()}


def get_rt() -> AgentRuntime:
    with _HOLDER["lock"]:
        rt = _HOLDER.get("rt")
        if rt is None:
            s = load_settings()
            rt = AgentRuntime(settings=s)
            _bootstrap(rt)
            _HOLDER["rt"] = rt
        return rt


def _bootstrap(rt: AgentRuntime) -> None:
    """装载示例数据（幂等）：示例笔记按文件缺失补写；示例知识仅在空库时写入。
    项目模式下不动用户的项目文件夹。"""
    if rt.settings.project_root:
        return
    ws = rt.settings.resolved_workspace()
    for fname, content in DEMO_NOTES.items():
        p = ws / "notes" / fname
        if not p.exists():
            p.write_text(content, encoding="utf-8")
    if rt.store.be.node_count() <= 30:
        for s_, r_, o_ in DEMO_FACTS:
            rt.store.add_fact_triple(s_, r_, o_, "示例数据")


# ---------------------------------------------------------------- 渲染 ----

from .views import (knowledge_md as _knowledge_md, raw_events_md as _raw_events_md,
                    recall_md as _recall_md, skills_md as _skills_md,
                    timeline_md as _timeline_md)


def _rt():
    return get_rt().store


def mem_md() -> str:
    """图谱世界主视图：它知道什么（hero + 知识卡片 + 已完成事项）。"""
    from .views import hero_md

    return hero_md(_rt()) + "\n\n" + _knowledge_md(_rt())


def skills_view_md() -> str:
    return _skills_md(_rt())


def audit_md() -> str:
    return _timeline_md(_rt(), limit=40)


def raw_audit_md() -> str:
    return _raw_events_md(_rt(), limit=60)


def recall_view_md(query: str) -> str:
    return _recall_md(_rt(), query)


def refresh_all():
    return mem_md(), skills_view_md(), audit_md(), raw_audit_md()


# ---------------------------------------------------------------- 对话 ----

def _ingest_uploads(files) -> list:
    """上传件落 inbox；压缩包自动解压。返回文件说明列表。"""
    from gna.uploads import extract_archive, is_archive, save_upload

    notes = []
    for f in files or []:
        src = Path(f)
        dest = save_upload(src.name, src.read_bytes())
        if is_archive(dest.name):
            extracted, skipped = extract_archive(dest)
            notes.append(f"{dest.name}（压缩包，已自动解压 {len(extracted)} 个文件 → inbox/{dest.stem}_extracted/）")
        else:
            notes.append(dest.name)
    return notes


def on_send(text: str, files: list, hist: list, auto_gate: bool):
    rt = get_rt()
    notes = _ingest_uploads(files)
    text = (text or "").strip()
    if notes:
        text = ((text or "请处理我上传的这些文件") +
                "\n\n【用户上传的文件（已存入 inbox/，可用 read_pdf 读论文、unzip 解压、read_file 读文本）】\n- "
                + "\n- ".join(notes))
    text = text.strip()
    if not text:
        return "", gr.update(), hist or [], gr.update(), *refresh_all()
    hist = list(hist or []) + [{"role": "user", "content": text}]
    lines: list[str] = []
    answer = None

    if auto_gate:
        confirm = lambda m: True  # noqa: E731
    else:
        def confirm(m: str) -> bool:
            lines.append(f"⛔【人工门控】{m} —— 未勾选自动放行，本次拒绝执行")
            return False

    try:
        for ev in rt.chat_turn(text, confirm=confirm, allow_write=auto_gate):
            t = ev.get("t")
            if t == "trace":
                lines.append(f"· {ev['line']}")
            elif t == "plan":
                tag = "（局部重规划）" if ev.get("replan") else ""
                lines.append(f"**计划路径**{tag}（代价 {ev.get('cost')}）：{' → '.join(ev['path'])}")
            elif t == "step":
                mark = "✅" if ev.get("ok") else "❌"
                lines.append(f"{mark} [{ev.get('index')}] {ev.get('skill')}：{str(ev.get('output'))[:140]}")
            elif t == "gate":
                lines.append(f"🚧【门控】{ev.get('message')} → {'放行' if ev.get('allowed') else '拒绝'}")
            elif t == "done":
                ok = "✅" if ev.get("ok") else "❌"
                lines.append(f"{ok} 任务收尾（状态已写回图，可到「执行审计」核对）")
                answer = ev.get("answer")
            elif t == "answer":
                answer = ev.get("text")
    except Exception as e:  # noqa: BLE001
        answer = f"❌ 执行出错：{e}\n\n（可在「设置」页测试模型连通性；Mock 模式无需 API Key）"
    if answer:
        hist.append({"role": "assistant", "content": answer})
    trace = "### 🧭 运行轨迹\n" + ("\n".join(lines) or "（无）")
    return "", gr.update(value=None), hist, trace, *refresh_all()


# ---------------------------------------------------------------- 设置 ----

def _runtime_reload() -> None:
    """按当前激活配置方案重建运行时（图数据不变）。"""
    with _HOLDER["lock"]:
        s = load_settings()
        _HOLDER["rt"] = AgentRuntime(settings=s)
        _bootstrap(_HOLDER["rt"])


def on_model_selected(pid):
    """下拉框选模型 = 立即切换并持久化（唯一的日常操作，0 按钮）。"""
    data = set_active_profile(pid)
    p = next(x for x in data["profiles"] if x["id"] == data["active"])
    _runtime_reload()
    return (p.get("name") or p["id"], p.get("model", ""), p.get("base_url", ""),
            p.get("api_key", ""), float(p.get("temperature") or 0.3),
            f"✅ 已启用「{p.get('name') or p['id']}」（{p.get('model') or 'Mock'}），重启后自动加载。")


def on_use_model(pid):
    """启用所选模型：持久化 + 热重载（幂等）。"""
    data = set_active_profile(pid if pid else load_profiles()["active"])
    p = next(x for x in data["profiles"] if x["id"] == data["active"])
    _runtime_reload()
    return f"✅ 已启用「{p.get('name') or p['id']}」（{p.get('model') or 'Mock'}），重启后自动加载。"


def on_save_profile(name, model, base, key, temp):
    """保存 = 按名称新增或覆盖；保存的是当前启用方案时即时生效。"""
    pname = (name or "").strip()
    if not pname:
        return gr.update(), name, model, base, key, float(temp), "❌ 请先填「方案名称」"
    provider = "mock" if not (base or "").strip() else "custom"
    upsert_profile({"id": pname, "name": pname, "provider": provider, "model": (model or "").strip(),
                    "base_url": (base or "").strip(), "api_key": (key or "").strip(),
                    "temperature": float(temp)})
    data = load_profiles()
    note = "（当前启用，已即时生效）" if data["active"] == pname else f"（已保存；上方下拉选择即可启用）"
    return (gr.update(choices=[(x.get("name") or x["id"], x["id"]) for x in data["profiles"]],
                      value=data["active"]),
            name, model, base, key, float(temp), f"💾 已保存「{pname}」{note}")


def on_delete_profile(name, model, base, key, temp):
    pname = (name or "").strip()
    data = delete_profile(pname) if pname else load_profiles()
    p = next(x for x in data["profiles"] if x["id"] == data["active"])
    _runtime_reload()
    return (gr.update(choices=[(x.get("name") or x["id"], x["id"]) for x in data["profiles"]],
                      value=data["active"]),
            p.get("name") or p["id"], p.get("model", ""), p.get("base_url", ""),
            p.get("api_key", ""), float(p.get("temperature") or 0.3),
            f"🗑 已删除「{pname}」，当前启用「{p.get('name') or p['id']}」。")


def on_test_profile(name, model, base, key, temp):
    s = load_settings()
    s.provider = "mock" if not (base or "").strip() else "custom"
    s.model, s.base_url = (model or "").strip(), (base or "").strip()
    s.api_key, s.temperature = (key or "").strip(), float(temp)
    from .llm import make_llm

    try:
        out = make_llm(s).chat([{"role": "system", "content": "[角色:PING]"},
                                {"role": "user", "content": "回复两个字：连通"}], temperature=0.0)
        return f"✅ 连通（{s.model}）：{out[:40]}"
    except Exception as e:  # noqa: BLE001
        return f"❌ 失败：{e}"


# ---------------------------------------------------------------- 组装 ----

def build_demo() -> gr.Blocks:
    rt = get_rt()
    s = rt.settings
    with gr.Blocks(title="GNA · 图原生智能体", theme=gr.themes.Soft()) as demo:
        gr.Markdown("# 🕸 GNA · 图原生智能体运行时　`一切皆图 · 查找皆遍历 · 变更留痕`")
        with gr.Tabs() as tabs:
            with gr.Tab("💬 对话"):
                with gr.Row():
                    with gr.Column(scale=3):
                        chat = gr.Chatbot(type="messages", height=470, label="GNA",
                                          show_copy_button=True)
                        upl = gr.File(label="📎 上传文件 / 压缩包（可拖拽、可多选；文件夹请打包成 zip 上传）",
                                      file_count="multiple")
                        msg = gr.Textbox(show_label=False, placeholder=(
                            "试试：记住：张三是李四的同事 ｜ 你在图谱里记得什么 ｜ "
                            "帮我算一下 (365*3+17)/4 ｜ 写一份关于图神经网络的简报并验证 ｜ /任务 强制走图规划"))
                        with gr.Row():
                            send = gr.Button("发送", variant="primary")
                            clr = gr.Button("清空对话")
                        auto_gate = gr.Checkbox(value=True, label="自动确认写盘门控")
                    with gr.Column(scale=2):
                        trace = gr.Markdown("### 🧭 运行轨迹\n（发送消息后显示：召回 → 路由 → 工具/计划 → ΔW 写回）")
            with gr.Tab("🧠 图谱世界"):
                mem = gr.Markdown(mem_md())
                sk = gr.Markdown(skills_view_md())
                with gr.Accordion("🔍 记忆检索（激活扩散——沿关系边召回，不是关键词匹配）", open=False):
                    recall_q = gr.Textbox(label="输入主题（人名 / 文件名 / 你让它记过的任何概念）")
                    recall_btn = gr.Button("检索")
                recall_out = gr.Markdown("")
                gr.Markdown("> 🎨 想看整张图？另开终端运行 `gna graph viz`，浏览器打开生成的 HTML（可拖拽缩放）。")
            with gr.Tab("🕘 行为审计"):
                audit = gr.Markdown(audit_md())
                with gr.Accordion("原始 ΔW 事件（调试视图）", open=False):
                    raw = gr.Markdown(raw_audit_md())
                gr.Markdown("> 一切变更皆图上的事件：可回放（`gna graph events`）、可回滚（`gna memory rollback`）。")
            with gr.Tab("⚙️ 模型设置"):
                gr.Markdown("方案保存在 `~/.gna/models.json`，开机自动加载。")
                with gr.Row():
                    prof_dd = gr.Dropdown(
                        choices=[(p.get("name") or p["id"], p["id"]) for p in load_profiles()["profiles"]],
                        value=load_profiles()["active"], label="当前模型", scale=4)
                    use_b = gr.Button("启用", variant="primary", scale=1)
                status = gr.Markdown("")
                with gr.Accordion("添加 / 修改 / 删除模型", open=False):
                    with gr.Row():
                        name_tb = gr.Textbox(label="方案名称", scale=1)
                        model_tb = gr.Textbox(label="模型名", scale=1)
                    base = gr.Textbox(label="API 地址（留空 = 离线 Mock）",
                                      value="https://api.z.ai/api/paas/v4")
                    key = gr.Textbox(label="API Key", type="password")
                    temp = gr.Slider(0, 1, value=0.3, step=0.1, label="温度")
                    with gr.Row():
                        save_b = gr.Button("保存", variant="primary")
                        test_b = gr.Button("测试连通")
                        del_b = gr.Button("删除")

                with gr.Accordion("项目目录（让 Agent 直接改这个文件夹里的代码）", open=False):
                    proj_tb = gr.Textbox(value=s.project_root,
                                         label="项目根目录（绝对路径；留空 = 默认沙箱 ~/.gna/workspace）")
                    proj_btn = gr.Button("应用项目目录")
                proj_status = gr.Markdown("")

                def on_apply_project(path):
                    from pathlib import Path as _P

                    path = (path or "").strip()
                    if path and not _P(path).is_dir():
                        return f"❌ 目录不存在：{path}"
                    st2 = load_settings()
                    st2.project_root = path
                    save_settings(st2)
                    _runtime_reload()
                    ws = st2.resolved_workspace()
                    return f"✅ 已切换 Agent 工作沙箱根 → {ws}" + ("（项目模式）" if path else "（默认沙箱）")

                proj_btn.click(on_apply_project, [proj_tb], [proj_status])

            proj_btn.click(on_apply_project, [proj_tb], [proj_status])

            FORM = [name_tb, model_tb, base, key, temp]
            use_b.click(on_use_model, [prof_dd], [status])
            prof_dd.change(on_model_selected, [prof_dd], FORM + [status])
            save_b.click(on_save_profile, FORM, [prof_dd] + FORM + [status])
            test_b.click(on_test_profile, FORM, [status])
            del_b.click(on_delete_profile, FORM, [prof_dd] + FORM + [status])

        out_all = [msg, upl, chat, trace, mem, sk, audit, raw]
        send.click(on_send, [msg, upl, chat, auto_gate], out_all)
        msg.submit(on_send, [msg, upl, chat, auto_gate], out_all)
        clr.click(lambda: [], None, [chat])
        tabs.select(lambda: refresh_all(), None, [mem, sk, audit, raw])
        recall_btn.click(recall_view_md, [recall_q], [recall_out])
    return demo


def launch(host: str = "127.0.0.1", port: int = 7860, inbrowser: bool = True):
    demo = build_demo()
    demo.queue(max_size=16, default_concurrency_limit=1)
    demo.launch(server_name=host, server_port=port, inbrowser=inbrowser,
                allowed_paths=[str(_VIZ_DIR)], show_error=True)


if __name__ == "__main__":
    launch()
