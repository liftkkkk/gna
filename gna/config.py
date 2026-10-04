"""GNA 配置层：目录约定、LLM 提供商预设、配置装载（env > 配置文件 > 默认）。"""
from __future__ import annotations

import json
import os
import time
from dataclasses import asdict, dataclass
from pathlib import Path

GNA_HOME = Path(os.environ.get("GNA_HOME", str(Path.home() / ".gna")))

PROVIDER_PRESETS: dict[str, dict] = {
    "mock": {"label": "Mock 离线演示（无需 API Key）", "base_url": "", "models": ["mock-1"], "env_key": None},
    "zai": {"label": "Z.ai（智谱国际）", "base_url": "https://api.z.ai/api/paas/v4", "models": ["GLM-5.3", "GLM-5.3-Flash"], "env_key": "ZAI_API_KEY"},
    "zhipu": {"label": "智谱 BigModel", "base_url": "https://open.bigmodel.cn/api/paas/v4/", "models": ["glm-4.6", "glm-4.5", "glm-4.5-air", "glm-4-flash"], "env_key": "ZHIPUAI_API_KEY"},
    "deepseek": {"label": "DeepSeek", "base_url": "https://api.deepseek.com/v1", "models": ["deepseek-chat", "deepseek-reasoner"], "env_key": "DEEPSEEK_API_KEY"},
    "moonshot": {"label": "月之暗面 Kimi", "base_url": "https://api.moonshot.cn/v1", "models": ["kimi-k2-0905-preview", "moonshot-v1-8k", "moonshot-v1-32k"], "env_key": "MOONSHOT_API_KEY"},
    "openai": {"label": "OpenAI", "base_url": "https://api.openai.com/v1", "models": ["gpt-4o-mini", "gpt-4o"], "env_key": "OPENAI_API_KEY"},
    "ollama": {"label": "Ollama 本地", "base_url": "http://localhost:11434/v1", "models": ["qwen3:1.7b", "qwen3:0.6b", "llama3.2:1b"], "env_key": None},
    "custom": {"label": "自定义 OpenAI 兼容端点", "base_url": "", "models": [], "env_key": None},
}

MODELS_FILE = GNA_HOME / "models.json"   # 多模型配置方案（持久化，重启自动加载）


# ------------------------------------------------- 配置方案（多模型） ----

def load_profiles() -> dict:
    """读取 models.json：{"active": "<id>", "profiles": [{...}]}。

    首次调用时从旧版 config.json 导入一条方案，并保证始终存在 mock 方案。
    """
    data: dict = {}
    if MODELS_FILE.exists():
        try:
            data = json.loads(MODELS_FILE.read_text(encoding="utf-8"))
        except Exception:
            data = {}
    profiles: list[dict] = [p for p in data.get("profiles", []) if isinstance(p, dict) and p.get("id")]
    if not profiles:
        legacy = {}
        if _config_file().exists():
            try:
                legacy = json.loads(_config_file().read_text(encoding="utf-8"))
            except Exception:
                legacy = {}
        if legacy.get("base_url") or legacy.get("provider"):
            profiles.append({
                "id": "legacy", "name": "导入的旧配置",
                "provider": legacy.get("provider", "custom"),
                "base_url": legacy.get("base_url", ""),
                "api_key": legacy.get("api_key", ""),
                "model": legacy.get("model", ""),
                "temperature": legacy.get("temperature", 0.3),
            })
        profiles.append({"id": "mock", "name": "Mock 离线演示（无需 Key）",
                         "provider": "mock", "base_url": "", "api_key": "",
                         "model": "mock-1", "temperature": 0.3})
    data["profiles"] = profiles
    ids = {p["id"] for p in profiles}
    active = data.get("active")
    data["active"] = active if active in ids else profiles[0]["id"]
    return data


def save_profiles(data: dict) -> None:
    GNA_HOME.mkdir(parents=True, exist_ok=True)
    MODELS_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def upsert_profile(p: dict) -> dict:
    """按 id 新增/覆盖一条方案。返回最新数据。"""
    data = load_profiles()
    pid = (p.get("id") or "").strip() or f"p{int(time.time())}"
    p = {**p, "id": pid}
    for i, old in enumerate(data["profiles"]):
        if old["id"] == pid:
            data["profiles"][i] = p
            break
    else:
        data["profiles"].append(p)
    save_profiles(data)
    return data


def set_active_profile(pid: str) -> dict:
    data = load_profiles()
    if pid in {p["id"] for p in data["profiles"]}:
        data["active"] = pid
        save_profiles(data)
    return data


def delete_profile(pid: str) -> dict:
    data = load_profiles()
    data["profiles"] = [p for p in data["profiles"] if p["id"] != pid] or data["profiles"]
    if data["active"] not in {p["id"] for p in data["profiles"]}:
        data["active"] = data["profiles"][0]["id"]
    save_profiles(data)
    return data


def active_profile() -> dict:
    data = load_profiles()
    aid = data["active"]
    return next(p for p in data["profiles"] if p["id"] == aid)


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
    auto_confirm_gates: bool = True   # False = 人工门控挂起等待确认（8.4 Human-in-the-loop）
    storage_path: str = ""
    workspace: str = ""
    project_root: str = ""            # 项目模式：Agent 的读写/运行沙箱根 = 该文件夹（空 = 默认工作区）

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
        """文件工具的沙箱根：项目模式优先（用户自己的项目文件夹），否则默认工作区。"""
        if self.project_root and Path(self.project_root).is_dir():
            return Path(self.project_root)
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
    # 当前激活的配置方案（models.json）覆盖旧版单配置字段
    try:
        prof = active_profile()
        s.provider = prof.get("provider") or s.provider
        s.base_url = prof.get("base_url") or s.base_url
        s.api_key = prof.get("api_key") or s.api_key
        s.model = prof.get("model") or s.model
        if prof.get("temperature") is not None:
            s.temperature = float(prof["temperature"])
    except Exception:
        pass
    # 环境变量最高优先
    env_map = {
        "GNA_LLM_PROVIDER": "provider", "GNA_LLM_BASE_URL": "base_url",
        "GNA_LLM_API_KEY": "api_key", "GNA_LLM_MODEL": "model",
        "GNA_LLM_TEMPERATURE": "temperature", "GNA_MAX_REACT_STEPS": "max_react_steps",
        "GNA_PROJECT_ROOT": "project_root",
    }
    for env, attr in env_map.items():
        if os.environ.get(env):
            v = os.environ[env]
            if attr == "temperature":
                v = float(v)
            elif attr == "max_react_steps":
                v = int(v)
            setattr(s, attr, v)
    return s


def save_settings(s: Settings) -> None:
    GNA_HOME.mkdir(parents=True, exist_ok=True)
    data = asdict(s)
    _config_file().write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
