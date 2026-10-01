"""图后端适配层：GX-1.5.3（默认）与 networkx（回退）的统一遍历 API。

设计约束（产品设计文档 A2）：上层 RuntimeGraph 只面向本模块的抽象 API，
"查找即遍历"的全部原语（邻接 / k-hop / 最短路 / 拓扑序）在这里落地。

GXBackend 注意点（吸取 gx-memory-mcp 修复经验）：
- GX 的邻接表只存出边，入边需自建反向索引（写操作后失效重建）；
- GX 原生序列化丢 Node.data/embedding —— 序列化统一由 RuntimeGraph 完成，不经过 GX 的 save()；
- 同 (src,dst,label) 边去重，避免重复边膨胀。
"""
from __future__ import annotations

import os
import sys
import uuid
from typing import Dict, Iterator, List, Optional, Tuple

EdgeDict = Dict  # {"label","weight","attributes"}
NodeDict = Dict  # {"id","name","class_","data","attributes","embedding","weight"}


class GraphBackend:
    name = "base"

    # ---- 节点 ----
    def add_node(self, node_id: str, name: str, class_: str, data: str = "",
                 attributes: Optional[dict] = None, embedding: Optional[list] = None) -> str:
        raise NotImplementedError

    def get_node(self, node_id: str) -> Optional[NodeDict]:
        raise NotImplementedError

    def update_node(self, node_id: str, fields: dict) -> None:
        raise NotImplementedError

    def remove_node(self, node_id: str) -> None:
        raise NotImplementedError

    # ---- 边 ----
    def add_edge(self, src: str, dst: str, label: str, weight: float = 1.0,
                 attributes: Optional[dict] = None) -> bool:
        raise NotImplementedError

    def get_edges(self, src: str, dst: str) -> List[EdgeDict]:
        raise NotImplementedError

    def find_edges(self, src: Optional[str] = None, dst: Optional[str] = None,
                   label: Optional[str] = None) -> List[dict]:
        raise NotImplementedError

    def has_edge(self, src: str, dst: str, label: Optional[str] = None) -> bool:
        raise NotImplementedError

    def remove_edges(self, src: str, dst: str, label: Optional[str] = None) -> int:
        raise NotImplementedError

    # ---- 遍历 ----
    def out_edges(self, node_id: str) -> List[Tuple[str, EdgeDict]]:
        raise NotImplementedError

    def in_edges(self, node_id: str) -> List[Tuple[str, EdgeDict]]:
        raise NotImplementedError

    def neighbors(self, node_id: str, direction: str = "both") -> List[Tuple[str, EdgeDict]]:
        out = self.out_edges(node_id)
        ins = self.in_edges(node_id) if direction in ("in", "both") else []
        if direction == "in":
            return ins
        if direction == "out":
            return out
        seen, both = set(), []
        for nid, e in out + ins:
            if nid not in seen:
                seen.add(nid)
                both.append((nid, e))
        return both

    def nodes(self) -> List[Tuple[str, NodeDict]]:
        raise NotImplementedError

    def node_ids(self) -> List[str]:
        return [nid for nid, _ in self.nodes()]

    def find_by_name_sub(self, sub: str, limit: int = 50) -> List[Tuple[str, str]]:
        sub = (sub or "").strip().lower()
        hits = []
        for nid, nd in self.nodes():
            name = str(nd.get("name") or "")
            if sub and sub in name.lower():
                hits.append((nid, name))
                if len(hits) >= limit:
                    break
        return hits

    # ---- 全局 ----
    def node_count(self) -> int:
        raise NotImplementedError

    def edge_count(self) -> int:
        raise NotImplementedError

    def clear(self) -> None:
        raise NotImplementedError

    def shortest_path(self, src: str, dst: str) -> Optional[List[str]]:
        raise NotImplementedError

    def topological_order(self) -> Optional[List[str]]:
        raise NotImplementedError


# ---------------------------------------------------------------- GX ----

def _load_gx(gx_path: str):
    if gx_path and gx_path not in sys.path:
        sys.path.insert(0, gx_path)
    from graph_engine import Graph, Node, Edge  # noqa: 延迟导入

    return Graph, Node, Edge


