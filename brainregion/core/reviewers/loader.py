"""reviewer 角色 yaml 加载。

仿 asset-generator-mcp 的 style preset 加载：inherits（子继承父，列表字段追加去重、标量覆盖）
+ 循环继承检测。新增角色 = 加 yaml，零代码。

reviewer yaml 结构：
    name: safety
    inherits: base                 # 可选，递归合并
    system_prompt: |               # 该角色立场（独立）
      ...
    temperature: 0.5               # 独立采样参数
    top_p: 0.95
    max_tokens: 4096
    focus_checklist:               # 该维度关注点
      - "..."
"""
from __future__ import annotations

import logging
from pathlib import Path

import yaml

logger = logging.getLogger("brainregion.reviewers")

# 合并语义为「列表」的字段（子追加到父后去重保序）；其余标量子覆盖父。
_LIST_FIELDS = {"focus_checklist"}


def _merge(base: dict, child: dict) -> dict:
    merged = dict(base)
    for k, v in child.items():
        if k in _LIST_FIELDS and isinstance(v, list):
            seen: set = set()
            combined: list = []
            for x in (merged.get(k) or []) + v:
                key = x if isinstance(x, (str, int)) else None
                if key is not None:
                    if key in seen:
                        continue
                    seen.add(key)
                combined.append(x)
            merged[k] = combined
        else:
            merged[k] = v
    return merged


def load_reviewer(
    name: str,
    reviewers_dir: str | Path,
    fallback_dir: str | Path | None = None,
    _seen: set[str] | None = None,
) -> dict:
    """加载 reviewer yaml，递归解析 inherits（子覆盖父，列表追加去重）。循环继承抛错。

    fallback_dir 用于 adapter reviewer 继承 core 通用基（如 unity/ecs_perf inherits base，
    base 在 core）：当前目录找不到 name 或其 parent 时回退 fallback_dir。
    """
    if _seen is None:
        _seen = set()
    if name in _seen:
        raise ValueError(f"reviewer 循环继承: {' -> '.join(list(_seen) + [name])}")
    _seen = _seen | {name}
    d = Path(reviewers_dir)
    path = d / f"{name}.yaml"
    if not path.exists() and fallback_dir is not None:
        d = Path(fallback_dir)
        path = d / f"{name}.yaml"
    if not path.exists():
        available = sorted(p.stem for p in Path(reviewers_dir).glob("*.yaml"))
        if fallback_dir:
            available += sorted(p.stem for p in Path(fallback_dir).glob("*.yaml"))
        raise ValueError(f"未知 reviewer: {name}，可用: {sorted(set(available))}")
    cfg = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    parent = cfg.pop("inherits", None)
    if parent:
        pdir = d if (d / f"{parent}.yaml").exists() else (Path(fallback_dir) if fallback_dir else d)
        base = load_reviewer(parent, pdir, fallback_dir, _seen)
        child_sp = cfg.get("system_prompt")
        base_sp = base.get("system_prompt")
        cfg = _merge(base, cfg)
        # system_prompt 特殊合并：base 铁律在前 + 子特化在后。
        # 不让子覆盖丢失 base 的 evidence 强制 / 不硬套教程 / JSON 要求等铁律。
        if child_sp and base_sp and child_sp != base_sp:
            cfg["system_prompt"] = base_sp.rstrip() + "\n\n" + child_sp.rstrip()
    cfg.setdefault("name", name)
    cfg.setdefault("system_prompt", "")
    cfg.setdefault("focus_checklist", [])
    cfg.setdefault("temperature", 0.3)
    cfg.setdefault("top_p", 0.95)
    cfg.setdefault("max_tokens", 4096)
    return cfg


def list_reviewers(reviewers_dir: str | Path) -> list[str]:
    """列出可用 reviewer 名（排除抽象基 base）。"""
    d = Path(reviewers_dir)
    if not d.exists():
        return []
    return sorted(p.stem for p in d.glob("*.yaml") if p.stem != "base")
