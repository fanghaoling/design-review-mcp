"""三层默认（builtin < config.json < env），仿 asset-generator-mcp defaults。

env 变量名：DESIGN_REVIEW_DEFAULT_<KEY>，如 DESIGN_REVIEW_DEFAULT_PANEL。
config 文件：UNITY_PROJECT_ROOT/Assets/Generated/AIGenerated/design_review_config.json。
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path

logger = logging.getLogger("design_review.defaults")

_ENV_PREFIX = "DESIGN_REVIEW_DEFAULT_"
_BUILTINS = {
    # 豆包需用户填 Ark endpoint id 后追加（如 volcengine/ep-...）
    "panel": ["claude-opus-4-8", "gpt-5"],
    "dimensions": [],  # 空 = 自动（core 核心 planner/safety + adapter 全部）
    "temperature": 0.3,
    "max_tokens": 4096,
    "consensus_threshold": 2,
    "retrieve_top_k": 5,
    "timeout": 90,
    "normalizer_model": "claude-opus-4-8",
    "output_format": "json",
    # v1.5 成本/思考强度控制（None=不启用：无成本上限 / 各模型用默认 effort）
    "effort": None,
    "max_cost_usd": None,
    # v1.6 中转站/自定义 endpoint：{id: {provider: openai|anthropic, base_url, api_key_env|api_key, headers?, timeout?}}
    "endpoints": {},
    # v1.7 隐私/脱敏策略（{policy: off|strict, trusted:{endpoint,model,label}, min_coverage}）。None/off=不脱敏
    "privacy_policy": None,
    # v1.8 发散/可行性维度：per-dimension 上下文压缩策略 + 压缩下限
    "context_modes": {},
    "min_compressed_chars": 50,
}


def _config_path() -> Path:
    root = os.environ.get("UNITY_PROJECT_ROOT", ".")
    return Path(root) / "Assets" / "Generated" / "AIGenerated" / "design_review_config.json"


def _load_config() -> dict:
    p = _config_path()
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def _coerce(key: str, val: str):
    if key in ("temperature", "timeout", "max_cost_usd"):
        try:
            return float(val)
        except ValueError:
            return val
    if key in ("max_tokens", "consensus_threshold", "retrieve_top_k", "min_compressed_chars"):
        try:
            return int(val)
        except ValueError:
            return val
    return val


def get_all() -> dict:
    """返回 {key: {value, source}}，source ∈ builtin|config|env。"""
    result = {k: {"value": v, "source": "builtin"} for k, v in _BUILTINS.items()}
    cfg = _load_config()
    for k, v in cfg.items():
        if k in _BUILTINS:
            result[k] = {"value": v, "source": "config"}
    for k in _BUILTINS:
        ev = os.environ.get(f"{_ENV_PREFIX}{k.upper()}")
        if ev is not None:
            result[k] = {"value": _coerce(k, ev), "source": "env"}
    return result


def apply(**overrides) -> dict:
    """合并：builtin < config < env < 显式 override（非 None 才覆盖）。"""
    merged = {k: v["value"] for k, v in get_all().items()}
    for k, v in overrides.items():
        if v is not None and k in merged:
            merged[k] = v
    return merged
