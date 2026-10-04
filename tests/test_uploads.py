"""上传能力测试：inbox 沙箱、zip 自动解压（zip-slip 防护）、PDF 阅读。"""
import io
import zipfile

from gna.tools import ToolContext, build_tools, dispatch
from gna.uploads import INBOX, extract_archive, save_upload


def _ctx(runtime, tmp_env):
    return ToolContext(store=runtime.store, llm=runtime.llm,
                       workspace=tmp_env["workspace"], source="测试",
                       extra_roots=[INBOX])


def test_save_upload_and_zip_slip_protection(runtime, tmp_env):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("paper/notes.md", "# 笔记\nhello")
        z.writestr("../../evil.txt", "x")          # zip-slip 逃逸尝试
        z.writestr("/abs/evil2.txt", "y")           # 绝对路径尝试
    dest = save_upload("pack.zip", buf.getvalue())
    assert dest.exists() and dest.parent == INBOX
    extracted, skipped = extract_archive(dest)
    assert "paper/notes.md" in extracted
    assert len(skipped) == 2                        # 两个危险条目被跳过
    assert not (tmp_env["workspace"] / "evil.txt").exists()


def test_inbox_in_sandbox(runtime, tmp_env):
    ctx = _ctx(runtime, tmp_env)
    dest = save_upload("hello.txt", "上传内容".encode("utf-8"))
    p = ctx.resolve(dest.name)                      # 相对路径：workspace 未命中 → inbox 命中
    assert p.exists() and "上传内容" in p.read_text(encoding="utf-8")
    ok, out = dispatch(build_tools(ctx)["read_file"], ctx, {"path": dest.name})
    assert ok and "上传内容" in out
    ok, out = dispatch(build_tools(ctx)["read_file"], ctx, {"path": "../../outside.txt"})
    assert not ok and "沙箱" in out                 # 越界仍被拒


def test_unzip_tool(runtime, tmp_env):
    ctx = _ctx(runtime, tmp_env)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("src/main.py", "print('hi')")
        z.writestr("src/utils.py", "x = 1")
    dest = save_upload("proj.zip", buf.getvalue())
    tools = build_tools(ctx)
    ok, out = dispatch(tools["unzip"], ctx, {"path": dest.name})
    assert ok and "src/main.py" in out
    extracted_dir = INBOX / f"{dest.stem}_extracted" / "src"
    assert (extracted_dir / "main.py").exists()


def test_read_pdf_extracts_text(runtime, tmp_env):
    """用 reportlab 生成一份带正文的 PDF → read_pdf 抽取。"""
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas as pdf_canvas

    pdf_path = INBOX / "demo_paper.pdf"
    c = pdf_canvas.Canvas(str(pdf_path), pagesize=A4)
    c.setFont("Helvetica", 12)
    c.drawString(72, 780, "Graph-Native Agent Runtime")
    c.drawString(72, 760, "Abstract: We propose GNA, a runtime where state, memory,")
    c.drawString(72, 742, "skills and audit are organized on a single in-memory graph.")
    c.drawString(72, 724, "Conclusion: the graph substrate beats context stuffing.")
    c.showPage()
    c.save()

    ctx = _ctx(runtime, tmp_env)
    tools = build_tools(ctx)
    ok, out = dispatch(tools["read_pdf"], ctx, {"path": pdf_path.name})
    assert ok
    assert "graph substrate" in out and "Abstract" in out
    txt = INBOX / "demo_paper.txt"
    assert txt.exists() and "GNA" in txt.read_text(encoding="utf-8")
