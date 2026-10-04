"""人话视图层：把世界模型图渲染成用户能看懂的页面（Gradio / Streamlit 共用）。

设计原则：先回答"它知道什么 / 它刚做了什么"，内部计数与原始载荷降级为调试折叠。
"""
from __future__ import annotations

from collections import defaultdict

from .graph import RuntimeGraph
from .recall import recall_subgraph, format_context
from .skills import BUILTIN_SKILLS


# ------------------------------------------------------------ 断言翻译 ----

_ASSERT_ZH = [
    ("read:", "已读取文件 {x}"),
    ("written:", "已写入文件 {x}"),
    ("ran:", "已运行程序 {x}"),
    ("summary:", "已完成《{x}》要点摘要"),
    ("draft:", "已起草简报《{x}》"),
    ("verified:", "已验证简报《{x}》（证据闭包完整）"),
]

_ASSERT_FIXED = {
    "facts:ready": "已把文件知识写入图谱",
    "session:ready": "会话就绪",
    "calc:done": "完成过一次计算",
    "fs:listed": "浏览过目录",
}


def zh_assertion(a: str) -> str:
    for pre, tpl in _ASSERT_ZH:
        if a.startswith(pre):
            return tpl.format(x=a[len(pre):])
    return _ASSERT_FIXED.get(a, a)


def _ts(n: dict) -> str:
    return str((n.get("attributes") or {}).get("ts") or "")[5:16]  # MM-DD HH:MM


# ------------------------------------------------------------ 图谱世界 ----

def hero_md(store: RuntimeGraph) -> str:
    st = store.stats()
    ents = st["by_type"].get("entity", 0)
    facts = st["by_type"].get("fact", 0)
    return (f"**{ents} 个实体 · {facts} 条事实 · {len(BUILTIN_SKILLS)} 项技能** ｜ "
            f"全部在一张图上：图引擎 `{st['backend']}`，变更留痕 {st['events']} 条（可回放、可回滚）")


def knowledge_md(store: RuntimeGraph, cap: int = 14) -> str:
    """知识卡片：人话 + 溯源（来源与时间），分组展示；失效历史折叠。"""
    kn = [n for n in store.find("fact")
          if (n["attributes"].get("kind") == "knowledge") and not n["attributes"].get("invalid")]
    invalid = [n for n in store.find("fact")
               if (n["attributes"].get("kind") == "knowledge") and n["attributes"].get("invalid")]
    done = [a for a in sorted(store.assertions())
            if "｜" not in a and a not in ("facts:ready", "session:ready")]

    lines = []
    for n in kn[-cap:]:
        a = n["attributes"]
        lines.append(f"- **{a.get('subject')}** 的 {a.get('relation')} 是 **{a.get('object')}**"
                     f"　〈{a.get('source', '?')} {_ts(n)}〉")
    if not lines:
        lines.append("-（还没有知识——跟它说「记住：XX 是 XX」就有了）")
    body = "\n".join(lines)
    more = f"\n\n*……共 {len(kn)} 条，仅显示最近 {cap} 条*" if len(kn) > cap else ""

    done_lines = [f"- ✅ {zh_assertion(a)}" for a in done[:12]]
    done_body = "\n".join(done_lines) or "-（暂无）"

    inv_body = ""
    if invalid:
        inv_lines = [f"- {n['attributes'].get('subject')} 的 {n['attributes'].get('relation')}（被新事实取代，"
                     f"原因：{n['attributes'].get('invalid_reason', '冲突更新')}，{_ts(n)}）" for n in invalid[-6:]]
        inv_body = ("\n\n<details><summary>🕰 已被更新的旧事实（历史保留，未删除）</summary>\n\n"
                    + "\n".join(inv_lines) + "\n\n</details>")

    return (f"### 🧠 它知道什么\n\n**知识记忆**（每条都带来源与时间，沿边可追溯）\n\n{body}{more}\n\n"
            f"**已完成的事项**（状态断言，规划器的依据）\n\n{done_body}{inv_body}")


def skills_md(store: RuntimeGraph) -> str:
    """技能清单：能力一目了然，健康度来自执行轨迹（图上真值）。"""
    lines = ["| 技能 | 说明 | 前置 → 效果 | 健康度 |",
             "|---|---|---|---|"]
    for s in BUILTIN_SKILLS:
        node = store.be.get_node(f"skill:{s.name}") or {}
        a = node.get("attributes") or {}
        sr = float(a.get("success_rate", s.success_rate))
        rc = int(a.get("run_count") or 0)
        gate = "🚧 " if s.gate else ""
        pre = "、".join(s.precondition).replace("{", "⟨").replace("}", "⟩") or "—"
        eff = "、".join(s.effect).replace("{", "⟨").replace("}", "⟩") or "—"
        lines.append(f"| {gate}**{s.title}** `{s.name}` | {s.description} | {pre} → {eff} | "
                     f"{int(sr * 100)}% / {rc} 次 |")
    return ("### 🔧 它会做什么（技能图：前置/效果断言机器可验证）\n\n"
            + "\n".join(lines)
            + "\n\n🚧 = 执行前需人工门控；健康度 = 成功率 / 累计执行次数（失败会自动降权，规划时绕开）")


