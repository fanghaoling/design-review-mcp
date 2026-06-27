"""Design Review MCP server — 多模型对抗设计审查框架。

工具：
  审查：review_document / review_plan / review_code
  自省：list_adapters / list_reviewers / list_knowledge / list_defaults / panel_stats
  健康：ping

设计要点：
- adapter="auto" 检测 Packages/manifest.json → UnityAdapter，否则 GenericAdapter。
- review_document 内部：先 retrieve 算缓存 key → 命中返回 → 未命中跑 8-Stage pipeline → record。
- 同步工具包 asyncio.run(engine.review)（engine 是 async，ReviewStage/NormalizeStage 内 gather/await）。
- 照搬 asset-gen：FastMCP + dict 返回 + stderr 日志 + 工具内直接 raise（FastMCP 自动 ToolError→isError）。
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys
from pathlib import Path

from mcp.server.fastmcp import FastMCP

# MCP stdio：stdout 必须干净（只走 JSON-RPC），日志统一写 stderr。
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stderr,
)
logger = logging.getLogger("design_review")

# 加载 .env（若存在）到 os.environ：litellm 据此读 API key。.env 已 gitignore，不进 git。
# 系统环境变量优先（load_dotenv 默认不覆盖已存在的 env）。
from dotenv import load_dotenv  # noqa: E402

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

mcp = FastMCP("design_review")

from . import defaults as _defaults_mod  # noqa: E402
from . import output, reviews_db  # noqa: E402
from .adapters.generic import GenericAdapter  # noqa: E402
from .adapters.unity import UnityAdapter  # noqa: E402
from .core.engine import ReviewEngine  # noqa: E402
from .core.report import CanonicalFinding, Finding, ReviewReport  # noqa: E402
from .core.reviewers.loader import list_reviewers as _list_reviewer_files  # noqa: E402
from .core.stages import CORE_REVIEWERS_DIR, build_default_pipeline  # noqa: E402
from .core import ReviewDocument  # noqa: E402
from .knowledge import YamlKnowledgeProvider  # noqa: E402
from .privacy import build_policy  # noqa: E402
from .providers import LiteLLMBackend  # noqa: E402

_ADAPTERS = {"unity": UnityAdapter, "generic": GenericAdapter}


def _resolve_adapter(name: str, project_root: str):
    if name == "auto":
        if (Path(project_root) / "Packages" / "manifest.json").exists():
            return UnityAdapter(project_root)
        return GenericAdapter(project_root)
    cls = _ADAPTERS.get(name)
    if cls is None:
        raise ValueError(f"未知 adapter: {name}，可用: {sorted(list(_ADAPTERS) + ['auto'])}")
    return cls(project_root)


def _knowledge_dirs(adapter) -> list:
    """framework 通用知识库 + 项目本地 overlay（本地存在才加）。"""
    dirs = [adapter.knowledge_dir()]
    local = getattr(adapter, "local_knowledge_dir", lambda: None)()
    if local and Path(str(local)).exists():
        dirs.append(local)
    return dirs


def _resolve_endpoints(cfg: dict) -> dict:
    """config endpoints 块 -> {id: EndpointConfig{provider, base_url, api_key, headers, timeout}}。

    api_key_env 优先（os.environ.get），fallback api_key 明文，都无 raise 清晰配置错误。
    credential **只在此处解析**并交给 backend 持有，不进 PipelineContext。
    """
    registry: dict = {}
    for eid, ep in (cfg or {}).items():
        if not isinstance(ep, dict):
            raise ValueError(f"endpoint {eid!r} 配置必须是对象")
        provider = ep.get("provider")
        if provider not in ("openai", "anthropic"):
            raise ValueError(
                f"endpoint {eid!r} provider 必须是 openai|anthropic（中转兼容网关协议），得到 {provider!r}。"
                f"gemini/bedrock/vertex 等原生 provider 请用 litellm model 字符串（如 zai/glm-5.2）走 env，不走 endpoint。"
            )
        base_url = ep.get("base_url")
        if not base_url:
            raise ValueError(f"endpoint {eid!r} 缺 base_url")
        api_key = None
        env_name = ep.get("api_key_env")
        if env_name:
            api_key = os.environ.get(env_name)
            if not api_key:
                raise ValueError(f"endpoint {eid!r} api_key_env={env_name!r} 环境变量未设置或为空")
        elif ep.get("api_key"):
            api_key = ep["api_key"]
        else:
            raise ValueError(f"endpoint {eid!r} 缺 api_key_env 或 api_key")
        registry[eid] = {
            "provider": provider,
            "base_url": base_url,
            "api_key": api_key,
            "headers": ep.get("headers") or {},
            "timeout": ep.get("timeout"),
        }
    return registry


def _normalize_panel(panel: list, endpoint_ids: set) -> list[dict]:
    """panel（list[str|dict]）-> list[PanelEntry{label, model, endpoint_id}]。

    str=官方（endpoint_id=None，走 litellm env）；dict={endpoint, model, label} 引用中转站。
    校验 label 全局唯一（撞名报错——label 是身份标识，撞名会让 consensus 错误合并）。
    PanelEntry **不含 credential**（key 只在 endpoint_registry，backend 边缘解析）。
    """
    entries: list[dict] = []
    labels: set[str] = set()
    for item in panel or []:
        if isinstance(item, dict):
            eid = item.get("endpoint")
            if eid not in endpoint_ids:
                raise ValueError(f"panel 项引用了未定义的 endpoint {eid!r}（config endpoints 里没声明）")
            model = item.get("model")
            if not model:
                raise ValueError(f"panel 项（endpoint={eid}）缺 model")
            label = item.get("label") or f"{eid}/{model}"
        elif isinstance(item, str):
            eid, model, label = None, item, item
        else:
            raise ValueError(f"panel 项必须是 str（官方模型）或 dict（中转引用），得到 {type(item).__name__}")
        if label in labels:
            raise ValueError(f"panel label 撞名：{label!r}（label 是模型身份标识，撞名会让 consensus 错误合并）")
        labels.add(label)
        entries.append({"label": label, "model": model, "endpoint_id": eid})
    return entries


def _normalize_one(spec, endpoint_ids: set) -> dict:
    """单个 model 规格（str|dict）-> PanelEntry。供 normalizer 复用（schema 与 panel 统一）。"""
    return _normalize_panel([spec], endpoint_ids)[0]


def _build_engine(adapter, dd: dict) -> ReviewEngine:
    registry = _resolve_endpoints(dd.get("endpoints") or {})
    endpoint_ids = set(registry.keys())
    backend = LiteLLMBackend(timeout=float(dd.get("timeout", 90)), endpoint_registry=registry)
    knowledge = YamlKnowledgeProvider(_knowledge_dirs(adapter))
    # v1.7 隐私策略：解析 trusted（复用 endpoint）+ build_policy（off→None / strict→StrictPolicy）
    privacy_cfg = dd.get("privacy_policy")
    trusted_entry = (
        _normalize_one(privacy_cfg["trusted"], endpoint_ids)
        if (isinstance(privacy_cfg, dict) and privacy_cfg.get("trusted"))
        else None
    )
    policy = build_policy(privacy_cfg, trusted_entry)
    pipeline = build_default_pipeline(
        normalizer=_normalize_one(dd.get("normalizer_model", "claude-opus-4-8"), endpoint_ids),
        threshold=int(dd.get("consensus_threshold", 2)),
        policy=policy,
    )
    return ReviewEngine(
        adapter=adapter, backend=backend, knowledge=knowledge,
        pipeline=pipeline, defaults=dd, policy=policy,
    )


def _rebuild_report(d: dict) -> ReviewReport:
    """从缓存的 dict 重建 ReviewReport（dataclass 字段过滤，忽略 cache_hit 等额外字段）。"""
    cf_fields = CanonicalFinding.__dataclass_fields__
    f_fields = Finding.__dataclass_fields__

    def _cf(c: dict) -> CanonicalFinding:
        return CanonicalFinding(**{k: v for k, v in c.items() if k in cf_fields})

    return ReviewReport(
        document_type=d.get("document_type", ""),
        adapter=d.get("adapter", ""),
        project_version=d.get("project_version", {}),
        panel=d.get("panel", []),
        failed_models=d.get("failed_models", []),
        retrieved_cases=d.get("retrieved_cases", []),
        consensus=[_cf(c) for c in d.get("consensus", [])],
        majority=[_cf(c) for c in d.get("majority", [])],
        individual={
            k: [Finding(**{kk: vv for kk, vv in f.items() if kk in f_fields})
                for f in v]
            for k, v in d.get("individual", {}).items()
        },
        knowledge_hit=d.get("knowledge_hit", []),
        usage=d.get("usage", {}),
        summary=d.get("summary", ""),
        risk=d.get("risk", {}),
    )


def _common_review_kwargs():
    """review_plan/review_code 共享的显式参数（FastMCP 需显式 schema）。"""
    return dict(
        adapter="auto", panel=None, dimensions=None, retrieve_top_k=5,
        extra_context="", output_format="json",
    )


@mcp.tool()
def ping() -> dict:
    """健康检查：确认 design-review MCP server 可达。"""
    from . import __version__

    return {"ok": True, "name": "design_review", "version": __version__}


@mcp.tool()
async def review_document(
    content: str,
    document_type: str = "markdown",
    files: dict | None = None,
    adapter: str = "auto",
    panel: list[str] | None = None,
    dimensions: list[str] | None = None,
    retrieve_top_k: int = 5,
    extra_context: str = "",
    output_format: str = "json",
    timeout: float | None = None,
    effort: str | None = None,
    max_cost_usd: float | None = None,
) -> dict:
    """审查一份文档（markdown/code/adr/rfc/config）。

    多模型 fan-out（panel × dimensions）+ 知识库 retrieve（版本过滤）+ canonical 归一
    + 校准共识。返回结构化报告（consensus/majority/individual + calibrated_confidence）。

    Args:
        content: 文档正文（markdown/adr/rfc/config）。
        document_type: 文档类型，影响 prompt 模板。
        files: 代码文件 {路径: 源码}（code 模式）。
        adapter: "auto" 自动检测，或 "unity"/"generic"。
        panel: 模型列表，None=默认面板（需配 OPENAI/ANTHROPIC/ARK key）。
        dimensions: 审查维度，None=自动（core planner/safety + adapter 特定）。
        retrieve_top_k: 知识库 retrieve 案例数。
        extra_context: 额外补充 context（核心 context 由 adapter 自动聚合）。
        output_format: json|markdown|sarif。json 返回结构化；其余额外加 rendered 字段。
        timeout: 单模型超时秒。
        effort: 思考强度 low/medium/high/xhigh/max；None=各模型默认。仅 Claude（output_config+thinking adaptive）/ OpenAI o 系列（reasoning_effort）生效，其余丢弃。Claude 默认 high 较贵，routine 方案可降 medium 省 token。
        max_cost_usd: 单次 review 总成本上限（USD）；None=无上限。设了则预 flight 估每 job 成本、按 panel 顺序裁剪直到估算超预算，report.budget.exhausted 标记是否裁过。

    Returns:
        报告 dict + cache_hit/reuse_count（+ rendered 若非 json）。
    """
    dd = _defaults_mod.apply(
        panel=panel, dimensions=dimensions, retrieve_top_k=retrieve_top_k,
        output_format=output_format, timeout=timeout, effort=effort, max_cost_usd=max_cost_usd,
    )
    panel_used = _normalize_panel(dd["panel"], set((dd.get("endpoints") or {}).keys()))
    dims_used = dd["dimensions"]
    root = os.environ.get("UNITY_PROJECT_ROOT", ".")
    ad = _resolve_adapter(adapter, root)
    knowledge = YamlKnowledgeProvider(_knowledge_dirs(ad))
    version = ad.read_version()
    text = content or ""
    if files:
        text += "\n" + "\n".join(files.values())
    retrieved = knowledge.retrieve(text, version, int(dd["retrieve_top_k"]))
    retrieved_ids = [c.id for c in retrieved]

    phash = reviews_db.compute_hash(
        document_content=content, document_files=files, panel=panel_used,
        dimensions=dims_used, adapter=ad.name, project_version=version,
        retrieved_cases_ids=retrieved_ids, extra_context=extra_context,
    )
    cached = reviews_db.lookup(phash)
    if cached is not None:
        result = dict(cached["report"])
        result["cache_hit"] = True
        result["reuse_count"] = cached["reuse_count"]
        if output_format != "json":
            result["rendered"] = output.render(_rebuild_report(cached["report"]), output_format)
        return result

    engine = _build_engine(ad, dd)
    doc = ReviewDocument(type=document_type, content=content or "", files=files)
    # v1.8 context_modes 校验（Fail Fast：用户配置错不该偷偷 fallback）+ 透传
    context_modes = dd.get("context_modes") or {}
    for dim, mode in context_modes.items():
        if mode not in ("full", "compressed", "minimal"):
            raise ValueError(f"context_modes.{dim}={mode!r} 无效（full|compressed|minimal）")
    ctx = await engine.review(
        doc, panel=panel_used, dimensions=dims_used,
        retrieve_top_k=int(dd["retrieve_top_k"]), extra_context=extra_context,
        effort=dd.get("effort"), max_cost_usd=dd.get("max_cost_usd"),
        context_modes=context_modes,
    )
    report = ctx.report
    report_dict = report.to_dict()
    reviews_db.record(phash, report_dict=report_dict, adapter=ad.name, panel=panel_used)
    result = dict(report_dict)
    result["cache_hit"] = False
    if output_format != "json":
        result["rendered"] = output.render(report, output_format)
    return result


@mcp.tool()
async def review_plan(
    plan_text: str,
    adapter: str = "auto",
    panel: list[str] | None = None,
    dimensions: list[str] | None = None,
    retrieve_top_k: int = 5,
    extra_context: str = "",
    output_format: str = "json",
    effort: str | None = None,
    max_cost_usd: float | None = None,
) -> dict:
    """审查实现方案/计划（design-question 模式）。等价 review_document(document_type="markdown")。"""
    return await review_document(
        content=plan_text, document_type="markdown", files=None, adapter=adapter,
        panel=panel, dimensions=dimensions, retrieve_top_k=retrieve_top_k,
        extra_context=extra_context, output_format=output_format,
        effort=effort, max_cost_usd=max_cost_usd,
    )


@mcp.tool()
async def review_code(
    files: dict[str, str],
    adapter: str = "auto",
    panel: list[str] | None = None,
    dimensions: list[str] | None = None,
    retrieve_top_k: int = 5,
    extra_context: str = "",
    output_format: str = "json",
    effort: str | None = None,
    max_cost_usd: float | None = None,
) -> dict:
    """审查代码实现（code-review 模式）。等价 review_document(document_type="code")。"""
    return await review_document(
        content="", document_type="code", files=files, adapter=adapter,
        panel=panel, dimensions=dimensions, retrieve_top_k=retrieve_top_k,
        extra_context=extra_context, output_format=output_format,
        effort=effort, max_cost_usd=max_cost_usd,
    )


@mcp.tool()
def list_adapters() -> dict:
    """列出可用 ProjectAdapter + auto 检测结果。"""
    root = os.environ.get("UNITY_PROJECT_ROOT", ".")
    detected = "unity" if (Path(root) / "Packages" / "manifest.json").exists() else "generic"
    return {
        "adapters": [
            {"name": "unity", "desc": "Unity ECS（entities/netcode/physics）"},
            {"name": "generic", "desc": "通用（无项目特定，用 core 通用 reviewer）"},
        ],
        "auto_detected": detected,
    }


@mcp.tool()
def list_reviewers(adapter: str = "auto") -> dict:
    """列出可用 reviewer 角色（core 通用 + adapter 特定）。"""
    root = os.environ.get("UNITY_PROJECT_ROOT", ".")
    ad = _resolve_adapter(adapter, root)
    core = _list_reviewer_files(CORE_REVIEWERS_DIR)
    specific = _list_reviewer_files(ad.reviewers_dir()) if ad.reviewers_dir().exists() else []
    return {"adapter": ad.name, "core": core, "adapter_specific": specific}


@mcp.tool()
def list_knowledge(adapter: str = "auto") -> dict:
    """列出知识库案例索引（id/title/category/triggers）。"""
    root = os.environ.get("UNITY_PROJECT_ROOT", ".")
    ad = _resolve_adapter(adapter, root)
    knowledge = YamlKnowledgeProvider(_knowledge_dirs(ad))
    return {
        "adapter": ad.name,
        "cases": [
            {"id": c.id, "title": c.title, "category": c.category, "triggers": c.triggers}
            for c in knowledge.list_cases()
        ],
    }


@mcp.tool()
def list_defaults() -> dict:
    """列出三层默认值及来源（builtin/config/env）。"""
    return _defaults_mod.get_all()


@mcp.tool()
def panel_stats() -> dict:
    """缓存统计：审查总数 + 缓存命中省掉的重复审查数。"""
    return reviews_db.stats()


def main() -> None:
    """MCP server 入口（默认 stdio transport）。"""
    from . import __version__

    logger.info("design-review-mcp %s starting (stdio)", __version__)
    mcp.run()


if __name__ == "__main__":
    main()
