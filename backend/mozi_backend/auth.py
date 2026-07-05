"""鉴权与身份依赖 (信创/本地优先): 可插拔签名 provider + 三模式鉴权矩阵。

三模式 (current_user_id):
- 模式 A (multiuser=0): 单用户。require_auth=1 时校验 Authorization: Bearer <device_token>,
  恒返回 default_user_id。逐字节保留旧单用户行为。
- 模式 B (multiuser=1, 有合法 user_token): 取签名 uid; 若同时带 X-User-Id 且不符 → 403。
- 模式 C (multiuser=1, 无合法令牌): allow_unsigned=1 且有 X-User-Id → 放行裸头 (过渡, 不安全);
  否则 401。

签名 provider 可插拔 (复刻 web_search/_resolve_backend 套路): 装 PyJWT → HS256 (自带 exp,
锁 algorithms 防 alg=none); 否则纯 stdlib HMAC-v2 (串 v2|uid|exp, 信创零依赖可运行)。
device_token 等价主密钥, 必须 0600 + gitignored; 泄露须删文件重启换发 (secret 变更=全体令牌失效)。
详见 docs/auth.md。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import os
import secrets
import time
from typing import Protocol

from fastapi import Header, HTTPException

from .config import settings

_LOG = logging.getLogger("mozi.auth")
_DEVICE_TOKEN: str | None = None
_TOKEN_PROVIDER: "_TokenProvider | None" = None  # 惰性缓存, 复刻 tools._resolve_backend


# ============================ 可插拔签名 provider ============================
class _TokenProvider(Protocol):
    name: str

    def sign(self, uid: str, secret: str, ttl: int) -> str: ...
    def verify(self, token: str, secret: str) -> str | None: ...


def _b64u(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode("ascii")


def _b64u_dec(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


class HmacV2Provider:
    """纯 stdlib 兜底 (零 PyJWT, 信创可运行)。串 v2|uid|exp 经 HMAC-SHA256 签名, 带过期。"""

    name = "hmac-v2"

    def sign(self, uid: str, secret: str, ttl: int) -> str:
        exp = int(time.time()) + ttl
        payload = f"v2|{uid}|{exp}".encode("utf-8")
        mac = hmac.new(secret.encode("utf-8"), payload, hashlib.sha256).digest()
        return f"{_b64u(payload)}.{_b64u(mac)}"

    def verify(self, token: str, secret: str) -> str | None:
        try:
            p_b64, mac_b64 = token.split(".", 1)
            payload = _b64u_dec(p_b64)
            expected = hmac.new(secret.encode("utf-8"), payload, hashlib.sha256).digest()
            if not hmac.compare_digest(_b64u_dec(mac_b64), expected):
                return None
            kind, uid, exp = payload.decode("utf-8").split("|", 2)
            if kind != "v2" or int(exp) < int(time.time()):
                return None
            return uid
        except Exception:
            return None


class JwtHs256Provider:
    """PyJWT HS256, 自带 exp, 解码锁 algorithms=['HS256'] 防 alg=none/算法混淆。"""

    name = "jwt-hs256"

    def __init__(self, jwt_mod) -> None:
        self._jwt = jwt_mod

    def sign(self, uid: str, secret: str, ttl: int) -> str:
        now_ts = int(time.time())
        return self._jwt.encode({"sub": uid, "iat": now_ts, "exp": now_ts + ttl},
                                secret, algorithm="HS256")

    def verify(self, token: str, secret: str) -> str | None:
        try:
            claims = self._jwt.decode(token, secret, algorithms=["HS256"])
            return claims.get("sub")
        except Exception:   # ExpiredSignature / DecodeError / 任何 → None, 绝不 500
            return None


def _verify_legacy_v1(token: str, secret: str) -> str | None:
    """双路兜底: 旧 v1 无 exp 串 mozi.user.v1|uid.<mac>。仅 allow_legacy_v1。"""
    if not settings.allow_legacy_v1:
        return None
    try:
        body, mac_b64 = token.rsplit(".", 1)
        if not body.startswith("mozi.user.v1|"):
            return None
        expected = hmac.new(secret.encode(), body.encode(), hashlib.sha256).digest()
        if not hmac.compare_digest(_b64u_dec(mac_b64), expected):
            return None
        return body.split("|", 1)[1]
    except Exception:
        return None


def _resolve_token_provider() -> _TokenProvider:
    global _TOKEN_PROVIDER
    if _TOKEN_PROVIDER is not None:
        return _TOKEN_PROVIDER
    try:
        import jwt  # 惰性导入: 未装则降级纯 Python HMAC
        _TOKEN_PROVIDER = JwtHs256Provider(jwt)
    except ImportError:
        _TOKEN_PROVIDER = HmacV2Provider()
    return _TOKEN_PROVIDER


def sign_user_token(uid: str, secret: str | None = None) -> str:
    sec = secret or get_device_token()
    if not sec:
        raise RuntimeError("device_token 未就绪, 无法签发 user_token")
    return _resolve_token_provider().sign(uid, sec, settings.user_token_ttl_sec)


def verify_user_token(token: str, secret: str | None = None) -> str | None:
    sec = secret or get_device_token()
    if not sec or not token:
        return None
    prov = _resolve_token_provider()
    uid = prov.verify(token, sec)              # 主路 (当前 provider)
    if uid is not None:
        return uid
    # 跨 provider 兜底: JWT 环境也试 HMAC-v2 串 (反之 JWT 串需 PyJWT, 无则只能 None)
    if prov.name != "hmac-v2":
        uid = HmacV2Provider().verify(token, sec)
        if uid is not None:
            return uid
    return _verify_legacy_v1(token, sec)       # 最后兜底旧 v1


# ============================ device_token (主密钥等价物) ============================
def generate_device_token() -> str:
    """生成并落盘 device_token (启动时调用, 幂等: 已存在则复用)。落盘后收紧 0600。"""
    global _DEVICE_TOKEN
    path = settings.device_token_path
    if path.exists():
        token = path.read_text(encoding="utf-8").strip()
        if token:
            _DEVICE_TOKEN = token
            _tighten_perm(path)
            return token
    token = secrets.token_urlsafe(32)
    path.parent.mkdir(parents=True, exist_ok=True)
    # 原子 0600 创建 (opener 在 os.open 时即定权限, 消除"先 write 默认 umask 再 chmod"的 TOCTOU 窗口)
    try:
        with open(path, "w", opener=_opener_0600) as f:
            f.write(token)
    except OSError:                         # 只读 FS / 不支持 opener → 兜底普通写
        path.write_text(token, encoding="utf-8")
    _tighten_perm(path)                     # 兜底: 已存在的宽权限文件 (opener 不改既存文件) 仍收紧
    _DEVICE_TOKEN = token
    return token


def _opener_0600(p, flags):
    return os.open(p, flags, 0o600)


def _tighten_perm(path) -> None:
    try:
        os.chmod(path, 0o600)   # 主密钥等价物, 信创只读 FS 容错
    except OSError:
        _LOG.warning("device_token 文件权限收紧失败 (只读 FS?), 请人工确认 0600")


def get_device_token() -> str | None:
    """读取当前 device_token (惰性从盘加载)。落盘权限非 0600 时告警一次 (不阻断)。"""
    global _DEVICE_TOKEN
    if _DEVICE_TOKEN is not None:
        return _DEVICE_TOKEN
    path = settings.device_token_path
    if path.exists():
        try:
            if os.name == "posix" and (path.stat().st_mode & 0o077):
                _LOG.warning("device_token 权限过宽 (应 0600); 视为泄露风险")
        except OSError:
            pass
        token = path.read_text(encoding="utf-8").strip()
        _DEVICE_TOKEN = token or None
    return _DEVICE_TOKEN


# ============================ 鉴权依赖 (三模式矩阵) ============================
def _bearer(authorization: str | None) -> str:
    if authorization and authorization.lower().startswith("bearer "):
        return authorization[7:].strip()
    return ""


def current_user_id(
    x_user_id: str | None = Header(default=None, alias="X-User-Id"),
    authorization: str | None = Header(default=None),
) -> str:
    """FastAPI 依赖: 解析请求身份 (契约 A)。"""
    # ---- 模式 A: 单用户 (multiuser=0) —— 逐字节保留旧逻辑 ----
    if not settings.multiuser:
        if settings.require_auth:
            expected = get_device_token()
            presented = _bearer(authorization)
            if not expected or not presented or not secrets.compare_digest(presented, expected):
                raise HTTPException(status_code=401, detail="invalid device token")
        return settings.default_user_id
    # ---- 模式 B/C: multiuser=1 ----
    token = _bearer(authorization)
    uid = verify_user_token(token) if token else None
    if uid is not None:
        if x_user_id and x_user_id != uid:        # 一致性校验, 防越权伪造
            raise HTTPException(status_code=403, detail="X-User-Id mismatches token")
        return uid
    if settings.multiuser_allow_unsigned and x_user_id:
        _LOG.warning("multiuser_allow_unsigned: 放行裸 X-User-Id=%s (过渡, 不安全)", x_user_id)
        return x_user_id
    raise HTTPException(status_code=401, detail="missing or invalid user token")
