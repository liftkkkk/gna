"""图后端适配层测试：GX 与 networkx 双后端等价性。"""
import pytest

from gna.backend import NXBackend, make_backend


def _exercise(be):
    assert be.add_node("entity:A", "A", "entity") == "created"
    assert be.add_node("entity:A", "A", "entity") == "exists"
    be.add_node("entity:B", "B", "entity")
    be.add_node("entity:C", "C", "entity")
    assert be.add_edge("entity:A", "entity:B", "知道") is True
    assert be.add_edge("entity:A", "entity:B", "知道") is False  # 同 label 去重
    assert be.add_edge("entity:A", "entity:missing", "x") is False
    assert be.has_edge("entity:A", "entity:B", "知道")
    outs = be.out_edges("entity:A")
    assert any(d["label"] == "知道" for _, d in outs)
    ins = be.in_edges("entity:B")
    assert any(src == "entity:A" for src, _ in ins)
    both = be.neighbors("entity:B")
    assert any(n == "entity:A" for n, _ in both)
    assert be.node_count() == 3 and be.edge_count() == 1
    nd = be.get_node("entity:A")
    assert nd["name"] == "A" and nd["class_"] == "entity"
    be.update_node("entity:A", {"attributes": {"k": 1}})
    assert be.get_node("entity:A")["attributes"]["k"] == 1
    assert be.shortest_path("entity:A", "entity:C") is None
    be.add_edge("entity:B", "entity:C", "认识")
    assert be.shortest_path("entity:A", "entity:C") == ["entity:A", "entity:B", "entity:C"]
    hits = be.find_by_name_sub("b")
    assert hits and hits[0][0] == "entity:B"
    es = be.find_edges(label="认识")
    assert len(es) == 1 and es[0]["src"] == "entity:B"


def test_nx_backend():
    _exercise(NXBackend())


def test_gx_backend_or_fallback():
    be = make_backend()
    _exercise(be)
    assert be.name in ("gx", "networkx")


def test_topological_order():
    be = make_backend()
    for n in ("s1", "s2", "s3"):
        be.add_node(n, n, "skill")
    be.add_edge("s1", "s2", "enables")
    be.add_edge("s2", "s3", "enables")
    order = be.topological_order()
    assert order and order.index("s1") < order.index("s2") < order.index("s3")
