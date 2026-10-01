"""执行器：把计划落成图上的执行轨迹（教材 8.3 反馈回路 + 8.4 脚手架 + 8.8 审计口径）。

每个执行步（step 节点）：
  1) 前置断言重核（世界可能已变；不过 → 局部重规划，不从零再来）；
  2) 约束检查（deny 约束在执行前拦截）；
  3) 人工门控（gate 非空的技能挂起，经 confirm_callback 决定放行/取消）；
  4) 工具调用（工具即图节点，经 uses_tool 边定位）；
  5) ΔW 写回：效果断言 fact 节点 + result 节点 + produced/logged 边 + episode 事件链；
  6) 失败：success_rate 减半（健康度 θ），局部重规划 ≤2 次。

生成器事件协议（CLI/前端共用）：
  {"t":"plan","path":[...],"cost":..} | {"t":"step",...} | {"t":"gate","message":..,"index":k}
  {"t":"done","ok":bool,"answer":str,"task_id":..} | {"t":"trace","line":str}
"""
from __future__ import annotations

import json
from typing import Callable, Dict, Generator, List, Optional

from .config import Settings
from .graph import RuntimeGraph, now
from .llm import BaseLLM, extract_json
from .planner import GraphPlanner
from .skills import BUILTIN_SKILLS, Skill, fill, state_satisfies
from .tools import ToolContext, build_tools, dispatch, sync_tool_nodes

ConfirmCallback = Callable[[str], bool]   # 收到门控说明 → True 放行 / False 取消


