# GX 图引擎接入与打包指南

GNA 的图后端是**可插拔**的：**GX 图引擎是主后端（最高优先级）**，networkx 仅为 GX 不可用时的兜底。

## 后端解析顺序（`gna/backend.py: make_backend`）

1. 显式参数 `make_backend(path)`；
2. `GX_PATH` 环境变量；
3. **自动探测**：当前目录 `./GX-1.5.3`、`./GX`、`D:/Downloads/GX-*`、`~/Downloads/GX-*` 等（含 `graph_engine.py` 的目录）；
4. pip 安装态：GX 已作为 Python 包装进环境（无需路径，直接 import）；
5. 以上全部不可用 → networkx 兜底（功能完整）。

`GNA_BACKEND=networkx` 可强制兜底（调试用）。

## 用户如何接入 GX（方式 A：目录 + 环境变量，零打包）

1. 拿到 GX-1.5.3 目录（含 `graph_engine.py` / `graph_rag.py`）；
2. 放到任意位置，设置环境变量：
   ```cmd
   setx GX_PATH "D:\path\to\GX-1.5.3"
   ```
   （或者什么都不设——放在当前目录 / 下载目录也会被自动探测到）
3. 启动 GNA，`gna graph stats` 里 `backend=gx` 即生效。

## GX 项目如何正式成为可安装的包（方式 B：发布到 PyPI，推荐主推时使用）

让 GX 自己变成一个 pip 包，GNA 用户 `pip install gx-engine` 后**无需任何路径设置**即被自动识别（make_backend 第 4 步会直接 import）。

**✅ 已完成**：gx-engine 仓库采用 py-modules 布局（`pip install -e .` 与 `python -m build` 双验证通过，PyPI 产物 `gx_engine-1.5.3` whl/sdist）。仓库结构（py-modules 方案，保持 `import graph_engine` 全局兼容，GNA / gx-memory 零改动）：

```
gx-engine/
  pyproject.toml      # name: gx-engine
  graph_engine.py
  graph_rag.py
  examples/
```

推送 GitHub 后：`git clone https://github.com/liftkkkk/gx-engine && cd gx-engine && pip install -e .`

若未来改用包目录布局（`gx_engine/` 包），在 GX-1.5.3 目录下新建 `pyproject.toml`（模板如下），然后把 `graph_engine.py` / `graph_rag.py` / `node_embeddings.py` 挪进 `gx_engine/` 包目录：

```toml
[build-system]
requires = ["setuptools>=68"]
build-backend = "setuptools.build_meta"

[project]
name = "gx-engine"
version = "1.5.3"
description = "GX 图引擎——内存图存储/搜索/分区/语义检索"
requires-python = ">=3.10"
dependencies = ["networkx>=3.0", "numpy"]

[tool.setuptools]
packages = ["gx_engine"]
```

目录结构：

```
GX-1.5.3/
  pyproject.toml
  gx_engine/
    __init__.py
    graph_engine.py
    graph_rag.py
    node_embeddings.py
```

验证：`pip install -e .` 后 `python -c "from graph_engine import Graph; print('ok')"`。
之后 `pip install gx-engine`（或发布 PyPI）即可让全世界的 GNA 用户自动用上 GX。

## 技术边界（为什么不直接把 GX 代码拷进 GNA 仓库）

GX 是独立项目（独立版本、独立版权、独立发布节奏）。以外部依赖方式引用是标准做法：
GNA 声明"用什么"，GX 决定"怎么提供"——两个仓库、一条 `GX_PATH`（或一条 pip 依赖）连接。
