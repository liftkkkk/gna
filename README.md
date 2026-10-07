# GNA · Graph-Native Agent Runtime

**English** | [简体中文](README.zh-CN.md)

> **Everything is a graph. Every lookup is a graph traversal. Every change leaves a trace.**
> Give your LLM a live "world map" it can query, mutate and replay — the agent's brain lives on the graph.

GNA is an **agent runtime**: the agent's **state, memory, skills, tasks, constraints and audit trail** all live on **one in-memory graph**. On top it speaks to any OpenAI-compatible LLM (GLM / DeepSeek / Kimi / OpenAI / Ollama / custom — or run keyless with Mock); underneath, it supports the self-developed [GX graph engine](#gx-engine-integration) with a networkx fallback.

## The four graph-native criteria (substrate vs. accessory)

| Criterion | How GNA delivers it |
|---|---|
| **State is addressable on the graph** | The current world state = the set of valid assertions (fact nodes); every state question is one graph traversal |
| **Evidence chains are traceable** | Every node/edge carries `ts + source`; conclusions backtrack along `produced/states` edges to the turn or tool response |
| **Actions & skills are machine-verifiable** | Skill nodes carry precondition/effect **assertion templates**, checked automatically before and after execution (wildcards + parameter unification) |
| **Constraints fire before the search** | Constraint nodes declare `deny_skill:` (blocked at planning) / `restrict:` (sandbox-enforced) / `gate:` (human approval) via `constrains` edges |
| **It does real work** | `run_code` (write & run) / `run_python`: LLM-written programs land in `workspace/scripts/`, execute as a subprocess (60s timeout), stream output back, and self-repair on errors; every execution lands on the graph as a `tool_call` event plus `written:/ran:` assertions |
| **Extensible ecosystem** | Custom MCP tools in the standard `mcpServers` JSON format and custom skills in the ZCode `SKILL.md` format — configured on the web Extensions page |

## Install & quick start

```cmd
:: Option 1 (recommended, once published): from PyPI
pip install gna

:: Option 2: from source
git clone https://github.com/liftkkkk/gna.git
cd gna && pip install .
```

Core dependencies (networkx / fastapi / uvicorn / openai) are installed automatically — **it works out of the box**.
The legacy views (Gradio / Streamlit) and graph visualization are extras: `pip install "gna-graph-native-agent[web-legacy,viz]"`.

```cmd
gna demo                :: one-command end-to-end demo (offline Mock, no API key needed)
gna web                 :: open the web UI (default http://127.0.0.1:8000)
                        :: first run: set your model key on the Settings page (or stay on Mock);
                        :: the Extensions page imports MCP servers / custom skills
gna chat                :: interactive CLI
```

Connect a real LLM (all providers speak the OpenAI-compatible protocol):

```cmd
set GNA_LLM_PROVIDER=zhipu
set GNA_LLM_API_KEY=your-key
set GNA_LLM_MODEL=glm-4.6
python -m gna chat
```

Optional: enable the self-developed GX graph engine as the primary backend (see below).

## Architecture

```
Views (pluggable): CLI │ HTML (default) │ Gradio │ Streamlit │ MCP (planned)
──────────────────────────────────────────────────────────────────────────
AgentRuntime: perceive → ingest → traversal recall → LLM reason → act → ΔW write-back
   ├ Work engine: ReAct tool loop (tools include graph operations)
   └ Task engine: goal → assertions → graph planning (A*) → executor (gates / replanning)
──────────────────────────────────────────────────────────────────────────
Graph-native mechanisms: extract (text→graph) │ recall (spreading activation) │
   tools (tools-as-nodes) │ skills (skill graph) │
   planner (path = plan) │ executor (ΔW write-back)
──────────────────────────────────────────────────────────────────────────
RuntimeGraph: typed nodes │ assertion state │ episode event chain │ traversal primitives │ compensating rollback
──────────────────────────────────────────────────────────────────────────
GXBackend (GX-1.5.3, primary) │ IgraphBackend │ NXBackend (fallback)
```

A sample execution trace (`python -m gna run "Summarize GNN key points into a brief report and verify it"`):

```
PLAN: session_start -> read_file -> summarize_text -> extract_facts -> draft_report -> verify_report
  [1/6] session_start        OK   (structural skill: advances state assertions only)
  [2/6] read_file            OK   reads notes/gnn.md
  [3/6] summarize_text       OK   key points: ...
  [4/6] extract_facts        OK   4 entities, 2 facts ingested
  [5/6] draft_report         OK   report drafted (2 graph citations)   <- human gate
  [6/6] verify_report        OK   all evidence citations resolve to live graph nodes
```

## CLI overview

| Command | What it does |
|---|---|
| `gna web` / `gna web-gradio` / `gna web-st` | three frontends, one kernel (HTML is default) |
| `gna chat` | interactive REPL: `/graph` `/facts` `/path A B` `/tools` `/skills` `/rollback` `/reset` |
| `gna ask "question"` | one-shot Q&A |
| `gna run "task"` | graph planning + execution (`--yes` auto-approves gates) |
| `gna demo` | end-to-end demo (sample knowledge -> task -> four-criteria audit) |
| `gna graph stats\|nodes\|edges\|neighbors\|path\|events\|export\|viz` | graph operations |
| `gna memory add\|query\|facts\|rollback\|clear` | graph memory (writes pass gating: dedup, conflicts -> invalidation) |
| `gna skill list\|show\|validate\|run` | skill graph (`validate` prints `skills=15 edges=20 acyclic=True components=1`) |
| `gna mcp list\|add\|remove\|test` / `gna uskill ...` / `gna memory-import` | user extensions (MCP servers, SKILL.md skills, memory files) |
| `gna workon <dir>` | project mode: point the sandbox at your own codebase |
| `gna tool list\|call` | atomic tools (sandbox demonstration included) |
| `gna llm info\|test\|set` | LLM configuration & connectivity |

