"""AgentRuntime 端到端（Mock LLM，全离线）：对话引擎 / 任务引擎 / 8.8 审计口径。"""
import json


def _events(rt, text, **kw):
    return list(rt.chat_turn(text, **kw))


def _answers(evs):
    return [e["text"] for e in evs if e["t"] == "answer"]


# ------------------------------------------------------------ 对话引擎 ----

def test_chat_memory_write_and_query(runtime):
    evs = _events(runtime, "记住：张三是李四的同事")
    assert any("已写入事实节点" in a for a in _answers(evs))
    assert "张三" in runtime.store.assertions().__str__() or \
        any(n["attributes"].get("subject") == "张三" for n in runtime.store.find("fact"))
    evs2 = _events(runtime, "你在图谱里记得关于张三的什么？")
    assert any("张三" in a for a in _answers(evs2))


def test_chat_calculator(runtime):
    evs = _events(runtime, "帮我算一下 365*3")
    assert any("1095" in a for a in _answers(evs))


def test_chat_time(runtime):
    evs = _events(runtime, "现在几点了？")
    assert any("当前时间" in a for a in _answers(evs))


def test_extraction_writes_knowledge(runtime):
    _events(runtime, "图原生智能体是图技术与大模型的结合产物")
    facts = [n for n in runtime.store.find("fact")
             if n["attributes"].get("kind") == "knowledge"]
    assert any("图原生智能体" in (n["attributes"].get("subject") or "") for n in facts)


# ------------------------------------------------------------ 任务引擎 ----

def test_task_report_pipeline_880(runtime, tmp_env):
    """8.8：≥3 次工具调用、每次调用后图有带时间戳的更新、输出路径、结果可复现。"""
    goal = "请整理关于图神经网络的要点，写一份简报并验证"
    events = list(runtime.executor.run_task(goal, confirm=lambda m: True))
    done = [e for e in events if e["t"] == "done"][0]
    assert done["ok"]
    # 计划路径（节点序列）
    plan = [e for e in events if e["t"] == "plan"][0]
    assert len(plan["path"]) >= 5
    # 工具调用 ≥3
    steps = [e for e in events if e["t"] == "step"]
    assert len([s for s in steps if s["ok"]]) >= 3
    # 每次调用后图有更新：事件链持续增长且带 ts/source
    assert runtime.store._ep_seq > 20
    ep = list(runtime.store.walk_episode_chain())[-1]
    assert ep["attributes"]["ts"] and ep["attributes"]["source"]
    # 效果断言齐备
    assert {"summary:图神经网络", "draft:图神经网络", "verified:图神经网络"} \
        <= runtime.store.assertions()
    # 报告文件落盘且含证据引用
    report = tmp_env["workspace"] / "reports" / "图神经网络.md"
    assert report.exists()
    assert "【F:" in report.read_text(encoding="utf-8")


def test_task_gate_denial_cancels(runtime):
    goal = "写一份关于知识图谱的简报并验证"
    events = list(runtime.executor.run_task(goal, confirm=lambda m: False))
    done = [e for e in events if e["t"] == "done"][0]
    assert not done["ok"]
    assert "取消" in done["answer"] or "门控" in done["answer"]
    gates = [e for e in events if e["t"] == "gate"]
    assert gates and gates[0]["allowed"] is False


def test_replan_on_skill_failure(runtime, monkeypatch):
    """read_file 失败一次 → 健康度衰减 + 局部重规划后仍可完成。"""
    from gna.tools import build_tools

    original = runtime.executor.tools["read_file"].fn

    calls = {"n": 0}

    def flaky(ctx, path):
        calls["n"] += 1
        if calls["n"] == 1:
            return False, "模拟读取失败"
        return original(ctx, path=path)

    runtime.executor.tools["read_file"].fn = flaky
    events = list(runtime.executor.run_task("请整理关于图神经网络的要点，写一份简报并验证",
                                            confirm=lambda m: True))
    done = [e for e in events if e["t"] == "done"][0]
    assert done["ok"]
    assert any(e.get("replan") for e in events if e["t"] == "plan")
    skill_node = next(n for n in runtime.store.find("skill")
                      if n["attributes"].get("title") == "读文件")
    assert skill_node["attributes"]["success_rate"] < 0.95  # 健康度已衰减


def test_rollback_undoes_last_skill(runtime):
    list(runtime.executor.run_task("请整理关于图神经网络的要点，写一份简报并验证",
                                   confirm=lambda m: True))
    before = runtime.store.assertions()
    assert "verified:图神经网络" in before
    r = runtime.store.rollback_last_step(actor="测试")
    assert r["skill"] == "verify_report"
    assert "verified:图神经网络" not in runtime.store.assertions()


def test_reproducibility_three_runs(tmp_env):
    """8.8 验证⑤：重复运行一致性。"""
    import copy

    results = []
    for i in range(3):
        from gna.agent import AgentRuntime

        s = copy.deepcopy(tmp_env["settings"])
        s.storage_path = str(tmp_env["tmp"] / f"rt{i}.json")
        rt = AgentRuntime(settings=s)
        list(rt.executor.run_task("请整理关于图神经网络的要点，写一份简报并验证",
                                  confirm=lambda m: True))
        results.append(sorted(rt.store.assertions()))
    assert results[0] == results[1] == results[2]
    assert {"summary:图神经网络", "draft:图神经网络", "verified:图神经网络"} <= set(results[0])
