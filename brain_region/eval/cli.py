"""`brain-region eval` 子命令的编排：加载 fixtures → 解析变体/judge → 建引擎 → run_eval → 导出。

复用 server 工厂（_resolve_adapter/_resolve_endpoints/_normalize_one）+ defaults.apply，
不重造。runner 做实际运行。
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

import yaml

from .. import defaults as _defaults_mod
from ..server import _normalize_one, _resolve_adapter, _resolve_endpoints
from . import store
from .metadata import rubric_hash
from .runner import build_engines, make_run_id, run_eval
from .schema import EvalTask, VariantSpec

logger = logging.getLogger("brain_region.eval.cli")

_DEFAULT_RUBRIC = Path(__file__).parent / "rubrics" / "review_v1.md"


def load_tasks(fixtures_dir: str) -> list[EvalTask]:
    tasks: list[EvalTask] = []
    for p in sorted(Path(fixtures_dir).glob("*.yaml")):
        data = yaml.safe_load(p.read_text(encoding="utf-8"))
        items = data if isinstance(data, list) else [data]
        for d in items:
            if not isinstance(d, dict):
                continue
            tasks.append(EvalTask(
                id=d.get("id", p.stem),
                task_type=d.get("task_type", "review"),
                difficulty=d.get("difficulty", ""),
                input=d.get("input") or {},
                notes=d.get("notes", ""),
                frozen=bool(d.get("frozen", True)),
            ))
    return tasks


def parse_variants(spec: str) -> list[VariantSpec]:
    """'retrieve_off:0,retrieve_on:5,retrieve_garbage:5g' → VariantSpec 列表。

    每项 'name:k' 或 'name:kg'(g=garbage 负对照) 或 'name:k:g'。
    """
    out: list[VariantSpec] = []
    for part in (spec or "").split(","):
        part = part.strip()
        if not part:
            continue
        bits = part.split(":")
        name = bits[0]
        k_tok = bits[1] if len(bits) > 1 else "5"
        garbage = False
        if k_tok.endswith("g"):
            garbage = True
            k_tok = k_tok[:-1]
        if len(bits) > 2 and bits[2] == "g":
            garbage = True
        try:
            k = int(k_tok)
        except ValueError:
            k = 5
        out.append(VariantSpec(name=name, retrieve_top_k=k, garbage=garbage))
    return out


def parse_judges(judges: list[str] | None, endpoint_ids: set, endpoints_cfg) -> list[dict]:
    specs = judges or []
    return [_normalize_one(s, endpoint_ids, endpoints_cfg) for s in specs]


async def run(args) -> dict:
    dd = _defaults_mod.apply()
    root = os.environ.get("UNITY_PROJECT_ROOT", ".")
    adapter = _resolve_adapter(args.adapter, root)

    variants = parse_variants(args.variants)
    if not variants:
        variants = [VariantSpec("retrieve_off", 0), VariantSpec("retrieve_on", 5),
                    VariantSpec("retrieve_garbage", 5, garbage=True)]

    endpoints_cfg = dd.get("endpoints") or {}
    endpoint_ids = set((_resolve_endpoints(endpoints_cfg) or {}).keys())
    judge_specs = args.judges or [dd.get("normalizer_model", "claude-opus-4-8")]
    judge_entries = parse_judges(judge_specs, endpoint_ids, endpoints_cfg)

    engines, backend = build_engines(adapter, dd, variants)

    rubric_path = Path(args.rubric) if getattr(args, "rubric", None) else _DEFAULT_RUBRIC
    rubric_text = rubric_path.read_text(encoding="utf-8") if rubric_path.exists() else ""
    rhash = rubric_hash(rubric_text)

    tasks = load_tasks(args.fixtures_dir)
    if not tasks:
        raise SystemExit(f"fixtures 目录无 *.yaml 任务: {args.fixtures_dir}")

    run_id = make_run_id()
    _, _, entry = await run_eval(
        tasks, variants, judge_entries, backend, engines, dd, adapter,
        rubric_text, rhash, run_id,
        effort=args.effort, max_cost_usd=args.max_cost_usd if args.max_cost_usd is not None else 1.0,
        panel_override=getattr(args, "panel", None),
    )

    exported = None
    if getattr(args, "export", None):
        exported = store.export_jsonl(run_id, args.export)

    return {
        "run_id": run_id,
        "n_tasks": entry.n_tasks,
        "variants": entry.variants,
        "judge_models": entry.judge_models,
        "metadata": {
            "git_sha": entry.git_sha, "rubric_hash": entry.rubric_hash,
            "knowledge_hash": entry.knowledge_hash, "reviewer_hash": entry.reviewer_hash,
            "defaults_hash": entry.defaults_hash,
        },
        "summary": entry.summary,
        "exported_jsonl": exported,
    }
