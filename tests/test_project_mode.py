"""项目模式测试：沙箱根切换、全局搜索、对话引擎写盘门控。"""
import pathlib

from gna.tools import ToolContext, build_tools, dispatch


def test_find_in_files(runtime, tmp_env):
    ctx = ToolContext(store=runtime.store, llm=runtime.llm,
                      workspace=tmp_env["workspace"], source="测试")
    tools = build_tools(ctx)
    ok, out = dispatch(tools["find_in_files"], ctx, {"pattern": r"def\s+\w+", "glob": "*.py"})
    assert ok and "find_in_files" not in out
    # conftest 里没有 py 文件 → 先造一个
    (tmp_env["workspace"] / "scripts").mkdir(exist_ok=True)
    (tmp_env["workspace"] / "scripts" / "mod.py").write_text("def hello():\n    return 1\n", encoding="utf-8")
    ok, out = dispatch(tools["find_in_files"], ctx, {"pattern": r"def hello", "glob": "*.py"})
    assert ok and "scripts/mod.py:1" in out


def test_list_dir_recursive(runtime, tmp_env):
    ctx = ToolContext(store=runtime.store, llm=runtime.llm,
                      workspace=tmp_env["workspace"], source="测试")
    (tmp_env["workspace"] / "pkg" / "sub").mkdir(parents=True, exist_ok=True)
    (tmp_env["workspace"] / "pkg" / "sub" / "a.py").write_text("x = 1\n", encoding="utf-8")
    tools = build_tools(ctx)
    ok, out = dispatch(tools["list_dir"], ctx, {"recursive": True})
    assert ok and "pkg/sub/a.py" in out


def test_chat_gate_blocks_write_when_disabled(runtime):
    evs = list(runtime.chat_turn("帮我写一个程序，输出斐波那契数列前10项并运行验证",
                                 confirm=lambda m: True, allow_write=False))
    answers = [e["text"] for e in evs if e["t"] == "answer"]
    assert answers  # 有回答
    assert not (runtime.settings.resolved_workspace() / "scripts" / "fib.py").exists() or \
        "fib.py" not in str(answers)
    traces = " ".join(e["line"] for e in evs if e["t"] == "trace")
    assert "门控拒绝" in traces


def test_workon_persists(tmp_path):
    from gna.config import load_settings, save_settings

    proj = tmp_path / "myproj"
    proj.mkdir()
    s = load_settings()
    s.project_root = str(proj)
    save_settings(s)
    s2 = load_settings()
    assert s2.project_root == str(proj)
    assert s2.resolved_workspace() == proj  # 沙箱根切到项目
    s2.project_root = ""
    save_settings(s2)
    assert load_settings().resolved_workspace() != proj  # 退出项目模式
