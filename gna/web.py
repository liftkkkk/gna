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

_COLOR = {"entity": "#4C9AFF", "fact": "#36B37E", "turn": "#F6A609", "task": "#6554C0",
          "step": "#8777D9", "result": "#00B8D9", "skill": "#FF5630", "tool": "#FF8B00",
          "constraint": "#BF2600", "goal": "#5243AA", "episode": "#B3B5B3"}


def stats_md() -> str:
    st = get_rt().stats()
    types = "、".join(f"{k} {v}" for k, v in sorted(st["by_type"].items(), key=lambda x: -x[1]))
    return (f"**后端** {st['backend']}　**节点** {st['nodes']}　**边** {st['edges']}　"
            f"**断言** {st['assertions']}　**事件** {st['events']}　**失效** {st['invalid']}\n\n"
            f"节点分布：{types}")


def facts_md() -> str:
    acts = sorted(get_rt().store.assertions())
    body = "\n".join(f"- `{a}`" for a in acts[:60]) or "（空）"
    return f"### 当前世界状态（{len(acts)} 条有效断言）\n{body}"


def kg_preview_md() -> str:
    """图结构轻量预览（纯文本，零重组件）。交互式网页版：CLI `gna graph viz`。"""
    rt = get_rt()
    lines: list[str] = []
    for nid, nd in rt.store.be.nodes():
        t = nd.get("class_") or "?"
        if t not in ("entity", "skill", "constraint", "tool"):
            continue
        name = str(nd.get("name") or nid)
        inv = " ⚠️已失效" if (nd.get("attributes") or {}).get("invalid") else ""
        icon = {"entity": "🔷", "skill": "🔥", "tool": "🛠", "constraint": "⛔"}.get(t, "•")
        lines.append(f"{icon} **{name}**{inv}")
        cnt = 0
        for m, ed in rt.store.be.out_edges(nid):
            mt = (rt.store.be.get_node(m) or {})
            if mt.get("class_") in ("episode",):
                continue
            lines.append(f"　　-{ed.get('label')}→ {mt.get('name', m)}")
            cnt += 1
            if cnt >= 6:
                lines.append("　　…")
                break
        if len(lines) > 80:
            break
    body = "\n".join(lines) or "（空）"
    return (f"### 图结构预览（节点 {rt.store.be.node_count()}，仅列 对象/技能/工具/约束 及其出边）\n\n"
            + body.replace("\n", "  \n")
            + "\n\n> 🎨 交互式网页版图谱：另开终端运行 `gna graph viz`，用浏览器打开生成的 gna_graph.html。")


def events_md(limit: int = 40) -> str:
    eps = list(get_rt().store.walk_episode_chain())[-limit:]
    lines = [f"**#{e['attributes'].get('seq')}** `{e['attributes'].get('op')}` "
             f"〈{e['attributes'].get('source')}〉 {str(e['attributes'].get('payload'))[:70]}"
             for e in reversed(eps)]
    body = "\n".join(lines) or "（暂无事件）"
    return f"### ΔW 事件链（最近 {len(eps)} 条，新→旧）\n\n{body}"


def refresh_all():
    return kg_preview_md(), stats_md(), facts_md(), events_md()


# ---------------------------------------------------------------- 对话 ----

def on_send(text: str, hist: list, auto_gate: bool):
    rt = get_rt()
    text = (text or "").strip()
    if not text:
        return "", hist or [], gr.update(), *refresh_all()
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
    return "", hist, trace, *refresh_all()


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
                        msg = gr.Textbox(show_label=False, placeholder=(
                            "试试：记住：张三是李四的同事 ｜ 你在图谱里记得什么 ｜ "
                            "帮我算一下 (365*3+17)/4 ｜ 写一份关于图神经网络的简报并验证 ｜ /任务 强制走图规划"))
                        with gr.Row():
                            send = gr.Button("发送", variant="primary")
                            clr = gr.Button("清空对话")
                        auto_gate = gr.Checkbox(value=True, label="自动确认写盘门控")
                    with gr.Column(scale=2):
                        trace = gr.Markdown("### 🧭 运行轨迹\n（发送消息后显示：召回 → 路由 → 工具/计划 → ΔW 写回）")
            with gr.Tab("🌐 图谱世界"):
                kg = gr.Markdown(kg_preview_md())
                stats = gr.Markdown(stats_md())
                facts = gr.Markdown(facts_md())
            with gr.Tab("📜 执行审计（ΔW 事件链）"):
                audit = gr.Markdown(events_md())
                gr.Markdown("> 一切变更皆事件：回放 = 沿 next 边遍历；CLI `gna memory rollback` 可补偿回滚。")
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

        out_all = [msg, chat, trace, kg, stats, facts, audit]
        send.click(on_send, [msg, chat, auto_gate], out_all)
        msg.submit(on_send, [msg, chat, auto_gate], out_all)
        clr.click(lambda: [], None, [chat])
        tabs.select(lambda: refresh_all(), None, [kg, stats, facts, audit])
    return demo


def launch(host: str = "127.0.0.1", port: int = 7860, inbrowser: bool = True):
    demo = build_demo()
    demo.queue(max_size=16, default_concurrency_limit=1)
    demo.launch(server_name=host, server_port=port, inbrowser=inbrowser,
                allowed_paths=[str(_VIZ_DIR)], show_error=True)


if __name__ == "__main__":
    launch()