class TaskExecutor:
    def __init__(self, store: RuntimeGraph, llm: BaseLLM, settings: Settings,
                 skills: Optional[List[Skill]] = None):
        self.store = store
        self.llm = llm
        self.settings = settings
        self.skills: Dict[str, Skill] = {s.name: s for s in (skills or BUILTIN_SKILLS)}
        self.ctx = ToolContext(store=store, llm=llm, workspace=settings.resolved_workspace())
        self.tools = build_tools(self.ctx)
        self.planner = GraphPlanner(list(self.skills.values()))

    def ensure_registry(self) -> None:
        """注册结构上图（幂等）：tool/skill/constraint 节点与边。"""
        from .skills import sync_skills

        sync_tool_nodes(self.store, self.tools)
        sync_skills(self.store, list(self.skills.values()), constraints=DEFAULT_CONSTRAINTS)

    # ------------------------------------------------------------ 主流程 ----
    def run_task(self, goal_text: str, params_hint: Optional[Dict[str, dict]] = None,
                 goal_assertions: Optional[List[str]] = None,
                 confirm: Optional[ConfirmCallback] = None) -> Generator[dict, None, None]:
        confirm = confirm or (lambda msg: True)
        store = self.store
        self.ensure_registry()
        self._task_ctx: Dict[str, str] = getattr(self, "_task_ctx", {})  # 每任务重置
        self._task_ctx = {}

        task_seq = store._task_seq + 1
        store._task_seq = task_seq
        task_id = store.add_node("task", f"任务{task_seq}", "执行器",
                                 props={"goal": goal_text, "status": "planning", "ts": now()})
        yield {"t": "trace", "line": f"目标入图：{task_id}（{goal_text[:60]}）"}

        # -- 目标断言化 --
        goals, params_all = self._resolve_goal(goal_text, params_hint, goal_assertions)
        goal_id = store.add_node("goal", f"目标{task_seq}", "执行器",
                                 props={"assertions": json.dumps(goals, ensure_ascii=False)})
        store.add_edge(task_id, goal_id, "has_goal", "执行器")

        # -- 规划（失败时不再徒劳重试；执行期的失败走局部重规划）--
        self._sync_health()
        plan = self.planner.plan(goals, store.assertions(), store=store, params_all=params_all)
        if not plan.ok:
            store.be.update_node(task_id, {"attributes": {"status": "failed"}})
            yield {"t": "done", "ok": False, "task_id": task_id,
                   "answer": f"无法完成目标：{plan.reason}"}
            return
        path = plan.path
        yield {"t": "plan", "task_id": task_id, "path": [s for s, _ in path], "cost": round(plan.cost, 2)}
        store.be.update_node(task_id, {"attributes": {"status": "executing", "plan": json.dumps([s for s, _ in path], ensure_ascii=False)}})

        # -- 逐步执行 --
        step_index = 0
        replans = 0
        while step_index < len(path):
            skill_name, planned_params = path[step_index]
            sk = self.skills.get(skill_name)
            if sk is None:
                yield {"t": "trace", "line": f"计划含未知技能 {skill_name}，终止。"}
                break
            params: Dict[str, str] = {**planned_params, **(params_all.get(skill_name) or {})}

            # 1) 前置重核（与规划器同一判定，含参数耦合）→ 局部重规划
            cur = store.assertions()
            if not self.planner.applicable(sk, cur, params_all):
                replans += 1
                if replans > 2:
                    store.be.update_node(task_id, {"attributes": {"status": "failed"}})
                    yield {"t": "done", "ok": False, "task_id": task_id,
                           "answer": f"重规划次数超限，止步于第 {step_index + 1} 步（{skill_name}）。"}
                    return
                yield {"t": "trace", "line": f"前置不满足（{skill_name}）→ 局部重规划（第{replans}次）"}
                self._sync_health()
                replan = self.planner.plan(goals, cur, store=store, params_all=params_all)
                if not replan.ok:
                    store.be.update_node(task_id, {"attributes": {"status": "failed"}})
                    yield {"t": "done", "ok": False, "task_id": task_id,
                           "answer": f"局部重规划失败：{replan.reason}"}
                    return
                path = replan.path   # 新计划从当前状态续算 → 下标归零
                step_index = 0
                yield {"t": "plan", "task_id": task_id, "path": [s for s, _ in path],
                       "cost": round(replan.cost, 2), "replan": True}
                continue

            # 2) 人工门控（约束图已把 deny 技能挡在搜索之外；gate 在执行前拦截）
            if sk.gate:
                allowed = confirm(f"【人工门控】技能 {sk.name}（{sk.title}）：{sk.gate}")
                yield {"t": "gate", "skill": skill_name, "message": sk.gate, "allowed": allowed}
                if not allowed:
                    store.be.update_node(task_id, {"attributes": {"status": "cancelled"}})
                    yield {"t": "done", "ok": False, "task_id": task_id,
                           "answer": f"人工门控拒绝执行 {sk.name}，任务已取消（已执行步骤保留在图上可审计）。"}
                    return

            # 3) 执行一步
            yield {"t": "step_start", "index": step_index + 1, "total": len(path),
                   "skill": skill_name, "params": params}
            ok, output = self._execute_skill(sk, params, task_id, step_index + 1)
            step_id = self._last_step_id
            store.add_edge(task_id, step_id, "has_step", "执行器")
            yield {"t": "step", "index": step_index + 1, "skill": skill_name,
                   "ok": ok, "output": output[:600], "step_id": step_id}
            if not ok:
                replans += 1
                if replans > 2:
                    store.be.update_node(task_id, {"attributes": {"status": "failed"}})
                    yield {"t": "done", "ok": False, "task_id": task_id,
                           "answer": f"技能 {skill_name} 连续失败（健康度已降至 {self._health_of(sk):.2f}），任务终止。"}
                    return
                yield {"t": "trace", "line": f"技能失败（{skill_name}）→ 局部重规划（第{replans}次）"}
                self._sync_health()
                replan = self.planner.plan(goals, store.assertions(), store=store, params_all=params_all)
                if not replan.ok:
                    store.be.update_node(task_id, {"attributes": {"status": "failed"}})
                    yield {"t": "done", "ok": False, "task_id": task_id,
                           "answer": f"局部重规划失败：{replan.reason}"}
                    return
                path = replan.path   # 新计划从当前状态续算 → 下标归零
                step_index = 0
                yield {"t": "plan", "task_id": task_id, "path": [s for s, _ in path],
                       "cost": round(replan.cost, 2), "replan": True}
                continue
            step_index += 1

        store.be.update_node(task_id, {"attributes": {"status": "done"}})
        answer = self._synthesize(goal_text, path)
        yield {"t": "done", "ok": True, "task_id": task_id, "answer": answer}

    # ------------------------------------------------------------ 细节 ----
    def _resolve_goal(self, goal_text, params_hint, goal_assertions):
        goals, params_all = list(goal_assertions or []), dict(params_hint or {})
        if not goals:
            obj = None
            try:
                obj = extract_json(self.llm.chat(
                    [{"role": "system", "content": GOAL_MAP_PROMPT +
                      "\n可用技能效果模板：" + json.dumps(
                          {s.name: s.effect for s in self.skills.values() if s.effect},
                          ensure_ascii=False)},
                     {"role": "user", "content": goal_text}], temperature=0.0))
            except Exception:
                obj = None
            if obj and obj.get("goal_assertions"):
                goals = [str(g) for g in obj["goal_assertions"]]
                params_all = {k: (v if isinstance(v, dict) else {})
                              for k, v in (obj.get("params") or {}).items()}
        if not goals:
            goals, _, kw_params = self.planner.goal_from_keywords(goal_text)
            for k, v in kw_params.items():
                params_all.setdefault(k, v)
        return goals, params_all

    def _execute_skill(self, sk: Skill, params: Dict[str, str], task_id: str, seq: int) -> tuple:
        store = self.store
        self.store._step_seq += 1
        step_id = f"task:任务{store._task_seq}/step:{self.store._step_seq}"
        step_node = store.add_node("step", f"步骤{store._step_seq}", f"技能 {sk.name}",
                                   nid=step_id,
                                   props={"skill": sk.name, "params": json.dumps(params, ensure_ascii=False),
                                          "status": "running", "seq": store._step_seq})
        self._last_step_id = step_id
        store.push_owner(step_id)
        self.ctx.step_id, self.ctx.task_id = step_id, task_id
        ok, output = True, ""
        try:
            if sk.tool:
                tool = self.tools.get(sk.tool)
                if tool is None:
                    ok, output = False, f"工具未注册：{sk.tool}"
                else:
                    args = self._resolve_args(sk, params)
                    missing = [p for p, m in tool.params.items()
                               if m.get("required") and p not in args]
                    if missing:
                        ok, output = False, f"缺少必填参数：{', '.join(missing)}（绑定/映射均未提供）"
                    else:
                        ok, output = dispatch(tool, self.ctx, args)
            else:
                ok, output = True, "（结构技能：仅推进状态断言）"
        except Exception as e:  # noqa: BLE001
            ok, output = False, f"执行异常：{type(e).__name__}: {e}"

        if ok:
            self._task_ctx[sk.name] = output  # 产出进入任务上下文，供后续技能 @bindings 引用
            produced: List[str] = []
            for eff in sk.effect:
                assertion = fill(eff, params)
                if "*" in assertion:
                    continue  # 占位符未解析的效果不落状态
                store.add_assertion(assertion, source=f"技能 {sk.name}")
                produced.append(f"fact:{assertion}")
                store.add_edge(step_id, f"fact:{assertion}", "produced", "执行器")
            result_id = ""
            if output:
                result_id = store.add_node("result", f"结果{store._step_seq}", f"技能 {sk.name}",
                                           data=output[:4000],
                                           props={"skill": sk.name, "ts": now()})
                store.add_edge(step_id, result_id, "produced", "执行器")
            store.be.update_node(step_id, {"attributes": {"status": "ok", "output": output[:2000]}})
        else:
            store.be.update_node(step_id, {"attributes": {"status": "failed", "output": output[:2000]}})
        # 健康度 θ：真值在图上（8.3 统计属性从执行轨迹持续更新），成功微升、失败减半
        snode = store.be.get_node(f"skill:{sk.name}")
        cur = float((snode.get("attributes") or {}).get("success_rate", sk.success_rate)) if snode else sk.success_rate
        runs = int((snode.get("attributes") or {}).get("run_count") or 0) + 1
        new_sr = min(1.0, cur * 1.02 + 0.001) if ok else max(0.05, cur * 0.5)
        store.be.update_node(f"skill:{sk.name}", {"attributes": {
            "success_rate": round(new_sr, 4), "run_count": runs, "last_run": now(), "last_ok": ok}})
        sk.success_rate = new_sr  # 同步内存副本供本轮规划启发使用
        store.pop_owner()
        store._autosave()
        return ok, output

    def _health_of(self, sk: Skill) -> float:
        snode = self.store.be.get_node(f"skill:{sk.name}")
        return float((snode.get("attributes") or {}).get("success_rate", sk.success_rate)) if snode else sk.success_rate

    def _sync_health(self) -> None:
        """规划前把图上的健康度同步进技能对象（代价 = cost × (2 - success_rate)）。"""
        for name, sk in self.skills.items():
            snode = self.store.be.get_node(f"skill:{name}")
            if snode:
                a = snode.get("attributes") or {}
                if "success_rate" in a:
                    sk.success_rate = float(a["success_rate"])
                if a.get("cost"):
                    sk.cost = float(a["cost"])

    def _resolve_args(self, sk: Skill, params: Dict[str, str]) -> Dict[str, str]:
        """合并三层参数：goal 映射参数 > 技能 bindings（@上一步产出 / {param} 插值）。"""
        args: Dict[str, str] = {}
        for pname, ref in (sk.bindings or {}).items():
            val = self._task_ctx.get(ref[1:], "") if ref.startswith("@") else fill(ref, params)
            if val:
                args[pname] = val
        args.update({k: v for k, v in params.items() if v})
        return args

    def _synthesize(self, goal_text: str, path) -> str:
        try:
            return self.llm.chat(
                [{"role": "system", "content": "[角色:SYNTH] 用三句话总结任务执行结果（计划路径、关键产出、图上证据）。"},
                 {"role": "user", "content": f"目标：{goal_text}\n执行路径：{' → '.join(s for s, _ in path)}"}],
                temperature=0.2).strip()
        except Exception:
            return f"任务执行完成，路径：{' → '.join(s for s, _ in path)}"


GOAL_MAP_PROMPT = """[角色:GOAL_MAP] 你是图规划器的目标映射器。把用户请求映射为目标断言集。
可用效果断言前缀：summary:{主题} / draft:{主题} / verified:{主题} / read:{路径} / written:{路径} / calc:done。
只输出 JSON：{"need_plan": true/false, "goal_assertions": ["..."],
 "params": {"技能名": {"参数名": "值"}}}
注意：写报告类任务的目标是 [summary:X, draft:X, verified:X]，并为 read_file 提供一个 workspace 内真实存在的 notes/*.md 相对路径。"""

DEFAULT_CONSTRAINTS = [
    {"name": "sandbox_workspace_only", "rule": "restrict: 文件读写仅限 workspace 目录（工具层沙箱强制）",
     "targets": ["tool:read_file", "tool:write_file", "skill:read_file", "skill:write_file"]},
    {"name": "gate_write_file", "rule": "gate: 写盘动作前需人工确认（执行前拦截）",
     "targets": ["skill:write_file", "skill:draft_report"]},
    {"name": "no_physical_delete", "rule": "invariant: 图谱节点只失效不物理删除（历史可回放）",
     "targets": []},
]
