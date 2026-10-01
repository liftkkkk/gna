"""技能图 Skill Graph（教材 8.2）：S = (S, E_dep, θ)。

- 技能节点：前置断言模板 / 效果断言模板 / 参数槽 / 代价 / 成功率（健康度 θ）/ 门控 / 版本；
- enables 边：由「效果模板 unify 前置模板」自动推导（可机器验证的依赖）；
- 技能 = 工具子图上的宏节点（uses_tool 边连接原子工具，工具即图节点）；
- 断言语法：`前缀:具体值` 或 `前缀:*`（通配）；效果模板含 {param} 占位符。
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set, Tuple

PLACEHOLDER = re.compile(r"\{(\w+)\}")


@dataclass
class Skill:
    name: str
    title: str
    description: str
    params: dict = field(default_factory=dict)     # {"path": {"type": "string", "required": True}}
    precondition: List[str] = field(default_factory=list)  # 断言模板（支持 前缀:* 通配）
    effect: List[str] = field(default_factory=list)        # 断言模板（支持 {param} 占位）
    tool: str = ""                                 # 原子工具名（空 = 纯结构技能）
    bindings: dict = field(default_factory=dict)   # 工具参数绑定：{"text": "@read_file"} = 取该技能上一步产出
    gate: str = ""                                 # 非空 = 人工介入门控说明（8.4）
    cost: float = 1.0
    success_rate: float = 0.95
    version: str = "1.0"


def fill(template: str, params: Dict[str, str], abstract: bool = False) -> str:
    """效果模板填参。abstract=True 时占位符替换为 *（规划期抽象状态用）。"""
    def rep(m):
        key = m.group(1)
        if key in params and str(params[key]):
            return str(params[key])
        return "*" if abstract else m.group(0)
    return PLACEHOLDER.sub(rep, template)


def match(pattern: str, assertion: str) -> bool:
    """前置模式是否被某断言满足：精确相等 / 模式含 {ph} 可 unify / 尾部 * 通配。"""
    if pattern == assertion:
        return True
    if "{" in pattern:
        return unify(pattern, assertion) is not None
    if pattern.endswith("*") and assertion.startswith(pattern[:-1]):
        return True
    return False


def unify(template: str, assertion: str) -> Optional[Dict[str, str]]:
    """效果模板 ↔ 具体断言的合一：同字面前缀且占位符能对上 → 返回参数。"""
    segs = PLACEHOLDER.split(template)  # [lit, ph, lit, ..., lit]
    pos, params = 0, {}
    for i, seg in enumerate(segs):
        if i % 2 == 0:  # 字面段
            if not assertion.startswith(seg, pos):
                return None
            pos += len(seg)
        else:  # 占位段
            nxt = segs[i + 1] if i + 1 < len(segs) else ""
            end = assertion.find(nxt, pos) if nxt else len(assertion)
            if end < pos:
                return None
            params[seg] = assertion[pos:end]
            pos = end
    return params


def state_satisfies(state: Set[str], pattern: str) -> bool:
    return any(match(pattern, a) for a in state)


def derive_enables(skills: List[Skill]) -> List[Tuple[str, str]]:
    """s_i --enables--> s_j：s_i 的某效果模板可满足 s_j 的某前置模板。"""
    edges = []
    for a in skills:
        for b in skills:
            if a.name == b.name:
                continue
            for eff in a.effect:
                for pre in b.precondition:
                    if _template_satisfies(eff, pre):
                        edges.append((a.name, b.name))
                        break
                else:
                    continue
                break
    return edges


def _template_satisfies(effect_tpl: str, pre_tpl: str) -> bool:
    """效果模板（可含 {ph}）是否满足前置模板（精确或 前缀:*）。"""
    if pre_tpl.endswith("*"):
        base = pre_tpl[:-1]
        if "{" in effect_tpl:
            lit = PLACEHOLDER.split(effect_tpl)[0]
            return lit.startswith(base) or base.startswith(lit)
        return effect_tpl.startswith(base)
    if "{" in effect_tpl:
        lit = PLACEHOLDER.split(effect_tpl)[0]
        if "{" not in pre_tpl:
            return False
        pre_lit = PLACEHOLDER.split(pre_tpl)[0]
        return lit == pre_lit and len(PLACEHOLDER.findall(effect_tpl)) == len(PLACEHOLDER.findall(pre_tpl))
    return effect_tpl == pre_tpl


# ------------------------------------------------------------ 内置技能 ----

BUILTIN_SKILLS: List[Skill] = [
    Skill("session_start", "会话就绪", "初始化运行时会话（所有技能的前置）",
          effect=["session:ready"], tool="", cost=0.1),
    Skill("list_dir", "列出目录", "列出 workspace 目录内容",
          precondition=["session:ready"], effect=["fs:listed"], tool="list_dir"),
    Skill("read_file", "读文件", "读取 workspace 内文本文件",
          params={"path": {"type": "string", "required": True, "desc": "相对路径"}},
          precondition=["session:ready"], effect=["read:{path}"], tool="read_file"),
    Skill("write_file", "写文件", "写入 workspace 内文本文件",
          params={"path": {"type": "string", "required": True},
                  "content": {"type": "string", "required": True}},
          precondition=["session:ready"], effect=["written:{path}"], tool="write_file",
          gate="写入文件为不可逆动作（沙箱内），执行前需人工确认", cost=1.5),
    Skill("calculate", "计算", "安全算术求值",
          params={"expression": {"type": "string", "required": True}},
          precondition=["session:ready"], effect=["calc:done"], tool="calculate"),
    Skill("current_time", "查时间", "获取当前时间",
          precondition=["session:ready"], effect=[], tool="current_time"),
    Skill("query_graph", "检索图谱", "图记忆检索（激活扩散）",
          params={"query": {"type": "string", "required": True}},
          precondition=["session:ready"], effect=[], tool="query_graph"),
    Skill("add_fact", "写事实", "写入知识三元组（写入门控）",
          params={"subject": {"type": "string", "required": True},
                  "relation": {"type": "string", "required": True},
                  "object": {"type": "string", "required": True}},
          precondition=["session:ready"], effect=[], tool="add_fact"),
    Skill("find_path", "找证据路径", "两实体间最短路径解释",
          params={"source": {"type": "string", "required": True},
                  "target": {"type": "string", "required": True}},
          precondition=["session:ready"], effect=[], tool="find_path"),
    Skill("summarize_text", "摘要", "对已读文件做要点摘要",
          params={"topic": {"type": "string", "required": True}},
          precondition=["session:ready", "read:{path}"], effect=["summary:{topic}"],
          tool="summarize", bindings={"text": "@read_file"}, cost=1.2),
    Skill("extract_facts", "知识入图", "把已读文本中的知识事实写入图谱",
          precondition=["session:ready", "read:{path}"], effect=["facts:ready"],
          tool="extract_facts", bindings={"text": "@read_file"}, cost=1.2),
    Skill("draft_report", "起草简报", "汇聚图谱证据起草主题简报（写盘为不可逆动作）",
          params={"topic": {"type": "string", "required": True}},
          precondition=["session:ready", "summary:{topic}", "facts:ready"],
          effect=["draft:{topic}"],
          tool="draft_report", gate="报告将写入 workspace/reports/（不可逆动作），执行前需人工确认",
          cost=1.5),
    Skill("verify_report", "验证简报", "核对简报证据闭包（引用 → 图谱有效节点）",
          params={"topic": {"type": "string", "required": True}},
          precondition=["session:ready", "draft:{topic}"], effect=["verified:{topic}"],
          tool="verify_report", cost=1.0),
]


# ------------------------------------------------------------ 图同步 ----

def sync_skills(store, skills: List[Skill], constraints: Optional[List[dict]] = None,
                source: str = "系统注册") -> dict:
    """把技能/约束/工具边写入图：skill 节点 + requires/has_effect/enables/uses_tool/constrains。"""
    from .tools import sync_tool_nodes  # 局部导入避免循环

    for sk in skills:
        store.add_node("skill", sk.name, source,
                       props={"title": sk.title, "desc": sk.description,
                              "params": json.dumps(sk.params, ensure_ascii=False),
                              "precondition": json.dumps(sk.precondition, ensure_ascii=False),
                              "effect": json.dumps(sk.effect, ensure_ascii=False),
                              "gate": sk.gate, "cost": sk.cost,
                              "success_rate": sk.success_rate, "version": sk.version,
                              "tool": sk.tool})
    for a, b in derive_enables(skills):
        store.add_edge(f"skill:{a}", f"skill:{b}", "enables", source)
    for sk in skills:
        if sk.tool:
            store.add_edge(f"skill:{sk.name}", f"tool:{sk.tool}", "uses_tool", source)
        for pre in sk.precondition:  # 前置/效果模板落为 tpl 节点（独立命名空间，不计入状态断言）
            store.add_node("fact", pre, source, nid=f"tpl:{pre}", props={"kind": "template"})
            store.add_edge(f"skill:{sk.name}", f"tpl:{pre}", "requires", source)
        for eff in sk.effect:
            store.add_node("fact", eff, source, nid=f"tpl:{eff}", props={"kind": "template"})
            store.add_edge(f"skill:{sk.name}", f"tpl:{eff}", "has_effect", source)
    for c in (constraints or []):
        store.add_node("constraint", c["name"], source,
                       props={"rule": c.get("rule", ""), "scope": c.get("scope", "")})
        for t in c.get("targets", []):
            store.add_edge(f"constraint:{c['name']}", t, "constrains", source)
    return {"skills": len(skills),
            "enables": len(derive_enables(skills)),
            "constraints": len(constraints or [])}


def validate(store, skills: List[Skill]) -> dict:
    """8.8 验证：连通（无孤立技能）且无环。"""
    edges = derive_enables(skills)
    adj: Dict[str, List[str]] = {s.name: [] for s in skills}
    und: Dict[str, Set[str]] = {s.name: set() for s in skills}
    for a, b in edges:
        adj[a].append(b)
        und[a].add(b)
        und[b].add(a)
    # 无环（Kahn）
    indeg = {n: 0 for n in adj}
    for a, b in edges:
        indeg[b] += 1
    queue = [n for n, d in indeg.items() if d == 0]
    seen = 0
    while queue:
        n = queue.pop()
        seen += 1
        for m in adj[n]:
            indeg[m] -= 1
            if indeg[m] == 0:
                queue.append(m)
    # 连通分量
    comps, visited = [], set()
    for n in und:
        if n in visited:
            continue
        stack, comp = [n], set()
        while stack:
            x = stack.pop()
            if x in comp:
                continue
            comp.add(x)
            stack.extend(und[x] - comp)
        visited |= comp
        comps.append(sorted(comp))
    return {"skills": len(skills), "edges": len(edges),
            "acyclic": seen == len(skills),
            "components": len(comps), "largest": max(len(c) for c in comps),
            "isolated": [c[0] for c in comps if len(c) == 1],
            "report": f"skills={len(skills)} edges={len(edges)} "
                      f"acyclic={seen == len(skills)} components={len(comps)}"}
