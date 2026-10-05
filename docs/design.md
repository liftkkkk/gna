# GNA · 图原生智能体运行时 —— 产品设计文档

> **Graph-Native Agent (GNA)**：一个把 Agent 的**状态、记忆、技能、任务、约束、审计**全部组织在同一张内存图上的智能体运行时。
> 一切皆图，一切查找皆图遍历，一切变更皆留痕。

- 版本：v0.1（Core + CLI 阶段）
- 状态：设计中 → 实现中
- 关联理论：《图原生智能体（Graph × LLM × Agent 技术详解）》第 5–9 章

---

## 0. 北极星：图是地基，不是配件（设计总纲）

GNA 与"表面相似"的 agent 框架的本质区别只有一条：**上层的一切都构建在同一张图之上**。
对话、任务、多智能体、技能市场、前端、MCP——表面看起来都一样（能聊天、能调工具、能干活），
但底层是图还是"拍脑袋的拼装"，决定的是运行效率、复杂功能的边际成本、以及智能的上限：

| 上层能力 | 拼装式底层的做法 | 图底座的机制 | 状态 |
|---|---|---|---|
| 长会话记忆 | 全历史塞上下文，token 随轮数增长 | 状态在图上可寻址，每轮只遍历相关子图，上下文 ≈ O(相关) 而非 O(历史) | v0.1 机制 ✓ / v0.2 出证据 |
| 多跳事实推理 | 向量检索逐跳补捞，链一长就断 | 关系即边，k-hop / 最短路一次遍历直达 | v0.1 ✓（find_path） |
| 审计 / 回放 / 回滚 | 外挂 tracing 系统 | episode 链即审计日志，补偿失效即回滚 | v0.1 ✓ |
| 记忆冲突治理 | 覆盖式写入，历史丢失 | 失效标记 + supersedes + 时间戳，永不物理删除 | v0.1 ✓ |
| 技能可验证 | 自由文本描述，每次重猜用法 | 前置/效果断言模板，执行前后机器核对 | v0.1 ✓ |
| 多智能体协作 | 另起一套框架和协议 | agent=节点、拓扑=边型，剪枝=图手术 | v0.3 |
| 越用越聪明 | 训练才长进 | 健康度 θ 已上图；经验图热启动规划 | θ ✓ / 经验图 v0.4 |
| 新上层应用 | 每个应用一套新底座 | 新应用 = 新节点类型 + 新遍历原语；CLI/前端/MCP 都只是视图 | 持续 |

**可证伪承诺（v0.2 交付 docs/benchmark.md）**：同一组任务下，图底座对比
"全历史上下文 + 向量库"基线，在 ①长会话 token 成本曲线 ②多跳事实问答准确率
③记忆治理（冲突/时效）④审计完备性 四项上占优。证据不达标就改设计，不嘴硬。

---

## 1. 产品定位

### 1.1 一句话

**给 LLM 一张随时能查、能改、能回放的"世界地图"，Agent 的脑子长在图上。**

GNA 不是一个 Chatbot 框架，也不是一个 RAG 库，而是一个 **agent runtime**：
上层可以接任何 OpenAI 兼容大模型（GLM / DeepSeek / Kimi / OpenAI / Ollama / 自定义端点），
底层把运行时需要的每一种机制——记忆、检索、规划、工具、技能、约束、审计——
都用**同一张内存图**来表达，所有查找都通过**图遍历**完成，所有变更都作为 **ΔW 事件**记录在图上的时间链中。

### 1.2 与三类邻近路线的分界（教材 8.1.1）

| 路线 | 图的角色 | 状态 | 证据 | 行动 |
|---|---|---|---|---|
| 知识图谱工具（建库） | 建库与本体工程 | 静态知识库 | 有溯源 | 不涉及 |
| 记忆工程（mem0 / Letta） | 记忆存取压缩 | 附件式存储 | 弱 | 不涉及 |
| GraphRAG（检索增强） | 离线抽取与检索 | 只读索引 | 有溯源 | 不涉及 |
| **GNA（本项目）** | **运行时状态机与证据层** | 可写、可增量、可回滚 | 可对账 | 技能/工具/约束全在图上 |

