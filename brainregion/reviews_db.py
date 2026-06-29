"""SQLite 缓存（仿 asset-generator-mcp generations_db）。

key = hash(document + panel + dimensions + adapter + project_version + retrieved_cases_ids + extra_context)。
retrieve/版本/adapter 变化都会让 key 变化 → 缓存自动失效。命中返回上次 report + cache_hit。
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import sqlite3
from pathlib import Path

logger = logging.getLogger("brainregion.reviews_db")


def _db_path() -> Path:
    root = os.environ.get("UNITY_PROJECT_ROOT", ".")
    generated = Path(root) / "Assets" / "Generated" / "AIGenerated"
    legacy = generated / "design_reviews.db"
    current = generated / "brain_region_reviews.db"
    p = legacy if legacy.exists() and not current.exists() else current
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(_db_path())
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=5000")  # v2：防极端并发写等待
    except Exception:  # noqa: BLE001 — 不支持 WAL 的文件系统静默回退
        pass
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS reviews (
            params_hash TEXT PRIMARY KEY,
            report_json TEXT NOT NULL,
            adapter TEXT,
            panel TEXT,
            created_at TEXT DEFAULT (datetime('now')),
            reuse_count INTEGER DEFAULT 0,
            last_used_at TEXT,
            stale INTEGER DEFAULT 0
        )
        """
    )
    # v2.1 软失效：旧表兼容加 stale 列（invalidate 不再删 report_json，防 mark_finding
    # 连续标多条时第一条失效后后续反查断链）
    try:
        conn.execute("ALTER TABLE reviews ADD COLUMN stale INTEGER DEFAULT 0")
    except sqlite3.OperationalError:
        pass  # 列已存在
    # v2 Review Memory：finding 采纳反馈。reliability 是 (label, dimension) 维度——
    # dimension=reviewer 身份（prompt.py:148 按 dimension 加载 reviewer yaml）。
    # 不复用 reviews（reviews 是 INSERT OR IGNORE 的 1:1 cache 语义）；多对一且可变。
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS finding_feedback (
            finding_id TEXT NOT NULL,
            params_hash TEXT NOT NULL,
            label TEXT NOT NULL,
            dimension TEXT NOT NULL,
            decision TEXT NOT NULL,
            note TEXT DEFAULT '',
            decided_at TEXT DEFAULT (datetime('now')),
            PRIMARY KEY (finding_id, params_hash)
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_fb_label_dim ON finding_feedback(label, dimension)"
    )
    # Consultation Memory：只存元数据，不存用户 prompt / logs / files / advice 全文。
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS consultations (
            consultation_id TEXT PRIMARY KEY,
            routing_json TEXT DEFAULT '{}',
            panel TEXT DEFAULT '[]',
            consultants TEXT DEFAULT '[]',
            mode TEXT DEFAULT '',
            usage_json TEXT DEFAULT '{}',
            budget_json TEXT DEFAULT '{}',
            guard_json TEXT DEFAULT '{}',
            advice_count INTEGER DEFAULT 0,
            created_at TEXT DEFAULT (datetime('now'))
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS consultation_advice (
            advice_id TEXT PRIMARY KEY,
            consultation_id TEXT NOT NULL,
            model TEXT NOT NULL,
            consultant TEXT NOT NULL,
            confidence REAL DEFAULT 0.0,
            summary_hash TEXT DEFAULT '',
            created_at TEXT DEFAULT (datetime('now'))
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_consult_advice_cid ON consultation_advice(consultation_id)"
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS advice_feedback (
            advice_id TEXT PRIMARY KEY,
            consultation_id TEXT NOT NULL,
            model TEXT NOT NULL,
            consultant TEXT NOT NULL,
            decision TEXT NOT NULL,
            reason TEXT DEFAULT '',
            outcome TEXT DEFAULT '',
            decided_at TEXT DEFAULT (datetime('now'))
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_advice_fb_model_consultant ON advice_feedback(model, consultant)"
    )
    conn.commit()
    return conn


def compute_hash(
    *,
    document_content: str,
    document_files: dict | None,
    panel: list[str],
    dimensions: list[str],
    adapter: str,
    project_version: dict[str, str],
    retrieved_cases_ids: list[str],
    extra_context: str,
    effort: str | None = None,
    max_cost_usd: float | None = None,
) -> str:
    # effort / max_cost_usd 必须进 hash：改这两个会改变实际产出（effort 影响思考强度；
    # max_cost_usd 影响预算裁剪 → 可能裁掉模型 → 结果不同）。否则改参重跑会静默命中旧缓存（ISS-002）。
    parts = [
        document_content or "",
        json.dumps(document_files or {}, sort_keys=True, ensure_ascii=False),
        json.dumps(panel or [], sort_keys=True, ensure_ascii=False),
        json.dumps(sorted(dimensions or []), ensure_ascii=False),
        adapter or "",
        json.dumps(project_version or {}, sort_keys=True, ensure_ascii=False),
        json.dumps(sorted(retrieved_cases_ids or []), ensure_ascii=False),
        extra_context or "",
        str(effort),
        str(max_cost_usd),
    ]
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()


def lookup(params_hash: str) -> dict | None:
    try:
        conn = _connect()
        row = conn.execute(
            "SELECT * FROM reviews WHERE params_hash=? AND stale=0", (params_hash,)
        ).fetchone()
        if row is None:
            return None
        conn.execute(
            "UPDATE reviews SET reuse_count=reuse_count+1, last_used_at=datetime('now') "
            "WHERE params_hash=?",
            (params_hash,),
        )
        conn.commit()
        return {
            "report": json.loads(row["report_json"]),
            "reuse_count": row["reuse_count"] + 1,
            "cache_hit": True,
        }
    except Exception as e:  # noqa: BLE001
        logger.warning("reviews_db lookup 失败: %s", e)
        return None


def record(
    params_hash: str, *, report_dict: dict, adapter: str, panel: list[str]
) -> None:
    try:
        conn = _connect()
        # v2.1：ON CONFLICT 覆盖（原 INSERT OR IGNORE）。invalidate 软失效标 stale=1 后，
        # 重算 record 须覆盖该行并把 stale 重置 0（IGNORE 会让 stale 行永驻不重算）。
        conn.execute(
            "INSERT INTO reviews(params_hash, report_json, adapter, panel) "
            "VALUES(?,?,?,?) "
            "ON CONFLICT(params_hash) DO UPDATE SET "
            "  report_json=excluded.report_json, adapter=excluded.adapter, "
            "  panel=excluded.panel, stale=0",
            (
                params_hash,
                json.dumps(report_dict, ensure_ascii=False),
                adapter,
                json.dumps(panel, ensure_ascii=False),
            ),
        )
        conn.commit()
    except Exception as e:  # noqa: BLE001
        logger.warning("reviews_db record 失败: %s", e)


def stats() -> dict:
    try:
        conn = _connect()
        total = conn.execute("SELECT COUNT(*) c FROM reviews").fetchone()["c"]
        savings = conn.execute(
            "SELECT COALESCE(SUM(reuse_count),0) s FROM reviews"
        ).fetchone()["s"]
        return {"total_reviews": total, "cache_savings": savings}
    except Exception as e:  # noqa: BLE001
        logger.warning("reviews_db stats 失败: %s", e)
        return {"total_reviews": 0, "cache_savings": 0}


# ===== v2 Review Memory：finding 采纳反馈 + 模型可信度 =====

# decision 枚举（record_feedback / model_reliability 共享，防漂移）
VALID_DECISIONS = {"accepted", "rejected", "partial"}
VALID_ADVICE_DECISIONS = {"accepted", "rejected", "partial", "unknown"}
# decision → 可靠度得分（model_reliability 聚合用）
_DECISION_SCORE = {"accepted": 1.0, "partial": 0.5, "rejected": 0.0}
_MIN_SAMPLE = 5  # (label,dim) 样本 < 此值 → 不进 reliability（调用方 .get(key,1.0) 兜底，向后兼容）
_ALPHA, _BETA = 2.0, 2.0  # Beta(2,2) 拉普拉斯先验：(score+α)/(n+α+β)


def record_feedback(
    *, finding_id: str, params_hash: str, label: str, dimension: str,
    decision: str, note: str = "",
) -> None:
    """记录一条 finding 的采纳反馈（UPSERT，用户改主意覆盖）。

    finding_id/label/dimension 非空 + decision 枚举校验。失败 warn 不抛（v1.8 降级规范）。
    """
    if not finding_id or not label or not dimension:
        raise ValueError(f"finding_id/label/dimension 不能为空: fid={finding_id!r}")
    if decision not in VALID_DECISIONS:
        raise ValueError(f"decision 必须是 {sorted(VALID_DECISIONS)}，得到 {decision!r}")
    note = (note or "").strip()[:2000]
    try:
        conn = _connect()
        conn.execute(
            "INSERT INTO finding_feedback(finding_id, params_hash, label, dimension, decision, note) "
            "VALUES(?,?,?,?,?,?) "
            "ON CONFLICT(finding_id, params_hash) DO UPDATE SET "
            "  decision=excluded.decision, note=excluded.note, decided_at=datetime('now')",
            (finding_id, params_hash, label, dimension, decision, note),
        )
        conn.commit()
    except Exception as e:  # noqa: BLE001
        logger.warning("reviews_db record_feedback 失败: %s", e)


# ===== Consultation Memory：advice 元数据 + 采纳反馈 =====

def _text_hash(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def record_consultation(report_dict: dict) -> None:
    """记录一次 consult 的最小元数据。

    不保存 problem/context/files/logs/prompt/advice 原文，只保存路由、用量、预算、guard
    和 advice 的稳定 id/model/consultant/confidence/summary_hash，供 mark_advice 反查。
    """
    cid = (report_dict or {}).get("consultation_id")
    if not cid:
        raise ValueError("consultation_id 不能为空")
    individual = [a for a in (report_dict.get("individual") or []) if isinstance(a, dict)]
    try:
        conn = _connect()
        conn.execute(
            "INSERT INTO consultations("
            "consultation_id, routing_json, panel, consultants, mode, usage_json, budget_json, guard_json, advice_count"
            ") VALUES(?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(consultation_id) DO UPDATE SET "
            "  routing_json=excluded.routing_json, panel=excluded.panel, consultants=excluded.consultants, "
            "  mode=excluded.mode, usage_json=excluded.usage_json, budget_json=excluded.budget_json, "
            "  guard_json=excluded.guard_json, advice_count=excluded.advice_count",
            (
                cid,
                json.dumps(report_dict.get("routing") or {}, ensure_ascii=False),
                json.dumps(report_dict.get("panel") or [], ensure_ascii=False),
                json.dumps(report_dict.get("consultants") or [], ensure_ascii=False),
                report_dict.get("mode") or "",
                json.dumps(report_dict.get("usage") or {}, ensure_ascii=False),
                json.dumps(report_dict.get("budget") or {}, ensure_ascii=False),
                json.dumps(report_dict.get("guard") or {}, ensure_ascii=False),
                len(individual),
            ),
        )
        for advice in individual:
            aid = advice.get("id")
            model = advice.get("model")
            consultant = advice.get("consultant")
            if not aid or not model or not consultant:
                continue
            conn.execute(
                "INSERT INTO consultation_advice("
                "advice_id, consultation_id, model, consultant, confidence, summary_hash"
                ") VALUES(?,?,?,?,?,?) "
                "ON CONFLICT(advice_id) DO UPDATE SET "
                "  consultation_id=excluded.consultation_id, model=excluded.model, "
                "  consultant=excluded.consultant, confidence=excluded.confidence, "
                "  summary_hash=excluded.summary_hash",
                (
                    aid,
                    cid,
                    model,
                    consultant,
                    float(advice.get("confidence") or 0.0),
                    _text_hash(advice.get("summary") or ""),
                ),
            )
        conn.commit()
    except Exception as e:  # noqa: BLE001
        logger.warning("reviews_db record_consultation 失败: %s", e)


def lookup_advice(advice_id: str) -> dict | None:
    """按 advice_id 查最小元数据，不返回 advice 原文。"""
    try:
        conn = _connect()
        row = conn.execute(
            "SELECT advice_id, consultation_id, model, consultant, confidence, summary_hash "
            "FROM consultation_advice WHERE advice_id=?",
            (advice_id,),
        ).fetchone()
        return dict(row) if row else None
    except Exception as e:  # noqa: BLE001
        logger.warning("reviews_db lookup_advice 失败: %s", e)
        return None


def record_advice_feedback(
    *,
    advice_id: str,
    decision: str,
    consultation_id: str | None = None,
    reason: str = "",
    outcome: str = "",
) -> dict:
    """记录 advice 是否有用（UPSERT）。

    advice_id 必须先由 record_consultation 落过元数据。consultation_id 可选；传入时作
    一致性校验。reason/outcome 限长，只存用户反馈，不存原始 advice/prompt。
    """
    if not advice_id:
        raise ValueError("advice_id 不能为空")
    if decision not in VALID_ADVICE_DECISIONS:
        raise ValueError(f"decision 必须是 {sorted(VALID_ADVICE_DECISIONS)}，得到 {decision!r}")
    advice = lookup_advice(advice_id)
    if advice is None:
        raise ValueError(f"找不到 advice_id={advice_id!r}，请先调用 consult_problem 并使用返回的 advice id")
    if consultation_id and advice["consultation_id"] != consultation_id:
        raise ValueError(
            f"advice_id={advice_id!r} 属于 consultation_id={advice['consultation_id']!r}，不是 {consultation_id!r}"
        )
    reason = (reason or "").strip()[:2000]
    outcome = (outcome or "").strip()[:2000]
    try:
        conn = _connect()
        conn.execute(
            "INSERT INTO advice_feedback(advice_id, consultation_id, model, consultant, decision, reason, outcome) "
            "VALUES(?,?,?,?,?,?,?) "
            "ON CONFLICT(advice_id) DO UPDATE SET "
            "  decision=excluded.decision, reason=excluded.reason, outcome=excluded.outcome, "
            "  decided_at=datetime('now')",
            (
                advice_id,
                advice["consultation_id"],
                advice["model"],
                advice["consultant"],
                decision,
                reason,
                outcome,
            ),
        )
        conn.commit()
        return {
            "advice_id": advice_id,
            "consultation_id": advice["consultation_id"],
            "model": advice["model"],
            "consultant": advice["consultant"],
            "decision": decision,
        }
    except Exception as e:  # noqa: BLE001
        logger.warning("reviews_db record_advice_feedback 失败: %s", e)
        raise


def advice_feedback_stats() -> dict:
    """咨询建议反馈统计（不含原文）。"""
    try:
        conn = _connect()
        total_advice = conn.execute("SELECT COUNT(*) c FROM consultation_advice").fetchone()["c"]
        total_feedback = conn.execute("SELECT COUNT(*) c FROM advice_feedback").fetchone()["c"]
        by_decision = {
            r["decision"]: r["c"]
            for r in conn.execute("SELECT decision, COUNT(*) c FROM advice_feedback GROUP BY decision").fetchall()
        }
        return {
            "total_advice": total_advice,
            "total_advice_feedback": total_feedback,
            "advice_feedback_by_decision": by_decision,
        }
    except Exception as e:  # noqa: BLE001
        logger.warning("reviews_db advice_feedback_stats 失败: %s", e)
        return {"total_advice": 0, "total_advice_feedback": 0, "advice_feedback_by_decision": {}}


def model_reliability(
    panel_labels: list[str], prior: dict | None = None
) -> dict[tuple[str, str], float]:
    """按 (label, dimension) 聚合 reliability（v2.2 加 prior warm-start）。

    prior: {(label,dim):(alpha,beta)} 公共先验（来自 prior.load()，warm-start）。None/{}=纯本地。
    返回 {(label, dimension): 0~1}。4 分支：
      1. 有本地 + 有先验 → Beta 共轭后验 (α+score)/(α+β+n)（Raykar 2010 / Efron-Morris 1973）
      2. 有本地 + 无先验 + n≥5 → Beta(2,2) 拉普拉斯 (score+2)/(n+4)（v2.1 现状）
      3. 有本地 + 无先验 + n<5 → 不进 out（.get 兜底 1.0，向后兼容）
      4. 无本地 + 有先验 → 先验均值 α/(α+β)（冷启动 warm-start）
    prior=None 时只走分支 2/3 + 不冷启动 → 逐字节同 v2.1（向后兼容）。
    空 panel_labels → {}（防 SQL IN() 语法错）。DB 异常 → {}（降级全 1.0 不加权）。
    """
    if not panel_labels:
        return {}
    out: dict[tuple[str, str], float] = {}
    try:
        conn = _connect()
        placeholders = ",".join("?" * len(panel_labels))
        rows = conn.execute(
            "SELECT label, dimension, "
            "  SUM(CASE decision WHEN 'accepted' THEN 1.0 WHEN 'partial' THEN 0.5 ELSE 0.0 END) AS score, "
            "  COUNT(*) AS n "
            f"FROM finding_feedback WHERE label IN ({placeholders}) "
            "GROUP BY label, dimension",
            tuple(panel_labels),
        ).fetchall()
        local_seen: set[tuple[str, str]] = set()
        for r in rows:
            ld = (r["label"], r["dimension"])
            local_seen.add(ld)
            n = int(r["n"] or 0)
            score = float(r["score"] or 0.0)
            if prior and ld in prior:
                # 分支1：共轭后验（先验锚定，小样本也合理估计）
                a, b = prior[ld]
                out[ld] = round((a + score) / (a + b + n), 3)
            elif n >= _MIN_SAMPLE:
                # 分支2：Beta(2,2) 拉普拉斯（v2.1 现状）
                out[ld] = round((score + _ALPHA) / (n + _ALPHA + _BETA), 3)
            # 分支3：有本地 + 无先验 + n<5 → 不进 out（.get 兜底 1.0）
        # 分支4：冷启动 warm-start（无本地 + 有先验 → 先验均值）
        if prior:
            panel_set = set(panel_labels)
            for ld, (a, b) in prior.items():
                if ld[0] in panel_set and ld not in local_seen:
                    out[ld] = round(a / (a + b), 3)
    except Exception as e:  # noqa: BLE001
        logger.warning("reviews_db model_reliability 失败（降级全 1.0 不加权）: %s", e)
        return {}
    return out


def invalidate_review_cache(params_hash: str) -> bool:
    """标某 review 缓存为 stale（软失效，不删 report_json——防 mark_finding 连续标
    多条时第一条失效后后续反查 report 断链）。lookup 命中需 stale=0，故强制下次重算。"""
    try:
        conn = _connect()
        cur = conn.execute(
            "UPDATE reviews SET stale=1 WHERE params_hash=?", (params_hash,)
        )
        conn.commit()
        return cur.rowcount > 0
    except Exception as e:  # noqa: BLE001
        logger.warning("reviews_db invalidate_review_cache 失败: %s", e)
        return False


def lookup_report(params_hash: str) -> dict | None:
    """返回某 review 的 report_dict（只读，不增 reuse_count，区别于 lookup）。"""
    try:
        conn = _connect()
        row = conn.execute(
            "SELECT report_json FROM reviews WHERE params_hash=?", (params_hash,)
        ).fetchone()
        return json.loads(row["report_json"]) if row else None
    except Exception as e:  # noqa: BLE001
        logger.warning("reviews_db lookup_report 失败: %s", e)
        return None


def lookup_review_by_finding(finding_id: str) -> tuple[str | None, str | None, str | None]:
    """扫 reviews 表找含此 finding_id 的最近 review（mark_finding 反查用）。

    扫 consensus + majority + individual 三 bucket source_findings + deduped_ids
    （断链点 B：不只 individual）。返回 (params_hash, label, dimension) 或 (None,None,None)。
    tie-break：ORDER BY created_at DESC（最新审查最相关）。
    """
    try:
        conn = _connect()
        rows = conn.execute(
            "SELECT params_hash, report_json FROM reviews ORDER BY created_at DESC"
        ).fetchall()
        for r in rows:
            rep = json.loads(r["report_json"])
            found = _scan_report_for_finding(rep, finding_id)
            if found is not None:
                return r["params_hash"], found[0], found[1]
        return None, None, None
    except Exception as e:  # noqa: BLE001
        logger.warning("reviews_db lookup_review_by_finding 失败: %s", e)
        return None, None, None


def _scan_report_for_finding(rep: dict, finding_id: str) -> tuple[str, str] | None:
    """在 report dict 里找 finding_id，返回 (label, dimension) 或 None。

    扫三 bucket：individual[label][].id、consensus/majority[].source_findings[].id
    + deduped_ids（断链点 A：被去重的 finding id 挂在代表上）。
    """
    # individual: {label: [{id, model(=label), dimension, ...}]}
    for label, findings in (rep.get("individual") or {}).items():
        for f in findings:
            if isinstance(f, dict) and f.get("id") == finding_id:
                return label, f.get("dimension", "")
    # consensus + majority: source_findings 带 id + deduped_ids
    for key in ("consensus", "majority"):
        for cf in (rep.get(key) or []):
            if not isinstance(cf, dict):
                continue
            for f in (cf.get("source_findings") or []):
                if isinstance(f, dict) and f.get("id") == finding_id:
                    return f.get("model", ""), f.get("dimension", "")
            # 断链点 A：deduped_ids（被去重 finding 的 label/dimension 不可考，用代表信息）
            for did in (cf.get("deduped_ids") or []):
                if did == finding_id:
                    flagged = cf.get("flagged_by") or []
                    return (flagged[0] if flagged else ""), cf.get("dimension", "")
    return None