class GXBackend(GraphBackend):
    """基于 GX-1.5.3 graph_engine.Graph（有向）。

    入边通过惰性反向索引实现；任何写操作后置脏，下次遍历前重建。
    """

    name = "gx"

    def __init__(self, gx_path: str):
        Graph, Node, Edge = _load_gx(gx_path)
        self._Graph, self._Node, self._Edge = Graph, Node, Edge
        self._g = Graph(directed=True)
        self._rev_dirty = True
        self._rev: Dict[str, List[Tuple[str, EdgeDict]]] = {}

    # -- 内部 --
    def _edict(self, e) -> EdgeDict:
        return {"label": e.label, "weight": float(e.weight or 1.0),
                "attributes": dict(e.attributes or {})}

    def _ndict(self, n) -> NodeDict:
        return {"id": n.id, "name": n.name, "class_": n.class_ or "", "data": n.data or "",
                "attributes": dict(n.attributes or {}), "embedding": n.embedding,
                "weight": float(n.weight or 1.0)}

    def _rebuild_rev(self) -> None:
        self._rev = {}
        for e in self._g.get_all_edges():
            self._rev.setdefault(e.target.id, []).append((e.source.id, self._edict(e)))
        self._rev_dirty = False

    # -- 节点 --
    def add_node(self, node_id, name, class_, data="", attributes=None, embedding=None):
        if self._g.get_node(node_id):
            return "exists"
        node = self._Node(id=node_id, name=name or node_id, class_=class_ or "",
                          data=data, attributes=dict(attributes or {}), embedding=embedding)
        self._g.add_node(node)
        self._rev_dirty = True
        return "created"

    def get_node(self, node_id):
        n = self._g.get_node(node_id)
        return self._ndict(n) if n else None

    def update_node(self, node_id, fields):
        n = self._g.get_node(node_id)
        if not n:
            return
        for k, v in fields.items():
            if k == "attributes" and isinstance(v, dict):
                n.attributes.update(v)
            elif hasattr(n, k):
                setattr(n, k, v)
        n.version += 1

    def remove_node(self, node_id):
        try:
            self._g.delete_node(node_id)
            self._rev_dirty = True
        except Exception:
            pass

    # -- 边 --
    def add_edge(self, src, dst, label, weight=1.0, attributes=None):
        s, t = self._g.get_node(src), self._g.get_node(dst)
        if not s or not t:
            return False
        for e in self._g.edge_lookup.get(src, {}).get(dst, []):
            if (e.label or "") == (label or ""):
                return False
        edge = self._Edge(id=str(uuid.uuid4()), source=s, target=t,
                          weight=weight, label=label, attributes=dict(attributes or {}))
        self._g.add_edge(edge)
        self._rev_dirty = True
        return True

    def get_edges(self, src, dst):
        return [self._edict(e) for e in self._g.edge_lookup.get(src, {}).get(dst, [])]

    def find_edges(self, src=None, dst=None, label=None):
        out = []
        for e in self._g.get_all_edges():
            if src and e.source.id != src:
                continue
            if dst and e.target.id != dst:
                continue
            if label and (e.label or "") != label:
                continue
            out.append({"src": e.source.id, "dst": e.target.id, **self._edict(e)})
        return out

    def has_edge(self, src, dst, label=None):
        for e in self._g.edge_lookup.get(src, {}).get(dst, []):
            if label is None or (e.label or "") == label:
                return True
        return False

    def remove_edges(self, src, dst, label=None):
        raise NotImplementedError("v0.1 以失效标记代替物理删除（8.1 失效语义）")

    # -- 遍历 --
    def out_edges(self, node_id):
        return [(e.target.id, self._edict(e)) for e in self._g.adj.get(node_id, [])]

    def in_edges(self, node_id):
        if self._rev_dirty:
            self._rebuild_rev()
        return list(self._rev.get(node_id, []))

    def nodes(self):
        return [(nid, self._ndict(n)) for nid, n in self._g.nodes.items()]

    def node_count(self):
        return len(self._g.nodes)

    def edge_count(self):
        return len(self._g.edges)

    def clear(self):
        self._g = self._Graph(directed=True)
        self._rev, self._rev_dirty = {}, True

    def shortest_path(self, src, dst):
        import networkx as nx

        g = nx.DiGraph()
        for e in self._g.get_all_edges():
            g.add_edge(e.source.id, e.target.id, weight=float(e.weight or 1.0))
        try:
            return nx.shortest_path(g, src, dst, weight="weight")
        except (nx.NetworkXNoPath, nx.NodeNotFound):
            return None

    def topological_order(self):
        import networkx as nx

        g = nx.DiGraph()
        g.add_nodes_from(self._g.nodes.keys())
        for e in self._g.get_all_edges():
            g.add_edge(e.source.id, e.target.id)
        try:
            return list(nx.topological_sort(g))
        except nx.NetworkXUnfeasible:
            return None


