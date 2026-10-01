"""一键演示：示例知识 → 图规划任务（读文件→摘要→起草→门控→验证）→ 图审计输出。

运行：python -m gna demo
"""
from __future__ import annotations

from .agent import AgentRuntime
from .cli import _print


DEMO_NOTES = {
    "图神经网络.md": """# 图神经网络（GNN）学习笔记

- GNN 的核心范式是消息传递：每个节点聚合邻居特征来更新自身表示。
- GCN 通过归一化拉普拉斯矩阵实现谱方法卷积。
- GAT 引入注意力机制，为不同邻居分配不同权重。
- 过平滑问题：层数过深时节点表示趋于同质，通常 2-3 层即可。
- 典型任务：节点分类、链路预测、图分类。
""",
    "知识图谱.md": """# 知识图谱学习笔记

- 知识图谱以三元组（主体-关系-客体）组织结构化知识。
- 资源描述框架 RDF 与属性图是两大数据模型路线。
- 知识图谱嵌入（KGE）把实体关系映射到向量空间，支持链接预测。
- 图增强检索（GraphRAG）用子图上下文弥补向量检索的语义鸿沟。
- 图原生智能体把世界模型、技能与约束都表达为图结构。
""",
}

DEMO_FACTS = [
    ("图神经网络", "属于", "深度学习"),
    ("知识图谱", "属于", "图技术"),
    ("图原生智能体", "结合", "图技术与大模型"),
    ("GX引擎", "提供", "内存图存储"),
]


def run_demo() -> int:
    rt = AgentRuntime()
    ws = rt.settings.resolved_workspace()
    for fname, content in DEMO_NOTES.items():
        p = ws / "notes" / fname
        if not p.exists():
            p.write_text(content, encoding="utf-8")
            _print(f"[init] 写入示例笔记 notes/{fname}")
    for s, r, o in DEMO_FACTS:
        rt.store.add_fact_triple(s, r, o, "示例数据")
    _print(f"[init] 示例知识已入图；LLM={rt.settings.provider}/{rt.settings.model}")

    _print("\n===== 演示任务：图规划 → 执行 → 审计 =====")
    goal = "请整理关于图神经网络的要点，写一份简报并验证"
    _print(f"目标：{goal}\n")
    answer = ""
    for ev in rt.executor.run_task(goal, confirm=lambda m: True):
        from .cli import _render_event

        _render_event(ev)
        if ev.get("t") == "done":
            answer = ev.get("answer", "")

    st = rt.stats()
    _print("\n===== 图审计（四判据核对） =====")
    _print(f"[状态可寻址] 当前断言集 {st['assertions']} 条：")
    for a in sorted(rt.store.assertions()):
        _print(f"  - {a}")
    _print(f"[证据可追溯] 事件链 {st['events']} 条、图规模 {st['nodes']} 节点 / {st['edges']} 边（backend={st['backend']}）")
    rep = rt.skill_report()
    _print(f"[技能可验证] {rep['report']}")
    _print("[约束可执行] 沙箱/门控/失效不删除 三条约束已上图")
    _print("\n继续探索：python -m gna chat | python -m gna graph viz | python -m gna memory facts")
    return 0