def recall_md(store: RuntimeGraph, query: str, hops: int = 2) -> str:
    """记忆检索预览：激活扩散——沿关系边扩散，不是关键词匹配。"""
    query = (query or "").strip()
    if not query:
        return "（输入主题后点「检索」——例如人名、文件名、你让它记过的任何概念）"
    act, ids = recall_subgraph(store, query, hops=hops, max_nodes=30)
    if not ids:
        return (f"### 🔍 记忆检索：「{query}」\n\n"
                f"记忆里没有找到相关内容。可以先告诉它一些事实（「记住：…」），"
                f"或者确认项目模式/上传文件是否已处理。")
    body = format_context(store, ids, max_lines=20)
    return (f"### 🔍 记忆检索：「{query}」\n\n"
            f"从 **{len(ids)} 个节点**的邻域沿关系边扩散召回（不是关键词匹配，是图遍历）：\n\n{body}")


# ------------------------------------------------------------ 行为审计 ----

def timeline_md(store: RuntimeGraph, limit: int = 40) -> str:
    """行为时间线：只保留有意义的动作（对话/工具/任务/门控/失效/回滚），注册噪音过滤。"""
    items: list[tuple[str, str]] = []

    for n in store.find("turn"):
        role = (n["attributes"] or {}).get("role", "user")
        who = "💬 **你**" if role == "user" else "🤖 **GNA**"
        items.append((_ts(n), f"{who}：{str(n.get('data'))[:70]}"))
    for n in store.find("step"):
        a = n["attributes"]
        mark = {"ok": "▶️", "failed": "❌", "rolled_back": "↩️"}.get(a.get("status"), "▸")
        out = str(a.get("output", "")).replace("\n", " ")[:60]
        items.append((_ts(n), f"{mark} **技能 {a.get('skill')}** {'成功' if a.get('status') == 'ok' else a.get('status', '')}：{out}"))
    for n in store.find("task"):
        a = n["attributes"]
        icon = {"done": "✅", "failed": "❌", "cancelled": "🚫"}.get(a.get("status"), "📋")
        items.append((_ts(n), f"{icon} **任务**（{a.get('status')}）：{str(a.get('goal'))[:56]}"))
    for ep in store.walk_episode_chain():
        a = ep.get("attributes", {})
        if a.get("op") == "tool_call":
            try:
                import json as _json
                pl = _json.loads(a.get("payload") or "{}")
            except Exception:
                pl = {}
            mark = "📝" if pl.get("tool") == "write_file" else ("▶️" if str(pl.get("tool", "")).startswith("run") else "🔧")
            items.append((a.get("ts", ""), f"{mark} **工具 {pl.get('tool')}**：{str(pl.get('path') or pl)[:60]}"
                                           f"{'（成功）' if pl.get('ok') else ''}"))
        elif a.get("op") == "invalidate_node":
            import json as _json
            try:
                pl = _json.loads(a.get("payload") or "{}")
            except Exception:
                pl = {}
            node = str(pl.get("node", "")).split(":", 1)[-1]
            reason = str(pl.get("reason", "") or "").strip()
            items.append((a.get("ts", ""), f"⚠️ **事实失效** `{node}`（历史保留）"
                                           f"{'——' + reason if reason else ''}"))
        elif a.get("op") == "rollback":
            items.append((a.get("ts", ""), f"⏪ **回滚**：{str(a.get('payload'))[:60]}"))

    items.sort(key=lambda x: x[0], reverse=True)
    if not items:
        return ("### 🕘 它刚做了什么\n\n（还没有动作记录——跟它聊一句、传个文件或跑个任务，"
                "这里就会出现带时间戳的行为时间线）")
    lines = [f"- {t}　{line}" for t, line in items[:limit]]
    more = f"\n\n*……更早的 {len(items) - limit} 条在图上（`gna graph events` 可回放全部）*" if len(items) > limit else ""
    return ("### 🕘 它刚做了什么（行为时间线，新 → 旧）\n\n"
            "每一条都对应图上的 episode 事件，可回放、可回滚。\n\n" + "\n".join(lines) + more)


def raw_events_md(store: RuntimeGraph, limit: int = 60) -> str:
    """原始 ΔW 事件（调试视图）。"""
    eps = list(store.walk_episode_chain())[-limit:]
    lines = [f"**#{e['attributes'].get('seq')}** `{e['attributes'].get('op')}` "
             f"〈{e['attributes'].get('source')}〉 {str(e['attributes'].get('payload'))[:80]}"
             for e in reversed(eps)]
    body = "\n".join(lines) or "（暂无事件）"
    return f"#### ΔW 事件链（最近 {len(eps)} 条，新 → 旧）\n\n{body}"
