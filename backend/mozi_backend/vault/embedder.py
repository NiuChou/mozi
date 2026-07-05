"""本地 Embedding (可插拔后端)。零外呼: 仅读本地权重目录, 无 HTTP。

三档 (auto 降级链 onnx→st→mock), 共用同签名:
- _OnnxEmbedder: BGE-M3 ONNX int8 (onnxruntime + tokenizers, 无 torch, CPU/信创/离线) —— 默认真实档
- _STEmbedder:   sentence-transformers/FlagEmbedding 高精度可选档
- _MockEmbedder: 确定性 MD5 词袋 (零依赖降级档, 维度 settings.embed_dim)

缺依赖/缺权重自动下沉到 mock。进程级单例带配置指纹, 配置变化自动重建; _reset_backend() 供测试。
"""
from __future__ import annotations

import hashlib
import math
import re
from pathlib import Path

from ..config import settings

_TOKEN_RE = re.compile(r"[一-鿿]|[a-zA-Z0-9]+")

_BACKEND = None          # 进程级单例 (模型加载昂贵)
_BACKEND_KEY = None      # (embed_backend, embed_model_path, embed_dim) 指纹


def tokenize(text: str) -> list[str]:
    """中英混排分词: 中文按字, 英文/数字按词。"""
    return [t.lower() for t in _TOKEN_RE.findall(text)]


def cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    return sum(x * y for x, y in zip(a, b))  # 输入已归一化


def _l2_normalize(vec: list[float]) -> list[float]:
    norm = math.sqrt(sum(v * v for v in vec))
    return [v / norm for v in vec] if norm > 0 else vec


class _MockEmbedder:
    name = "bge-m3-mock"

    def __init__(self, dim: int) -> None:
        self.dim = dim

    @staticmethod
    def _hash_bucket(token: str, dim: int) -> int:
        h = hashlib.md5(token.encode("utf-8")).digest()
        return int.from_bytes(h[:4], "big") % dim

    def embed(self, text: str) -> list[float]:
        vec = [0.0] * self.dim
        toks = tokenize(text)
        if not toks:
            return vec
        for tok in toks:
            vec[self._hash_bucket(tok, self.dim)] += 1.0
        return _l2_normalize(vec)


def _mean_pool_l2(states, mask) -> list[float]:
    """token 向量按 attention_mask 均值池化 + L2 归一 (纯 Python, numpy 可选)。"""
    pooled: list[float] = []
    dim = len(states[0])
    total = sum(mask) or 1
    for j in range(dim):
        pooled.append(sum(states[i][j] * mask[i] for i in range(len(states))) / total)
    return _l2_normalize(pooled)


class _OnnxEmbedder:        # 默认真实档, 无 torch
    name = "bge-m3-onnx"

    def __init__(self, path: str | None) -> None:
        import onnxruntime as ort                  # 惰性, 未装抛 ImportError → auto 回退
        from tokenizers import Tokenizer
        if not path or not Path(path).exists():
            raise FileNotFoundError("BGE-M3 ONNX 权重不存在, 回退")
        self._sess = ort.InferenceSession(str(Path(path) / "model.onnx"),
                                          providers=["CPUExecutionProvider"])  # 信创可换 NPU EP
        self._tok = Tokenizer.from_file(str(Path(path) / "tokenizer.json"))
        self.dim = 1024                            # BGE-M3 dense 维度

    def embed(self, text: str) -> list[float]:
        enc = self._tok.encode(text or " ")
        out = self._sess.run(None, {"input_ids": [enc.ids], "attention_mask": [enc.attention_mask]})
        return _mean_pool_l2(out[0][0], enc.attention_mask)


class _STEmbedder:          # 高精度可选档
    name = "bge-m3"

    def __init__(self, path: str | None) -> None:
        from sentence_transformers import SentenceTransformer
        if not path or not Path(path).exists():
            raise FileNotFoundError("BGE-M3 权重不存在, 回退")
        self._m = SentenceTransformer(path, device="cpu")
        self.dim = self._m.get_sentence_embedding_dimension()

    def embed(self, text: str) -> list[float]:
        return self._m.encode(text or " ", normalize_embeddings=True).tolist()


def _reset_backend() -> None:
    """测试钩子: 清空单例 (配套 module.settings=Settings(...) 重载, 防多后端用例互污染)。"""
    global _BACKEND, _BACKEND_KEY
    _BACKEND = None
    _BACKEND_KEY = None


def _resolve_backend():
    """惰性单例。指纹变化自动重建。auto: onnx→st→mock 降级。零外呼: 仅读本地权重目录。"""
    global _BACKEND, _BACKEND_KEY
    key = (settings.embed_backend, settings.embed_model_path, settings.embed_dim)
    if _BACKEND is not None and _BACKEND_KEY == key:
        return _BACKEND
    mode = settings.embed_backend
    if mode in ("auto", "onnx"):
        try:
            _BACKEND = _OnnxEmbedder(settings.embed_model_path)
            _BACKEND_KEY = key
            return _BACKEND
        except Exception:
            if mode == "onnx":
                raise
    if mode in ("auto", "st"):
        try:
            _BACKEND = _STEmbedder(settings.embed_model_path)
            _BACKEND_KEY = key
            return _BACKEND
        except Exception:
            if mode == "st":
                raise
    _BACKEND = _MockEmbedder(settings.embed_dim)
    _BACKEND_KEY = key
    return _BACKEND


def embed(text: str, dim: int | None = None) -> list[float]:
    """确定性/真实向量。dim 参数保留兼容 (真实后端维度由模型定)。"""
    return _resolve_backend().embed(text)


def active_model() -> str:
    return _resolve_backend().name


def active_dim() -> int:
    return getattr(_resolve_backend(), "dim", settings.embed_dim)
