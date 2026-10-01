"""RuntimeGraph —— 图原生智能体的状态与证据层（单图运行时）。

四判据的落实（教材 8.1.1）：
- 状态可寻址：断言集 = 有效 fact 节点名集合；任何状态问题都是一次图遍历；
- 证据可追溯：节点/边携带 ts + source；fact 由 episode/turn 经 states 边溯源；
- 变更留痕：一切写操作产生 episode 节点（ΔW 事件），串成 next 时间链（事件溯源）；
- 失效不删除：过时事实打 invalid 标记保留历史；回滚 = 沿 step 的 logged 边做补偿失效。

节点类型（class_）：entity/fact/turn/task/step/result/skill/tool/constraint/goal/episode
节点 id 约定：f"{type}:{name}"
"""
from __future__ import annotations

import json
import os
import threading
from datetime import datetime
from typing import Dict, Iterator, List, Optional, Set, Tuple

from .backend import GraphBackend, make_backend

NODE_TYPES = ("entity", "fact", "turn", "task", "step", "result",
              "skill", "tool", "constraint", "goal", "episode")

now = lambda: datetime.now().strftime("%Y-%m-%d %H:%M:%S")  # noqa: E731


def knowledge_fact_name(s: str, r: str, o: str) -> str:
    return f"{s}｜{r}｜{o}"


