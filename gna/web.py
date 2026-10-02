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
from .config import PROVIDER_PRESETS, Settings, load_settings, save_settings
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
    """装载示例数据（幂等）：示例笔记按文件缺失补写；示例知识仅在空库时写入。"""
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


def on_seed():
    _bootstrap(get_rt())
    return refresh_all()


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
        for ev in rt.chat_turn(text, confirm=confirm):
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

def on_apply(provider, model, base_url, api_key, temperature):
    s = load_settings()
    s.provider, s.model, s.base_url = provider, model, base_url.strip()
    s.api_key = api_key.strip()
    s.temperature = float(temperature)
    save_settings(s)
    with _HOLDER["lock"]:
        _HOLDER["rt"] = AgentRuntime(settings=s)
        _bootstrap(_HOLDER["rt"])
    return f"✅ 已切换：{provider} / {model}（运行时已重载，图数据不变）", *refresh_all()


def on_test(provider, model, base_url, api_key, temperature):
    s = load_settings()
    s.provider, s.model, s.base_url = provider, model, base_url.strip()
    s.api_key = api_key.strip()
    s.temperature = float(temperature)
    from .llm import make_llm

    try:
        out = make_llm(s).chat([{"role": "system", "content": "[角色:PING]"},
                                {"role": "user", "content": "回复两个字：连通"}], temperature=0.0)
        return f"✅ 连通：{out[:40]}"
    except Exception as e:  # noqa: BLE001
        return f"❌ 失败：{e}"


# ---------------------------------------------------------------- 组装 ----

def build_demo() -> gr.Blocks:
    rt = get_rt()
    s = rt.settings
    with gr.Blocks(title="GNA · 图原生智能体", theme=gr.themes.Soft()) as demo:
        gr.Markdown("# 🕸 GNA · 图原生智能体运行时　`一切皆图 · 查找皆遍历 · 变更留痕`")
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
                    auto_gate = gr.Checkbox(value=True,
                                            label="人工门控自动放行（取消后写盘类动作将被拒绝并在轨迹说明）")
                with gr.Column(scale=2):
                    trace = gr.Markdown("### 🧭 运行轨迹\n（发送消息后显示：召回 → 路由 → 工具/计划 → ΔW 写回）")
        with gr.Tab("🌐 图谱世界"):
            with gr.Row():
                refresh = gr.Button("🔄 刷新视图")
                seed = gr.Button("📦 载入示例数据")
            kg = gr.Markdown(kg_preview_md())
            stats = gr.Markdown(stats_md())
            facts = gr.Markdown(facts_md())
        with gr.Tab("📜 执行审计（ΔW 事件链）"):
            audit = gr.Markdown(events_md())
            gr.Markdown("> 一切变更皆事件：回放 = 沿 next 边遍历；CLI `gna memory rollback` 可补偿回滚。")
        with gr.Tab("⚙️ 设置（LLM 接入）"):
            with gr.Row():
                prov = gr.Dropdown(choices=list(PROVIDER_PRESETS), value=s.provider,
                                   label="提供商", scale=1)
                model = gr.Textbox(value=s.model, label="模型", scale=1)
            base = gr.Textbox(value=s.resolved_base_url(), label="Base URL（兼容 OpenAI 协议）")
            key = gr.Textbox(value=s.api_key, label="API Key（仅存本机 ~/.gna/config.json）",
                             type="password")
            temp = gr.Slider(0, 1, value=s.temperature, step=0.1, label="温度")
            with gr.Row():
                test = gr.Button("🔌 测试连通")
                apply = gr.Button("💾 保存并重载运行时", variant="primary")
            status = gr.Markdown("")
            test.click(on_test, [prov, model, base, key, temp], [status])
            apply.click(on_apply, [prov, model, base, key, temp], [status, kg, stats, facts, audit])

        out_all = [msg, chat, trace, kg, stats, facts, audit]
        send.click(on_send, [msg, chat, auto_gate], out_all)
        msg.submit(on_send, [msg, chat, auto_gate], out_all)
        clr.click(lambda: [], None, [chat])
        refresh.click(lambda: refresh_all(), None, [kg, stats, facts, audit])
        seed.click(on_seed, None, [kg, stats, facts, audit])
    return demo


def launch(host: str = "127.0.0.1", port: int = 7860, inbrowser: bool = True):
    demo = build_demo()
    demo.queue(max_size=16, default_concurrency_limit=1)
    demo.launch(server_name=host, server_port=port, inbrowser=inbrowser,
                allowed_paths=[str(_VIZ_DIR)], show_error=True)


if __name__ == "__main__":
    launch()
