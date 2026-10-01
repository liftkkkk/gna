"""可插拔 Embedding 层（零依赖默认 mock，向量为节点属性随图存储，不建图外向量库）。

GNA_EMBEDDING_BACKEND: mock（默认，确定性哈希向量，可复现）/ st / openai
"""
from __future__ import annotations

import hashlib
import math
import os
from typing import List, Sequence


class EmbeddingProvider:
    dim: int = 256

    def embed(self, text: str) -> List[float]:
        raise NotImplementedError

    def embed_batch(self, texts: Sequence[str]) -> List[List[float]]:
        return [self.embed(t) for t in texts]


class MockEmbedding(EmbeddingProvider):
    """确定性哈希随机投影：相同文本 → 相同单位向量。仅用于流程验证与测试复现。"""

    def __init__(self, dim: int = 256):
        self.dim = dim
        self._cache: dict[str, List[float]] = {}

    def embed(self, text: str) -> List[float]:
        if text in self._cache:
            return self._cache[text]
        seed = int(hashlib.md5(text.encode("utf-8")).hexdigest(), 16) % (2 ** 32)
        # xorshift 确定性伪随机（不依赖 numpy）
        vec: List[float] = []
        x = seed or 1
        for _ in range(self.dim):
            x ^= (x << 13) & 0xFFFFFFFF
            x ^= x >> 17
            x ^= (x << 5) & 0xFFFFFFFF
            vec.append((x % 10000) / 10000.0 - 0.5)
        norm = math.sqrt(sum(v * v for v in vec)) or 1.0
        out = [v / norm for v in vec]
        self._cache[text] = out
        return out


class SentenceTransformerEmbedding(EmbeddingProvider):
    """本地 sentence-transformers 语义向量（lazy 导入，未安装不报错）。"""

    def __init__(self, model_name: str = "paraphrase-multilingual-MiniLM-L12-v2", device: str = "cpu"):
        from sentence_transformers import SentenceTransformer  # noqa: 延迟导入

        self._model = SentenceTransformer(model_name, device=device)
        self.dim = int(self._model.get_sentence_embedding_dimension())

    def embed(self, text: str) -> List[float]:
        return self._model.encode(text, normalize_embeddings=True, convert_to_numpy=True).tolist()


class OpenAIEmbedding(EmbeddingProvider):
    """OpenAI 兼容 embedding 接口。"""

    def __init__(self, model: str = "embedding-3", base_url: str = "", api_key: str = ""):
        from openai import OpenAI

        self._client = OpenAI(api_key=api_key or "EMPTY", base_url=base_url or None, timeout=60.0)
        self._model = model
        self.dim = -1

    def embed(self, text: str) -> List[float]:
        resp = self._client.embeddings.create(model=self._model, input=text)
        return list(resp.data[0].embedding)


def get_embedder(backend: str | None = None) -> EmbeddingProvider:
    backend = backend or os.environ.get("GNA_EMBEDDING_BACKEND", "mock")
    if backend == "st":
        try:
            return SentenceTransformerEmbedding()
        except ImportError:
            return MockEmbedding()
    if backend == "openai":
        try:
            return OpenAIEmbedding(
                model=os.environ.get("GNA_EMBEDDING_MODEL", "embedding-3"),
                base_url=os.environ.get("GNA_EMBEDDING_BASE_URL", ""),
                api_key=os.environ.get("GNA_EMBEDDING_API_KEY", ""),
            )
        except ImportError:
            return MockEmbedding()
    return MockEmbedding()


def cosine(a: List[float], b: List[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a)) or 1.0
    nb = math.sqrt(sum(y * y for y in b)) or 1.0
    return dot / (na * nb)
