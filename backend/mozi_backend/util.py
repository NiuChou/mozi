"""通用工具: ID 生成 / 时间 / JSON。"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Any


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


def now() -> str:
    """SQLite CURRENT_TIMESTAMP 同构: 'YYYY-MM-DD HH:MM:SS' (UTC)。"""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def period_now() -> str:
    """计量周期: 'YYYY-MM'。"""
    return datetime.now(timezone.utc).strftime("%Y-%m")


def jdump(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False)


def jload(s: str | None, default: Any = None) -> Any:
    if not s:
        return default
    try:
        return json.loads(s)
    except (json.JSONDecodeError, TypeError):
        return default
