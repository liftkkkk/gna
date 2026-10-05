# GNA · 图原生智能体运行时（Graph-Native Agent）

> **一切皆图，一切查找皆图遍历，一切变更留痕。**
> 给 LLM 一张随时能查、能改、能回放的"世界地图"——Agent 的脑子长在图上。

GNA 是一个 **agent runtime**：把 Agent 的**状态、记忆、技能、任务、约束、审计**全部组织在**同一张内存图**上，
上层接任何 OpenAI 兼容大模型（GLM / DeepSeek / Kimi / OpenAI / Ollama / 自定义，无 Key 可跑 Mock），
底层支持自研图引擎 [GX](#gx-引擎集成) 与 networkx 双后端。

## 图原生四判据（与"图当配件"的分界）

| 判据 | GNA 的落实 |
|---|---|
| **状态在图上可寻址** | 当前世界状态 = 有效断言（fact 节点）集合，任何状态问题都是一次图遍历 |
| **证据链在图上可追溯** | 每个节点/边携带 `ts + source`；结论沿 `produced/states` 边回溯到回合或工具返回 |
| **行动与技能可机器验证** | 技能节点携带前置/效果**断言模板**，执行前后自动核对（支持通配/参数合一） |
| **约束在搜索前生效** | 约束节点经 `constrains` 边声明 `deny_skill:`（规划前封锁）/ `restrict:`（沙箱强制）/ `gate:`（人工门控） |
| **能直接干活（执行型）** | `run_code`（写入并运行）/ `run_python`：LLM 生成的程序落盘 `workspace/scripts/`、子进程执行（60s 超时）、输出回传，报错自我修复；每次执行以 `tool_call` 事件与 `written:/ran:` 断言上图 |

## 30 秒上手

```cmd
:: Windows cmd（anaconda python）
cd graph-native-agent
pip install networkx pytest
set GNA_LLM_PROVIDER=mock
python -m gna demo

python -m gna chat            :: 交互式 REPL（/help 看命令）
python -m gna graph stats     :: 图统计
python -m gna graph viz       :: 生成交互式 HTML 可视化（需 pyvis）
gna web                       :: 自研 HTML 前端（http://127.0.0.1:8000，默认，流式）
gna web-gradio                :: Gradio 旧版视图   gna web-st :: Streamlit 旧版视图
```

> 双前端共享同一 headless 内核与 `~/.gna` 图存储；模型配置（多方案、持久化、热切换）见 `~/.gna/models.json`。
> 注意：v0.2 增量持久化落地前，建议同一时间只用一个前端写入。

接真实大模型（任选其一，其余走 OpenAI 兼容）：

```cmd
set GNA_LLM_PROVIDER=zhipu
set GNA_LLM_API_KEY=你的key
set GNA_LLM_MODEL=glm-4.6
python -m gna chat
```

## 架构

```
CLI（本期） │ Gradio/Web（规划中） │ MCP（规划中）
──────────────────────────────────────────────
AgentRuntime：感知→入图→遍历召回→LLM推理→行动→ΔW写回
   ├ 对话引擎：ReAct 工具循环（工具含图操作）
   └ 任务引擎：目标断言化 → 图规划(A*) → 执行器(门控/重规划)
──────────────────────────────────────────────
图原生机制层：extract(文本→图谱) │ recall(激活扩散) │
              tools(工具即图节点) │ skills(技能图) │
              planner(路径即计划) │ executor(ΔW写回)
──────────────────────────────────────────────
RuntimeGraph：类型化节点 │ 断言状态 │ episode 事件时间链 │ 遍历原语 │ 补偿回滚
──────────────────────────────────────────────
GXBackend（GX-1.5.3，默认）│ NXBackend（networkx 回退）
```

一条示例执行轨迹（`python -m gna run "请整理关于图神经网络的要点，写一份简报并验证"`）：

```
PLAN: session_start -> read_file -> summarize_text -> extract_facts -> draft_report -> verify_report
  [1/6] session_start        OK   （结构技能：仅推进状态断言）
  [2/6] read_file            OK   读取 notes/图神经网络.md
  [3/6] summarize_text       OK   要点如下：…
  [4/6] extract_facts        OK   入图完成：实体 4 条、事实 2 条
  [5/6] draft_report         OK   报告已起草（引用图谱证据 2 条）  ← 人工门控点
  [6/6] verify_report        OK   验证通过：2 条证据引用全部可回溯到有效图谱节点
```

## CLI 命令总览

| 命令 | 说明 |
|---|---|
| `gna chat` | 交互式 REPL：`/graph` `/facts` `/path A B` `/tools` `/skills` `/rollback` `/reset` |
| `gna ask "问题"` | 单轮问答 |
| `gna run "任务"` | 图规划 + 执行（`--yes` 自动确认门控） |
| `gna demo` | 一键演示（示例知识 → 任务 → 四判据审计输出） |
| `gna graph stats\|nodes\|edges\|neighbors\|path\|events\|export\|viz` | 图操作（遍历/最短路/事件回放/可视化） |
| `gna memory add\|query\|facts\|rollback\|clear` | 图记忆（写入走门控：去重/冲突→失效标记） |
| `gna skill list\|show\|validate\|run` | 技能图（validate 输出 `skills=13 edges=17 acyclic=True components=1`） |
| `gna tool list\|call` | 原子工具（含沙箱约束演示） |
| `gna llm info\|test\|set` | LLM 配置与连通性 |

## 统一图模型（Schema）

节点 11 类：`entity` 对象 · `fact` 事实/断言 · `turn` 回合 · `task` 任务 · `step` 执行步 ·
`result` 结果 · `skill` 技能 · `tool` 工具 · `constraint` 约束 · `goal` 目标 · `episode` 事件。

关键边：`mentions`（回合→实体）· `states`（溯源）· `requires` / `has_effect`（断言模板）·
`enables`（技能依赖，自动推导）· `uses_tool` · `constrains` · `has_step` · `produced`（证据链）·
`next`（episode 时间链）· `supersedes`（冲突替代）。

**ΔW 事件溯源**：每个原子变更都是一个 `episode` 节点，串成 `next` 时间链——可回放（`gna graph events`）、
可回滚（`gna memory rollback`，补偿式失效，历史永不物理删除）。

## 设计文档

完整产品设计（公理、Schema、遍历原语映射、运行时循环、GX 集成、Roadmap）见
[docs/design.md](docs/design.md)。

## GX 引擎集成

GNA 默认使用自研图引擎 **GX-1.5.3**（内存图、邻接表、名称索引、Dijkstra、拓扑序、分区/社区发现）：

```cmd
:: GX 图引擎为主后端（最高优先级）——GNA 启动时自动探测（GX_PATH → 当前目录 → 下载目录的 GX-*），
:: 探测到即使用 GX；仅当 GX 不可用时才回退 networkx 兜底。
set GX_PATH=D:\path	o\GX-1.5.3     :: 显式指定（通常不需要，会自动探测）
set GNA_BACKEND=networkx              :: 强制回退 networkx（调试用）
```

> 开源仓库本身不包含 GX 引擎代码（GNA 通过适配层在运行时引用它，详见 docs/gx-packaging.md）；
> 没有 GX 的环境自动回退 networkx，功能完整。

适配细节：GX 邻接表只存出边（GNA 自建惰性反向索引）；GX 原生序列化丢 `data/embedding`
（GNA 在 RuntimeGraph 层做全保真 JSON 原子写，不经 GX 的 save()）；存储路径 `GNA_STORAGE`
（默认 `~/.gna/runtime.json`）。

## 配置

| 环境变量 | 说明 | 默认 |
|---|---|---|
| `GNA_LLM_PROVIDER` | mock / zhipu / deepseek / moonshot / openai / ollama / custom | mock |
| `GNA_LLM_API_KEY` / `GNA_LLM_MODEL` / `GNA_LLM_BASE_URL` | 模型接入 | - |
| `GNA_STORAGE` | 图存储文件 | `~/.gna/runtime.json` |
| `GNA_WORKSPACE` | 文件工具沙箱根目录 | `~/.gna/workspace` |
| `GX_PATH` | GX 引擎目录（**主后端**，自动探测；networkx 仅为兜底） | 自动探测 |
| `GNA_EMBEDDING_BACKEND` | mock / st / openai（语义召回，可选） | mock |

## 开发

```cmd
pip install -e ".[dev]"
python -m pytest -q          :: 35 个用例（双后端/规划/执行/审计/CLI 冒烟，全离线）
python examples/quickstart.py
```

## Roadmap

- [x] **v0.1 Core + CLI**：单图运行时、双引擎、GX 双后端、事件溯源/回滚、技能图 13 节点
- [ ] v0.2 前端：Gradio 多页（对话 / 图谱世界 / 执行审计 / 设置）
- [ ] v0.3 多智能体图拓扑（链/树/DAG/网），消息历史外化为共享执行图
- [ ] v0.4 自演化：经验图热启动规划、技能健康度自动升降权、轨迹→技能提取
- [ ] v0.5 MCP server 化、技能包导入导出、GraphML 互通

## License

MIT
