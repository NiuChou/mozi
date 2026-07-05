"""数据主权路由 (§9 被遗忘权 / 契约 D)。

GET  /v1/export  —— 按 user_id 打包该用户全部数据为 JSON (可移植性 / data portability)。
DELETE /v1/account —— 级联删该 user_id 全表行 + 写 audit_log (被遗忘权 / right to erasure)。

身份统一经 auth.current_user_id; 删除段用 database.transaction 保证原子 (失败整体回滚)。
"""
from __future__ import annotations

import secrets as _secrets

from fastapi import APIRouter, Depends, Header, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from . import __version__
from .auth import _bearer, current_user_id, get_device_token, sign_user_token
from .config import settings
from .db import dal
from .db.database import get_conn, transaction
from .util import now

router = APIRouter(prefix="/v1", tags=["sovereign"])


class IssueTokenBody(BaseModel):
    user_id: str
    email: str | None = None
    region: str | None = None


@router.post("/auth/token")
def issue_user_token(body: IssueTokenBody,
                     authorization: str | None = Header(default=None)):
    """换发 user_token: 仅 device_token 持有者可为任意 uid 签发 (信创/本地可信边界)。"""
    dev = get_device_token()
    presented = _bearer(authorization)
    if not dev or not presented or not _secrets.compare_digest(presented, dev):
        raise HTTPException(status_code=401, detail="invalid device token")
    with get_conn() as conn:
        with transaction(conn):
            dal.ensure_user(conn, body.user_id,
                            body.email or f"{body.user_id}@mozi.local",
                            body.region or settings.default_region)
    return {"user_id": body.user_id, "user_token": sign_user_token(body.user_id)}


@router.get("/export")
def export_account(user_id: str = Depends(current_user_id)):
    """导出该用户全部数据 (JSON 下载)。本地优先: 数据始终归用户所有, 可随时取回。"""
    with get_conn() as conn:
        data = dal.export_user_data(conn, user_id)
        dal.log_audit(conn, user_id=user_id, action="export", resource="account", egress=False)
    payload = {
        "schema": "mozi.export.v1",
        "exported_at": now(),
        "version": __version__,
        "user_id": user_id,
        "data": data,
    }
    return JSONResponse(
        content=payload,
        headers={"Content-Disposition": f'attachment; filename="mozi-export-{user_id}.json"'},
    )


@router.delete("/account")
def delete_account(user_id: str = Depends(current_user_id)):
    """级联删除该用户全部数据 + 留存审计 (被遗忘权)。

    删除与审计写入同一事务: 先级联删 (含旧 audit_log 行), 再写删除审计条目,
    故审计记录在删除后留存; 任一步失败整体回滚, 数据不丢半。
    """
    with get_conn() as conn:
        with transaction(conn):
            counts = dal.delete_user_data(conn, user_id)
            # 删除后写审计 (该行不在被删范围内, 留作合规凭证)
            dal.log_audit(conn, user_id=user_id, action="delete", resource="account", egress=False)
    return {"status": "deleted", "user_id": user_id, "deleted": counts}
