"""编码执行能力测试：run_python / run_code 工具 + 对话引擎自主写程序并运行。"""
import pathlib

from gna.tools import ToolContext, build_tools, dispatch


def _ctx(runtime, tmp_env):
    return ToolContext(store=runtime.store, llm=runtime.llm,
                       workspace=tmp_env["workspace"], source="测试")


def test_run_code_writes_and_executes(runtime, tmp_env):
    ctx = _ctx(runtime, tmp_env)
    tools = build_tools(ctx)
    ok, out = dispatch(tools["run_code"], ctx, {"code": "print(2 + 3)", "filename": "t1.py"})
    assert ok and "5" in out
    f = tmp_env["workspace"] / "scripts" / "t1.py"
    assert f.exists() and "print(2 + 3)" in f.read_text(encoding="utf-8")
    # ΔW 留痕 + 断言上图（四判据②）
    assert "ran:scripts/t1.py" in runtime.store.assertions()
    assert "written:scripts/t1.py" in runtime.store.assertions()
    ops = [e["attributes"]["op"] for e in runtime.store.walk_episode_chain()]
    assert "tool_call" in ops


def test_run_python_stderr_and_missing(runtime, tmp_env):
    ctx = _ctx(runtime, tmp_env)
    tools = build_tools(ctx)
    ok, out = dispatch(tools["run_code"], ctx,
                       {"code": "import sys; print('到stderr', file=sys.stderr); sys.exit(3)",
                        "filename": "bad.py"})
    assert not ok and "[stderr]" in out
    ok, out = dispatch(tools["run_python"], ctx, {"path": "scripts/不存在.py"})
    assert not ok and "不存在" in out


def test_run_timeout(runtime, tmp_env):
    ctx = _ctx(runtime, tmp_env)
    tools = build_tools(ctx)
    ok, out = dispatch(tools["run_code"], ctx,
                       {"code": "import time; time.sleep(5)", "filename": "slow.py",
                        "timeout": 2})
    assert not ok and "超时" in out


def test_sandbox_blocks_escape(runtime, tmp_env):
    ctx = _ctx(runtime, tmp_env)
    tools = build_tools(ctx)
    ok, out = dispatch(tools["run_python"], ctx, {"path": "../../evil.py"})
    assert not ok and "沙箱" in out


def test_chat_writes_and_runs_program(runtime, tmp_env):
    """对话引擎自主规划：写文件 → 运行 → 汇报（Mock 三步流）。"""
    evs = list(runtime.chat_turn("帮我写一个程序，输出斐波那契数列前10项并运行验证",
                                 confirm=lambda m: True))
    answers = [e["text"] for e in evs if e["t"] == "answer"]
    assert any("scripts/fib.py" in a for a in answers)
    assert any("fib:" in a for a in answers)
    f = tmp_env["workspace"] / "scripts" / "fib.py"
    assert f.exists()
    assert "ran:scripts/fib.py" in runtime.store.assertions()
    # 轨迹里能看到计划与两步工具调用
    traces = " ".join(e["line"] for e in evs if e["t"] == "trace")
    assert "write_file" in traces and "run_python" in traces
