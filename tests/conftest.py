import os

import pytest

from gna.agent import AgentRuntime
from gna.config import Settings


@pytest.fixture()
def tmp_env(tmp_path, monkeypatch):
    """隔离的 GNA 运行环境：存储/工作区全部落在临时目录，LLM 强制 mock。"""
    monkeypatch.setenv("GNA_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("GNA_STORAGE", str(tmp_path / "runtime.json"))
    monkeypatch.setenv("GNA_WORKSPACE", str(tmp_path / "workspace"))
    monkeypatch.setenv("GNA_LLM_PROVIDER", "mock")
    for k in ("ZHIPUAI_API_KEY", "DEEPSEEK_API_KEY", "MOONSHOT_API_KEY", "OPENAI_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    ws = tmp_path / "workspace"
    (ws / "notes").mkdir(parents=True)
    (ws / "notes" / "图神经网络.md").write_text(
        "# GNN\n- 消息传递是核心范式，节点聚合邻居特征更新自身表示\n"
        "- GCN 通过归一化拉普拉斯矩阵实现谱卷积\n"
        "- GAT 引入注意力机制为邻居分配权重\n"
        "- 过平滑是层数过深时的风险\n", encoding="utf-8")
    s = Settings(provider="mock", model="mock-1",
                 storage_path=str(tmp_path / "runtime.json"), workspace=str(ws))
    return {"settings": s, "workspace": ws, "tmp": tmp_path}


@pytest.fixture()
def runtime(tmp_env):
    return AgentRuntime(settings=tmp_env["settings"])


@pytest.fixture()
def isolated_store(tmp_env):
    from gna.graph import RuntimeGraph

    return RuntimeGraph(storage_path=str(tmp_env["tmp"] / "store.json"))
