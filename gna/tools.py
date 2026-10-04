"""原子工具层（教材 8.2.4 工具即图节点）。

每个 Tool 注册后会在图上生成 tool 节点（schema 携带在节点属性里）；
技能是工具子图上的宏节点（skills.py 经 uses_tool 边连接）。
所有文件工具受沙箱约束：只能访问 settings.workspace 之内（约束图在搜索前生效）。
"""
from __future__ import annotations

import ast
import json
import math
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, Dict, Optional, Tuple

from .graph import RuntimeGraph
from .llm import BaseLLM

ToolResult = Tuple[bool, str]  # (ok, output)


@dataclass
class ToolContext:
    store: RuntimeGraph
    llm: BaseLLM
    workspace: Path
    source: str = "工具调用"
    step_id: str = ""   # 当前执行步节点（工具产出经 produced 边挂到该节点）
    task_id: str = ""   # 当前任务节点（任务级证据汇聚用）

    def resolve(self, path: str) -> Path:
        """沙箱路径解析：相对路径基于 workspace，绝对路径必须落在 workspace 内。"""
        p = Path(path)
        root = self.workspace.resolve()
        full = (p if p.is_absolute() else root / p).resolve()
        if root not in full.parents and full != root:
            raise PermissionError(f"沙箱约束：路径越界 {path}（仅允许 workspace 内）")
        return full


@dataclass
class Tool:
    name: str
    desc: str
    params: dict  # {"param": {"type": "string", "required": True, "desc": "..."}}
    fn: Callable[..., ToolResult]
    perm: str = "normal"  # normal | sandbox-write | llm


# ------------------------------------------------------------ 基础工具 ----

def _t_list_dir(ctx: ToolContext, path: str = ".", recursive: bool = False) -> ToolResult:
    root = ctx.resolve(path)
    if not root.exists():
        return False, f"目录不存在：{path}"
    if not recursive:
        items = sorted(root.iterdir(), key=lambda p: (p.is_file(), p.name))
        lines = [f"{'[目录]' if it.is_dir() else '[文件]'} {_rel(ctx, it)}" for it in items[:60]]
        return True, "\n".join(lines) or "（空目录）"
    lines, count = [], 0
    for p in sorted(root.rglob("*")):
        if p.is_dir() or any(part.startswith(".") or part == "__pycache__" for part in p.parts):
            continue
        lines.append(f"{_rel(ctx, p)}  ({p.stat().st_size}B)")
        count += 1
        if count >= 100:
            lines.append("…（超过 100 个文件，已截断；可用 find_in_files 精确搜索）")
            break
    return True, "\n".join(lines) or "（空目录）"


def _t_find_in_files(ctx: ToolContext, pattern: str, glob: str = "*.py",
                     max_results: int = 30) -> ToolResult:
    """在沙箱根内全局搜索代码行（项目模式定位脚本用）。"""
    import re as _re

    root = ctx.workspace.resolve()
    try:
        rx = _re.compile(pattern)
    except _re.error as e:
        return False, f"正则不合法：{e}"
    hits, truncated = [], False
    for p in sorted(root.rglob(glob or "*")):
        if not p.is_file() or p.stat().st_size > 300_000:
            continue
        if any(part.startswith((".", "__")) for part in p.parts):
            continue
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        per_file = 0
        for i, line in enumerate(text.splitlines(), 1):
            if rx.search(line):
                hits.append(f"{_rel(ctx, p)}:{i}: {line.strip()[:140]}")
                per_file += 1
                if per_file >= 5 or len(hits) >= max_results:
                    if len(hits) >= max_results:
                        truncated = True
                    break
        if truncated:
            break
    body = "\n".join(hits) or "（无匹配）"
    return True, body + ("…（已截断）" if truncated else "")


def _t_read_file(ctx: ToolContext, path: str) -> ToolResult:
    p = ctx.resolve(path)
    if not p.is_file():
        return False, f"文件不存在：{path}"
    text = p.read_text(encoding="utf-8", errors="replace")[:20000]
    return True, text


