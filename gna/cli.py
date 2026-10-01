"""GNA 命令行接口（第一公民界面）。

cmd 直接运行：  python -m gna <子命令> ...
pip install -e 后：gna <子命令> ...
"""
from __future__ import annotations

import argparse
import json
import sys
from typing import List, Optional

from .agent import AgentRuntime
from .config import PROVIDER_PRESETS, Settings, load_settings, save_settings
from .graph import RuntimeGraph
from .llm import make_llm
from .skills import BUILTIN_SKILLS, validate
from .tools import ToolContext, build_tools, dispatch


# ---------------------------------------------------------------- 工具函数 ----

def _print(*args):
    print(*args, flush=True)


def _make_runtime(settings: Optional[Settings] = None, storage: Optional[str] = None,
                  fresh: bool = False) -> AgentRuntime:
    s = settings or load_settings()
    if storage:
        s.storage_path = storage
    rt = AgentRuntime(settings=s)
    if fresh:
        rt.reset(keep_registry=False)
    return rt


def _confirm_interactive(message: str) -> bool:
    try:
        ans = input(f"{message}\n放行？[y/N] ").strip().lower()
    except EOFError:
        return False
    return ans in ("y", "yes", "是")


def _render_event(ev: dict) -> None:
    t = ev.get("t")
    if t == "trace":
        _print(f"  · {ev['line']}")
    elif t == "plan":
        tag = "（局部重规划）" if ev.get("replan") else ""
        _print(f"  计划路径{tag}（代价 {ev.get('cost')}）：{' -> '.join(ev['path'])}")
    elif t == "step_start":
        _print(f"  [{ev['index']}/{ev['total']}] 技能 {ev['skill']} 参数 {ev['params']}")
    elif t == "step":
        mark = "OK" if ev.get("ok") else "FAIL"
        out = str(ev.get("output", "")).replace("\n", " ")[:160]
        _print(f"      [{mark}] {out}")
    elif t == "gate":
        _print(f"  【门控】{ev.get('message', '')} -> {'放行' if ev.get('allowed') else '拒绝'}")
    elif t == "answer":
        _print(f"\nGNA> {ev['text']}\n")


# ---------------------------------------------------------------- 命令实现 ----

def cmd_chat(args) -> int:
    rt = _make_runtime(storage=args.storage)
    confirm = (lambda m: True) if args.yes else _confirm_interactive
    _print(f"GNA 图原生智能体（backend={rt.store.be.name}, llm={rt.settings.provider}/{rt.settings.model}）")
    _print("输入消息对话；/help 查看命令，/quit 退出。")
    history: List[dict] = []
    while True:
        try:
            line = input("你> ").strip()
        except (EOFError, KeyboardInterrupt):
            _print()
            break
        if not line:
            continue
        low = line.lower()
        if low in ("/quit", "/exit", "/q"):
            break
        if low in ("/help", "/?"):
            _print("  /help 帮助 | /graph 图统计 | /facts 当前断言 | /path A B 证据路径")
            _print("  /tools 工具列表 | /skills 技能校验 | /rollback 回滚上一步 | /reset 清空记忆 | /quit 退出")
            continue
        if low == "/graph":
            _print(json.dumps(rt.stats(), ensure_ascii=False, indent=2))
            continue
        if low == "/facts":
            for a in sorted(rt.store.assertions()):
                _print("  -", a)
            continue
        if low.startswith("/path"):
            parts = line.split()
            if len(parts) >= 3:
                a = rt.store.entity_link(parts[1], limit=1)
                b = rt.store.entity_link(parts[2], limit=1)
                p = rt.store.shortest_path(a[0], b[0]) if a and b else None
                _print("  " + (" -> ".join(p) if p else "不可达"))
            continue
        if low == "/tools":
            for name in sorted(rt.chat_tools):
                _print(f"  - {name}: {rt.chat_tools[name].desc}")
            continue
        if low == "/skills":
            _print(" ", rt.skill_report()["report"])
            continue
        if low == "/rollback":
            r = rt.store.rollback_last_step()
            _print("  " + (f"已回滚 {r['skill']}：失效 {len(r['undone'])} 个节点" if r else "无可回滚的执行步"))
            continue
        if low == "/reset":
            rt.reset()
            _print("  记忆已清空（技能/工具/约束注册结构保留）")
            continue
        if line.startswith("/"):
            _print(f"  未知命令 {line}（/help 查看帮助）")
            continue
        for ev in rt.chat_turn(line, confirm=confirm, history=history):
            _render_event(ev)
            if ev.get("t") == "answer":
                history.append({"role": "user", "content": line})
                history.append({"role": "assistant", "content": ev.get("text", "")})
    return 0


