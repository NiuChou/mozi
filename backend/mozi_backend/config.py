"""墨子后端配置。环境变量经 .env.local 注入 (不入仓)。"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

# 仓库根 / 数据目录
BACKEND_ROOT = Path(__file__).resolve().parent.parent


def _load_env_file() -> None:
    """启动时把 .env.local 注入 os.environ (本地优先: 密钥不入仓, 进程内注入)。

    - 已存在的真实环境变量优先, 文件不覆盖 (显式 export / CI 注入说了算)。
    - 无文件静默跳过 (零依赖, 不引 python-dotenv)。
    - 路径可经 MOZI_ENV_FILE 覆盖。须在任何 os.getenv 之前调用。
    """
    env_path = Path(os.getenv("MOZI_ENV_FILE", BACKEND_ROOT / ".env.local"))
    try:
        text = env_path.read_text(encoding="utf-8")
    except OSError:
        return
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        key, sep, val = line.partition("=")
        if not sep:
            continue
        key = key.strip()
        val = val.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = val


_load_env_file()

DATA_DIR = Path(os.getenv("MOZI_DATA_DIR", BACKEND_ROOT / "data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = Path(os.getenv("MOZI_DB_PATH", DATA_DIR / "mozi.db"))
# 设备令牌 (信创/本地优先): 启动时生成, 写 gitignored 文件, require_auth 时校验
DEVICE_TOKEN_PATH = Path(os.getenv("MOZI_DEVICE_TOKEN_PATH", DATA_DIR / ".device_token"))


@dataclass(frozen=True)
class ProviderKey:
    """单个模型供应商的密钥与端点。缺 key 时该 provider 不激活,自动走 mock。"""

    name: str
    env_key: str
    base_url: str
    kind: str  # "openai" | "anthropic"

    @property
    def api_key(self) -> str | None:
        return os.getenv(self.env_key) or None

    @property
    def active(self) -> bool:
        return bool(self.api_key)


# 模型供应商目录 (国产优先, 全球版次之) —— 对齐 §4 环境表
PROVIDERS: dict[str, ProviderKey] = {
    "glm":      ProviderKey("glm",      "GLM_API_KEY",       "https://open.bigmodel.cn/api/paas/v4", "openai"),
    "minimax":  ProviderKey("minimax",  "MINIMAX_API_KEY",   "https://api.minimax.chat/v1",          "openai"),
    "kimi":     ProviderKey("kimi",     "KIMI_API_KEY",      "https://api.moonshot.cn/v1",           "openai"),
    "deepseek": ProviderKey("deepseek", "DEEPSEEK_API_KEY",  "https://api.deepseek.com/v1",          "openai"),
    "ark":      ProviderKey("ark",      "ARK_API_KEY",       "https://ark.cn-beijing.volces.com/api/v3", "openai"),
    "openai":   ProviderKey("openai",   "OPENAI_API_KEY",    "https://api.openai.com/v1",            "openai"),
    "anthropic":ProviderKey("anthropic","ANTHROPIC_API_KEY", "https://api.anthropic.com/v1",         "anthropic"),
}


@dataclass(frozen=True)
class Settings:
    db_path: Path = DB_PATH
    data_dir: Path = DATA_DIR
    device_token_path: Path = DEVICE_TOKEN_PATH
    embed_model: str = "bge-m3-mock"
    embed_dim: int = 256
    # 检索后端 (可插拔, 全 auto→缺依赖降级 mock/bruteforce/内存 BM25, 零外呼)
    embed_backend: str = field(default_factory=lambda: os.getenv("MOZI_EMBED_BACKEND", "auto"))   # auto|mock|onnx|st
    embed_model_path: str | None = field(default_factory=lambda: os.getenv("MOZI_EMBED_MODEL_PATH") or None)
    ann_backend: str = field(default_factory=lambda: os.getenv("MOZI_ANN_BACKEND", "auto"))        # auto|vec|bruteforce
    bm25_backend: str = field(default_factory=lambda: os.getenv("MOZI_BM25_BACKEND", "auto"))      # auto|fts5|memory
    fts_simple_path: str | None = field(default_factory=lambda: os.getenv("MOZI_FTS_SIMPLE_PATH") or None)
    # 注入门: 空 → 自适应标定; 显式 float → 固定地板 (压测后标定)
    inject_floor: float | None = field(
        default_factory=lambda: (lambda v: float(v) if v else None)(os.getenv("MOZI_INJECT_FLOOR")))
    default_user_id: str = "u_demo"
    default_user_email: str = "demo@mozi.local"
    default_region: str = "CN"
    # 本地优先: 缺 key 全部走 mock, 零外呼 (§9 数据主权)
    local_first: bool = os.getenv("MOZI_LOCAL_FIRST", "1") == "1"
    # 鉴权开关 (默认全 0 == 旧单用户行为, 现有 smoke 全绿)
    require_auth: bool = os.getenv("MOZI_REQUIRE_AUTH", "0") == "1"
    multiuser: bool = os.getenv("MOZI_MULTIUSER", "0") == "1"
    # multiuser 过渡门: 仅本地可信网络临时放行裸 X-User-Id (无签名令牌), 默认关, 不安全
    multiuser_allow_unsigned: bool = os.getenv("MOZI_MULTIUSER_ALLOW_UNSIGNED", "0") == "1"
    # 用户令牌 TTL (秒); HMAC/JWT 均用此 exp。默认 30 天
    user_token_ttl_sec: int = int(os.getenv("MOZI_USER_TOKEN_TTL_SEC", str(30 * 24 * 3600)))
    # 兼容旧 v1 无过期令牌解析 (升级期平滑), 默认开
    allow_legacy_v1: bool = os.getenv("MOZI_ALLOW_LEGACY_V1", "1") == "1"
    posthog_key: str | None = field(default_factory=lambda: os.getenv("POSTHOG_KEY") or None)
    posthog_host: str = field(default_factory=lambda: os.getenv("POSTHOG_HOST") or "https://us.i.posthog.com")
    telemetry_max_bytes: int = field(
        default_factory=lambda: int(os.getenv("MOZI_TELEMETRY_MAX_BYTES", str(5 * 1024 * 1024))))
    telemetry_max_rolls: int = field(default_factory=lambda: int(os.getenv("MOZI_TELEMETRY_MAX_ROLLS", "3")))
    # 联网搜索后端 (可插拔): 配 SEARXNG_URL → 自托管 SearXNG; 否则 ddgs 库; 都无则 DDG 爬虫兜底
    searxng_url: str | None = field(default_factory=lambda: os.getenv("MOZI_SEARXNG_URL") or None)
    # KG 实体消歧: 向量 cosine 阈值 (真实 BGE-M3 上线后由 calibrate 脚本标定) + 防爆扫描上限
    kg_dedup_sim_threshold: float = float(os.getenv("MOZI_KG_DEDUP_SIM", "0.92"))
    kg_dedup_scan_cap: int = int(os.getenv("MOZI_KG_DEDUP_SCAN_CAP", "2000"))
    # 迁移遇孤儿/脏 FK 的降级行为: purge(默认清孤儿放行) | skip_table | raise(严格中止)
    migration_on_dirty: str = os.getenv("MOZI_MIGRATION_ON_DIRTY", "purge")
    # 破坏性迁移前自动备份 DB 文件的保留份数; 0=禁用备份 (database.py 实际按 env 即时读取以便测试)
    migration_backup_keep: int = int(os.getenv("MOZI_MIGRATION_BACKUP_KEEP", "5"))

    def active_providers(self) -> list[str]:
        return [n for n, p in PROVIDERS.items() if p.active]

    def egress_allowed(self) -> bool:
        """遥测上报受 local_first 总闸约束 (不引第二套门, 守唯一 egress 门)。"""
        return not self.local_first


settings = Settings()
