"""GNA 配置层：目录约定、LLM 提供商预设、配置装载（env > 配置文件 > 默认）。"""
from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path

GNA_HOME = Path(os.environ.get("GNA_HOME", str(Path.home() / ".gna")))

PROVIDER_PRESETS: dict[str, dict] = {
    "mock": {"label": "Mock 离线演示（无需 API Key）", "base_url": "", "models": ["mock-1"], "env_key": None},
    "zhipu": {"label": "智谱 GLM", "base_url": "https://open.bigmodel.cn/api/paas/v4/", "models": ["glm-4.6", "glm-4.5", "glm-4.5-air", "glm-4-flash"], "env_key": "ZHIPUAI_API_KEY"},
    "deepseek": {"label": "DeepSeek", "base_url": "https://api.deepseek.com/v1", "models": ["deepseek-chat", "deepseek-reasoner"], "env_key": "DEEPSEEK_API_KEY"},
    "moonshot": {"label": "月之暗面 Kimi", "base_url": "https://api.moonshot.cn/v1", "models": ["kimi-k2-0905-preview", "moonshot-v1-8k", "moonshot-v1-32k"], "env_key": "MOONSHOT_API_KEY"},
    "openai": {"label": "OpenAI", "base_url": "https://api.openai.com/v1", "models": ["gpt-4o-mini", "gpt-4o"], "env_key": "OPENAI_API_KEY"},
    "ollama": {"label": "Ollama 本地", "base_url": "http://localhost:11434/v1", "models": ["qwen3:1.7b", "qwen3:0.6b", "llama3.2:1b"], "env_key": None},
    "custom": {"label": "自定义 OpenAI 兼容端点", "base_url": "", "models": [], "env_key": None},
}


def default_storage() -> Path:
    return Path(os.environ.get("GNA_STORAGE", str(GNA_HOME / "runtime.json")))


def default_workspace() -> Path:
    ws = Path(os.environ.get("GNA_WORKSPACE", str(GNA_HOME / "workspace")))
    (ws / "notes").mkdir(parents=True, exist_ok=True)
    (ws / "reports").mkdir(parents=True, exist_ok=True)
    return ws


@dataclass
class Settings:
    provider: str = "mock"
    base_url: str = ""
    api_key: str = ""
    model: str = "mock-1"
    temperature: float = 0.3
    max_react_steps: int = 8
    memory_hops: int = 2
    auto_confirm_gates: bool = True   # CLI 非交互场景自动确认；chat REPL 默认改为人工确认
    storage_path: str = ""
    workspace: str = ""

    # ---- 解析 ----
    def resolved_api_key(self) -> str:
        if self.api_key:
            return self.api_key
        env_key = PROVIDER_PRESETS.get(self.provider, {}).get("env_key")
        return os.environ.get(env_key, "") if env_key else ""

    def resolved_base_url(self) -> str:
        if self.base_url:
            return self.base_url.rstrip("/")
        return PROVIDER_PRESETS.get(self.provider, {}).get("base_url", "").rstrip("/")

    def resolved_storage(self) -> Path:
        return Path(self.storage_path) if self.storage_path else default_storage()

    def resolved_workspace(self) -> Path:
        ws = Path(self.workspace) if self.workspace else default_workspace()
        (ws / "notes").mkdir(parents=True, exist_ok=True)
        (ws / "reports").mkdir(parents=True, exist_ok=True)
        return ws


def _config_file() -> Path:
    return GNA_HOME / "config.json"


def load_settings() -> Settings:
    s = Settings(storage_path=str(default_storage()), workspace=str(default_workspace()))
    if _config_file().exists():
        try:
            for k, v in json.loads(_config_file().read_text(encoding="utf-8")).items():
                if hasattr(s, k) and v is not None:
                    setattr(s, k, v)
        except Exception:
            pass
    # 环境变量优先
    env_map = {
        "GNA_LLM_PROVIDER": "provider", "GNA_LLM_BASE_URL": "base_url",
        "GNA_LLM_API_KEY": "api_key", "GNA_LLM_MODEL": "model",
        "GNA_LLM_TEMPERATURE": "temperature", "GNA_MAX_REACT_STEPS": "max_react_steps",
    }
    for env, attr in env_map.items():
        if os.environ.get(env):
            v = os.environ[env]
            if attr == "temperature":
                v = float(v)
            elif attr == "max_react_steps":
                v = int(v)
            setattr(s, attr, v)
    # 从未配置过且检测到主流 Key → 自动选提供商
    if s.provider == "mock" and not os.environ.get("GNA_LLM_PROVIDER"):
        for prov, meta in PROVIDER_PRESETS.items():
            ek = meta.get("env_key")
            if ek and os.environ.get(ek):
                s.provider, s.model, s.api_key = prov, meta["models"][0], ""
                break
    return s


def save_settings(s: Settings) -> None:
    GNA_HOME.mkdir(parents=True, exist_ok=True)
    data = asdict(s)
    _config_file().write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
