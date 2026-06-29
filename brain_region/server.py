"""BrainRegion MCP server — AI collaboration infrastructure.

工具：
  审查：review_document / review_plan / review_code
  会诊：consult_problem / list_consultants / mark_advice
  自省：list_adapters / list_reviewers / list_knowledge / list_defaults / panel_stats
  健康：ping

设计要点：
- adapter="auto" 检测 Packages/manifest.json → UnityAdapter，否则 GenericAdapter。
- review_document 内部：先 retrieve 算缓存 key → 命中返回 → 未命中跑 8-Stage pipeline → record。
- 同步工具包 asyncio.run(engine.review)（engine 是 async，ReviewStage/NormalizeStage 内 gather/await）。
- 照搬 asset-gen：FastMCP + dict 返回 + stderr 日志 + 工具内直接 raise（FastMCP 自动 ToolError→isError）。
"""
from __future__ import annotations

import logging
import os
import re
import sys
from pathlib import Path

from mcp.server.fastmcp import FastMCP

# MCP stdio：stdout 必须干净（只走 JSON-RPC），日志统一写 stderr。
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stderr,
)
logger = logging.getLogger("brain_region")

# 加载 .env（若存在）到 os.environ：litellm 据此读 API key。.env 已 gitignore，不进 git。
# 系统环境变量优先（load_dotenv 默认不覆盖已存在的 env）。
from dotenv import load_dotenv  # noqa: E402

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

mcp = FastMCP("brain_region")

from . import defaults as _defaults_mod  # noqa: E402
from . import output, prior as prior_mod, reviews_db  # noqa: E402
from .adapters.generic import GenericAdapter  # noqa: E402
from .adapters.unity import UnityAdapter  # noqa: E402
from .core.consult import ConsultEngine, ConsultRequest  # noqa: E402
from .core.consultants import CONSULTANTS_DIR, list_consultants as _list_consultant_files  # noqa: E402
from .core.engine import ReviewEngine  # noqa: E402
from .core.planner import PlanRequest, PlannerEngine  # noqa: E402
from .core.regions import REGIONS_DIR, load_regions as _load_regions  # noqa: E402
from .core.regions import route_regions as _route_regions  # noqa: E402
from .core.report import CanonicalFinding, Finding, ReviewReport  # noqa: E402
from .core.reviewers.loader import list_reviewers as _list_reviewer_files  # noqa: E402
from .core.workflow import suggest_workflow as _suggest_workflow  # noqa: E402
from .core.stages import CORE_REVIEWERS_DIR, build_default_pipeline  # noqa: E402
from .core import ReviewDocument  # noqa: E402
from .knowledge import YamlKnowledgeProvider  # noqa: E402
from .privacy import build_policy  # noqa: E402
from .providers import LiteLLMBackend  # noqa: E402

_ADAPTERS = {"unity": UnityAdapter, "generic": GenericAdapter}

_CONSULT_MODE_CONSULTANTS = {
    "debugging": ["debugger"],
    "architecture": ["architect", "critic"],
    "performance": ["performance", "critic"],
    "simplicity": ["simplicity", "maintenance"],
    "game_design": ["game_design", "critic"],
    "challenge": ["challenge", "critic"],
    "planning": ["architect", "test_designer", "critic"],
}


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
    if hasattr(adapter, "local_knowledge_dirs"):
        candidates = adapter.local_knowledge_dirs()
    else:
        local = getattr(adapter, "local_knowledge_dir", lambda: None)()
        candidates = [local] if local else []
    for local in candidates:
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


