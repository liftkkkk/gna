"""路径引用（通用接口）测试：消息内本地路径识别、文件/文件夹区分、会话挂载。"""
import pytest

from gna.agent import AgentRuntime


@pytest.fixture()
def ref_paths(tmp_path):
    folder = tmp_path / "my_notes"
    (folder / "src").mkdir(parents=True)
    (folder / "src" / "a.py").write_text("print('a')\n", encoding="utf-8")
    (folder / "readme.md").write_text("# 说明\n内容行", encoding="utf-8")
    file = tmp_path / "report.pdf"
    file.write_bytes(b"%PDF-1.4 fake")
    return {"folder": folder, "file": file}


def test_detect_paths_distinguishes_file_and_dir(runtime, ref_paths):
    text = f"帮我看一下 {ref_paths['folder']} 里的内容，以及 \"{ref_paths['file']}\" 这篇"
    detected = runtime.detect_paths(text)
    assert ref_paths["folder"].resolve().as_posix() in [d.replace("\\", "/") for d in detected] or \
        str(ref_paths["folder"].resolve()) in detected
    assert str(ref_paths["file"].resolve()) in detected


def test_detect_paths_ignores_nonexistent(runtime):
    assert runtime.detect_paths("看看 D:\\不存在的路径\\xyz") == []


def test_attach_mounts_into_both_engine_sandboxes(runtime, ref_paths):
    infos = runtime.attach([str(ref_paths["folder"]), str(ref_paths["file"])])
    kinds = {i["type"] for i in infos}
    assert kinds == {"dir", "file"}
    dir_info = next(i for i in infos if i["type"] == "dir")
    assert dir_info["n_files"] == 2 and "a.py" in dir_info["samples"]
    # 双引擎沙箱同步：chat ctx 与 executor ctx 都能解析挂载路径下的文件
    rel_probe = "src/a.py"
    p1 = runtime.ctx.resolve(rel_probe)
    p2 = runtime.executor.ctx.resolve(rel_probe)
    assert p1.exists() and p2.exists()
    assert "readme.md" in runtime.ctx.rel(runtime.ctx.resolve("readme.md"))


def test_chat_turn_auto_mounts_and_notes(runtime, ref_paths):
    text = f"浏览一下 {ref_paths['folder']} 这个文件夹"
    evs = list(runtime.chat_turn(text, confirm=lambda m: True))
    traces = " ".join(e["line"] for e in evs if e["t"] == "trace")
    assert "已挂载 1 个本地路径" in traces
    turn = next(n for n in runtime.store.find("turn") if "浏览一下" in str(n.get("data")))
    assert "【用户引用的本地路径" in str(turn.get("data"))
    assert "[文件夹]" in str(turn.get("data")) and "2 个文件" in str(turn.get("data"))


def test_path_browse_routes_to_react_not_planner(runtime, ref_paths):
    """贴路径 + 浏览类动词 → ReAct 工具循环（而非图规划引擎）。"""
    text = f"帮我看看 {ref_paths['folder']} 这个文件夹里有什么，用中文总结要点"
    evs = list(runtime.chat_turn(text, confirm=lambda m: True))
    traces = " ".join(e["line"] for e in evs if e["t"] == "trace")
    assert "ReAct 对话引擎" in traces
    assert "图规划任务引擎" not in traces


def test_path_with_report_goal_still_plans(runtime, ref_paths):
    """贴路径 + 产出物要求（写简报）→ 仍走图规划任务引擎。"""
    text = f"根据 {ref_paths['folder']} 里的材料，写一份报告"
    evs = list(runtime.chat_turn(text, confirm=lambda m: True))
    traces = " ".join(e["line"] for e in evs if e["t"] == "trace")
    assert "图规划任务引擎" in traces
