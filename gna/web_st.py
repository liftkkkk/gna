"""GNA Streamlit 前端 —— 与 Gradio 共享同一 headless 内核的第二个视图层。

运行：gna web-st   （或 streamlit run gna/web_st.py）
数据与 CLI / Gradio 共享 ~/.gna/runtime.json（建议同一时间只用一个前端写入；
v0.2 增量持久化与单写者落地后可多前端并行）。
"""
from __future__ import annotations

import streamlit as st

st.set_page_config(page_title="GNA · 图原生智能体", page_icon="🕸", layout="wide")


# ------------------------------------------------------------ 内核单例 ----

@st.cache_resource(show_spinner="正在启动 GNA 内核（GX 图引擎 + 技能图注册）…")
def get_rt():
    from gna.agent import AgentRuntime
    from gna.config import load_settings

    rt = AgentRuntime(settings=load_settings())
    _bootstrap(rt)
    return rt


def _bootstrap(rt) -> None:
    """与 Gradio 版一致：示例笔记按缺失补写，示例知识仅空库写入。项目模式下不动用户文件夹。"""
    if rt.settings.project_root:
        return
    from gna.demo import DEMO_FACTS, DEMO_NOTES

    ws = rt.settings.resolved_workspace()
    for fname, content in DEMO_NOTES.items():
        p = ws / "notes" / fname
        if not p.exists():
            p.write_text(content, encoding="utf-8")
    if rt.store.be.node_count() <= 30:
        for s_, r_, o_ in DEMO_FACTS:
            rt.store.add_fact_triple(s_, r_, o_, "示例数据")


# ------------------------------------------------------------ 四个页面 ----

def page_chat(auto_gate: bool) -> None:
    rt = get_rt()
    if "messages" not in st.session_state:
        st.session_state.messages = []

    ups = st.file_uploader("📎 上传文件 / 压缩包（可多选、可拖拽；文件夹请打包成 zip 上传）",
                           accept_multiple_files=True)
    if "sent_keys" not in st.session_state:
        st.session_state.sent_keys = set()

    trace_ph = st.empty()
    for m in st.session_state.messages:
        with st.chat_message(m["role"]):
            st.markdown(m["content"])

    prompt = st.chat_input(
        "试试：上传论文让我读 ｜ 记住：张三是李四的同事 ｜ 写个程序输出斐波那契前10项并运行 ｜ 写一份简报并验证")
    if prompt is None:
        return

    from gna.uploads import extract_archive, is_archive, save_upload

    notes: list[str] = []
    for up in (ups or []):
        k = (up.name, up.size)
        if k in st.session_state.sent_keys:
            continue
        st.session_state.sent_keys.add(k)
        dest = save_upload(up.name, up.getvalue())
        if is_archive(dest.name):
            extracted, _ = extract_archive(dest)
            notes.append(f"{dest.name}（压缩包，已自动解压 {len(extracted)} 个文件 → inbox/{dest.stem}_extracted/）")
        else:
            notes.append(dest.name)
    if notes:
        prompt = ((prompt.strip() or "请处理我上传的这些文件") +
                  "\n\n【用户上传的文件（已存入 inbox/，可用 read_pdf 读论文、unzip 解压、read_file 读文本）】\n- "
                  + "\n- ".join(notes))
    if not prompt.strip():
        return

    st.session_state.messages.append({"role": "user", "content": prompt})
    with st.chat_message("user"):
        st.markdown(prompt)

    lines: list[str] = []
    answer = None

    if auto_gate:
        def confirm(msg: str) -> bool:
            return True
    else:
        def confirm(msg: str) -> bool:
            lines.append(f"⛔【人工门控】{msg} —— 未勾选自动确认，本次拒绝执行")
            return False

    with st.chat_message("assistant"):
        answer_ph = st.empty()
        with st.expander("🧭 运行轨迹", expanded=False):
            trace_ph = st.empty()
        for ev in rt.chat_turn(prompt, confirm=confirm, allow_write=auto_gate):
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
                lines.append(("✅" if ev.get("ok") else "❌") + " 任务收尾（ΔW 已写回图）")
                answer = ev.get("answer")
            elif t == "answer":
                answer = ev.get("text")
            trace_ph.markdown("\n".join(lines) or "…")
        answer_ph.markdown(answer or "（无输出）")

    st.session_state.messages.append({"role": "assistant", "content": answer or "（无输出）"})


def page_graph() -> None:
    from gna.web import facts_md, kg_preview_md, stats_md

    st.markdown(stats_md())
    st.markdown(facts_md())
    st.markdown(kg_preview_md())