### 1.3 图原生四判据（产品的验收标准）

1. **状态在图上可寻址**："当前世界什么样"能被一条图查询回答，而不是 prompt 里的一段文本；
2. **证据链在图上可追溯**：每个结论沿边可回溯到来源节点（回合 / 工具返回 / 人工确认），带时间戳；
3. **行动与技能在图上可验证**：技能节点携带机器可验证的前置/效果断言，执行前后自动核对；
4. **约束在图上可执行**：红线（沙箱边界、危险操作门控）作为图结构在**搜索前**生效，而不是执行后追责。

---

## 2. 设计公理（工程铁律）

| # | 公理 | 含义 | 落实位置 |
|---|---|---|---|
| A1 | **一切皆图** | 世界模型、记忆、技能、工具、任务、约束、事件日志共用一张图 | `RuntimeGraph` 单图 + 节点类型分区 |
| A2 | **一切查找皆遍历** | 不建图外索引：实体链接、记忆召回、技能选择、路径解释全部是图遍历原语（k-hop、激活扩散、最短路、拓扑序） | `graph.py` 遍历原语层 |
| A3 | **一切变更留痕** | 任何写操作都产生 ΔW 事件；事件本身是图上的 `episode` 节点，串成时间链，可回放、可回滚 | `episode` 链 + 补偿回滚 |
| A4 | **LLM 可插拔** | 大模型只是"挂在图上的推理引擎"：同一个图，换模型不影响状态与证据 | `llm.py` 适配层 |
| A5 | **先 Core 后皮** | CLI 是第一公民；Gradio / Web 前端只是同一 runtime 之上的视图层 | `agent.py` 与 `cli.py` 分离 |

---

## 3. 系统架构

```
┌───────────────────────────────────────────────────────────────────┐
│  视图层（可插拔，同一内核）：CLI │ Gradio │ Streamlit │ MCP(v0.5)  │
├───────────────────────────────────────────────────────────────────┤
│  Agent 层：AgentRuntime                                           │
│    感知 → 入图(抽取) → 遍历召回 → LLM 推理(ReAct) → 行动 → ΔW 写回  │
│    ├─ 任务引擎：目标断言化 → 图规划 → 执行器(门控/重规划)           │
│    └─ 对话引擎：ReAct 工具循环                                     │
├───────────────────────────────────────────────────────────────────┤
│  图原生机制层（全部跑在图上）                                      │
│    extract.py   文本→图谱（写入门控：去重/冲突→失效标记）           │
│    recall.py    记忆召回（激活扩散 / 语义扩展 / 子图序列化）        │
│    tools.py     工具注册表（工具即图节点）                         │
│    skills.py    技能图 S=(S,E_dep,θ)（前置/效果断言 + 健康度）      │
│    planner.py   图规划（断言集状态空间上的 A*，代价含健康度）       │
│    executor.py  沿计划走图：断言核对→门控→调用→ΔW写回→失败重规划    │
├───────────────────────────────────────────────────────────────────┤
│  运行时图层：RuntimeGraph（单图）                                  │
│    类型化节点 │ 断言集(有效 fact) │ episode 时间链 │ 遍历原语 │ 回滚 │
├───────────────────────────────────────────────────────────────────┤
│  图后端适配层：graph_backend                                      │
│    GXBackend（GX-1.5.3，默认）│ NXBackend（纯 networkx 回退）      │
├───────────────────────────────────────────────────────────────────┤
│  LLM 适配层：OpenAI 兼容（GLM/DeepSeek/Kimi/Ollama/自定义）+ Mock  │
└───────────────────────────────────────────────────────────────────┘
```

---

## 4. 统一图模型（Schema）

一张 `RuntimeGraph`（有向图），节点用 `class_` 划分为 11 类。节点 id 约定 `{type}:{name}`。

### 4.1 节点类型（对应教材 8.8 schema 的五类 + 记忆扩展）

