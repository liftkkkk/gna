"""CLI 冒烟测试：模拟 cmd 下 python -m gna 的调用面。"""
import json

import pytest

from gna.cli import build_parser, main


def run(argv):
    return main(argv)


def test_graph_stats(tmp_env, capsys):
    assert run(["--storage", str(tmp_env["tmp"] / "s.json"), "graph", "stats"]) == 0
    out = capsys.readouterr().out
    data = json.loads(out)
    assert data["nodes"] > 0 and data["backend"] in ("gx", "networkx")


def test_ask_and_memory_facts(tmp_env, capsys):
    storage = str(tmp_env["tmp"] / "s.json")
    run(["--storage", storage, "ask", "帮我算一下 12*12"])
    assert "144" in capsys.readouterr().out
    run(["--storage", storage, "memory", "add", "图原生智能体", "是", "教材第八章主题"])
    capsys.readouterr()
    run(["--storage", storage, "memory", "facts"])
    out = capsys.readouterr().out
    assert "session:ready" not in out  # 未跑任务，只有知识事实
    assert "｜" in out


def test_memory_query(tmp_env, capsys):
    storage = str(tmp_env["tmp"] / "s.json")
    run(["--storage", storage, "memory", "add", "张三", "是", "李四的同事"])
    capsys.readouterr()
    run(["--storage", storage, "memory", "query", "张三"])
    out = capsys.readouterr().out
    assert "图记忆检索" in out and "张三" in out


def test_skill_validate(tmp_env, capsys):
    assert run(["--storage", str(tmp_env["tmp"] / "s.json"), "skill", "validate"]) == 0
    out = capsys.readouterr().out
    assert "skills=15" in out and "acyclic=True" in out


def test_tool_call(tmp_env, capsys):
    assert run(["--storage", str(tmp_env["tmp"] / "s.json"),
                "tool", "call", "calculate", "--json", '{"expression": "2**10"}']) == 0
    assert "1024" in capsys.readouterr().out
    assert run(["--storage", str(tmp_env["tmp"] / "s.json"),
                "tool", "call", "read_file", "--json", '{"path": "../outside.txt"}']) == 1
    assert "沙箱" in capsys.readouterr().out


def test_llm_info_and_bad_path_sandbox(tmp_env, capsys):
    assert run(["--storage", str(tmp_env["tmp"] / "s.json"), "llm", "info"]) == 0
    assert "provider=mock" in capsys.readouterr().out


def test_parser_builds():
    p = build_parser()
    assert p.parse_args(["graph", "stats"]).fn is not None
