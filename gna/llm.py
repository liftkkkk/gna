"""LLM 适配层：OpenAI 兼容端点 + Mock 离线实现 + 稳健 JSON 解析。

约定：Agent 各环节通过系统提示中的角色标记（GOAL_MAP/EXTRACT/REACT/SUMMARIZE/SYNTH/REPAIR）
声明自己的职责；Mock 模型据此返回确定性结构化结果，真实模型则按同一协议输出。
"""
from __future__ import annotations

import json
import re
from typing import List, Optional

from .config import Settings, default_workspace

Message = dict  # {"role": "system"|"user"|"assistant", "content": str}


class LLMError(RuntimeError):
    pass


class BaseLLM:
    def chat(self, messages: List[Message], temperature: Optional[float] = None) -> str:
        raise NotImplementedError

    def info(self) -> dict:
        return {"kind": self.__class__.__name__}


class OpenAICompatClient(BaseLLM):
    """任意 OpenAI 兼容端点。"""

    def __init__(self, settings: Settings):
        from openai import OpenAI

        self.settings = settings
        base_url = settings.resolved_base_url()
        if not base_url:
            raise LLMError("未配置 base_url：请设置 GNA_LLM_PROVIDER/BASE_URL 或选择提供商")
        self.client = OpenAI(api_key=settings.resolved_api_key() or "EMPTY",
                             base_url=base_url, timeout=120.0)
        self.model = settings.model

    def chat(self, messages: List[Message], temperature: Optional[float] = None) -> str:
        last_err: Optional[Exception] = None
        for _ in range(2):
            try:
                resp = self.client.chat.completions.create(
                    model=self.model, messages=messages,
                    temperature=self.settings.temperature if temperature is None else temperature)
                text = (resp.choices[0].message.content or "").strip()
                if not text:
                    raise LLMError("模型返回空内容")
                return text
            except Exception as e:  # noqa: BLE001
                last_err = e
        raise LLMError(f"LLM 调用失败（{self.model}）：{last_err}")

    def info(self) -> dict:
        return {"kind": "openai-compat", "model": self.model,
                "base_url": self.settings.resolved_base_url()}


# ---------------------------------------------------------------- Mock ----

_CN = r"([\u4e00-\u9fffA-Za-z0-9·\-]+)"


def extract_triples_fallback(text: str) -> List[tuple]:
    """规则兜底三元组抽取（无模型也可用）。先归一化中英混排空格，兼容 CLI 分词输入。"""
    text = re.sub(r"(?<=[\u4e00-\u9fffA-Za-z0-9])\s+(?=[\u4e00-\u9fff])", "", text or "")
    triples: List[tuple] = []
    pats = [
        (rf"{_CN}是{_CN}", ("{0}", "是", "{1}")),
        (rf"{_CN}喜欢{_CN}", ("{0}", "喜欢", "{1}")),
        (rf"{_CN}认识{_CN}", ("{0}", "认识", "{1}")),
        (rf"{_CN}在{_CN}工作", ("{0}", "任职于", "{1}")),
        (rf"{_CN}的{_CN}是{_CN}", ("{0}", "{1}", "{2}")),
    ]
    taken: List[tuple] = []
    for pat, tpl in pats:
        for m in re.finditer(pat, text):
            span = m.span()
            if any(s < span[1] and span[0] < e for s, e in taken):
                continue
            g = m.groups()
            triples.append(tuple(t.format(*g) for t in tpl))
            taken.append(span)
    return triples