## Unified graph model (schema)

11 node classes: `entity` · `fact` (assertion) · `turn` · `task` · `step` ·
`result` · `skill` · `tool` · `constraint` · `goal` · `episode`.

Key edges: `mentions` (turn->entity) · `states` (provenance) · `requires` / `has_effect` (assertion templates) ·
`enables` (skill dependencies, auto-derived) · `uses_tool` · `constrains` · `has_step` · `produced` (evidence chain) ·
`next` (episode time chain) · `supersedes` (conflict replacement).

**ΔW event sourcing**: every atomic change is an `episode` node chained along `next` edges —
replayable (`gna graph events`), rollbackable (`gna memory rollback`, compensating invalidation; history is never physically deleted).

## Design doc

The full product design (axioms, schema, traversal-primitive mapping, runtime loop, GX integration, roadmap) lives in
[docs/design.md](docs/design.md) (Chinese).

## GX engine integration

GNA's primary backend is the self-developed **GX graph engine** (in-memory graphs, adjacency lists, name index, Dijkstra, topological sort, partitioning / community detection):

```cmd
:: GX is the PRIMARY backend (highest priority) - GNA auto-detects it at startup
:: (GX_PATH -> ./GX-1.5.3 -> download folders matching GX-*); networkx is the fallback.
set GX_PATH=D:\path\to\GX-1.5.3
set GNA_BACKEND=networkx              :: force the networkx fallback (debugging)
```

> This repository does not contain the GX engine source - GNA references it at runtime through an adapter layer;
> environments without GX fall back to networkx automatically and remain fully functional. See [docs/gx-packaging.md](docs/gx-packaging.md).

**How to get GX** (depends on how the author publishes it):
1. **pip** (recommended once published): `pip install gx-engine` - GNA detects it automatically, no path setup;
2. **GitHub**: download the GX-1.5.3 folder from the GX repository, then `setx GX_PATH <path>`;
3. **Bundled** with a GNA release (if the GX license allows redistribution).

Adapter details: GX adjacency lists store out-edges only (GNA builds a lazy reverse index); GX's native
serialization drops `data/embedding` (GNA does full-fidelity atomic JSON writes at the RuntimeGraph layer,
bypassing GX's `save()`); storage path `GNA_STORAGE` (default `~/.gna/runtime.json`).

## Configuration

| Environment variable | Purpose | Default |
|---|---|---|
| `GNA_LLM_PROVIDER` | mock / zai / zhipu / deepseek / moonshot / openai / ollama / custom | mock |
| `GNA_LLM_API_KEY` / `GNA_LLM_MODEL` / `GNA_LLM_BASE_URL` | model access | - |
| `GNA_STORAGE` | graph store file | `~/.gna/runtime.json` |
| `GNA_WORKSPACE` | sandbox root for file tools | `~/.gna/workspace` |
| `GX_PATH` | GX engine directory (**primary backend**, auto-detected; networkx is the fallback) | auto-detect |
| `GNA_EMBEDDING_BACKEND` | mock / st / openai (semantic recall, optional) | mock |

## Development

```bash
pip install -e ".[dev]"
python -m pytest -q          :: 65 tests (dual backends / planning / execution / audit / CLI smoke, fully offline)
python examples/quickstart.py
```

## Roadmap

- [x] **v0.1 Core + CLI**: single-graph runtime, dual engines, dual backends, event sourcing / rollback, 15-skill graph
- [x] **v0.1.x Executable kernel**: run_code / run_python, project mode, uploads & path references, MCP tools, user skills, memory folder, three frontends
- [ ] **v0.2 Substrate delivery**: incremental persistence + same-task benchmark (graph substrate vs. context-stuffing baseline)
- [ ] v0.3 Multi-agent graph topologies (chain / tree / DAG / mesh), shared execution graph
- [ ] v0.4 Self-evolution: experience-graph warm-start planning, skill-health auto-weighting, trajectory->skill extraction
- [ ] v0.5 MCP server mode, skill packs, GraphML interop

## License

MIT

---

## Buy me a coffee

GNA · Graph-Native Agent Runtime is an open-source project I develop and maintain in my spare time, free forever.

If it helped you bring order to your agent's state, memory and audit trail - or saved you the time of wiring
together an agent framework - consider buying me a coffee (9.9 CNY is enough). Every bit of support goes
straight into new features and bug fixes.

<p align="center">
  <img src="icon.jpg" alt="Buy me a coffee" width="280">
</p>

<p align="center"><i>Leave a note with your donation about the feature you want most - I prioritize those ;)</i></p>

**Can't donate?** Starring the repo, opening an Issue, or sharing it with someone who needs it helps just as much!

---
