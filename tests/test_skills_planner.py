"""技能图与图规划器测试（教材 8.2/8.3/8.8 口径）。"""
from gna.planner import GraphPlanner
from gna.skills import BUILTIN_SKILLS, derive_enables, fill, match, unify, validate


# ------------------------------------------------------------ 断言模板 ----

def test_unify_and_match():
    assert unify("summary:{topic}", "summary:图神经网络") == {"topic": "图神经网络"}
    assert unify("read:{path}", "read:notes/a.md") == {"path": "notes/a.md"}
    assert unify("summary:{topic}", "draft:图神经网络") is None
    assert match("read:*", "read:notes/a.md")
    assert not match("read:*", "write:x")
    assert match("session:ready", "session:ready")
    assert fill("summary:{topic}", {"topic": "图神经网络"}) == "summary:图神经网络"
    assert fill("summary:{topic}", {}, abstract=True) == "summary:*"


def test_derive_enables_shapes():
    edges = derive_enables(BUILTIN_SKILLS)
    assert ("session_start", "read_file") in edges
    assert ("read_file", "summarize_text") in edges
    assert ("summarize_text", "draft_report") in edges
    assert ("draft_report", "verify_report") in edges


def test_skill_graph_validate():
    rep = validate(None, BUILTIN_SKILLS)
    assert rep["skills"] == 13 and rep["edges"] >= 12
    assert rep["acyclic"] and rep["components"] == 1  # 连通无环（8.8 验证②）


# ------------------------------------------------------------ 图规划 ----

def test_plan_report_pipeline_and_precondition_chain():
    """8.8 验证③：路径中每个节点的前置均由前序节点效果（或初始状态）满足。"""
    p = GraphPlanner()
    goal = ["summary:X", "draft:X", "verified:X"]
    plan = p.plan(goal, set())
    assert plan.ok
    sk = {s.name: s for s in BUILTIN_SKILLS}
    state: set = set()
    for name, params in plan.path:
        s = sk[name]
        for pre in s.precondition:
            assert any(match(pre, a) for a in state), f"{name} 前置 {pre} 未被满足（{state}）"
        for eff in s.effect:
            state.add(fill(eff, params, abstract=True))
    assert names(plan) [0] == "session_start"
    for g in goal:  # 终态覆盖全部目标断言
        assert any(match(a, g) for a in state), f"目标 {g} 未被终态 {state} 覆盖"


def names(plan):
    return [s for s, _ in plan.path]


def test_plan_goal_already_satisfied():
    p = GraphPlanner()
    plan = p.plan(["summary:X"], {"summary:X"})
    assert plan.ok and plan.path == []


def test_plan_unreachable():
    p = GraphPlanner()
    assert not p.plan(["绝不存在的前缀:目标"], set()).ok


def test_plan_constraint_prunes_search():
    """8.4 约束在搜索前生效：deny_skill 封锁 read_file → summary 无来源 → 规划失败。"""
    p = GraphPlanner()

    class FakeBe:
        def find_edges(self, label=None):
            return [{"src": "constraint:c1", "dst": "skill:read_file"}]

        def get_node(self, nid):
            if nid == "constraint:c1":
                return {"attributes": {"rule": "deny_skill: 测试封锁读写"}}
            return None

    class FakeStore:
        be = FakeBe()

    plan = p.plan(["summary:X", "draft:X", "verified:X"], set(), store=FakeStore())
    assert not plan.ok
    assert "read_file" not in [s for s, _ in plan.path]


def test_goal_from_keywords():
    p = GraphPlanner()
    goals, need_plan, params = p.goal_from_keywords("帮我写一份关于知识图谱的报告")
    assert need_plan and "draft:知识图谱" in goals
