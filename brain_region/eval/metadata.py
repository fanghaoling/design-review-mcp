"""run metadata 的 sha 计算（可追溯）。

知识/reviewer/默认值/rubric 一改 → 结果变 → hash 变 → ledger 能告诉你"为什么变了"。
不调模型、只读文件/provider。对齐 GPT Strong Rec 2/5。
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path


def _sha(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()[:16]


def knowledge_hash(provider) -> str:
    """loaded knowledge cases 的 hash：sorted (id, title)。"""
    try:
        cases = provider.list_cases() if provider is not None else []
    except Exception:  # noqa: BLE001
        cases = []
    items = sorted((str(getattr(c, "id", "")), str(getattr(c, "title", ""))) for c in cases)
    return _sha(json.dumps(items, ensure_ascii=False))


def defaults_hash(dd: dict) -> str:
    """resolved defaults 的 hash（config/env/override 合并后的扁平 dict）。"""
    return _sha(json.dumps(dd or {}, sort_keys=True, ensure_ascii=False, default=str))


def rubric_hash(rubric_text: str) -> str:
    """rubric 文件内容的 hash（不是版本字符串，防偷偷改 v1）。"""
    return _sha(rubric_text or "")


def reviewer_hash(adapter, dimensions: list[str]) -> str:
    """reviewer prompt 的 hash：adapter.reviewers_dir() 下 *.yaml 内容 + sorted dimensions。

    best-effort：reviewer 目录不存在/为空 → 返回 dims 自身 hash（仍能捕捉 dimensions 变化）。
    """
    dims = json.dumps(sorted(dimensions or []), ensure_ascii=False)
    files_blob = ""
    try:
        rdir = Path(str(adapter.reviewers_dir())) if adapter is not None else None
        if rdir and rdir.exists():
            blobs = []
            for p in sorted(rdir.glob("*.yaml")):
                blobs.append(f"{p.name}:{p.read_text(encoding='utf-8')}")
            files_blob = "\n".join(blobs)
    except Exception:  # noqa: BLE001
        files_blob = ""
    return _sha(dims + "\n" + files_blob)


def git_sha() -> str:
    """当前 git sha（拿不到则 'unknown'）。eval 不强依赖 git。"""
    import subprocess

    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, timeout=5,
        )
        if out.returncode == 0:
            return out.stdout.strip() or "unknown"
    except Exception:  # noqa: BLE001
        pass
    return "unknown"