def _t_write_file(ctx: ToolContext, path: str, content: str) -> ToolResult:
    p = ctx.resolve(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    rel = _rel(ctx, p)
    ctx.store.add_assertion(f"written:{rel}", source=ctx.source)
    ctx.store._emit("tool_call", ctx.source, {"tool": "write_file", "path": rel, "bytes": len(content)})
    return True, f"已写入 {len(content)} 字符 → {rel}"


def _rel(ctx: ToolContext, p: Path) -> str:
    """workspace 内相对路径（POSIX 风格）。"""
    return p.relative_to(ctx.workspace.resolve()).as_posix()


def _t_run_python(ctx: ToolContext, path: str, timeout: float = 60) -> ToolResult:
    """运行 workspace 内的 Python 脚本（子进程执行，捕获 stdout/stderr，超时保护）。"""
    import subprocess
    import sys as _sys

    p = ctx.resolve(path)
    if not p.is_file():
        return False, f"文件不存在：{path}（先用 write_file 写入）"
    try:
        proc = subprocess.run([_sys.executable, str(p)], cwd=str(ctx.workspace.resolve()),
                              capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=float(timeout))
        out = (proc.stdout or "") + (("\n[stderr] " + proc.stderr) if proc.stderr.strip() else "")
        ok = proc.returncode == 0
        rel = _rel(ctx, p)
        ctx.store.add_assertion(f"ran:{rel}", source=ctx.source)
        ctx.store._emit("tool_call", ctx.source,
                        {"tool": "run_python", "path": rel, "ok": ok, "returncode": proc.returncode})
        return ok, (out.strip() or f"(退出码 {proc.returncode}，无输出)")[:4000]
    except subprocess.TimeoutExpired:
        return False, f"执行超时（>{timeout}s），已终止"
    except Exception as e:  # noqa: BLE001
        return False, f"执行异常：{type(e).__name__}: {e}"


def _t_run_code(ctx: ToolContext, code: str, filename: str = "", timeout: float = 60) -> ToolResult:
    """写入并运行一段 Python 代码（写程序一步到位：落盘 scripts/ 下 → 子进程执行）。"""
    fname = (filename or f"code_{datetime.now().strftime('%H%M%S')}.py").strip()
    if not fname.endswith(".py"):
        fname += ".py"
    rel_path = f"scripts/{fname}" if not fname.startswith("scripts/") else fname
    ok_w, out_w = _t_write_file(ctx, rel_path, code)
    if not ok_w:
        return False, out_w
    ok_r, out_r = _t_run_python(ctx, rel_path, timeout=timeout)
    return ok_r, f"{out_w}\n--- 运行输出 ---\n{out_r}"


_SAFE_FUNCS = {"sqrt": math.sqrt, "sin": math.sin, "cos": math.cos, "tan": math.tan,
               "log": math.log, "log2": math.log2, "log10": math.log10,
               "exp": math.exp, "abs": abs, "pow": pow, "floor": math.floor, "ceil": math.ceil}
_SAFE_CONSTS = {"pi": math.pi, "e": math.e}


def safe_calc(expr: str) -> float:
    """AST 白名单安全求值。"""

    def _ev(node):
        if isinstance(node, ast.Expression):
            return _ev(node.body)
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
            return node.value
        if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Add, ast.Sub, ast.Mult,
                                                                ast.Div, ast.Pow, ast.Mod, ast.FloorDiv)):
            a, b = _ev(node.left), _ev(node.right)
            return {ast.Add: a + b, ast.Sub: a - b, ast.Mult: a * b, ast.Div: a / b,
                    ast.Pow: a ** b, ast.Mod: a % b, ast.FloorDiv: a // b}[type(node.op)]
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
            return +_ev(node.operand) if isinstance(node.op, ast.UAdd) else -_ev(node.operand)
        if isinstance(node, ast.Name) and node.id in _SAFE_CONSTS:
            return _SAFE_CONSTS[node.id]
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in _SAFE_FUNCS:
            return _SAFE_FUNCS[node.func.id](*[_ev(a) for a in node.args])
        raise ValueError(f"不允许的表达式成分：{ast.dump(node)[:60]}")

    return float(_ev(ast.parse(expr.strip(), mode="eval")))


def _t_calculate(ctx: ToolContext, expression: str) -> ToolResult:
    try:
        val = safe_calc(expression)
        r = int(val) if abs(val - round(val)) < 1e-9 and abs(val) < 1e15 else round(val, 8)
        return True, f"{expression} = {r}"
    except Exception as e:  # noqa: BLE001
        return False, f"计算失败：{e}"


def _t_current_time(ctx: ToolContext) -> ToolResult:
    n = datetime.now()
    return True, f"当前时间：{n.strftime('%Y-%m-%d %H:%M:%S')}（{n.strftime('%A')}）"


# ------------------------------------------------------------ 图操作工具 ----

def _t_query_graph(ctx: ToolContext, query: str) -> ToolResult:
    from .recall import recall_context

    return True, recall_context(ctx.store, query, hops=2, max_nodes=32)


def _t_add_fact(ctx: ToolContext, subject: str, relation: str, object: str) -> ToolResult:
    fid, conflict = ctx.store.add_fact_triple(subject.strip(), relation.strip() or "相关",
                                              object.strip(), ctx.source)
    msg = f"已写入事实节点 {fid}"
    if conflict:
        msg += f"（冲突：旧事实 {conflict} 已标记失效，历史保留）"
    return True, msg


def _t_find_path(ctx: ToolContext, source: str, target: str) -> ToolResult:
    a = ctx.store.entity_link(source, limit=1)
    b = ctx.store.entity_link(target, limit=1)
    if not a or not b:
        return False, f"实体定位失败：{source if not a else target} 不在图中"
    path = ctx.store.shortest_path(a[0], b[0])
    if not path:
        return False, "图中不存在可达路径"
    parts = [ctx.store.be.get_node(path[0]).get("name", path[0])]
    for i in range(len(path) - 1):
        eds = ctx.store.be.get_edges(path[i], path[i + 1])
        label = (eds[0]["label"] if eds else "?") or "?"
        parts.append(f"-[{label}]->")
        parts.append(ctx.store.be.get_node(path[i + 1]).get("name", path[i + 1]))
    return True, "证据路径：" + " ".join(parts)


def _t_graph_stats(ctx: ToolContext) -> ToolResult:
    st = ctx.store.stats()
    return True, json.dumps(st, ensure_ascii=False)


# ------------------------------------------------------------ LLM 技能工具 ----

def _t_summarize(ctx: ToolContext, text: str, topic: str = "") -> ToolResult:
    out = ctx.llm.chat([{"role": "system", "content": "[角色:SUMMARIZE] 提取要点，输出 3-5 条要点列表。"},
                        {"role": "user", "content": text}], temperature=0.2)
    return True, out.strip()


def _t_extract_facts(ctx: ToolContext, text: str, source_note: str = "") -> ToolResult:
    from .extract import ingest

    stat = ingest(ctx.store, ctx.llm, text, source=f"技能 extract_facts {source_note}".strip())
    # 知识事实沿 produced 边挂到当前执行步（证据链：任务→步骤→事实）
    for fid in stat.get("fact_ids", []):
        if ctx.step_id:
            ctx.store.add_edge(ctx.step_id, fid, "produced", "执行器")
    return True, (f"入图完成：实体 {stat['entities']} 条、事实 {stat['facts']} 条"
                  f"（冲突失效 {stat['conflicts']}）")


def _t_draft_report(ctx: ToolContext, topic: str) -> ToolResult:
    """起草报告：沿图汇聚证据（主题邻域激活 ∪ 本任务步骤 produced 的事实节点）。"""
    facts, refs = [], []
    seen: set = set()
    act = ctx.store.spreading_activation(ctx.store.entity_link(topic), hops=2, max_nodes=40)
    for nid in act:
        nd = ctx.store.be.get_node(nid) or {}
        a = nd.get("attributes", {})
        if nd.get("class_") == "fact" and a.get("kind") == "knowledge" and not a.get("invalid"):
            seen.add(nid)
    # 任务级证据：task → has_step → produced → fact（知识事实）
    if ctx.task_id:
        for step_id, _ in ctx.store.be.out_edges(ctx.task_id):
            ed_list = ctx.store.be.get_edges(ctx.task_id, step_id)
            if not ed_list or ed_list[0]["label"] != "has_step":
                continue
            for nid, _e in ctx.store.be.out_edges(step_id):
                pe = ctx.store.be.get_edges(step_id, nid)
                if not pe or pe[0]["label"] != "produced":
                    continue
                nd = ctx.store.be.get_node(nid) or {}
                a = nd.get("attributes", {})
                if nd.get("class_") == "fact" and a.get("kind") == "knowledge" and not a.get("invalid"):
                    seen.add(nid)
    for nid in sorted(seen):
        a = (ctx.store.be.get_node(nid) or {}).get("attributes", {})
        facts.append(f"{a.get('subject')} 的 {a.get('relation')} 是 {a.get('object')} 【F:{nid}】")
        refs.append(nid)
    steps = [n for n in ctx.store.find("step") if not n["attributes"].get("invalid")
             and n["attributes"].get("status") == "ok"]
    md = [f"# 简报：{topic}", "", f"> 由 GNA 图原生智能体起草于 {datetime.now().strftime('%Y-%m-%d %H:%M')}"
          f"；证据均回溯到世界模型图节点。", "", "## 要点", ""]
    summary_lines, summary_result = [], ""
    for n in reversed(steps[-8:]):
        if n["attributes"].get("skill") == "summarize_text":
            summary_lines.append(n["attributes"].get("output", ""))
            if not summary_result:  # 要点部分的直接依据 = 摘要步骤的产出节点
                for nid, _e in ctx.store.be.out_edges(n["id"]):
                    pe = ctx.store.be.get_edges(n["id"], nid)
                    if pe and pe[0]["label"] == "produced" \
                            and (ctx.store.be.get_node(nid) or {}).get("class_") == "result":
                        summary_result = nid
                        break
    if summary_lines:
        md.append(summary_lines[-1][:1200])
        md.append("")
    if summary_result and summary_result not in refs:
        facts.append(f"要点依据摘要步骤产出节点 【F:{summary_result}】")
        refs.append(summary_result)
    md += ["## 图谱证据", ""] + ([f"- {x}" for x in facts[:20]] or ["- （图谱中暂无该主题知识事实）"]) + [""]
    content = "\n".join(md)
    out = ctx.resolve(f"reports/{topic}.md")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(content, encoding="utf-8")
    return True, f"报告已起草：reports/{out.name}（引用图谱证据 {len(refs)} 条）"


def _t_verify_report(ctx: ToolContext, topic: str) -> ToolResult:
    """验证报告：每个【F:...】引用必须能回溯到图上有效节点（fact/result 均可，证据闭包检查）。"""
    p = ctx.resolve(f"reports/{topic}.md")
    if not p.is_file():
        return False, f"报告不存在：reports/{topic}.md"
    text = p.read_text(encoding="utf-8")
    refs = re.findall(r"【F:((?:entity|fact|result):[^\]】]+)】", text)
    if not refs:
        return False, "报告不含任何图谱证据引用，验证不通过（证据闭包为空）"
    bad = [r for r in refs if not (ctx.store.be.get_node(r)
           and not (ctx.store.be.get_node(r).get("attributes") or {}).get("invalid"))]
    if bad:
        return False, f"验证不通过：{len(bad)} 条引用失效或不存在 → {bad[:3]}"
    return True, f"验证通过：{len(refs)} 条证据引用全部可回溯到有效图谱节点（证据闭包完整）"


# ------------------------------------------------------------ 注册表 ----

def build_tools(ctx: ToolContext) -> Dict[str, Tool]:
    T = lambda name, desc, params, fn, perm="normal": Tool(name, desc, params, fn, perm)  # noqa: E731
    tools = [
        T("list_dir", "列出目录内容（recursive=true 递归浏览整个项目）", {"path": {"type": "string", "required": False, "desc": "相对路径，默认 ."}, "recursive": {"type": "boolean", "required": False}}, _t_list_dir),
        T("find_in_files", "在项目内全局搜索代码行（正则）", {"pattern": {"type": "string", "required": True}, "glob": {"type": "string", "required": False}, "max_results": {"type": "number", "required": False}}, _t_find_in_files),
        T("read_file", "读取 workspace 内文本文件", {"path": {"type": "string", "required": True, "desc": "相对路径"}}, _t_read_file),
        T("write_file", "写入 workspace 内文本文件（沙箱）", {"path": {"type": "string", "required": True}, "content": {"type": "string", "required": True}}, _t_write_file, perm="sandbox-write"),
        T("run_python", "运行 workspace 内的 Python 脚本（子进程，60s 超时）", {"path": {"type": "string", "required": True}, "timeout": {"type": "number", "required": False}}, _t_run_python, perm="sandbox-write"),
        T("run_code", "写入并运行一段 Python 代码（一步到位，输出回传）", {"code": {"type": "string", "required": True}, "filename": {"type": "string", "required": False}, "timeout": {"type": "number", "required": False}}, _t_run_code, perm="sandbox-write"),
        T("calculate", "安全算术求值", {"expression": {"type": "string", "required": True}}, _t_calculate),
        T("current_time", "当前时间", {}, _t_current_time),
        T("query_graph", "图记忆检索（激活扩散召回子图）", {"query": {"type": "string", "required": True}}, _t_query_graph),
        T("add_fact", "写入知识三元组（写入门控）", {"subject": {"type": "string", "required": True}, "relation": {"type": "string", "required": True}, "object": {"type": "string", "required": True}}, _t_add_fact),
        T("find_path", "两实体间最短证据路径", {"source": {"type": "string", "required": True}, "target": {"type": "string", "required": True}}, _t_find_path),
        T("graph_stats", "图统计", {}, _t_graph_stats),
        T("summarize", "LLM 摘要", {"text": {"type": "string", "required": True}, "topic": {"type": "string", "required": False}}, _t_summarize, perm="llm"),
        T("extract_facts", "文本知识入图", {"text": {"type": "string", "required": True}, "source_note": {"type": "string", "required": False}}, _t_extract_facts, perm="llm"),
        T("draft_report", "起草主题简报（引用图谱证据）", {"topic": {"type": "string", "required": True}}, _t_draft_report),
        T("verify_report", "验证报告证据闭包", {"topic": {"type": "string", "required": True}}, _t_verify_report),
    ]
    return {t.name: t for t in tools}


def sync_tool_nodes(store: RuntimeGraph, tools: Dict[str, Tool], source: str = "系统注册") -> None:
    """工具即图节点：把注册表同步为图上 tool 节点（属性携带 schema）。"""
    for t in tools.values():
        store.add_node("tool", t.name, source,
                       props={"desc": t.desc, "params": json.dumps(t.params, ensure_ascii=False),
                              "perm": t.perm})


def dispatch(tool: Tool, ctx: ToolContext, args: dict) -> ToolResult:
    """按 schema 校验并调用工具。"""
    for pname, meta in tool.params.items():
        if meta.get("required") and pname not in args:
            return False, f"缺少必填参数：{pname}"
    try:
        return tool.fn(ctx, **args)
    except PermissionError as e:
        return False, str(e)
    except TypeError as e:
        return False, f"参数不匹配：{e}"
    except Exception as e:  # noqa: BLE001
        return False, f"工具异常：{type(e).__name__}: {e}"
