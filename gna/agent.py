"""AgentRuntime —— 图原生智能体运行时总循环。

一轮对话：
  (1) 感知入图：turn 节点 + mentions 边 + 实体/事实抽取（写入门控）
  (2) 遍历召回：以输入为种子做激活扩散 → 子图 → LLM 上下文
  (3) 路由：任务类（写报告/简报/调研…或 /任务 前缀）→ 图规划+执行器；
            其余 → ReAct 工具循环（工具含图操作：query_graph/add_fact/find_path…）
  (4) 收尾：本轮知识入图、episode 链收口、输出答案

事件流（生成器）：{"t":"trace"|"answer"|"task"|"gate"|"done"...}，CLI 与未来前端共用。
"""
from __future__ import annotations

import re
from typing import Callable, Dict, Generator, List, Optional

from .config import Settings, load_settings
from .extract import ingest
from .executor import ConfirmCallback, TaskExecutor
from .graph import RuntimeGraph, now
from .llm import BaseLLM, extract_json, make_llm
from .recall import recall_context
from .skills import BUILTIN_SKILLS
from .tools import ToolContext, build_tools, dispatch, sync_tool_nodes

TASK_PATTERN = re.compile(
    r"(写|做|生成|起草|整理|总结|输出).{0,20}(报告|简报|总结|摘要|要点)"
    r"|调研|帮我?整理|/任务")
REACT_SYSTEM = """[角色:REACT] 你是图原生智能体 GNA：状态、记忆、证据全部在一张家里的图上。
可用工具（action 取值）：
{tools}
输出协议：只输出一个 JSON 对象——
  需要工具：{{"thought": "简短理由", "action": "工具名", "action_input": {{...}}}}
  直接回答：{{"thought": "简短理由", "final": "给用户的完整回答"}}
规则：一次只调一个工具；观察结果会以 OBSERVATION: 前缀回给你；引用事实时注明来源。"""


class AgentRuntime:
    def __init__(self, settings: Optional[Settings] = None, store: Optional[RuntimeGraph] = None,
                 llm: Optional[BaseLLM] = None):
        self.settings = settings or load_settings()
        self.store = store or RuntimeGraph(storage_path=str(self.settings.resolved_storage()),
                                           autosave=True)
        self.llm = llm or make_llm(self.settings)
        self.executor = TaskExecutor(self.store, self.llm, self.settings, BUILTIN_SKILLS)
        self.executor.ensure_registry()
        self.ctx = ToolContext(store=self.store, llm=self.llm,
                               workspace=self.settings.resolved_workspace(), source="对话引擎")
        self.chat_tools = build_tools(self.ctx)
        sync_tool_nodes(self.store, self.chat_tools)

    # ================================================================ 对话 ====
    def chat_turn(self, text: str, confirm: Optional[ConfirmCallback] = None,
                  history: Optional[List[dict]] = None) -> Generator[dict, None, None]:
        text = (text or "").strip()
        if not text:
            return
        store = self.store
        store._turn_seq += 1
        turn_id = store.add_node("turn", f"回合{store._turn_seq}", "用户",
                                 data=text[:500], props={"role": "user", "ts": now()})
        store.push_owner(turn_id)

        # (1)(2) 感知入图 + 遍历召回
        ctx = recall_context(store, text, hops=self.settings.memory_hops)
        yield {"t": "trace", "line": f"图记忆召回：{len(ctx.splitlines()) - 1} 条关系/回合"}

        # (3) 路由
        forced_task = text.startswith("/任务")
        body = text[3:].strip() if forced_task else text
        if forced_task or TASK_PATTERN.search(text):
            yield from self._task_engine(body, confirm, turn_id)
        else:
            yield from self._react_engine(text, ctx, history or [], turn_id)

        # (4) 本轮知识入图
        stat = ingest(store, self.llm, text, turn_id=turn_id, source=f"第{store._turn_seq}轮抽取")
        yield {"t": "trace",
               "line": f"知识入图：实体 {stat['entities']}、事实 {stat['facts']}"
                       + (f"（冲突失效 {stat['conflicts']}）" if stat["conflicts"] else "")}
        store.pop_owner()
        store._autosave()
        yield {"t": "turn_done", "turn_id": turn_id}

    # ------------------------------------------------------------ 任务引擎 ----
    def _task_engine(self, body: str, confirm: Optional[ConfirmCallback], turn_id: str) -> Generator[dict, None, None]:
        yield {"t": "trace", "line": "路由 → 图规划任务引擎（路径即计划）"}
        result: Dict = {}
        for ev in self.executor.run_task(body, confirm=confirm):
            if ev.get("t") == "done":
                result = ev
            yield ev
        if result.get("ok"):
            self.store.add_edge(turn_id, result.get("task_id", ""), "triggered", "路由")

    # ------------------------------------------------------------ ReAct 引擎 ----
    def _react_engine(self, text: str, ctx: str, history: List[dict], turn_id: str) -> Generator[dict, None, None]:
        yield {"t": "trace", "line": "路由 → ReAct 对话引擎（工具循环）"}
        tools_desc = "\n".join(
            f"- {t.name}: {t.desc} 参数: {list(t.params.keys())}"
            for t in self.chat_tools.values())
        system = REACT_SYSTEM.format(tools=tools_desc)
        messages: List[dict] = [{"role": "system", "content": system}]
        for h in history[-6:]:
            messages.append({"role": h.get("role", "user"), "content": str(h.get("content", ""))[:500]})
        messages.append({"role": "user", "content": f"{ctx}\n\n用户输入：{text}"})

        for step in range(self.settings.max_react_steps):
            raw = self.llm.chat(messages)
            obj = extract_json(raw)
            if obj is None:
                repair = self.llm.chat(messages + [
                    {"role": "system", "content": "[角色:REPAIR] 上一次输出不是合法 JSON。请重新只输出一个 JSON 对象。"}])
                obj = extract_json(repair) or {"final": raw[:800]}
            if "final" in obj:
                answer = str(obj["final"]).strip() or "（无内容）"
                yield {"t": "answer", "text": answer}
                aid = self.store.add_node("turn", f"回合{self.store._turn_seq}A", "GNA",
                                          data=answer[:500], props={"role": "assistant"})
                self.store.add_edge(turn_id, aid, "replied_by", "对话引擎")
                return
            action = str(obj.get("action", "")).strip()
            tool = self.chat_tools.get(action)
            if tool is None:
                messages.append({"role": "user",
                                 "content": f"OBSERVATION: 未知工具 {action}。可用：{list(self.chat_tools)}"})
                yield {"t": "trace", "line": f"未知工具 {action}，已提示重试"}
                continue
            ok, output = dispatch(tool, self.ctx, obj.get("action_input") or {})
            messages.append({"role": "assistant", "content": raw[:800]})
            messages.append({"role": "user", "content": f"OBSERVATION: {output[:2000]}"})
            yield {"t": "trace", "line": f"工具 {action} → {'成功' if ok else '失败'}：{output[:80]}"}
        yield {"t": "answer", "text": "（达到推理步数上限，请拆分问题或提高 max_react_steps）"}

    # ================================================================ 工具 ====
    def stats(self) -> dict:
        return self.store.stats()

    def skill_report(self) -> dict:
        from .skills import validate

        return validate(self.store, BUILTIN_SKILLS)

    def reset(self, keep_registry: bool = True) -> None:
        self.store.clear_memory(keep_registry=keep_registry)
        self.executor.ensure_registry()
