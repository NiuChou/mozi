"""EPIC-RETRIEVAL: embedder 后端降级 + 单例指纹 + 召回回归 (量化掉点门, 缺依赖自动 skip)。"""
from __future__ import annotations

import dataclasses
import os
import unittest

try:
    from ._helpers import fresh_conn
except ImportError:
    from _helpers import fresh_conn

from mozi_backend.db import dal  # noqa: E402
from mozi_backend.vault import embedder, retrieval, service  # noqa: E402

KNOWN = ("墨子是本地优先的桌面应用。\nUMA 是多模型路由网关。\nVault 使用 SQLite。\n"
         "墨子依赖 BGE-M3。\n检索引擎使用 RRF 融合。")


def _backend_loadable(name: str) -> bool:
    if name == "mock":
        return True
    # 零外呼: 真实后端须有本地权重路径才算可加载 (无 MOZI_EMBED_MODEL_PATH 时不联网下载 → 跳过,
    # 而非强制构造导致 FileNotFoundError 硬失败)。
    path = embedder.settings.embed_model_path
    if not path or not os.path.exists(path):
        return False
    try:
        if name == "onnx":
            import onnxruntime  # noqa: F401
            import tokenizers  # noqa: F401
        elif name == "st":
            import sentence_transformers  # noqa: F401
        return True
    except ImportError:
        return False


class EmbedBackendTest(unittest.TestCase):
    def tearDown(self) -> None:
        embedder._reset_backend()      # 防多后端用例互污染

    def test_default_is_mock_zero_dep(self) -> None:
        embedder._reset_backend()
        self.assertEqual(embedder.active_model(), "bge-m3-mock")
        self.assertEqual(embedder.active_dim(), 256)
        self.assertEqual(len(embedder.embed("墨子")), 256)

    def test_backend_fallback_on_missing_weights(self) -> None:
        orig = embedder.settings
        embedder.settings = dataclasses.replace(orig, embed_backend="auto",
                                                embed_model_path="/nonexistent/bge-m3")
        embedder._reset_backend()
        try:
            self.assertEqual(embedder.active_model(), "bge-m3-mock", "缺权重须降级 mock")
        finally:
            embedder.settings = orig
            embedder._reset_backend()

    def test_singleton_fingerprint_repointing(self) -> None:
        orig = embedder.settings
        self.assertEqual(embedder.active_dim(), 256)
        embedder.settings = dataclasses.replace(orig, embed_dim=128)
        embedder._reset_backend()
        try:
            self.assertEqual(embedder.active_dim(), 128, "指纹 (embed_dim) 变化须重建后端")
        finally:
            embedder.settings = orig
            embedder._reset_backend()

    def test_recall_per_backend(self) -> None:
        """量化掉点门: 每个可加载后端 dense 路 top1 须命中关键词 (onnx/st 缺依赖 skip)。"""
        for name in ("mock", "onnx", "st"):
            if not _backend_loadable(name):
                continue
            orig = embedder.settings
            embedder.settings = dataclasses.replace(orig, embed_backend=name)
            embedder._reset_backend()
            conn = fresh_conn()
            try:
                dal.ensure_user(conn, "u", "u@x.cn")
                service.archive_document(conn, user_id="u", title="架构", content=KNOWN)
                res = retrieval.search(conn, "u", "UMA 路由网关", routes=["dense"], k=3)
                self.assertTrue(res.hits, f"{name} dense 须有命中")
                self.assertIn("UMA", res.hits[0].text, f"{name} top1 须含关键词 UMA")
            finally:
                conn.close()
                embedder.settings = orig
                embedder._reset_backend()


if __name__ == "__main__":
    unittest.main()
