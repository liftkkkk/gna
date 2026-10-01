"""GNA SDK 快速上手：同一张图上完成 记忆写入 → 遍历召回 → 图规划任务 → 审计。

运行：python examples/quickstart.py
"""
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # 零安装：直接用仓库源码

from gna.agent import AgentRuntime
from gna.config import Settings

tmp = tempfile.mkdtemp()
settings = Settings(provider="mock", model="mock-1",           # 离线 Mock；接真实模型改 provider/model/api_key
                    storage_path=os.path.join(tmp, "rt.json"),
                    workspace=os.path.join(tmp, "workspace"))
os.makedirs(os.path.join(tmp, "workspace", "notes"), exist_ok=True)
with open(os.path.join(tmp, "workspace", "notes", "知识图谱.md"), "w", encoding="utf-8") as f:
    f.write("# 知识图谱\n- 知识图谱以三元组组织结构化知识\n- GraphRAG 用子图上下文增强检索\n")

rt = AgentRuntime(settings=settings)

# 1) 对话：记忆写入（一切变更留痕在图上）
for ev in rt.chat_turn("记住：GX引擎 是 图原生智能体的图底座"):
    if ev["t"] == "answer":
        print("GNA:", ev["text"][:80])

# 2) 任务：图规划（路径即计划）→ 执行（ΔW 写回 + 人工门控）
for ev in rt.executor.run_task("请整理关于知识图谱的要点，写一份简报并验证", confirm=lambda m: True):
    if ev["t"] == "plan":
        print("计划：", " -> ".join(ev["path"]))
    elif ev["t"] == "step":
        print(f"  步骤{ev['index']} {ev['skill']}: {'OK' if ev['ok'] else 'FAIL'}")
    elif ev["t"] == "done":
        print("完成：" if ev["ok"] else "失败：", ev["answer"][:60])

# 3) 审计：世界状态（断言集）与事件链
print("当前断言集：", sorted(rt.store.assertions()))
print("图统计：", rt.stats())