def page_audit() -> None:
    from gna.web import events_md

    st.markdown(events_md())
    st.caption("一切变更皆事件：回放 = 沿 next 边遍历；CLI `gna memory rollback` 可补偿回滚。")


def page_models() -> None:
    from gna.config import (delete_profile, load_profiles, set_active_profile,
                            upsert_profile)

    data = load_profiles()
    ids = [p["id"] for p in data["profiles"]]
    names = {p["id"]: (p.get("name") or p["id"]) for p in data["profiles"]}

    c1, c2 = st.columns([4, 1])
    sel = c1.selectbox("当前模型", ids, index=ids.index(data["active"]),
                       format_func=lambda i: names.get(i, i))
    if c2.button("启用", type="primary", use_container_width=True):
        set_active_profile(sel)
        st.cache_resource.clear()
        st.rerun()
    p = next(x for x in load_profiles()["profiles"] if x["id"] == load_profiles()["active"])
    st.success(f"✅ 已启用「{names.get(p['id'])}」（{p.get('model') or 'Mock'}），重启后自动加载。"
               f" ｜ 配置文件 `~/.gna/models.json`")

    with st.form("模型方案", border=True):
        st.caption("保存 = 按名称新增或覆盖；API 地址留空 = 离线 Mock。修改现有方案：名称填成一样的再保存。")
        name = st.text_input("方案名称")
        model = st.text_input("模型名")
        base = st.text_input("API 地址（Base URL，OpenAI 兼容）")
        key = st.text_input("API Key", type="password")
        temp = st.slider("温度", 0.0, 1.0, 0.3, 0.1)
        b1, b2, b3 = st.columns(3)
        do_save = b1.form_submit_button("保存", type="primary")
        do_test = b2.form_submit_button("测试连通")
        do_del = b3.form_submit_button("删除")

    if do_save:
        pname = name.strip()
        if not pname:
            st.error("请先填「方案名称」")
        else:
            upsert_profile({"id": pname, "name": pname,
                            "provider": "mock" if not base.strip() else "custom",
                            "model": model.strip(), "base_url": base.strip(),
                            "api_key": key.strip(), "temperature": float(temp)})
            note = "已即时生效" if load_profiles()["active"] == pname else "在上方选择即可启用"
            st.success(f"💾 已保存「{pname}」（{note}）")
    if do_test:
        from gna.config import load_settings
        from gna.llm import make_llm

        s = load_settings()
        s.provider = "mock" if not base.strip() else "custom"
        s.model, s.base_url = model.strip(), base.strip()
        s.api_key, s.temperature = key.strip(), float(temp)
        try:
            out = make_llm(s).chat([{"role": "system", "content": "[角色:PING]"},
                                    {"role": "user", "content": "回复两个字：连通"}], temperature=0.0)
            st.success(f"✅ 连通（{s.model}）：{out[:40]}")
        except Exception as e:  # noqa: BLE001
            st.error(f"❌ 失败：{e}")
    if do_del:
        target = name.strip() or sel
        delete_profile(target)
        st.cache_resource.clear()
        st.rerun()


def page_project() -> None:
    from gna.config import load_settings, save_settings

    st.caption("项目模式：Agent 的读/写/运行沙箱根切到你的项目文件夹（可浏览、修改、运行其中的代码）。")
    path = st.text_input("项目根目录（绝对路径，留空 = 默认沙箱）",
                         value=load_settings().project_root)
    if st.button("应用项目目录", type="primary"):
        import pathlib as _pl

        path = path.strip()
        if path and not _pl.Path(path).is_dir():
            st.error(f"目录不存在：{path}")
        else:
            s = load_settings()
            s.project_root = path
            save_settings(s)
            st.cache_resource.clear()
            st.rerun()


def main() -> None:
    st.title("🕸 GNA · 图原生智能体运行时")
    st.caption("`一切皆图 · 查找皆遍历 · 变更留痕` ｜ 同一 headless 内核，Gradio / Streamlit 双前端")
    page = st.sidebar.radio("页面", ["💬 对话", "🌐 图谱世界", "📜 执行审计", "⚙️ 模型设置",
                                      "📁 项目目录"], label_visibility="collapsed")
    auto_gate = st.sidebar.checkbox("自动确认写盘门控", value=True)
    if page == "💬 对话":
        page_chat(auto_gate)
    elif page == "🌐 图谱世界":
        page_graph()
    elif page == "📜 执行审计":
        page_audit()
    elif page == "📁 项目目录":
        page_project()
    else:
        page_models()


if __name__ == "__main__":
    main()