def cmd_ask(args) -> int:
    rt = _make_runtime(storage=args.storage)
    for ev in rt.chat_turn(args.text, confirm=lambda m: True):
        _render_event(ev)
    return 0


def cmd_run(args) -> int:
    rt = _make_runtime(storage=args.storage)
    confirm = (lambda m: True) if args.yes else _confirm_interactive
    goal = " ".join(args.goal)
    _print(f"目标：{goal}")
    for ev in rt.executor.run_task(goal, confirm=confirm):
        _render_event(ev)
    return 0


def cmd_demo(args) -> int:
    from .demo import run_demo

    return run_demo()


def cmd_graph(args) -> int:
    rt = _make_runtime(storage=args.storage)
    store: RuntimeGraph = rt.store
    if args.action == "stats":
        _print(json.dumps(store.stats(), ensure_ascii=False, indent=2))
    elif args.action == "nodes":
        rows = store.find(ntype=args.type, name_sub=args.sub, include_invalid=not args.valid_only)
        for n in rows[: args.limit]:
            inv = " [失效]" if n["attributes"].get("invalid") else ""
            _print(f"  {n['id']}  {str(n.get('data'))[:40]}{inv}")
        _print(f"  （共 {len(rows)} 个节点）")
    elif args.action == "edges":
        es = store.be.find_edges(label=args.label)
        for e in es[: args.limit]:
            _print(f"  {e['src']} -[{e['label']}]-> {e['dst']}")
        _print(f"  （共 {len(es)} 条边）")
    elif args.action == "neighbors":
        hits = store.entity_link(args.node, limit=1) or ([args.node] if store.be.get_node(args.node) else [])
        if not hits:
            _print("  未定位到节点")
            return 1
        n = 0
        for nid, ed in store.be.neighbors(hits[0], direction=args.direction):
            _print(f"  {hits[0]} -[{ed.get('label')}]-> {nid}")
            n += 1
            if n >= args.limit:
                break
        _print(f"  （{hits[0]} 的 {args.direction} 邻居 ≤{n}）")
    elif args.action == "path":
        a = store.entity_link(args.src, limit=1)
        b = store.entity_link(args.dst, limit=1)
        if not a or not b:
            _print("  实体定位失败")
            return 1
        p = store.shortest_path(a[0], b[0])
        _print("  " + (" -> ".join(p) if p else "不可达"))
    elif args.action == "events":
        n = 0
        for ep in store.walk_episode_chain():
            a = ep.get("attributes", {})
            _print(f"  #{a.get('seq')} [{a.get('ts')}] {a.get('op')} by {a.get('source')} :: {a.get('payload', '')[:100]}")
            n += 1
            if n >= args.limit:
                break
    elif args.action == "export":
        path = store.save(args.out or None)
        _print(f"  已导出 → {path}")
    elif args.action == "viz":
        path = _visualize(store, args.out)
        _print(f"  可视化已生成 → {path}" if path else "  可视化需要 pyvis：pip install pyvis")
    return 0