class MockLLM(BaseLLM):
    """确定性离线模型：按角色标记返回结构化结果（演示 + 测试可复现）。"""

    def __init__(self, settings: Optional[Settings] = None):
        self.settings = settings

    def chat(self, messages: List[Message], temperature: Optional[float] = None) -> str:
        system = messages[0]["content"] if messages else ""
        user = next((m["content"] for m in reversed(messages) if m["role"] == "user"), "")
        if "GOAL_MAP" in system:
            return self._goal_map(user)
        if "EXTRACT" in system:
            return self._extract(user)
        if "REACT" in system:
            return self._react(messages, user)
        if "SUMMARIZE" in system:
            return self._summarize(user)
        if "SYNTH" in system:
            return "任务已完成：计划路径上的技能全部执行成功，状态更新已写回世界模型图（gna graph stats 可核对）。"
        if "REPAIR" in system:
            return json.dumps({"final": "（重试）基于已有信息作答：" + user[:160]}, ensure_ascii=False)
        return json.dumps({"final": user}, ensure_ascii=False)

    def _goal_map(self, user: str) -> str:
        m = re.search(r"关于(.+?)(的|，|。|,|$)", user)
        topic = (m.group(1) if m else "图神经网络").strip()
        notes_dir = (self.settings.resolved_workspace()
                     if self.settings else default_workspace()) / "notes"
        notes = sorted(notes_dir.glob("*.md"))
        target = notes_dir / f"{topic}.md"
        path = f"notes/{target.name}" if target.exists() else (f"notes/{notes[0].name}" if notes else "notes/图神经网络.md")
        return json.dumps({
            "need_plan": True, "topic": topic,
            "goal_assertions": [f"summary:{topic}", f"draft:{topic}", f"verified:{topic}"],
            "params": {"read_file": {"path": path}, "summarize_text": {"topic": topic},
                       "draft_report": {"topic": topic}, "verify_report": {"topic": topic}},
        }, ensure_ascii=False)

    def _extract(self, user: str) -> str:
        triples = extract_triples_fallback(user)
        ents: List[str] = []
        for s, _, o in triples:
            for e in (s, o):
                if e and e not in ents:
                    ents.append(e)
        return json.dumps({"entities": [{"name": e, "etype": "实体"} for e in ents[:12]],
                           "facts": [{"subject": s, "relation": r, "object": o}
                                     for s, r, o in triples[:12]]}, ensure_ascii=False)

    def _react(self, messages: List[Message], user: str) -> str:
        last_obs = next((m["content"] for m in reversed(messages)
                         if m["role"] == "user" and m["content"].startswith("OBSERVATION:")), "")
        # 有效输入 = 本轮用户原始请求（在含「用户输入：」的消息里；最后一条 user 可能是 OBSERVATION）
        orig = next((m["content"] for m in reversed(messages) if "用户输入：" in m["content"]), "")
        eff = orig.split("用户输入：", 1)[-1] if orig else user
        # 编码多步流：中间观察不收敛，继续写→跑→汇报
        if re.search(r"写.{0,12}(程序|代码|脚本)|编程", eff):
            if not last_obs:
                return json.dumps({"thought": "计划：1)写入 scripts/fib.py 2)运行验证 3)汇报",
                                   "action": "write_file",
                                   "action_input": {"path": "scripts/fib.py",
                                                    "content": "print('fib:', [1, 1, 2, 3, 5, 8, 13, 21, 34, 55])"}},
                                  ensure_ascii=False)
            if "已写入" in last_obs:
                return json.dumps({"thought": "运行验证", "action": "run_python",
                                   "action_input": {"path": "scripts/fib.py"}}, ensure_ascii=False)
            return json.dumps({"thought": "运行成功，汇报产出",
                               "final": "已创建 scripts/fib.py 并运行成功，输出：" + last_obs[:160]},
                              ensure_ascii=False)
        if last_obs:
            body = last_obs.replace("OBSERVATION:", "", 1).strip()
            return json.dumps({"thought": "已获得工具结果，整理作答", "final": body[:1200]}, ensure_ascii=False)
        if re.search(r"\d+\s*[\+\-\*/×÷]\s*\d+", eff):
            cands = re.findall(r"[\d\.\+\-\*/×÷\(\)\s%]+", eff)
            expr = next((c.strip() for c in sorted(cands, key=len, reverse=True)
                         if any(ch.isdigit() for ch in c)
                         and any(ch in "+-*/×÷" for ch in c)), "")
            expr = expr.replace("×", "*").replace("÷", "/")
            if expr:
                return json.dumps({"thought": "需要计算", "action": "calculate",
                                   "action_input": {"expression": expr}}, ensure_ascii=False)
        if re.search(r"几点|时间|日期|今天", eff):
            return json.dumps({"thought": "查询时间", "action": "current_time",
                               "action_input": {}}, ensure_ascii=False)
        m = re.match(rf"(?:请|帮我)?记住[：:]?\s*(.*)", eff)
        if m and re.search(rf"{_CN}(是|喜欢|认识|的){_CN}", m.group(1)):
            triples = extract_triples_fallback(m.group(1))
            if triples:
                s, r, o = triples[0]
                return json.dumps({"thought": "写入图谱记忆", "action": "add_fact",
                                   "action_input": {"subject": s, "relation": r, "object": o}},
                                  ensure_ascii=False)
        if re.search(r"图谱|记忆|知道什么|记得", eff):
            return json.dumps({"thought": "检索图记忆", "action": "query_graph",
                               "action_input": {"query": eff}}, ensure_ascii=False)
        mem = ""
        um = re.search(r"【图记忆检索】(.*?)(?:\n\n|$)", user, re.S)
        if um:
            mem = "根据图记忆：" + "；".join(x.strip("- ").strip() for x in um.group(1).strip().splitlines()[:3] if x.strip())
        return json.dumps({"thought": "直接回答",
                           "final": mem or ("（Mock 模式）已收到：" + eff[:60] + "。配置真实模型可获得完整能力。")},
                          ensure_ascii=False)

    def _summarize(self, user: str) -> str:
        lines = [ln.strip().lstrip("-*• ") for ln in user.splitlines()
                 if ln.strip() and not ln.strip().startswith("#")]
        bullets = [ln for ln in lines if len(ln) > 6][:4]
        return "要点如下：\n" + "\n".join(f"- {b}" for b in
                                          (bullets or ["（原文较短，未提取到显著要点）"]))


def make_llm(settings: Settings) -> BaseLLM:
    if settings.provider == "mock":
        return MockLLM(settings)
    return OpenAICompatClient(settings)


# ----------------------------------------------------------- JSON 工具 ----

def extract_json(text: str) -> Optional[dict]:
    """从模型输出稳健取出第一个平衡 JSON 对象（容忍代码围栏与前后缀）。"""
    text = re.sub(r"```(?:json)?", "", text or "")
    start = text.find("{")
    while start != -1:
        depth, in_str, esc = 0, False, False
        for i in range(start, len(text)):
            c = text[i]
            if in_str:
                if esc:
                    esc = False
                elif c == "\\":
                    esc = True
                elif c == '"':
                    in_str = False
            elif c == '"':
                in_str = True
            elif c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    try:
                        obj = json.loads(text[start:i + 1])
                        return obj if isinstance(obj, dict) else None
                    except json.JSONDecodeError:
                        break
        start = text.find("{", start + 1)
    return None