class RuntimeGraph:
    """单图运行时：图后端之上的类型化操作 + ΔW 事件链 + 遍历原语。"""

    def __init__(self, backend: Optional[GraphBackend] = None, storage_path=None,
                 autosave: bool = True):
        self.be = backend or make_backend()
        self.storage_path = storage_path
        self.autosave = autosave
        self._lock = threading.RLock()
        self._ep_seq = 0
        self._turn_seq = 0
        self._task_seq = 0
        self._step_seq = 0
        self._goal_seq = 0
        self._last_ep: Optional[str] = None
        self._owner: List[str] = []          # episode 归属栈（step / turn 节点）
        if storage_path and os.path.exists(storage_path):
            self.load()

    # ================================================================ ΔW ====
    def _emit(self, op: str, source: str, payload: dict) -> str:
        """追加一个 episode 节点并挂到时间链上。返回 episode 节点 id。"""
        self._ep_seq += 1
        ep_id = f"episode:{self._ep_seq}"
        self.be.add_node(ep_id, f"事件{self._ep_seq}", "episode",
                         data=op, attributes={"seq": self._ep_seq, "op": op,
                                              "source": source, "ts": now(),
                                              "payload": json.dumps(payload, ensure_ascii=False)})
        if self._last_ep:
            self.be.add_edge(self._last_ep, ep_id, "next")
        for owner in self._owner:
            self.be.add_edge(owner, ep_id, "logged")
        self._last_ep = ep_id
        return ep_id

    def push_owner(self, owner_id: str) -> None:
        self._owner.append(owner_id)

    def pop_owner(self) -> None:
        if self._owner:
            self._owner.pop()

    # ============================================================ 节点操作 ====
    def add_node(self, ntype: str, name: str, source: str, *, data: str = "",
                 nid: Optional[str] = None, props: Optional[dict] = None) -> str:
        """新增（或补全）一个类型化节点。已存在时仅补属性并记 correct 事件。"""
        node_id = nid or f"{ntype}:{name}"
        with self._lock:
            created = self.be.add_node(node_id, name, ntype, data=data, attributes=props)
            if created == "created":
                self.be.update_node(node_id, {"attributes": {"ts": now(), "source": source}})
                self._emit("insert_node", source, {"node": node_id, "type": ntype, "name": name})
            else:
                nd = self.be.get_node(node_id) or {}
                fill = {k: v for k, v in (props or {}).items()
                        if v is not None and not nd.get("attributes", {}).get(k)}
                if fill:
                    self.be.update_node(node_id, {"attributes": fill})
                    self._emit("correct_node", source, {"node": node_id, "fields": list(fill)})
            self._autosave()
            return node_id

    def get_node(self, node_id: str) -> Optional[dict]:
        return self.be.get_node(node_id)

    def invalidate(self, node_id: str, source: str, reason: str = "") -> None:
        with self._lock:
            self.be.update_node(node_id, {"attributes": {"invalid": True,
                                                         "invalid_reason": reason,
                                                         "invalid_ts": now()}})
            self._emit("invalidate_node", source, {"node": node_id, "reason": reason})
            self._autosave()

    def add_edge(self, src: str, dst: str, etype: str, source: str, *,
                 weight: float = 1.0, props: Optional[dict] = None) -> bool:
        if not (self.be.get_node(src) and self.be.get_node(dst)):
            return False
        with self._lock:
            attrs = dict(props or {})
            attrs.setdefault("ts", now())
            attrs.setdefault("source", source)
            created = self.be.add_edge(src, dst, etype, weight=weight, attributes=attrs)
            if created:
                self._emit("insert_edge", source,
                           {"src": src, "dst": dst, "etype": etype})
                self._autosave()
            return created

    def link(self, src: str, dst: str, etype: str, source: str) -> bool:
        """静默连边（结构登记，不产生事件，用于注册类边缘操作）。"""
        return self.be.add_edge(src, dst, etype)

    # ============================================================ 断言状态 ====
    def assertions(self) -> Set[str]:
        out = set()
        for nid, nd in self.be.nodes():
            a = nd.get("attributes", {})
            if nd.get("class_") == "fact" and not a.get("invalid") and a.get("kind") != "template":
                out.add(str(nd.get("name")))
        return out

    def add_assertion(self, assertion: str, source: str, kind: str = "state") -> str:
        return self.add_node("fact", assertion, source,
                             props={"kind": kind, "assertion": assertion})

    def invalidate_assertion(self, assertion: str, source: str, reason: str = "") -> bool:
        nid = f"fact:{assertion}"
        if self.be.get_node(nid):
            self.invalidate(nid, source, reason)
            return True
        return False

    def has_assertion(self, assertion: str) -> bool:
        return assertion in self.assertions()

    # ============================================================ 知识事实 ====
    def add_fact_triple(self, s: str, r: str, o: str, source: str,
                        turn_id: Optional[str] = None) -> Tuple[str, str]:
        """写入知识三元组（写入门控在此层统一：去重 / 冲突 → 旧事实失效）。"""
        fname = knowledge_fact_name(s, r, o)
        existing = [n for n in self.find("fact", include_invalid=True)
                    if n["attributes"].get("kind") == "knowledge"
                    and n["attributes"].get("subject") == s
                    and n["attributes"].get("relation") == r]
        conflict = next((n for n in existing
                         if n["attributes"].get("object") != o and not n["attributes"].get("invalid")), None)
        fid = self.add_node("fact", fname, source,
                            props={"kind": "knowledge", "subject": s, "relation": r, "object": o})
        sid = self.add_node("entity", s, source)
        oid = self.add_node("entity", o, source)
        self.add_edge(sid, fid, "subject_of", source)
        self.add_edge(fid, oid, "object_of", source)
        if turn_id:
            self.add_edge(turn_id, fid, "states", source)
        if conflict:
            self.invalidate(conflict["id"], source, reason=f"冲突更新：{o}（第{self._ep_seq}事件）")
            self.add_edge(fid, conflict["id"], "supersedes", source)
        return fid, (conflict["id"] if conflict else "")

    # ================================================================ 查询 ====
    def find(self, ntype: Optional[str] = None, name_sub: Optional[str] = None,
             include_invalid: bool = True) -> List[dict]:
        out = []
        for nid, nd in self.be.nodes():
            if ntype and nd.get("class_") != ntype:
                continue
            if name_sub and name_sub.lower() not in str(nd.get("name") or "").lower():
                continue
            if not include_invalid and nd.get("attributes", {}).get("invalid"):
                continue
            out.append({"id": nid, **nd})
        out.sort(key=lambda d: str(d.get("attributes", {}).get("ts") or ""))
        return out

    def entity_link(self, text: str, limit: int = 6) -> List[str]:
        """实体链接（快定位）：文本 → 图上实体节点。精确 > 前缀/包含。"""
        t = (text or "").strip().lower()
        if not t:
            return []
        scored: List[Tuple[int, int, str]] = []
        for nid, nd in self.be.nodes():
            if nd.get("class_") != "entity" or nd.get("attributes", {}).get("invalid"):
                continue
            name = str(nd.get("name") or "")
            low = name.lower()
            if not low:
                continue
            if low == t:
                scored.append((0, -len(low), nid))
            elif low in t:
                scored.append((1, -len(low), nid))
            elif t in low and len(t) >= 2:
                scored.append((2, len(low), nid))
        scored.sort()
        return [nid for _, _, nid in scored[:limit]]

    # ============================================================ 遍历原语 ====
    def khop(self, seeds: List[str], hops: int = 2, max_nodes: int = 64) -> Set[str]:
        """k 跳邻域（双向 BFS）。"""
        seen = set(n for n in seeds if self.be.get_node(n))
        frontier = set(seen)
        for _ in range(max(0, hops)):
            nxt: Set[str] = set()
            for n in frontier:
                for m, _ in self.be.neighbors(n):
                    if m not in seen:
                        nxt.add(m)
            seen |= nxt
            frontier = nxt
            if len(seen) >= max_nodes:
                break
        return set(list(seen)[:max_nodes])

    def spreading_activation(self, seeds: List[str], hops: int = 2, decay: float = 0.6,
                             min_act: float = 0.05, max_nodes: int = 64) -> Dict[str, float]:
        """激活扩散召回：种子 1.0，沿边（双向）每跳 × decay×边权，保留最大激活值。"""
        act: Dict[str, float] = {}
        frontier: Dict[str, float] = {}
        for s in seeds:
            if self.be.get_node(s):
                frontier[s] = 1.0
        for _ in range(max(0, hops)):
            if not frontier:
                break
            nxt: Dict[str, float] = {}
            for n, a in frontier.items():
                for m, ed in self.be.neighbors(n):
                    spread = a * decay * float(ed.get("weight") or 1.0)
                    if spread >= min_act and spread > act.get(m, 0.0) and spread > nxt.get(m, 0.0):
                        nxt[m] = spread
            for m, a in nxt.items():
                act[m] = max(act.get(m, 0.0), a)
            frontier = nxt  # 全部作为下一跳种子（act 已保证不重复放大）
            act = dict(sorted(act.items(), key=lambda kv: -kv[1])[:max_nodes])
        for s in seeds:
            act[s] = max(act.get(s, 0.0), 1.0)
        return dict(sorted(act.items(), key=lambda kv: -kv[1])[:max_nodes])

    def subgraph_edges(self, node_ids: Set[str]) -> List[dict]:
        return [e for e in self.be.find_edges()
                if e["src"] in node_ids and e["dst"] in node_ids]

    def shortest_path(self, a: str, b: str) -> Optional[List[str]]:
        return self.be.shortest_path(a, b)

    def walk_episode_chain(self) -> Iterator[dict]:
        """沿 next 边回放事件链。"""
        ep = "episode:1" if self.be.get_node("episode:1") else None
        seen: Set[str] = set()
        while ep and ep not in seen:
            seen.add(ep)
            nd = self.be.get_node(ep) or {}
            yield {"id": ep, **nd}
            nxt = [m for m, e in self.be.out_edges(ep) if e.get("label") == "next"]
            ep = nxt[0] if nxt else None

    # ================================================================ 回滚 ====
    def rollback_last_step(self, actor: str = "用户") -> Optional[dict]:
        """补偿式回滚最近一个 ok 的执行步：其产生的 fact/result 节点打失效标记。"""
        steps = [n for n in self.find("step") if n["attributes"].get("status") == "ok"]
        if not steps:
            return None
        steps.sort(key=lambda n: int(n["attributes"].get("seq") or 0))
        step = steps[-1]
        undone: List[str] = []
        for ep_id, ed in self.be.out_edges(step["id"]):
            if ed.get("label") != "logged":
                continue
            ep = self.be.get_node(ep_id) or {}
            attrs = ep.get("attributes", {})
            if attrs.get("op") != "insert_node":
                continue
            try:
                payload = json.loads(attrs.get("payload") or "{}")
            except json.JSONDecodeError:
                continue
            target = payload.get("node", "")
            tnode = self.be.get_node(target)
            if tnode and tnode.get("class_") in ("fact", "result") \
                    and not tnode.get("attributes", {}).get("invalid"):
                self.be.update_node(target, {"attributes": {"invalid": True,
                                                            "invalid_reason": f"回滚 step:{step['attributes'].get('seq')}",
                                                            "invalid_ts": now()}})
                undone.append(target)
        self.be.update_node(step["id"], {"attributes": {"status": "rolled_back"}})
        self._emit("rollback", actor, {"step": step["id"], "undone": undone})
        self._autosave()
        return {"step": step["id"], "skill": step["attributes"].get("skill"), "undone": undone}

    # ============================================================ 序列化 ====
    def to_dict(self) -> dict:
        nodes, edges = [], []
        for nid, nd in self.be.nodes():
            nodes.append({"id": nid, "name": nd.get("name"), "class_": nd.get("class_"),
                          "data": nd.get("data", ""), "attributes": nd.get("attributes") or {},
                          "embedding": nd.get("embedding")})
        for e in self.be.find_edges():
            edges.append({"src": e["src"], "dst": e["dst"], "label": e.get("label"),
                          "weight": e.get("weight", 1.0), "attributes": e.get("attributes") or {}})
        return {"version": "0.1", "nodes": nodes, "edges": edges,
                "counters": {"ep": self._ep_seq, "turn": self._turn_seq,
                             "task": self._task_seq, "step": self._step_seq,
                             "goal": self._goal_seq}}

    def load_dict(self, data: dict) -> None:
        self.be.clear()
        for nd in data.get("nodes", []):
            self.be.add_node(nd["id"], nd.get("name") or nd["id"], nd.get("class_") or "",
                             data=nd.get("data", ""), attributes=nd.get("attributes") or {},
                             embedding=nd.get("embedding"))
        for e in data.get("edges", []):
            self.be.add_edge(e["src"], e["dst"], e.get("label") or "",
                             weight=float(e.get("weight") or 1.0), attributes=e.get("attributes") or {})
        c = data.get("counters", {})
        self._ep_seq = c.get("ep", 0)
        self._turn_seq = c.get("turn", 0)
        self._task_seq = c.get("task", 0)
        self._step_seq = c.get("step", 0)
        self._goal_seq = c.get("goal", 0)
        self._last_ep = f"episode:{self._ep_seq}" if self._ep_seq else None

    def save(self, path: Optional[str] = None) -> str:
        path = path or self.storage_path
        if not path:
            return ""
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        tmp = f"{path}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.to_dict(), f, ensure_ascii=False)
        os.replace(tmp, path)
        return path

    def load(self, path: Optional[str] = None) -> bool:
        path = path or self.storage_path
        if not path or not os.path.exists(path):
            return False
        try:
            with open(path, "r", encoding="utf-8") as f:
                self.load_dict(json.load(f))
            return True
        except Exception:
            return False

    def clear_memory(self, keep_registry: bool = True) -> None:
        keep: Dict[str, dict] = {}
        if keep_registry:
            for nid, nd in self.be.nodes():
                if nd.get("class_") in ("skill", "tool", "constraint"):
                    keep[nid] = nd
        self.be.clear()
        for nid, nd in keep.items():
            self.be.add_node(nid, nd.get("name") or nid, nd.get("class_") or "",
                             data=nd.get("data", ""), attributes=nd.get("attributes") or {},
                             embedding=nd.get("embedding"))
        self._ep_seq = self._turn_seq = self._task_seq = self._step_seq = self._goal_seq = 0
        self._last_ep = None
        self._autosave()

    def _autosave(self) -> None:
        if self.autosave and self.storage_path:
            try:
                self.save()
            except Exception:
                pass

    # ================================================================ 统计 ====
    def stats(self) -> dict:
        by_type: Dict[str, int] = {}
        invalid = 0
        for _, nd in self.be.nodes():
            t = nd.get("class_") or "?"
            by_type[t] = by_type.get(t, 0) + 1
            if nd.get("attributes", {}).get("invalid"):
                invalid += 1
        return {"backend": self.be.name, "nodes": self.be.node_count(),
                "edges": self.be.edge_count(), "by_type": by_type,
                "invalid": invalid, "events": self._ep_seq,
                "assertions": len(self.assertions())}
