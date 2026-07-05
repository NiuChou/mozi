"""遥测: 本地按 用户/月 分片 + 大小轮转 (stdlib RotatingFileHandler) + 可选 PostHog 上报。

零 key 零外呼默认: 缺 POSTHOG_KEY 或 local_first=1 → 仅本地分片, 不外呼。上报经唯一 egress 门
(gateway.egress.audit, telemetry.report 已入白名单)。recent 先按 user_id 物理定位目录再跨分片
反向读 (守 #7 行级隔离: 绝不先取末 N 条再过滤)。
"""
from __future__ import annotations

import json
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

import httpx

from ..config import settings
from ..util import now, period_now


def _telemetry_dir() -> Path:          # 调用期求值, 测试 rebind data_dir 即生效
    return settings.data_dir / "telemetry"


def _safe_seg(s: str) -> str:          # 防路径穿越
    seg = "".join(ch if (ch.isalnum() or ch in "_-") else "_" for ch in (s or ""))
    return seg or "unknown"


def _user_dir(user_id: str) -> Path:
    return _telemetry_dir() / _safe_seg(user_id)


def _active_shard(user_id: str) -> Path:
    return _user_dir(user_id) / f"{period_now()}.jsonl"


class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return record.getMessage()       # record.msg 已是序列化 JSON 串


_HANDLERS: dict[str, logging.Logger] = {}   # key: shard 路径 → Logger (含 RotatingFileHandler)


def _reset_handlers() -> None:
    """测试钩子: 清空 handler 缓存 (rebind telemetry_max_bytes 后须重建)。"""
    for lg in _HANDLERS.values():
        for h in lg.handlers:
            h.close()
        lg.handlers.clear()
    _HANDLERS.clear()


def _shard_logger(shard: Path) -> logging.Logger:
    key = str(shard)
    lg = _HANDLERS.get(key)
    if lg is not None and getattr(lg, "_mozi_max_bytes", None) == settings.telemetry_max_bytes:
        return lg
    shard.parent.mkdir(parents=True, exist_ok=True)
    lg = logging.getLogger(f"mozi.telemetry.{key}")
    lg.handlers.clear()
    lg.propagate = False
    lg.setLevel(logging.INFO)
    h = RotatingFileHandler(shard, maxBytes=settings.telemetry_max_bytes,
                            backupCount=settings.telemetry_max_rolls, encoding="utf-8")
    h.setFormatter(_JsonFormatter())
    lg.addHandler(h)
    lg._mozi_max_bytes = settings.telemetry_max_bytes   # type: ignore[attr-defined]
    _HANDLERS[key] = lg
    return lg


def capture(event: str, props: dict[str, Any] | None = None, user_id: str | None = None) -> None:
    uid = user_id or settings.default_user_id
    record = {"event": event, "user_id": uid, "ts": now(), "props": props or {}}
    _append_local(uid, record)         # 永不抛
    _maybe_report(record)              # 内部自吞


def _append_local(user_id: str, record: dict) -> None:
    try:
        _shard_logger(_active_shard(user_id)).info(json.dumps(record, ensure_ascii=False))
    except Exception:  # noqa: BLE001 — 遥测不阻断主链路
        pass


# ---------- recent: 物理目录隔离 + 跨全分片反向读 ----------
def _ordered_shards(d: Path) -> list[Path]:
    files = list(d.glob("*.jsonl")) + list(d.glob("*.jsonl.*"))   # 含轮转代 .1/.2/.3

    def key(p: Path):
        name = p.name
        month = name[:7]
        roll = 0
        if ".jsonl." in name:
            try:
                roll = int(name.rsplit(".", 1)[1])
            except ValueError:
                roll = 999
        return (month, -roll)          # 月降序; roll 越小(.1)越新, 活动(无后缀 roll=0)最新
    return sorted(files, key=key, reverse=True)


def _read_lines_reverse(path: Path, block: int = 8192):
    try:
        with path.open("rb") as f:
            f.seek(0, 2)
            pos = f.tell()
            buf = b""
            while pos > 0:
                read = min(block, pos)
                pos -= read
                f.seek(pos)
                buf = f.read(read) + buf
                parts = buf.split(b"\n")
                buf = parts[0]
                for piece in reversed(parts[1:]):
                    if piece.strip():
                        yield piece.decode("utf-8", "replace")
            if buf.strip():
                yield buf.decode("utf-8", "replace")
    except OSError:
        return


def recent(limit: int = 50, user_id: str | None = None) -> list[dict[str, Any]]:
    """新→旧 ≤limit。user_id 非空 → 只读其目录 (物理隔离, 杜绝越权); None → 聚合所有 (仅 CLI 诊断)。"""
    base = _telemetry_dir()
    dirs = [_user_dir(user_id)] if user_id is not None else (
        sorted(base.glob("*")) if base.exists() else [])
    out: list[dict[str, Any]] = []
    for d in dirs:
        if not d.exists():
            continue
        for shard in _ordered_shards(d):
            for line in _read_lines_reverse(shard):
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
                if len(out) >= limit:
                    return out
    return out


# ---------- 可选 PostHog 上报 (唯一 egress 门; 零 key/local_first 默认不外呼) ----------
def _posthog_enabled() -> bool:
    return bool(settings.posthog_key) and settings.egress_allowed()


def _maybe_report(record: dict) -> None:
    # 防递归: egress.audit 自身会 capture('egress'), 若再上报→audit→capture('egress')→… 无限递归
    if record["event"] == "egress" or not _posthog_enabled():
        return
    try:
        payload = {"api_key": settings.posthog_key, "event": record["event"],
                   "distinct_id": record["user_id"],
                   "properties": {**record["props"], "ts": record["ts"]},
                   "timestamp": record["ts"]}
        with httpx.Client(timeout=3.0) as c:
            c.post(f"{settings.posthog_host}/capture/", json=payload)
        _audit_egress(record["user_id"], record["event"])
    except Exception:  # noqa: BLE001 — 上报失败绝不阻断主链路
        pass


def _audit_egress(user_id: str, event: str) -> None:
    try:
        from ..db.database import get_conn       # 真实路径 (非 db.session)
        from ..gateway import egress             # 唯一出网门 (telemetry.report 已入白名单)
        with get_conn() as conn:
            egress.audit(conn, user_id=user_id, provider="posthog", action="telemetry.report",
                         resource=event[:80], privacy_tier="local_first", is_real=True)
    except Exception:  # noqa: BLE001
        pass
