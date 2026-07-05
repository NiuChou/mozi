"""测试公共脚手架。

每个测试模块 import 前必须先经此处把 MOZI_DATA_DIR / MOZI_DB_PATH 指向独立临时目录,
避免污染开发库, 也保证各模块库隔离。因 config.settings 在 import 期即定型 (frozen dataclass),
故环境变量须在任何 mozi_backend import 之前注入 —— 各测试模块顶部 `from ._helpers import *`
即触发本模块的副作用 (建临时库 + 设环境)。
"""
from __future__ import annotations

import os
import tempfile

# ---- 1. 在任何 mozi_backend import 之前锁定独立临时库 ----
_TMP = tempfile.mkdtemp(prefix="mozi_utest_")
os.environ["MOZI_DATA_DIR"] = _TMP
os.environ["MOZI_DB_PATH"] = os.path.join(_TMP, "utest.db")
# 测试须 hermetic: 指向不存在的 env 文件, 阻断 config 自动加载真实 .env.local (否则真 key 入测试环境,
# provider 被激活 → 单测误打真实模型 (慢/不确定)。开发/生产路径不受影响)。
os.environ["MOZI_ENV_FILE"] = os.path.join(_TMP, ".env.absent")
os.environ.setdefault("MOZI_LOCAL_FIRST", "1")
# 默认关闭鉴权/多用户 (个别用例内显式开启再重载 settings)
os.environ.setdefault("MOZI_REQUIRE_AUTH", "0")
os.environ.setdefault("MOZI_MULTIUSER", "0")

import contextlib  # noqa: E402
import json  # noqa: E402
import sqlite3  # noqa: E402

import httpx  # noqa: E402

from mozi_backend.db.database import _connect, init_db  # noqa: E402

TMP_DIR = _TMP


def fresh_conn() -> sqlite3.Connection:
    """建一个全新隔离库的连接 (autocommit, 与产品同构)。每个用例独立, 互不干扰。"""
    fd, path = tempfile.mkstemp(prefix="mozi_case_", suffix=".db", dir=_TMP)
    os.close(fd)
    init_db(path)
    conn = _connect(path)
    return conn


def _auth_headers(uid: str) -> dict:
    """multiuser 测试用: 直接构造签名 Bearer 头 (不走 /v1/auth/token, 保留 users 表初始为空)。"""
    from mozi_backend import auth
    return {"Authorization": f"Bearer {auth.sign_user_token(uid)}"}


def parse_sse(text: str) -> list[dict]:
    out: list[dict] = []
    for line in text.splitlines():
        if line.startswith("data:"):
            with contextlib.suppress(json.JSONDecodeError):
                out.append(json.loads(line[5:].strip()))
    return out


def sse_body(*frames: str) -> str:
    """把若干 SSE data 帧拼成响应体 (每帧 `data: <frame>\\n\\n`)。"""
    return "".join(f"data: {f}\n\n" for f in frames)


def mock_async_client_factory(transport: httpx.MockTransport):
    """生成可替换 adapter 模块内 `httpx.AsyncClient` 的工厂, 注入 MockTransport。

    适配器内部以 `httpx.AsyncClient(timeout=...)` 直接构造 (无 transport 注入点),
    故测试期 monkeypatch 模块级 httpx.AsyncClient 为本工厂, 把真实出网替换为内存 Mock。
    """

    real_client_cls = httpx.AsyncClient  # 捕获真类, 防与被 patch 的模块级名递归

    def factory(*args, **kwargs):
        kwargs["transport"] = transport
        # MockTransport 不需要真实超时配置, 但保留调用方传参以贴近真实路径
        return real_client_cls(*args, **kwargs)

    return factory


def make_mock_transport(handler) -> httpx.MockTransport:
    """正式 transport 注入点: 直接传给 adapter.astream(transport=...), 不依赖 monkeypatch。"""
    return httpx.MockTransport(handler)


async def collect_astream(adapter, spec, convo, usage: dict | None = None, *, transport=None) -> str:
    """把 adapter.astream 的所有 delta 收集成完整字符串。"""
    parts: list[str] = []
    if transport is not None:
        agen = adapter.astream(spec, convo, usage, transport=transport)
    else:
        agen = adapter.astream(spec, convo, usage)
    async for delta in agen:
        parts.append(delta)
    return "".join(parts)
