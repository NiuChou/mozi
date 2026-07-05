"""Skill 兼容层 (§8.5 Harness-lite)。公共子集 = SKILL.md 开放标准; 各家扩展经 Adapter 归一。

三级渐进式加载 (P-026): ①启动级 name+description ②激活级 正文 ③资源级 脚本/资源。
Capability Manifest 驱动选模; allowed-tools 沙箱白名单; 装载前静态扫描 (供应链安全基线)。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

# 危险命令 / 外联 / 明文密钥 模式 (装载前静态扫描)
_DANGER = [
    re.compile(r"\brm\s+-rf\b"), re.compile(r":\(\)\s*\{.*\};:"),
    re.compile(r"\bcurl\b.*\bhttp"), re.compile(r"\bwget\b.*\bhttp"),
    re.compile(r"(?i)(api[_-]?key|secret|token)\s*[:=]\s*['\"][A-Za-z0-9]{16,}"),
    re.compile(r"\beval\s*\("), re.compile(r"\bsudo\b"),
]

# 默认发现来源 (§8.5.4): 墨子原生优先 / claude / codex
_BUNDLED = Path(__file__).resolve().parent.parent.parent / "sample_skills"
DEFAULT_ROOTS = [
    ("mozi", _BUNDLED),                                # 墨子原生 (优先级最高)
    ("mozi", Path.home() / ".mozi" / "skills"),
    ("claude", Path.home() / ".claude" / "skills"),
    ("codex", Path.home() / ".codex" / "skills"),
]


@dataclass
class SkillDescriptor:
    name: str
    description: str
    source: str
    origin_path: str
    version: str = "0"
    allowed_tools: list[str] = field(default_factory=list)
    auto_invoke: bool = True
    tier: str = "A"
    capability: dict = field(default_factory=dict)
    scan_status: str = "ok"
    body: str = ""


def parse_frontmatter(text: str) -> tuple[dict, str]:
    """解析 SKILL.md 的 YAML frontmatter (轻量, 无 pyyaml 依赖)。返回 (meta, body)。"""
    if not text.startswith("---"):
        return {}, text
    parts = text.split("---", 2)
    if len(parts) < 3:
        return {}, text
    raw, body = parts[1], parts[2]
    meta: dict = {}
    cur_key: str | None = None
    for line in raw.splitlines():
        if not line.strip():
            continue
        if re.match(r"^\s*-\s+", line) and cur_key:
            meta.setdefault(cur_key, [])
            if isinstance(meta[cur_key], list):
                meta[cur_key].append(line.strip()[2:].strip().strip("'\""))
            continue
        m = re.match(r"^([A-Za-z0-9_\-]+):\s*(.*)$", line)
        if m:
            key, val = m.group(1).strip(), m.group(2).strip().strip("'\"")
            cur_key = key
            meta[key] = val if val else []
    return meta, body.strip()


def _scan_with_bandit(py_files: list[Path]) -> bool:
    """bandit AST 扫描 skill 脚本 (MEDIUM/HIGH→危险)。缺库 raise ImportError 由 static_scan 兜底。"""
    from bandit.core import config as b_config
    from bandit.core import manager as b_manager
    mgr = b_manager.BanditManager(b_config.BanditConfig(), "file")
    mgr.discover_files([str(f) for f in py_files])
    mgr.run_tests()
    return any(i.severity in ("MEDIUM", "HIGH") for i in mgr.get_issue_list())


def _scan_with_detect_secrets(py_files: list[Path]) -> bool:
    """detect-secrets 熵+关键字扫密钥。命中即 True。缺库 raise ImportError。"""
    from detect_secrets.core.scan import scan_file
    for f in py_files:
        if any(True for _ in scan_file(str(f))):
            return True
    return False


def static_scan(text: str, skill_dir: Path | None = None) -> str:
    """返回 'ok'/'warn'。优先 bandit(AST)+detect-secrets(熵) 扫 skill_dir/scripts/**/*.py
    (零误报正文); 任一扫描器缺失或无 scripts 时降级回 _DANGER 正则扫 text (信创可降级, 规则7)。"""
    if skill_dir is not None:
        scripts = skill_dir / "scripts"
        py_files = list(scripts.rglob("*.py")) if scripts.is_dir() else []
        if py_files:
            try:
                return "warn" if (_scan_with_bandit(py_files)
                                  or _scan_with_detect_secrets(py_files)) else "ok"
            except Exception:  # noqa: BLE001 — ImportError/版本漂移 → 降级正则
                pass
    for rx in _DANGER:                       # 降级兜底: 旧 7 条正则扫全文
        if rx.search(text):
            return "warn"
    return "ok"


def scan_gate(scan_status: str, confirm: bool, tier: str | None = None) -> tuple[bool, str]:
    """scan 门 (供应链安全基线): warn 或 tier C 默认拒绝执行, 除非显式 confirm。

    返回 (allowed, reason) ∈ {ok, scan_warn, tier_c}。tier 默认 None 退化原 2 参行为。
    """
    if tier == "C" and not confirm:
        return False, "tier_c"
    if scan_status == "warn" and not confirm:
        return False, "scan_warn"
    return True, "ok"


def read_reference(skill_dir: Path, rel_path: str, max_bytes: int = 8000) -> str | None:
    """读 skill references 资源, 防路径穿越 (须落在 references/ 下)。"""
    base = (skill_dir / "references").resolve()
    target = (skill_dir / rel_path).resolve()
    if not str(target).startswith(str(base) + "/") or not target.is_file():
        return None
    try:
        return target.read_text(encoding="utf-8")[:max_bytes]
    except (OSError, UnicodeDecodeError):
        return None


def list_resources(skill_dir: Path, limit: int = 50) -> list[dict[str, str]]:
    """③资源级 (level=3): 枚举 skill 目录下 scripts/references 资源清单。

    只列文件名 + 相对路径 + 类别, 不读正文 (按需加载, 控制上下文预算)。
    """
    resources: list[dict[str, str]] = []
    if not skill_dir.exists():
        return resources
    for sub, kind in (("scripts", "script"), ("references", "reference"), ("assets", "asset")):
        d = skill_dir / sub
        if not d.exists() or not d.is_dir():
            continue
        for f in sorted(d.rglob("*")):
            if f.is_file():
                resources.append({
                    "kind": kind,
                    "name": f.name,
                    "path": str(f.relative_to(skill_dir)),
                })
                if len(resources) >= limit:
                    return resources
    return resources


def infer_capability(name: str, description: str, allowed_tools: list[str]) -> dict:
    """从 description + 工具声明推断 Capability Manifest → 交 UMA 选模 (模型无关)。"""
    blob = f"{name} {description}".lower()
    cap = {
        "reasoning_high": any(k in blob for k in ("analyze", "reason", "review", "audit", "plan", "推理", "分析")),
        "chinese_native": bool(re.search(r"[一-鿿]", f"{name} {description}")),
        "code": any(k in blob for k in ("code", "build", "test", "refactor", "lint", "代码", "编译")),
        "tool_calling": bool(allowed_tools),
        "vision": any(k in blob for k in ("image", "vision", "screenshot", "视觉", "图像")),
    }
    return cap


def classify_tier(skill_dir: Path, scan_status: str = "ok",
                  allowed_tools: list[str] | None = None) -> str:
    """A 纯 SKILL.md 且 scan ok 且无越权工具; B 含厂商专有 (hooks/openai.yaml);
    C 深耦合越权: scan warn 或声明了注册表之外的工具。C 优先级最高 (安全保守)。"""
    from .tools import is_registered   # 局部 import 避免循环 (tools 不 import loader)
    overreach = [t for t in (allowed_tools or []) if not is_registered(t)]
    if scan_status == "warn" or overreach:
        return "C"
    if (skill_dir / "openai.yaml").exists() or (skill_dir / "hooks").exists():
        return "B"
    return "A"


def _parse_one(source: str, skill_md: Path) -> SkillDescriptor | None:
    try:
        text = skill_md.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    meta, body = parse_frontmatter(text)
    name = str(meta.get("name") or skill_md.parent.name)
    desc = str(meta.get("description") or "")
    allowed = meta.get("allowed-tools") or meta.get("allowed_tools") or []
    if isinstance(allowed, str):
        allowed = [t.strip() for t in allowed.split(",") if t.strip()]
    disable = str(meta.get("disable-model-invocation", "")).lower() in ("true", "1", "yes")
    scan = static_scan(text, skill_md.parent)        # 优先扫 scripts/*.py, 缺库降级正则
    return SkillDescriptor(
        name=name,
        description=desc,
        source=source,
        origin_path=str(skill_md),
        version=str(meta.get("version", "0")),
        allowed_tools=list(allowed),
        auto_invoke=not disable,
        tier=classify_tier(skill_md.parent, scan_status=scan, allowed_tools=list(allowed)),
        capability=infer_capability(name, desc, list(allowed)),
        scan_status=scan,
        body=body,
    )


def discover(roots: list[tuple[str, Path]] | None = None, limit: int = 40) -> list[SkillDescriptor]:
    """递归扫描各来源, 命中 SKILL.md 即登记 (§8.5.4 发现规则)。"""
    roots = roots or DEFAULT_ROOTS
    found: list[SkillDescriptor] = []
    for source, root in roots:
        if not root.exists():
            continue
        for skill_md in sorted(root.rglob("SKILL.md")):
            d = _parse_one(source, skill_md)
            if d:
                found.append(d)
            if len(found) >= limit:
                return found
    return found