def _normalize_panel(
    panel: list, endpoint_ids: set, endpoints_cfg: dict | None = None
) -> list[dict]:
    """panel（list[str|dict]）-> list[PanelEntry{label, model, endpoint_id}]。

    str 四态（v2.3 加通配/短引用/全展开，向后兼容 litellm 原生）：
    - == "endpoints" → 通配：展开所有 endpoints.<id>.models（一行引全部中转站，厂商/模型都不用逐个填）
    - == endpoint_id → 全展开该 endpoint 的 models
    - endpoint_id/model → 短引用（label=id/model）
    - 否则 → litellm 原生官方（endpoint_id=None，走 env）
    dict={endpoint, model, label} 引用中转站（v1.6，自定义 label）。
    校验 label 全局唯一（撞名报错——label 是身份标识，撞名会让 consensus 错误合并）。
    PanelEntry **不含 credential**（key 只在 endpoint_registry，backend 边缘解析）。
    """
    entries: list[dict] = []
    labels: set[str] = set()
    for item in panel or []:
        if isinstance(item, dict):
            specs = [_dict_spec(item, endpoint_ids)]
        elif isinstance(item, str):
            specs = _str_specs(item, endpoint_ids, endpoints_cfg)
        else:
            raise ValueError(
                f"panel 项必须是 str（官方模型/短引用/全展开）或 dict（中转引用），得到 {type(item).__name__}"
            )
        for eid, model, label in specs:
            if label in labels:
                raise ValueError(f"panel label 撞名：{label!r}（label 是模型身份标识，撞名会让 consensus 错误合并）")
            labels.add(label)
            entries.append({"label": label, "model": model, "endpoint_id": eid})
    return entries


def _str_specs(item: str, endpoint_ids: set, endpoints_cfg: dict | None) -> list[tuple]:
    """str panel 项 → [(endpoint_id, model, label)]（通配/全展开返回多个）。

    - item == "endpoints" → 通配：展开所有 endpoints.<id>.models（一行引全部中转站模型，v2.3）
    - item == endpoint_id → 全展开该 endpoint 的 models
    - item = endpoint_id/model → 短引用（label=item）
    - 否则 → litellm 原生官方（endpoint_id=None）
    """
    # 通配：str == "endpoints" → 展开所有 endpoints 的所有 models（各 endpoint 须声明 models）
    if item == "endpoints":
        all_models: list[tuple] = []
        for eid, ep in (endpoints_cfg or {}).items():
            for m in (ep.get("models") or []):
                all_models.append((eid, m, f"{eid}/{m}"))
        if not all_models:
            raise ValueError(
                "panel 'endpoints' 通配但无任何 endpoints.<id>.models 声明（每个中转站须声明 models）"
            )
        return all_models
    # 全展开：str 本身是 endpoint_id
    if item in endpoint_ids:
        models = ((endpoints_cfg or {}).get(item) or {}).get("models") or []
        if not models:
            raise ValueError(
                f"panel {item!r} 是 endpoint_id 但 endpoints.{item}.models 未声明（全展开需 models 列表）"
            )
        return [(item, m, f"{item}/{m}") for m in models]
    # 短引用：endpoint_id/model
    if "/" in item:
        prefix, _, model = item.partition("/")
        if prefix in endpoint_ids:
            return [(prefix, model, item)]  # label = id/model
    # litellm 原生官方
    return [(None, item, item)]


def _dict_spec(item: dict, endpoint_ids: set) -> tuple:
    """dict panel 项 → (endpoint_id, model, label)。引用中转站（v1.6，自定义 label）。"""
    eid = item.get("endpoint")
    if eid not in endpoint_ids:
        raise ValueError(f"panel 项引用了未定义的 endpoint {eid!r}（config endpoints 里没声明）")
    model = item.get("model")
    if not model:
        raise ValueError(f"panel 项（endpoint={eid}）缺 model")
    label = item.get("label") or f"{eid}/{model}"
    return (eid, model, label)


def _normalize_one(spec, endpoint_ids: set, endpoints_cfg: dict | None = None) -> dict:
    """单个 model 规格（str|dict）-> PanelEntry。供 normalizer 复用（schema 与 panel 统一）。"""
    return _normalize_panel([spec], endpoint_ids, endpoints_cfg)[0]


def _official_credential_hint(model: str) -> str:
    """Best-effort hint for bare LiteLLM model strings."""
    m = str(model or "").lower()
    if m.startswith("claude-") or m.startswith("anthropic/"):
        return "ANTHROPIC_API_KEY"
    if m.startswith(("gpt-", "o1", "o3", "o4", "openai/")):
        return "OPENAI_API_KEY"
    if m.startswith("zai/"):
        return "ZAI_API_KEY"
    if m.startswith("deepseek/"):
        return "DEEPSEEK_API_KEY"
    if m.startswith("gemini/"):
        return "GEMINI_API_KEY"
    return "provider-specific environment variable"