| 类型 | 中文 | 语义 | 关键属性 |
|---|---|---|---|
| `entity` | 对象 | 人/物/概念等实体 | name, aliases |
| `fact` | 事实 | **断言节点**：name 即断言串（知识三元组或状态断言） | subject/relation/object（知识型）或裸断言（状态型），invalid 标记 |
| `turn` | 回合 | 一轮对话（用户或助手发言） | text, role, ts |
| `task` | 任务 | 一次图规划执行 | goal_text, status |
| `step` | 执行步 | 任务中的一个技能执行 | skill, params, ok, output |
| `result` | 结果 | 技能产出物（摘要/报告/计算结果） | content |
| `skill` | 技能 | 宏节点：可复用能力单元 | precondition/effects 断言模板, cost, success_rate, gate, version |
| `tool` | 工具 | 原子能力节点 | schema, 权限级别 |
| `constraint` | 约束 | 红线/沙箱/门控声明 | rule, scope |
| `goal` | 目标 | 目标断言集的载体 | assertions |
| `episode` | 事件 | **ΔW 日志节点**（追加式） | seq, op, source, payload |

### 4.2 边类型

| 边 | 含义 |
|---|---|
| `mentions` | turn → entity（回合提及实体） |
| `states` / `invalidates` | episode/turn → fact（断言的产生与失效，证据链） |
| `subject_of` / `object_of` | entity ↔ fact（知识三元组的物化） |
| `requires` | skill → fact 模板（前置断言；`read:*` 通配） |
| `has_effect` | skill → fact 模板（效果断言） |
| `enables` | skill → skill（技能依赖：前置被另一技能的效果满足） |
| `uses_tool` | skill → tool（技能由原子工具兑现，工具即图节点） |
| `constrains` | constraint → skill/tool（约束在搜索前裁剪） |
| `has_step` / `next_step` | task → step → step（执行路径） |
| `produced` | step → result / fact（执行产物，溯源边） |
| `logged` / `next` | task/turn → episode → episode（**事件时间链**） |

### 4.3 断言（Assertion）编码

状态断言是 `fact` 节点的 name 字符串，如：

```
read:notes/gnn.md        # 已读某文件
summary:图神经网络        # 已生成某主题摘要
draft:图神经网络          # 已起草
verified:图神经网络       # 已验证
calc:done                # 已完成一次计算
```

当前世界状态 = 全部未被 `invalid` 标记的 fact 节点名集合 **W_t**。
规划器起点是 W_t，目标是目标断言集，技能可执行当且仅当其前置断言模板（支持尾部 `*` 通配）⊆ W_t。

### 4.4 事件时间链（一切变更留痕）

```
task:t1 ──logged──▶ ep:1 ──next──▶ ep:2 ──next──▶ ep:3 ...
                     │insert_node fact:read:x 〈ts, source=技能read_file〉
                     │insert_edge step:s2 --produced--> result:r1
                     ...
```

- 每个原子变更 = 一个 `episode` 节点（op / ts / source / payload）；
- 回放：沿 `next` 边顺序重放；
- 回滚：定位某 `step` 的事件子链，对增量做**补偿失效**（不物理删除，保留历史，教材 8.1 失效语义 / 第 9 章事件溯源）。

---

## 5. 查找即遍历（六种查找场景 → 遍历原语）

| 场景 | 原语 | 实现 |
|---|---|---|
| 实体链接（把文本对到图） | 名称索引收敛 + 邻域打分 | backend name index（GX 内建），精确>包含 |
| 记忆召回 | **激活扩散**：种子节点沿边加权衰减 k 跳 | `recall.spreading_activation` |
| 语义召回（可选） | 向量余弦定入口 + 图邻域扩展 | `embedder`（mock/st/openai）+ k-hop |
| 技能选择/规划 | 断言子集检查 + 状态空间 **A***（代价 = cost × 1/success_rate） | `planner.plan` |
| 执行路径解释 | 最短路 / 路径枚举 | GX `GraphSearch.cached_dijkstra` / nx |
| 依赖排序 | 拓扑序 | GX `GraphUtils.topological_sort` / nx |
| 社区/重要度（v0.2） | Louvain / PageRank / 中心性 | GX `GraphPartition`、nx |

