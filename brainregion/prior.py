"""模型可信度先验加载（v2.2 warm-start）。

yaml 加载 / cache / 合并 / (r,κ)→(α,β) 转换都是 reliability domain logic，不该塞进 MCP server
（GPT 第三轮 Strong Rec）。server 只调 ``load(config)`` 拿到 ``{(label,dim):(alpha,beta)}``，
不碰 yaml；以后 official-prior-vN.yaml 升级只改本模块，server 不动。

机制：Beta-Binomial 共轭（Raykar 2010 / Efron-Morris 1973）。先验 ``α=r·κ, β=(1-r)·κ``，
与本地 feedback 共轭 ``reliability = (α+score)/(α+β+n)``，n=0→先验均值，n→∞→本地真相。
"""
from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger("brainregion.prior")

_CACHE: dict | None = None  # preset 框架文件不变，模块级缓存（启动加载一次，非每次 review 读）


def _convert(node: dict) -> dict:
    """``{label:{dim:{r,kappa}}}`` → ``{(label,dim):(alpha,beta)}``，``α=r·κ β=(1-r)·κ``。

    非法条目（r/kappa 缺失或非数值、kappa≤0）静默跳过（best-effort，不抛错）。
    """
    out: dict = {}
    for label, dims in (node or {}).items():
        if not isinstance(dims, dict):
            continue
        for dim, rk in dims.items():
            if not isinstance(rk, dict):
                continue
            try:
                r = float(rk.get("r", 0.5))
                k = float(rk.get("kappa", 10))
            except (TypeError, ValueError):
                continue
            if k > 0:
                out[(label, dim)] = (r * k, (1.0 - r) * k)
    return out


def _builtin() -> dict:
    """读框架 preset（``presets/model_reliability_prior.yaml``），模块级缓存。"""
    global _CACHE
    if _CACHE is None:
        _CACHE = {}
        p = Path(__file__).resolve().parent / "presets" / "model_reliability_prior.yaml"
        if p.exists():
            try:
                import yaml

                data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
                _CACHE = _convert(data.get("priors") or {})
            except Exception as e:  # noqa: BLE001 — preset 损坏不阻断 review，降级空先验
                logger.warning("preset reliability 先验加载失败（降级空先验）: %s", e)
                _CACHE = {}
    return _CACHE


def load(config: dict | None) -> dict:
    """按 config 合成先验 ``{(label,dim):(alpha,beta)}``。

    config = ``{mode: none|builtin|custom, custom: {label:{dim:{r,kappa}}}}``：
    - ``none``：完全禁用（返回 {}，等同 v2.1）
    - ``builtin``（默认）：读框架 preset（今天空 = v2.1；official 填入后自动生效）
    - ``custom``：builtin + 用户 custom 覆盖/扩展（即时自填价值）
    """
    cfg = config or {}
    mode = cfg.get("mode", "builtin")
    if mode == "none":
        return {}
    prior: dict = {}
    if mode in ("builtin", "custom"):
        prior.update(_builtin())
    if mode == "custom":
        prior.update(_convert(cfg.get("custom")))
    return prior