def _endpoint_key_status(ep: dict) -> str:
    env_name = ep.get("api_key_env")
    if env_name:
        return "set" if os.environ.get(str(env_name)) else "missing"
    if ep.get("api_key"):
        return "plaintext_configured"
    return "missing"


def _configured_endpoint_models(endpoints_cfg: dict) -> list[dict]:
    endpoints: list[dict] = []
    for eid, ep in sorted((endpoints_cfg or {}).items()):
        models = [str(model) for model in (ep.get("models") or [])]
        endpoints.append(
            {
                "id": eid,
                "provider": ep.get("provider"),
                "base_url": ep.get("base_url"),
                "api_key_env": ep.get("api_key_env") or "",
                "api_key_status": _endpoint_key_status(ep),
                "models": models,
                "model_refs": [f"{eid}/{model}" for model in models],
            }
        )
    return endpoints


def _describe_model_routes(panel: list | None, defaults: dict, *, panel_source: str = "explicit") -> dict:
    """Describe how model specs resolve without touching credentials or calling models."""
    endpoints_cfg = defaults.get("endpoints") or {}
    raw_panel = list(panel if panel is not None else defaults.get("panel") or [])
    endpoint_ids = set(endpoints_cfg.keys())
    resolved = _normalize_panel(raw_panel, endpoint_ids, endpoints_cfg)
    endpoints = _configured_endpoint_models(endpoints_cfg)

    endpoint_model_refs: dict[str, list[str]] = {}
    for endpoint in endpoints:
        for model in endpoint["models"]:
            endpoint_model_refs.setdefault(model, []).append(f"{endpoint['id']}/{model}")

    routes: list[dict] = []
    bare_models = set()
    for entry in resolved:
        endpoint_id = entry.get("endpoint_id")
        model = entry["model"]
        if endpoint_id is None:
            bare_models.add(model)
            routes.append(
                {
                    "label": entry["label"],
                    "model": model,
                    "endpoint_id": None,
                    "route_type": "official_litellm",
                    "credential_hint": _official_credential_hint(model),
                    "note": "Bare model strings bypass configured endpoints. Use endpoint_id/model to route through a gateway.",
                }
            )
            continue
        ep = endpoints_cfg.get(endpoint_id) or {}
        routes.append(
            {
                "label": entry["label"],
                "model": model,
                "endpoint_id": endpoint_id,
                "route_type": "configured_endpoint",
                "provider": ep.get("provider"),
                "base_url": ep.get("base_url"),
                "api_key_env": ep.get("api_key_env") or "",
                "api_key_status": _endpoint_key_status(ep),
            }
        )

    ambiguous_models: list[dict] = []
    for model, refs in sorted(endpoint_model_refs.items()):
        reasons: list[str] = []
        if len(refs) > 1:
            reasons.append("declared_under_multiple_endpoints")
        if model in bare_models:
            reasons.append("bare_model_string_also_used")
        if reasons:
            ambiguous_models.append(
                {
                    "model": model,
                    "endpoint_refs": refs,
                    "official_ref": model if model in bare_models else "",
                    "reasons": reasons,
                }
            )

    return {
        "panel_source": panel_source,
        "panel": raw_panel,
        "resolved_panel": routes,
        "endpoints": endpoints,
        "available_model_refs": sorted(
            ref for refs in endpoint_model_refs.values() for ref in refs
        ),
        "ambiguous_models": ambiguous_models,
        "notes": [
            "A bare model string such as 'claude-opus-4-8' uses the official LiteLLM provider route.",
            "Use 'endpoint_id/model' such as 'modelbridge_anthropic/claude-opus-4-8' to use a configured gateway key.",
        ],
    }