**不建任何图外索引**：没有 BMIS、没有独立向量库；embedding 只作为节点属性随图序列化，语义检索的最终定位仍在图上（先向量收敛入口，再图遍历扩展）。

---

## 6. 运行时循环（AgentRuntime）

```
用户输入
  │
  ├─(1) 感知入图：turn 节点 + mentions 边 + 实体/事实抽取（LLM 或规则，写入门控）
  ├─(2) 遍历召回：以输入实体为种子做激活扩散 → 子图 → 序列化为 LLM 上下文
  ├─(3) 路由：
  │     ├─ 对话类 → ReAct 循环：LLM 输出 {thought, action, action_input} / {final}
  │     │            action = 图操作(query_graph/add_fact/find_path...) 或工具(文件/计算/时间)
  │     │            每次行动后观察结果写回 prompt，直至 final 或步数上限
  │     └─ 任务类 → 目标断言化(LLM/关键词) → 图规划(路径=计划) → 执行器逐技能走图：
  │                  前置核对(不过→局部重规划) → 约束检查(搜索前) → 人工门控(挂起/确认)
  │                  → 工具调用 → ΔW 写回(断言/结果/episode 链) → 失败降健康度并重规划
  └─(4) 收尾：本轮事实抽取入图；episode 链收口；输出答案 + 证据路径
```

任务类执行满足教材 8.8 验证口径：每任务 ≥3 次工具调用、每次调用后有带时间戳的图更新、输出执行路径与一致性可复现。

---

## 7. LLM 接入层

- 协议：OpenAI Chat Completions 兼容；`base_url + api_key + model` 三元组任意切换；
- 预设：智谱 GLM / DeepSeek / Moonshot Kimi / OpenAI / Ollama 本地 / 自定义；
- **Mock 模式**：确定性离线模型（目标映射/抽取/ReAct/摘要全角色可跑），保证无 Key 可演示、CI 可测试；
- 配置优先级：CLI 参数 > 环境变量（`GNA_LLM_PROVIDER/GNA_LLM_BASE_URL/GNA_LLM_API_KEY/GNA_LLM_MODEL`）> `~/.gna/config.json`；
- 输出协议：强制 JSON + 容错解析（围栏剥离、首个平衡对象截取），解析失败带错误信息重试一次（REPAIR）。

---

## 8. 图引擎后端（GX 集成）

| 后端 | 说明 | 定位 |
|---|---|---|
| `GXBackend` | 加载 `GX_PATH` 指向的 GX-1.5.3 目录下的 `graph_engine.Graph/Node/Edge`、`GraphSearch`、`GraphUtils`；`SemanticGraph` 思路用于可选语义检索 | 默认，用户自有引擎 |
| `NXBackend` | networkx MultiDiGraph 等价实现 | 开源回退，零额外依赖 |

适配要点（吸取 gx-memory-mcp 的修复经验）：

1. **序列化修复**：不使用 GX 原生 `to_dict()`（丢失 `Node.data`/`embedding`），RuntimeGraph 自带完整 JSON 序列化（节点/边/episode 全量保留），原子写（临时文件 + rename）；
2. **线程安全**：`RLock` 包裹全部写操作；
3. **Embedding 可插拔**：`GNA_EMBEDDING_BACKEND = mock（默认，确定性哈希向量）/ st / openai`，向量作为节点属性存储；
4. **环境变量对齐 gx-memory 惯例**：`GX_PATH / GNA_STORAGE（默认 ~/.gna/memory.json）/ GNA_AUTOSAVE`。

---

## 9. CLI 规格（本期交付面）

所有命令 `python -m gna ...` 或（`pip install -e .` 后）`gna ...`，Windows cmd 直接可跑。