def _visualize(store: RuntimeGraph, out: Optional[str]) -> Optional[str]:
    try:
        from pyvis.network import Network
    except ImportError:
        return None
    net = Network(height="720px", directed=True, bgcolor="#ffffff",
                  font_color="#222222", cdn_resources="remote")
    color = {"entity": "#4C9AFF", "fact": "#36B37E", "turn": "#F6A609", "task": "#6554C0",
             "step": "#8777D9", "result": "#00B8D9", "skill": "#FF5630", "tool": "#FF8B00",
             "constraint": "#BF2600", "goal": "#5243AA", "episode": "#B3B5B3"}
    for nid, nd in store.be.nodes():
        t = nd.get("class_") or "?"
        inv = (nd.get("attributes") or {}).get("invalid")
        net.add_node(nid, label=str(nd.get("name") or nid)[:24], title=f"{nid}\n{nd.get('data') or ''}",
                     color=color.get(t, "#999999") if not inv else "#CCCCCC")
    for e in store.be.find_edges():
        net.add_edge(e["src"], e["dst"], title=e.get("label") or "")
    out = out or "gna_graph.html"
    net.write_html(out, open_browser=False)
    return out


def cmd_memory(args) -> int:
    rt = _make_runtime(storage=args.storage)
    store = rt.store
    if args.action == "add":
        turn = store.add_node("turn", f"回合{store._turn_seq + 1}", "CLI",
                              data=" ".join(args.text)[:200])
        store._turn_seq += 1
        from .extract import ingest

        stat = ingest(store, rt.llm, " ".join(args.text), turn_id=turn, source="CLI 写入")
        _print(f"  已入图：实体 {stat['entities']}、事实 {stat['facts']}、冲突失效 {stat['conflicts']}")
    elif args.action == "query":
        from .recall import recall_context

        _print(recall_context(store, " ".join(args.text), hops=args.hops))
    elif args.action == "facts":
        for a in sorted(store.assertions()):
            _print("  -", a)
        _print(f"  （当前世界状态共 {len(store.assertions())} 条断言）")
    elif args.action == "rollback":
        r = store.rollback_last_step()
        _print("  " + (f"已回滚 {r['skill']}：{len(r['undone'])} 个节点标记失效" if r else "无可回滚的执行步"))
    elif args.action == "clear":
        store.clear_memory(keep_registry=not args.purge)
        rt.executor.ensure_registry()
        _print("  记忆已清空" + ("" if not args.purge else "（含注册结构）"))
    return 0


def cmd_skill(args) -> int:
    rt = _make_runtime(storage=args.storage)
    if args.action == "list":
        for s in BUILTIN_SKILLS:
            gate = " [门控]" if s.gate else ""
            _print(f"  {s.name:<16} {s.title:<8} pre={s.precondition} eff={s.effect}{gate}")
    elif args.action == "show":
        s = next((x for x in BUILTIN_SKILLS if x.name == args.name), None)
        _print(json.dumps(s.__dict__, ensure_ascii=False, indent=2) if s else "  未找到技能")
    elif args.action == "validate":
        rep = validate(rt.store, BUILTIN_SKILLS)
        _print("  " + rep["report"])
        _print(f"  连通分量: {rep['components']}（最大 {rep['largest']}）孤立: {rep['isolated'] or '无'}")
        return 0 if rep["acyclic"] else 1
    elif args.action == "run":
        params = json.loads(args.json) if args.json else {}
        goal = " ".join(args.goal) if args.goal else ""
        if not goal:
            _print("  请给出任务描述（会自动规划到该技能）")
            return 1
        for ev in rt.executor.run_task(goal, params_hint=params, confirm=lambda m: True):
            _render_event(ev)
    return 0


def cmd_tool(args) -> int:
    rt = _make_runtime(storage=args.storage)
    ctx = ToolContext(store=rt.store, llm=rt.llm, workspace=rt.settings.resolved_workspace(),
                      source="CLI 工具调用")
    tools = build_tools(ctx)
    if args.action == "list":
        for name, t in sorted(tools.items()):
            _print(f"  {name:<16} [{t.perm}] {t.desc}")
    elif args.action == "call":
        t = tools.get(args.name)
        if not t:
            _print("  工具不存在")
            return 1
        argd = json.loads(args.json) if args.json else {}
        ok, out = dispatch(t, ctx, argd)
        _print(("  [OK] " if ok else "  [FAIL] ") + str(out))
        return 0 if ok else 1
    return 0