def _build_engine(adapter, dd: dict) -> ReviewEngine:
    registry = _resolve_endpoints(dd.get("endpoints") or {})
    endpoint_ids = set(registry.keys())
    backend = LiteLLMBackend(timeout=float(dd.get("timeout", 90)), endpoint_registry=registry)
    knowledge = YamlKnowledgeProvider(_knowledge_dirs(adapter))
    # v1.7 隐私策略：解析 trusted（复用 endpoint）+ build_policy（off→None / strict→StrictPolicy）
    privacy_cfg = dd.get("privacy_policy")
    trusted_entry = (
        _normalize_one(privacy_cfg["trusted"], endpoint_ids, dd.get("endpoints"))
        if (isinstance(privacy_cfg, dict) and privacy_cfg.get("trusted"))
        else None
    )
    policy = build_policy(privacy_cfg, trusted_entry)
    pipeline = build_default_pipeline(
        normalizer=_normalize_one(dd.get("normalizer_model", "claude-opus-4-8"), endpoint_ids, dd.get("endpoints")),
        threshold=int(dd.get("consensus_threshold", 2)),
        policy=policy,
    )
    return ReviewEngine(
        adapter=adapter, backend=backend, knowledge=knowledge,
        pipeline=pipeline, defaults=dd, policy=policy,
    )


def _build_consult_engine(dd: dict) -> ConsultEngine:
    registry = _resolve_endpoints(dd.get("endpoints") or {})
    backend = LiteLLMBackend(timeout=float(dd.get("timeout", 90)), endpoint_registry=registry)
    return ConsultEngine(backend=backend, consultants_dir=CONSULTANTS_DIR)


def _build_planner_engine(dd: dict) -> PlannerEngine:
    registry = _resolve_endpoints(dd.get("endpoints") or {})
    backend = LiteLLMBackend(timeout=float(dd.get("timeout", 90)), endpoint_registry=registry)
    return PlannerEngine(backend=backend)


def _resolve_consultants(consultants: list[str] | None, mode: str | None, defaults: dict) -> tuple[list[str], str | None]:
    """Resolve consultant roles. Explicit consultants win; mode picks a preset."""
    effective_mode = mode if mode is not None else defaults.get("consult_mode")
    if consultants is not None:
        return list(consultants), effective_mode
    if effective_mode:
        preset = _CONSULT_MODE_CONSULTANTS.get(effective_mode)
        if preset is None:
            raise ValueError(f"未知 consult mode: {effective_mode!r}，可用: {sorted(_CONSULT_MODE_CONSULTANTS)}")
        return list(preset), effective_mode
    return list(defaults.get("consult_consultants") or []), None


def _resolve_consult_panel(panel: list[str] | None, defaults: dict) -> tuple[list, str]:
    """Resolve consult panel and track the source for debugging/testing."""
    if panel is not None:
        return panel, "explicit"
    if defaults.get("consult_panel"):
        return defaults.get("consult_panel") or [], "consult_panel"
    return defaults.get("panel") or [], "panel"


def _resolve_planner_panel(panel: list[str] | None, defaults: dict) -> tuple[list, str]:
    """Resolve planner panel without making planning depend on the full review panel by default."""
    if panel is not None:
        return panel, "explicit"
    if defaults.get("planner_panel"):
        return defaults.get("planner_panel") or [], "planner_panel"
    if defaults.get("consult_panel"):
        return defaults.get("consult_panel") or [], "consult_panel"
    return defaults.get("panel") or [], "panel"


def _resolve_consultants_with_source(
    consultants: list[str] | None, mode: str | None, defaults: dict
) -> tuple[list[str], str | None, str, str]:
    """Resolve consultant roles and source labels for routing metadata."""
    effective_mode = mode if mode is not None else defaults.get("consult_mode")
    mode_source = "explicit" if mode is not None else ("consult_mode" if defaults.get("consult_mode") else "none")
    if consultants is not None:
        return list(consultants), effective_mode, "explicit", mode_source
    if effective_mode:
        resolved, mode_used = _resolve_consultants(None, effective_mode, defaults)
        return resolved, mode_used, "mode", mode_source
    return list(defaults.get("consult_consultants") or []), None, "consult_consultants", mode_source


