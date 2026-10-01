"""文本 → 图谱（教材第 7 章 分层管线的最小闭环；第 6.3 冲突检测与合并）。

写入门控（graph.py.add_fact_triple 内统一执行）：
- 去重：同一 (主体,关系,客体) 不重复落图；
- 冲突：同一 (主体,关系) 出现新客体 → 新事实入图 + 旧事实失效（不删除，保历史）；
- 实体链接：能对上既有实体就不新建（entity_link 遍历打分）。
"""
from __future__ import annotations

from typing import Optional

from .graph import RuntimeGraph
from .llm import BaseLLM, extract_json, extract_triples_fallback

EXTRACT_PROMPT = """[角色:EXTRACT] 你是图谱抽取器。从用户文本中抽取实体与知识三元组，只输出 JSON：
{"entities": [{"name": "...", "etype": "人物|组织|概念|地点|其他"}],
 "facts": [{"subject": "...", "relation": "...", "object": "..."}]}
要求：relation 用简短中文谓词（如 是/属于/任职于/位于/发明）；不要编造文本中没有的内容。"""


def extract_from_text(llm: BaseLLM, text: str) -> dict:
    """LLM 抽取，失败回退规则抽取。返回 {"entities": [...], "facts": [...]}。"""
    ents, facts = [], []
    try:
        obj = extract_json(llm.chat([{"role": "system", "content": EXTRACT_PROMPT},
                                     {"role": "user", "content": text}], temperature=0.0))
        if obj:
            ents = [e for e in obj.get("entities", []) if isinstance(e, dict) and e.get("name")]
            facts = [f for f in obj.get("facts", [])
                     if isinstance(f, dict) and f.get("subject") and f.get("object")]
    except Exception:
        pass
    if not facts:
        triples = extract_triples_fallback(text)
        facts = [{"subject": s, "relation": r, "object": o} for s, r, o in triples]
        ents = [{"name": e, "etype": "实体"} for e in dict.fromkeys(
            [f["subject"] for f in facts] + [f["object"] for f in facts])]
    return {"entities": ents, "facts": facts}


def ingest(store: RuntimeGraph, llm: BaseLLM, text: str, turn_id: Optional[str] = None,
           source: str = "对话抽取") -> dict:
    """把一轮文本入图（实体链接 + 事实写入 + 回合提及边）。返回写入统计。"""
    ext = extract_from_text(llm, text)
    n_ent = n_fact = n_conflict = 0
    fact_ids = []
    for e in ext["entities"]:
        name = str(e["name"]).strip()
        if not name:
            continue
        linked = store.entity_link(name, limit=1)
        if linked and store.be.get_node(linked[0]).get("name", "").lower() == name.lower():
            nid = linked[0]
        else:
            nid = store.add_node("entity", name, source,
                                 props={"etype": e.get("etype", "实体")})
        if turn_id:
            store.add_edge(turn_id, nid, "mentions", source)
        n_ent += 1
    for f in ext["facts"]:
        s, r, o = (str(f["subject"]).strip(), str(f.get("relation") or "相关").strip(),
                   str(f["object"]).strip())
        if not (s and o):
            continue
        fid, conflict = store.add_fact_triple(s, r, o, source, turn_id=turn_id)
        fact_ids.append(fid)
        n_fact += 1
        n_conflict += 1 if conflict else 0
    return {"entities": n_ent, "facts": n_fact, "conflicts": n_conflict, "fact_ids": fact_ids}
