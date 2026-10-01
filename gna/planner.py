"""图规划（教材 8.3）：路径即计划。

起点 = 当前断言集 W_t（图上有效 fact 状态节点），终点 = 目标断言集；
每步扩展「前置模板全部可满足」的技能；代价 = cost × (2 - success_rate)（健康度进启发，案例 8-3）；
约束图在搜索前裁剪（8.4）：被 deny 约束的技能直接不进搜索空间；
执行期失败 → success_rate 减半 → 局部重规划（executor 调本模块重新搜路）。
"""
from __future__ import annotations

import heapq
import itertools
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set, Tuple

from .skills import BUILTIN_SKILLS, PLACEHOLDER, Skill, fill, match, state_satisfies, unify


@dataclass
class Plan:
    goal: List[str]
    path: List[Tuple[str, Dict[str, str]]] = field(default_factory=list)  # [(skill_name, params)]
    cost: float = 0.0
    ok: bool = False
    reason: str = ""


def _skill_health(skill: Skill) -> float:
    return skill.cost * (2.0 - max(0.05, min(1.0, skill.success_rate)))


class GraphPlanner:
    def __init__(self, skills: Optional[List[Skill]] = None):
        self.skills: Dict[str, Skill] = {s.name: s for s in (skills or BUILTIN_SKILLS)}

    # ---- 约束图：搜索前裁剪（规则前缀 deny_skill: 的约束封锁技能；restrict/gate 在执行层生效）----
    def denied_skills(self, store=None) -> Set[str]:
        if store is None:
            return set()
        denied = set()
        for e in store.be.find_edges(label="constrains"):
            cnode = store.be.get_node(e["src"]) or {}
            rule = str((cnode.get("attributes") or {}).get("rule", ""))
            if rule.startswith("deny_skill"):
                denied.add((e["dst"] or "").split(":", 1)[-1])
        return denied

    def applicable(self, skill: Skill, state: Set[str],
                   params_all: Optional[Dict[str, Dict[str, str]]] = None) -> bool:
        """前置可满足性。参数耦合前置（如 summary:{topic} / read:{path}）在目标参数已知时，
        只认「同一参数值具体化后」的命中——防止别的话题/别的文件的断言串扰满足。
        参数查找：先本技能参数映射，再全局合并（read_file.path 可满足 summarize 的 read:{path}）。"""
        hint = dict((params_all or {}).get(skill.name) or {})
        for m in (params_all or {}).values():  # 全局兜底：read_file.path 可满足 summarize 的 read:{path}
            for k, v in (m or {}).items():
                hint.setdefault(k, v)
        for p in skill.precondition:
            if PLACEHOLDER.search(p):
                hinted = False
                for ph in PLACEHOLDER.findall(p):
                    val = hint.get(ph)
                    if val is not None:
                        hinted = True  # 参数已知 → 只认具体化命中
                        concrete = PLACEHOLDER.sub(val, p)
                        # 规划期抽象状态里的通配断言（read:*）可覆盖具体前置
                        if not any(str(a) == concrete or (str(a).endswith("*") and concrete.startswith(str(a)[:-1]))
                                   for a in state):
                            return False
                        break
                if not hinted:  # 抽象规划（无参数提示）→ 通配/union 均可
                    if not any(match(p, a) for a in state):
                        return False
                continue
            if not state_satisfies(state, p):
                return False
        return True

    def goal_missing(self, state: Set[str], goal: List[str]) -> List[str]:
        return [g for g in goal if not any(match(a, g) for a in state)]

    def plan(self, goal: List[str], state: Set[str], store=None,
             params_all: Optional[Dict[str, Dict[str, str]]] = None,
             max_states: int = 4000, max_depth: int = 10) -> Plan:
        """A* 搜索：状态 = frozenset(断言)，返回技能节点序列 = 计划。"""
        goal = [g for g in goal if g]
        if not goal:
            return Plan(goal=goal, ok=True, reason="空目标")
        missing0 = self.goal_missing(state, goal)
        if not missing0:
            return Plan(goal=goal, ok=True, path=[], reason="目标已满足")
        denied = self.denied_skills(store)
        skills = [s for s in self.skills.values() if s.name not in denied]

        def abstract_state(st: Set[str]) -> frozenset:
            return frozenset(st)

        def apply(skill: Skill, st: Set[str]) -> Set[str]:
            new = set(st)
            for eff in skill.effect:
                params = next((u for g in goal if (u := unify(eff, g))), {})
                new.add(fill(eff, params, abstract=True))
            return new

        def h(st: Set[str]) -> float:
            return 0.5 * len(self.goal_missing(st, goal))

        start = abstract_state(state)
        counter = itertools.count()
        heap = [(h(set(start)), 0.0, next(counter), start, [])]
        best_g: Dict[frozenset, float] = {start: 0.0}
        expansions = 0
        while heap:
            f, g, _, st, path = heapq.heappop(heap)
            if self.goal_missing(set(st), goal) == []:
                return Plan(goal=goal, path=path, cost=g, ok=True)
            expansions += 1
            if expansions > max_states or len(path) >= max_depth:
                continue
            for sk in skills:
                if not self.applicable(sk, set(st), params_all):
                    continue
                ns = apply(sk, set(st))
                nf = frozenset(ns)
                ng = g + _skill_health(sk)
                if ng < best_g.get(nf, float("inf")) - 1e-9:
                    best_g[nf] = ng
                    heapq.heappush(heap, (ng + h(ns), ng, next(counter), nf,
                                          path + [(sk.name, self._params_for(sk, goal, path))]))
        return Plan(goal=goal, ok=False,
                    reason=f"无可达路径（已扩展 {expansions} 个状态；目标 {goal}）"
                           + (f"；被约束封锁技能：{sorted(denied)}" if denied else ""))

    def _params_for(self, skill: Skill, goal: List[str], path) -> Dict[str, str]:
        """规划期为技能解析能从目标断言合一出来的参数（执行期再与 goal_map.params 合并）。"""
        params: Dict[str, str] = {}
        for eff in skill.effect:
            for g in goal:
                u = unify(eff, g)
                if u:
                    params.update(u)
        return params

    # ---- 目标映射兜底（无 LLM 时把中文请求映射到技能效果断言）----
    def goal_from_keywords(self, text: str) -> Tuple[List[str], bool, Dict[str, Dict[str, str]]]:
        import re

        t = text.strip()
        m = re.search(r"关于(.+?)(的|，|。|,|$)", t)
        topic = (m.group(1) if m else "").strip()
        if re.search(r"报告|简报|总结|摘要|要点", t) and topic:
            return ([f"summary:{topic}", f"draft:{topic}", f"verified:{topic}"],
                    True,
                    {"read_file": {}, "summarize_text": {"topic": topic},
                     "draft_report": {"topic": topic}, "verify_report": {"topic": topic}})
        if re.search(r"记住|写入|记录", t):
            return (["session:ready"], False, {})
        return ([], False, {})