def _rebuild_report(d: dict) -> ReviewReport:
    """从缓存的 dict 重建 ReviewReport（dataclass 字段过滤，忽略 cache_hit 等额外字段）。"""
    cf_fields = CanonicalFinding.__dataclass_fields__
    f_fields = Finding.__dataclass_fields__

    def _cf(c: dict) -> CanonicalFinding:
        return CanonicalFinding(**{k: v for k, v in c.items() if k in cf_fields})

    def _f(f: dict, fallback_id: str) -> Finding:
        kw = {k: v for k, v in f.items() if k in f_fields}
        if not kw.get("id"):  # v2 旧缓存 finding 无 id → 就地补填（让旧 review 也能被 mark_finding）
            kw["id"] = fallback_id
        return Finding(**kw)

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
            k: [_f(f, f"{k}-{idx}") for idx, f in enumerate(v)]
            for k, v in d.get("individual", {}).items()
        },
        knowledge_hit=d.get("knowledge_hit", []),
        usage=d.get("usage", {}),
        summary=d.get("summary", ""),
        risk=d.get("risk", {}),
        privacy=d.get("privacy", {}),  # v2 修 bug：缓存命中补回（原 :197 risk= 后停漏了）
        context_compression=d.get("context_compression", {}),
    )


def _common_review_kwargs():
    """review_plan/review_code 共享的显式参数（FastMCP 需显式 schema）。"""
    return dict(
        adapter="auto", panel=None, dimensions=None, retrieve_top_k=5,
        extra_context="", output_format="json",
    )


@mcp.tool()
def ping() -> dict:
    """健康检查：确认 BrainRegion MCP server 可达。"""
    from . import __version__

    return {"ok": True, "name": "brain_region", "legacy_name": "design_review", "version": __version__}


# v2 Review Memory：标记 finding 采纳，写入 reliability 飞轮
_FINDING_ID_RE = re.compile(r"^.+-\d+$")


def _label_from_id(finding_id: str) -> str:
    """从 finding_id '{label}-{seq}' 解析 label（rsplit 仅 fallback；主路径查 report）。
    label 可含 '-'/'/'（如 "智谱-Anthropic端点"、"zai/glm-5.2"）——rsplit('-',1) 只切末尾 seq。"""
    return finding_id.rsplit("-", 1)[0] if "-" in finding_id else finding_id


@mcp.tool()
def mark_finding(
    finding_id: str,
    decision: str,
    params_hash: str | None = None,
    note: str = "",
    invalidate_cache: bool = True,
) -> dict:
    """标记一条 finding 的采纳情况，写入 Review Memory，供下次 review 模型可信度加权。

    finding_id/params_hash 从 review_document 返回取。未传 params_hash 时按 finding_id
    反查最近含此 id 的 review（扫 consensus+majority+individual+deduped_ids）。
    decision: accepted|rejected|partial。标记后默认失效该 review 缓存，下次同内容审查重算
    reliability（该模型该维度按历史采纳率降/升权）。note 是 decision reason 自由文本。
    """
    if not finding_id or not _FINDING_ID_RE.match(finding_id):
        raise ValueError(f"finding_id 格式无效（应为 '{{label}}-{{seq}}'）: {finding_id!r}")
    if decision not in reviews_db.VALID_DECISIONS:
        raise ValueError(f"decision 必须是 {sorted(reviews_db.VALID_DECISIONS)}，得到 {decision!r}")
    try:
        if params_hash is not None:
            phash = params_hash
            report = reviews_db.lookup_report(phash)
            if report is None:
                raise ValueError(f"params_hash={phash[:8]}… 找不到缓存 review")
            scanned = reviews_db._scan_report_for_finding(report, finding_id)
            if scanned is None:
                raise ValueError(f"review {phash[:8]}… 中找不到 finding_id={finding_id!r}")
            label, dimension = scanned
        else:
            phash, label, dimension = reviews_db.lookup_review_by_finding(finding_id)
            if phash is None:
                raise ValueError(f"找不到含 finding_id={finding_id!r} 的 review，请显式传 params_hash")
            if not label:  # deduped_ids 分支 label 可能空 → rsplit fallback
                label = _label_from_id(finding_id)
            if not dimension:
                dimension = ""
        if not label:
            raise ValueError(f"无法确定 finding_id={finding_id!r} 的 model label")

        reviews_db.record_feedback(
            finding_id=finding_id, params_hash=phash, label=label,
            dimension=dimension, decision=decision, note=note,
        )
        invalidated = reviews_db.invalidate_review_cache(phash) if invalidate_cache else False
        return {
            "ok": True, "finding_id": finding_id, "params_hash": phash,
            "label": label, "dimension": dimension, "decision": decision,
            "cache_invalidated": invalidated,
        }
    except ValueError:
        raise
    except Exception as e:  # noqa: BLE001 — 不抛错，返回 ok=False（v1.8 降级规范）
        return {"ok": False, "finding_id": finding_id, "error": str(e)}


