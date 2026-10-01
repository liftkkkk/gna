"""RuntimeGraph 核心行为：断言状态、知识三元组冲突、事件链、遍历原语、回滚、序列化。"""
from gna.graph import RuntimeGraph


def test_assertions_lifecycle(isolated_store):
    st = isolated_store
    assert st.assertions() == set()
    st.add_assertion("session:ready", "测试")
    st.add_assertion("read:notes/x.md", "测试")
    assert {"session:ready", "read:notes/x.md"} <= st.assertions()
    st.invalidate_assertion("read:notes/x.md", "测试", reason="过期")
    assert "read:notes/x.md" not in st.assertions()
    node = st.be.get_node("fact:read:notes/x.md")
    assert node["attributes"]["invalid"] and node["attributes"]["invalid_reason"] == "过期"


def test_knowledge_triple_and_conflict(isolated_store):
    st = isolated_store
    fid, conflict = st.add_fact_triple("张三", "任职于", "A公司", "测试")
    assert fid and not conflict
    fid2, conflict = st.add_fact_triple("张三", "任职于", "B公司", "测试")
    assert conflict  # 同 (s,r) 不同 o → 旧事实失效
    old = st.be.get_node(conflict)
    assert old["attributes"]["invalid"]
    valid = [n for n in st.find("fact", include_invalid=False)
             if n["attributes"].get("kind") == "knowledge"]
    assert len(valid) == 1 and valid[0]["attributes"]["object"] == "B公司"
    # 实体链接已建立
    assert st.entity_link("张三") == ["entity:张三"]


def test_entity_link_scoring(isolated_store):
    st = isolated_store
    st.add_node("entity", "图神经网络", "测试")
    st.add_node("entity", "图", "测试")
    st.add_node("entity", "神经网络架构", "测试")
    assert st.entity_link("图神经网络")[0] == "entity:图神经网络"
    assert "entity:图" in st.entity_link("图神经网络的相关内容")


def test_spreading_activation(isolated_store):
    st = isolated_store
    st.add_fact_triple("张三", "认识", "李四", "测试")
    st.add_fact_triple("李四", "认识", "王五", "测试")
    act = st.spreading_activation(["entity:张三"], hops=2)
    assert act["entity:张三"] == 1.0
    assert act.get("entity:李四", 0) > act.get("entity:王五", 0)  # 距离衰减
    assert set(act) <= {n for n in act if st.be.get_node(n)}


def test_episode_chain_and_replay(isolated_store):
    st = isolated_store
    st.add_assertion("a:1", "srcA")
    st.add_assertion("a:2", "srcB")
    chain = list(st.walk_episode_chain())
    assert len(chain) >= 2
    seqs = [c["attributes"]["seq"] for c in chain]
    assert seqs == sorted(seqs)
    assert chain[0]["attributes"]["op"] == "insert_node"


def test_rollback_last_step(isolated_store):
    st = isolated_store
    st._step_seq += 1
    step_id = "task:t/step:1"
    st.add_node("step", "步骤1", "测试", nid=step_id, props={"status": "running", "seq": 1})
    st.push_owner(step_id)
    st.add_assertion("calc:done", "技能 calculate")
    rid = st.add_node("result", "结果1", "测试")
    st.add_edge(step_id, "fact:calc:done", "produced", "测试")
    st.add_edge(step_id, rid, "produced", "测试")
    st.be.update_node(step_id, {"attributes": {"status": "ok"}})
    st.pop_owner()
    assert "calc:done" in st.assertions()
    r = st.rollback_last_step(actor="测试")
    assert r and r["skill"] is None and len(r["undone"]) == 2
    assert "calc:done" not in st.assertions()          # 效果断言已失效
    assert st.be.get_node(rid)["attributes"]["invalid"]  # 结果节点已失效（不删除）
    assert st.rollback_last_step() is None              # 无更多可回滚步骤


def test_save_load_roundtrip(tmp_path, isolated_store):
    st = isolated_store
    st.add_fact_triple("张三", "认识", "李四", "测试")
    st.add_assertion("session:ready", "测试")
    path = str(tmp_path / "rt.json")
    assert st.save(path) == path
    st2 = RuntimeGraph(storage_path=path)
    st2.load(path)
    assert st2.be.node_count() == st.be.node_count()
    assert st2.be.edge_count() == st.be.edge_count()
    assert "session:ready" in st2.assertions()
    assert st2.entity_link("张三") == ["entity:张三"]
    assert st2._ep_seq == st._ep_seq


def test_clear_memory_keeps_registry(isolated_store):
    from gna.skills import BUILTIN_SKILLS, sync_skills
    from gna.tools import ToolContext, build_tools
    from gna.config import Settings
    from gna.llm import MockLLM
    import pathlib

    st = isolated_store
    ctx = ToolContext(store=st, llm=MockLLM(), workspace=pathlib.Path("."))
    from gna.tools import sync_tool_nodes

    sync_tool_nodes(st, build_tools(ctx))
    sync_skills(st, BUILTIN_SKILLS)
    n_registry = st.be.node_count()
    st.add_assertion("session:ready", "测试")
    st.clear_memory(keep_registry=True)
    assert "session:ready" not in st.assertions()
    assert st.be.node_count() >= 26  # skill/tool/constraint 节点保留
    assert n_registry > 0