def cmd_llm(args) -> int:
    s = load_settings()
    if args.action == "info":
        _print(f"  provider={s.provider} model={s.model}")
        _print(f"  base_url={s.resolved_base_url() or '-'} key={'已配置' if s.resolved_api_key() else '未配置'}")
    elif args.action == "test":
        llm = make_llm(s)
        try:
            out = llm.chat([{"role": "system", "content": "[角色:PING] 回答一个词即可"},
                            {"role": "user", "content": "回复：连通"}], temperature=0.0)
            _print(f"  [OK] {type(llm).__name__} → {out[:80]}")
        except Exception as e:  # noqa: BLE001
            _print(f"  [FAIL] {e}")
            return 1
    elif args.action == "set":
        s.provider = args.provider or s.provider
        if args.provider and args.provider in PROVIDER_PRESETS:
            s.model = PROVIDER_PRESETS[args.provider]["models"][0]
        s.model = args.model or s.model
        s.base_url = args.base_url or s.base_url
        s.api_key = args.api_key or s.api_key
        save_settings(s)
        _print(f"  已保存：provider={s.provider} model={s.model}")
    return 0


# ---------------------------------------------------------------- 入口 ----

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="gna", description="GNA 图原生智能体运行时（一切皆图，查找皆遍历）")
    p.add_argument("--storage", help="图谱存储文件路径（默认 ~/.gna/runtime.json）")
    sub = p.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("chat", help="交互式 REPL")
    c.add_argument("--yes", action="store_true", help="人工门控自动放行")
    c.set_defaults(fn=cmd_chat)

    a = sub.add_parser("ask", help="单轮问答")
    a.add_argument("text")
    a.set_defaults(fn=cmd_ask)

    r = sub.add_parser("run", help="执行任务（图规划+执行器）")
    r.add_argument("goal", nargs="+")
    r.add_argument("--yes", action="store_true")
    r.set_defaults(fn=cmd_run)

    d = sub.add_parser("demo", help="一键演示")
    d.set_defaults(fn=cmd_demo)

    g = sub.add_parser("graph", help="图操作")
    g.add_argument("action", choices=["stats", "nodes", "edges", "neighbors", "path", "events", "export", "viz"])
    g.add_argument("--type", dest="type")
    g.add_argument("--sub")
    g.add_argument("--label")
    g.add_argument("--valid-only", action="store_true")
    g.add_argument("--direction", default="both", choices=["in", "out", "both"])
    g.add_argument("--limit", type=int, default=30)
    g.add_argument("--out")
    g.add_argument("node", nargs="?")
    g.add_argument("src", nargs="?")
    g.add_argument("dst", nargs="?")
    g.set_defaults(fn=cmd_graph)

    m = sub.add_parser("memory", help="图记忆操作")
    m.add_argument("action", choices=["add", "query", "facts", "rollback", "clear"])
    m.add_argument("text", nargs="*")
    m.add_argument("--hops", type=int, default=2)
    m.add_argument("--purge", action="store_true")
    m.set_defaults(fn=cmd_memory)

    sk = sub.add_parser("skill", help="技能图")
    sk.add_argument("action", choices=["list", "show", "validate", "run"])
    sk.add_argument("name", nargs="?")
    sk.add_argument("--json", dest="json")
    sk.add_argument("goal", nargs="*")
    sk.set_defaults(fn=cmd_skill)

    t = sub.add_parser("tool", help="原子工具")
    t.add_argument("action", choices=["list", "call"])
    t.add_argument("name", nargs="?")
    t.add_argument("--json", dest="json")
    t.set_defaults(fn=cmd_tool)

    l = sub.add_parser("llm", help="LLM 配置与连通性")
    l.add_argument("action", choices=["info", "test", "set"])
    l.add_argument("--provider")
    l.add_argument("--model")
    l.add_argument("--base-url", dest="base_url")
    l.add_argument("--api-key", dest="api_key")
    l.set_defaults(fn=cmd_llm)
    return p


def main(argv: Optional[List[str]] = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.fn(args)
    except KeyboardInterrupt:
        _print("\n(中断)")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
