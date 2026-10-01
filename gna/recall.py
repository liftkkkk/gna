"""记忆召回（教材第 5 章 GraphRAG 模式一「局部子图」+ HippoRAG 传播检索思想）。

召回 = 图遍历，不建图外索引：
  种子定位（实体链接，可选语义入口） → 激活扩散（k 跳加权衰减） → 子图序列化。
"""
from __future__ import annotations

from typing import List, Optional

from .graph import RuntimeGraph

CTX_HEADER = "【图记忆检索】"


def recall_subgraph(store: RuntimeGraph, query: str, hops: int = 2, max_nodes: int = 48,
                    embedder=None, semantic_topk: int = 3) -> tuple[dict, set]:
    """返回 (激活值表, 命中节点集)。种子 = 实体链接 + 可选语义入口。"""
    seeds = store.entity_link(query)
    if embedder is not None and len(seeds) < 2:
        # 语义收敛入口：向量相似定锚，再交回图遍历扩展（第 3/5 章 召回-验证闭环）
        qv = embedder.embed(query)
        scored = []
        for nid, nd in store.be.nodes():
            if nd.get("class_") == "entity" and nd.get("embedding"):
                from .embedder import cosine

                scored.append((cosine(qv, nd["embedding"]), nid))
        scored.sort(reverse=True)
        seeds += [nid for sc, nid in scored[:semantic_topk] if sc > 0.05 and nid not in seeds]
    if not seeds:
        return {}, set()
    act = store.spreading_activation(seeds, hops=hops, max_nodes=max_nodes)
    return act, set(act.keys())


def format_context(store: RuntimeGraph, node_ids: set, max_lines: int = 24) -> str:
    """把召回子图序列化为 LLM 可读上下文（带来源与时间戳 = 证据链）。"""
    if not node_ids:
        return f"{CTX_HEADER}\n（暂无相关图记忆）"
    lines: List[str] = []
    for e in sorted(store.subgraph_edges(node_ids),
                    key=lambda x: str((x.get("attributes") or {}).get("ts") or "")):
        s, d = store.be.get_node(e["src"]), store.be.get_node(e["dst"])
        if not s or not d or e.get("label") in ("next", "logged"):
            continue
        sname, dname = s.get("name"), d.get("name")
        inv = "（已失效）" if (s.get("attributes") or {}).get("invalid") or \
                              (d.get("attributes") or {}).get("invalid") else ""
        src = (e.get("attributes") or {}).get("source", "?")
        ts = (e.get("attributes") or {}).get("ts", "")
        lines.append(f"- {sname} -[{e.get('label')}]-> {dname}{inv} 〈{src} {ts}〉")
    for nid in list(node_ids)[:max_lines]:
        nd = store.be.get_node(nid) or {}
        if nd.get("class_") == "turn":
            lines.append(f"- 回合「{str(nd.get('name'))[:40]}」：{str(nd.get('data'))[:80]}")
    if not lines:
        return f"{CTX_HEADER}\n（种子节点孤立，无可用子图）"
    return CTX_HEADER + "\n" + "\n".join(lines[:max_lines])


def recall_context(store: RuntimeGraph, query: str, hops: int = 2,
                   max_nodes: int = 48, embedder=None) -> str:
    act, ids = recall_subgraph(store, query, hops=hops, max_nodes=max_nodes, embedder=embedder)
    return format_context(store, ids)