# ---------------------------------------------------------- networkx ----

class NXBackend(GraphBackend):
    """纯 networkx MultiDiGraph 实现，开源零门槛回退。"""

    name = "networkx"

    def __init__(self):
        import networkx as nx

        self._nx = nx
        self._g = nx.MultiDiGraph()

    def add_node(self, node_id, name, class_, data="", attributes=None, embedding=None):
        if node_id in self._g:
            return "exists"
        self._g.add_node(node_id, name=name or node_id, class_=class_ or "", data=data,
                         attributes=dict(attributes or {}), embedding=embedding, weight=1.0)
        return "created"

    def get_node(self, node_id):
        if node_id not in self._g:
            return None
        d = self._g.nodes[node_id]
        return {"id": node_id, "name": d.get("name"), "class_": d.get("class_", ""),
                "data": d.get("data", ""), "attributes": dict(d.get("attributes") or {}),
                "embedding": d.get("embedding"), "weight": float(d.get("weight") or 1.0)}

    def update_node(self, node_id, fields):
        if node_id not in self._g:
            return
        d = self._g.nodes[node_id]
        for k, v in fields.items():
            if k == "attributes" and isinstance(v, dict):
                d.setdefault("attributes", {}).update(v)
            else:
                d[k] = v

    def remove_node(self, node_id):
        if node_id in self._g:
            self._g.remove_node(node_id)

    def add_edge(self, src, dst, label, weight=1.0, attributes=None):
        if src not in self._g or dst not in self._g:
            return False
        if self._g.has_edge(src, dst):
            if any((d.get("label") or "") == (label or "") for d in self._g[src][dst].values()):
                return False
        self._g.add_edge(src, dst, key=str(uuid.uuid4()), label=label,
                         weight=weight, attributes=dict(attributes or {}))
        return True

    def get_edges(self, src, dst):
        return [{"label": d.get("label"), "weight": float(d.get("weight") or 1.0),
                 "attributes": dict(d.get("attributes") or {})}
                for d in self._g[src][dst].values()] if src in self._g and dst in self._g else []

    def find_edges(self, src=None, dst=None, label=None):
        out = []
        for u, v, d in self._g.edges(data=True):
            if src and u != src:
                continue
            if dst and v != dst:
                continue
            if label and (d.get("label") or "") != label:
                continue
            out.append({"src": u, "dst": v, "label": d.get("label"),
                        "weight": float(d.get("weight") or 1.0),
                        "attributes": dict(d.get("attributes") or {})})
        return out

    def has_edge(self, src, dst, label=None):
        if src not in self._g or dst not in self._g:
            return False
        return any((d.get("label") or "") == (label or "") for d in self._g[src][dst].values())

    def remove_edges(self, src, dst, label=None):
        raise NotImplementedError("v0.1 以失效标记代替物理删除（8.1 失效语义）")

    def out_edges(self, node_id):
        return [(v, {"label": d.get("label"), "weight": float(d.get("weight") or 1.0),
                     "attributes": dict(d.get("attributes") or {})})
                for _, v, d in self._g.out_edges(node_id, data=True)]

    def in_edges(self, node_id):
        return [(u, {"label": d.get("label"), "weight": float(d.get("weight") or 1.0),
                     "attributes": dict(d.get("attributes") or {})})
                for u, _, d in self._g.in_edges(node_id, data=True)]

    def nodes(self):
        return [(nid, self.get_node(nid)) for nid in self._g.nodes]

    def node_count(self):
        return self._g.number_of_nodes()

    def edge_count(self):
        return self._g.number_of_edges()

    def clear(self):
        self._g = self._nx.MultiDiGraph()

    def shortest_path(self, src, dst):
        try:
            return list(self._nx.shortest_path(self._g, src, dst, weight="weight"))
        except (self._nx.NetworkXNoPath, self._nx.NodeNotFound):
            return None

    def topological_order(self):
        try:
            return list(self._nx.topological_sort(self._g))
        except self._nx.NetworkXUnfeasible:
            return None


# ------------------------------------------------------------ 工厂 ----

def make_backend(gx_path: str | None = None) -> GraphBackend:
    """优先 GX（设 GX_PATH 或默认 D:/Downloads/GX-1.5.3），失败回退 networkx。"""
    path = gx_path or os.environ.get("GX_PATH", r"D:/Downloads/GX-1.5.3")
    if os.environ.get("GNA_BACKEND", "gx").lower() == "networkx":
        return NXBackend()
    try:
        return GXBackend(path)
    except Exception:
        return NXBackend()