```
gna chat                          交互式 REPL（/help /graph /facts /path /tools /skills /reset /quit）
gna ask "问题"                    单轮问答（对话引擎）
gna run "任务描述"                单任务（图规划 + 执行，打印计划路径/每步ΔW/最终答案）
gna demo                          一键演示：载入示例知识 → 跑示例任务 → 图统计
gna graph stats                   图统计（节点/边/断言/事件数）
gna graph nodes [--type fact]     节点列表（可按类型过滤）
gna graph neighbors <id> [--hops] 邻域遍历
gna graph path <A> <B>            两点最短路（证据链解释）
gna graph export [--format json]  导出图
gna graph viz [--out file.html]   交互式可视化（pyvis，可选依赖）
gna memory add "A 是 B"           写入事实（走写入门控）
gna memory query "..."            记忆召回（激活扩散子图）
gna memory facts                  当前断言集（世界状态 W_t）
gna memory rollback               回滚最近一次技能执行的增量
gna memory clear [--keep-registry]
gna skill list / show <name> / validate（连通无环校验，输出 skills=N edges=M）/ run <name> --json
gna tool list / call <name> --json
gna llm test / info               连通性与当前配置
```

---

## 10. 模块清单与教材映射

| 模块 | 职责 | 教材对应 |
|---|---|---|
| `gna/graph_backend.py` | GX / networkx 双后端适配 | 第 1 章图基础设施 |
| `gna/graph.py` | RuntimeGraph：类型化节点、断言状态、episode 时间链、遍历原语、回滚 | 8.1 世界模型 / 8.4 脚手架 / 9.2 事件溯源 |
| `gna/embedder.py` | 可插拔 embedding（mock/st/openai） | 3.1 浅层嵌入（简化） |
| `gna/extract.py` | 文本→图谱，写入门控（去重/冲突→旧事实失效） | 第 7 章从文本到图谱 / 6.3 冲突合并 |
| `gna/recall.py` | 激活扩散召回、子图序列化、局部子图检索 | 第 5 章 GraphRAG 模式一 / HippoRAG 思想 |
| `gna/tools.py` | 原子工具（工具即图节点） | 8.2.4 |
| `gna/skills.py` | 技能图（前置/效果断言、enables、健康度 θ） | 8.2 |
| `gna/planner.py` | 图规划（A* over 断言集，代价含健康度，约束先行裁剪） | 8.3 / 8.4 |
| `gna/executor.py` | 执行器（门控、ΔW 写回、失败局部重规划、回滚） | 8.3/8.4/8.8 |
| `gna/llm.py` | LLM 适配 + Mock | 第 0 章 词元时代 |
| `gna/agent.py` | AgentRuntime 总循环 | 8.1–8.5 汇合 |
| `gna/cli.py` | CLI | — |

---

## 11. Roadmap

| 阶段 | 内容 | 状态 |
|---|---|---|
| **v0.1 Core + CLI** | 单图运行时、四判据闭环、双引擎（对话/任务）、GX 双后端、CLI 全命令、pytest | ✅ 本期 |
| **v0.2 底座兑现** | 增量持久化（替换全量序列化）、遍历缓存、**同题对比基准**（图底座 vs 全历史上下文+向量库，四项可证伪指标） | 下期（优先于前端——先证明底座价值，再展示它） |
| v0.2.5 前端 | Gradio 多页（对话/图谱世界/执行审计/设置）+ 图谱可视化增强 | 排队 |
| v0.3 协作 | 多智能体图拓扑（链/树/DAG/网）、共享执行图、消息外化 | 规划 |
| v0.4 自演化 | 经验图热启动规划、技能健康度自动升降权、技能从轨迹提取 | 规划 |
| v0.5 生态 | MCP server 化（对齐 gx-memory 惯例）、技能包导入导出、GraphML 互通 | 规划 |

## 12. 开源工程约定

- 语言：Python ≥ 3.10；依赖最小化（networkx 必选；numpy/pyvis/openai 按需可选）；
- 测试：pytest 覆盖图后端、遍历原语、规划器、执行器、CLI 冒烟（Mock LLM 全离线）；
- 提交：Conventional Commits；`docs/` 与代码同步演进；
- 许可：MIT。