@mcp.tool()
def mark_advice(
    advice_id: str,
    decision: str,
    consultation_id: str | None = None,
    reason: str = "",
    outcome: str = "",
) -> dict:
    """标记一条外援 advice 是否有用，写入 Advice Memory。

    advice_id/consultation_id 从 consult_problem 返回取。decision:
    accepted|rejected|partial|unknown。只记录最小反馈元数据和用户反馈文本，不保存原始
    prompt、问题正文或 advice 全文。
    """
    res = reviews_db.record_advice_feedback(
        advice_id=advice_id,
        consultation_id=consultation_id,
        decision=decision,
        reason=reason,
        outcome=outcome,
    )
    return {"ok": True, **res}


@mcp.tool()
async def review_document(
    content: str,
    document_type: str = "markdown",
    files: dict | None = None,
    adapter: str = "auto",
    panel: list[str] | None = None,
    dimensions: list[str] | None = None,
    retrieve_top_k: int | None = None,
    extra_context: str = "",
    output_format: str | None = None,
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
    panel_used = _normalize_panel(
        dd["panel"], set((dd.get("endpoints") or {}).keys()), dd.get("endpoints")
    )
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
    effective_output_format = dd["output_format"]
    if cached is not None:
        result = dict(cached["report"])
        result["cache_hit"] = True
        result["reuse_count"] = cached["reuse_count"]
        result["params_hash"] = phash  # v2 mark_finding 引用
        if effective_output_format != "json":
            result["rendered"] = output.render(_rebuild_report(cached["report"]), effective_output_format)
        return result

    engine = _build_engine(ad, dd)
    doc = ReviewDocument(type=document_type, content=content or "", files=files)
    # v1.8 context_modes 校验（Fail Fast：用户配置错不该偷偷 fallback）+ 透传
    context_modes = dd.get("context_modes") or {}
    for dim, mode in context_modes.items():
        if mode not in ("full", "compressed", "minimal"):
            raise ValueError(f"context_modes.{dim}={mode!r} 无效（full|compressed|minimal）")
    # v2 模型可信度（纯 dict 注入 core，core 不依赖 reviews_db；命中分支不重算——用缓存的 calibrated）
    # v2.2 加 warm-start 先验（prior.load：mode 三态，默认 builtin 今天空=v2.1，official 填入自动生效）
    reliability = reviews_db.model_reliability(
        [e["label"] for e in panel_used],
        prior=prior_mod.load(dd.get("model_reliability_prior")),
    )
    ctx = await engine.review(
        doc, panel=panel_used, dimensions=dims_used,
        retrieve_top_k=int(dd["retrieve_top_k"]), extra_context=extra_context,
        effort=dd.get("effort"), max_cost_usd=dd.get("max_cost_usd"),
        context_modes=context_modes, reliability=reliability,
    )
    report = ctx.report
    report_dict = report.to_dict()
    reviews_db.record(phash, report_dict=report_dict, adapter=ad.name, panel=panel_used)
    result = dict(report_dict)
    result["cache_hit"] = False
    result["params_hash"] = phash  # v2 mark_finding 引用
    if effective_output_format != "json":
        result["rendered"] = output.render(report, effective_output_format)
    return result


@mcp.tool()
async def review_plan(
    plan_text: str,
    adapter: str = "auto",
    panel: list[str] | None = None,
    dimensions: list[str] | None = None,
    retrieve_top_k: int | None = None,
    extra_context: str = "",
    output_format: str | None = None,
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
    retrieve_top_k: int | None = None,
    extra_context: str = "",
    output_format: str | None = None,
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
async def plan_task(
    goal: str,
    context: str = "",
    constraints: list[str] | None = None,
    success_criteria: list[str] | None = None,
    existing_plan: str = "",
    files: dict[str, str] | None = None,
    panel: list[str] | None = None,
    effort: str | None = None,
    max_cost_usd: float | None = None,
    max_input_chars: int | None = None,
) -> dict:
    """把目标拆成可执行、可审查的计划。

    Planner MVP 只返回结构化计划，不执行命令、不修改文件。它优先使用 planner_panel；
    未配置时回退 consult_panel，再回退 review panel。首版按 panel 顺序尝试模型，
    取第一个可解析计划作为结果，其余模型只作为失败回退，不做多模型 debate。
    """
    dd = _defaults_mod.apply(effort=effort)
    endpoint_ids = set((dd.get("endpoints") or {}).keys())
    raw_panel, panel_source = _resolve_planner_panel(panel, dd)
    panel_used = _normalize_panel(raw_panel, endpoint_ids, dd.get("endpoints"))
    cost_limit = max_cost_usd if max_cost_usd is not None else dd.get("planner_max_cost_usd")
    if cost_limit is None:
        cost_limit = dd.get("consult_max_cost_usd")
    if cost_limit is None:
        cost_limit = dd.get("max_cost_usd")
    input_limit = int(
        max_input_chars
        if max_input_chars is not None
        else dd.get("planner_max_input_chars", dd.get("consult_max_input_chars", 24000))
    )

    engine = _build_planner_engine(dd)
    report = await engine.plan(
        PlanRequest(
            goal=goal,
            context=context,
            constraints=constraints or [],
            success_criteria=success_criteria or [],
            existing_plan=existing_plan,
            files=files or {},
        ),
        panel=panel_used,
        max_input_chars=input_limit,
        max_cost_usd=cost_limit,
        effort=dd.get("effort"),
    )
    result = report.to_dict()
    result["routing"] = {
        "panel_source": panel_source,
        "resolved_panel": [entry["label"] for entry in panel_used],
        "strategy": "first_parseable_plan",
    }
    return result


@mcp.tool()
async def consult_problem(
    problem: str,
    context: str = "",
    files: dict[str, str] | None = None,
    logs: str = "",
    attempts: list[str] | None = None,
    goal: str = "",
    current_attempt: str = "",
    why_stuck: str = "",
    question: str = "",
    desired_output: str = "",
    constraints: list[str] | None = None,
    panel: list[str] | None = None,
    consultants: list[str] | None = None,
    mode: str | None = None,
    effort: str | None = None,
    max_cost_usd: float | None = None,
    max_input_chars: int | None = None,
) -> dict:
    """外援会诊：当主模型卡住、没把握、连续调试失败或需要第三方视角时调用。

    该工具只返回结构化建议，不执行命令、不修改文件。mode 可选 debugging/architecture/
    performance/simplicity/game_design/challenge/planning。发送给外部模型前会做基础敏感信息
    脱敏、输入长度上限控制和 consultant 白名单校验。panel None 时优先使用 consult_panel，
    未配置则回退 review panel；consultants None 时使用 consult_consultants。
    """
    dd = _defaults_mod.apply(effort=effort)
    endpoint_ids = set((dd.get("endpoints") or {}).keys())
    raw_panel, panel_source = _resolve_consult_panel(panel, dd)
    panel_used = _normalize_panel(raw_panel, endpoint_ids, dd.get("endpoints"))
    consultants_used, mode_used, consultants_source, mode_source = _resolve_consultants_with_source(consultants, mode, dd)
    cost_limit = max_cost_usd if max_cost_usd is not None else dd.get("consult_max_cost_usd")
    if cost_limit is None:
        cost_limit = dd.get("max_cost_usd")
    input_limit = int(max_input_chars if max_input_chars is not None else dd.get("consult_max_input_chars", 24000))

    engine = _build_consult_engine(dd)
    report = await engine.consult(
        ConsultRequest(
            problem=problem,
            context=context,
            files=files or {},
            logs=logs,
            attempts=attempts or [],
            goal=goal,
            current_attempt=current_attempt,
            why_stuck=why_stuck,
            question=question,
            desired_output=desired_output,
            constraints=constraints or [],
        ),
        panel=panel_used,
        consultants=consultants_used,
        max_input_chars=input_limit,
        max_cost_usd=cost_limit,
        effort=dd.get("effort"),
    )
    result = report.to_dict()
    result["panel"] = [entry["label"] for entry in panel_used]
    result["consultants"] = list(consultants_used)
    result["mode"] = mode_used
    result["routing"] = {
        "panel_source": panel_source,
        "mode_source": mode_source,
        "consultants_source": consultants_source,
        "resolved_panel": [entry["label"] for entry in panel_used],
        "resolved_consultants": list(consultants_used),
    }
    # 只记录 consult 元数据与 advice id，不记录 prompt/问题正文/advice 全文。
    reviews_db.record_consultation(result)
    return result


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
def list_consultants() -> dict:
    """列出可用外援会诊角色。"""
    return {"consultants": _list_consultant_files(CONSULTANTS_DIR)}


@mcp.tool()
def list_regions() -> dict:
    """List available Brain Regions."""
    regions = [region.to_dict() for region in _load_regions(REGIONS_DIR)]
    return {"regions": regions}


@mcp.tool()
def route_regions(
    goal: str = "",
    problem: str = "",
    context: str = "",
    files: dict[str, str] | None = None,
    top_k: int = 3,
    min_score: int = 2,
) -> dict:
    """Recommend relevant Brain Regions from local deterministic rules.

    This tool is read-only: it does not call models, read memory, or trigger
    review/consult/planner tools. File contents are ignored; file paths are
    used only as weak metadata.
    """
    return _route_regions(
        goal=goal,
        problem=problem,
        context=context,
        files=files or {},
        top_k=top_k,
        min_score=min_score,
        regions_dir=REGIONS_DIR,
    )


@mcp.tool()
def suggest_workflow(
    goal: str = "",
    problem: str = "",
    context: str = "",
    files: dict[str, str] | None = None,
    top_k: int = 3,
    min_score: int = 2,
) -> dict:
    """Suggest explicit manual next tool calls from Brain Region routing.

    This tool is advisory only: it calls the local deterministic router, then
    returns candidate next actions such as plan_task, consult_problem,
    review_document, or review_code. It never calls those tools or models.
    """
    return _suggest_workflow(
        goal=goal,
        problem=problem,
        context=context,
        files=files or {},
        top_k=top_k,
        min_score=min_score,
        regions_dir=REGIONS_DIR,
    )


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
def list_model_routes(panel: list[str] | None = None) -> dict:
    """Show how model specs resolve to official providers or configured endpoints.

    This is a diagnostic tool only: it does not call models and never returns
    API key values. It helps distinguish bare model strings like
    ``claude-opus-4-8`` from endpoint refs like
    ``modelbridge_anthropic/claude-opus-4-8``.
    """
    all_defaults = _defaults_mod.get_all()
    defaults = {key: value["value"] for key, value in all_defaults.items()}
    panel_source = "explicit" if panel is not None else all_defaults.get("panel", {}).get("source", "unknown")
    return _describe_model_routes(panel, defaults, panel_source=panel_source)


@mcp.tool()
def panel_stats() -> dict:
    """缓存统计：审查总数 + 缓存命中省掉的重复审查数。"""
    return {**reviews_db.stats(), **reviews_db.advice_feedback_stats()}


def main() -> None:
    """MCP server 入口（默认 stdio transport）。"""
    from . import __version__

    logger.info("brain-region-mcp %s starting (stdio)", __version__)
    mcp.run()


if __name__ == "__main__":
    main()
