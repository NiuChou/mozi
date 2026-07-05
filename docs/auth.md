# 墨子鉴权与身份 (auth.py)

> 信创/本地优先: 零 key 离线可运行, 单一对称密钥 (device_token), 远期可换国密。

## 三模式鉴权矩阵 (current_user_id)

| 模式 | 条件 | 行为 |
|---|---|---|
| A 单用户 | `multiuser=0` | `require_auth=1` 时校验 `Authorization: Bearer <device_token>`, 失败 401; 恒返回 `default_user_id`。逐字节保留旧单用户行为。 |
| B 多用户·签名 | `multiuser=1` 且有合法 user_token | 取令牌内签名 uid; 若同时带 `X-User-Id` 且不符 → **403** (防越权伪造)。 |
| C 多用户·过渡 | `multiuser=1` 且无合法令牌 | `multiuser_allow_unsigned=1` 且有 `X-User-Id` → 放行裸头 (过渡, **不安全**, 告警一次); 否则 **401**。 |

收紧点: multiuser 旧版无条件回传裸 `X-User-Id` (任填即越权)。现要求 Bearer user_token 自验证。
`MOZI_MULTIUSER_ALLOW_UNSIGNED=1` 仅本地可信网灰度过渡用, 前端发版后应关闭 (默认 0)。

## 可插拔签名 provider

复刻 web_search 可插拔后端套路 (`_resolve_token_provider`):
- **JwtHs256Provider** (装 PyJWT): HS256, 自带 exp/iat, 解码锁 `algorithms=['HS256']` 防 alg=none/算法混淆。
- **HmacV2Provider** (纯 stdlib 兜底, 信创零依赖): 串 `v2|uid|exp` 经 HMAC-SHA256 签名, 带过期。
- **旧 v1 兼容** (`allow_legacy_v1=1`, 默认开): 解析无 exp 的 `mozi.user.v1|uid` 旧串, 升级期平滑。

`verify_user_token` 双路兜底: 主路当前 provider → JWT 环境也试 HMAC-v2 串 → 最后试旧 v1。任何异常统一返 None (绝不 500)。

切库运维: 装/卸 PyJWT 切换 provider 后, 旧 JWT 串 (卸库后) 无法验 → 401, 前端引导重新
`POST /v1/auth/token` 换发 (走当前可用 provider)。HMAC-v2 串始终可验 (纯 stdlib)。
启动日志打印 active provider 名便于运维发现切换。

## device_token = 主密钥安全边界 (硬约束)

`.device_token` 既是 `require_auth` 根凭证, 又是 user_token 的 HMAC 签名密钥 (单密钥模型)。

- **必须 0600 + gitignored**: `generate_device_token` 落盘后 `os.chmod(0o600)` (只读 FS 容错告警);
  `get_device_token` 惰性读入时若权限过宽 (`st_mode & 0o077`) 告警一次 (不阻断, 信创只读 FS 兼容)。
- **泄露处置**: 删 `.device_token` 文件重启换发 — 新 secret 使旧令牌全体失效 (隐式吊销)。
- **吊销**: 无独立吊销表; 换 device_token 是唯一手段。`user_token_ttl_sec` (默认 30 天) 提供有限期天然兜底,
  把"无法吊销"从永久降为 TTL 上限。

不引入第二/非对称密钥 (当前范围外, 违背零 key 离线)。后续可选: 非对称/第二密钥、
jti 黑名单吊销表、refresh token 滑动续期。

## 换发端点

`POST /v1/auth/token` (sovereign.py): 仅 device_token 持有者 (Bearer device_token) 可为任意 uid 签发
user_token, 同时 `ensure_user`。Body: `{user_id, email?, region?}` → `{user_id, user_token}`。
