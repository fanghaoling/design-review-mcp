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

logger = logging.getLogger("design_review.reviews_db")


def _db_path() -> Path:
    root = os.environ.get("UNITY_PROJECT_ROOT", ".")
    p = Path(root) / "Assets" / "Generated" / "AIGenerated" / "design_reviews.db"
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
            last_used_at TEXT
        )
        """
    )
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
) -> str:
    parts = [
        document_content or "",
        json.dumps(document_files or {}, sort_keys=True, ensure_ascii=False),
        json.dumps(panel or [], sort_keys=True, ensure_ascii=False),
        json.dumps(sorted(dimensions or []), ensure_ascii=False),
        adapter or "",
        json.dumps(project_version or {}, sort_keys=True, ensure_ascii=False),
        json.dumps(sorted(retrieved_cases_ids or []), ensure_ascii=False),
        extra_context or "",
    ]
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()


def lookup(params_hash: str) -> dict | None:
    try:
        conn = _connect()
        row = conn.execute(
            "SELECT * FROM reviews WHERE params_hash=?", (params_hash,)
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
        conn.execute(
            "INSERT OR IGNORE INTO reviews(params_hash, report_json, adapter, panel) "
            "VALUES(?,?,?,?)",
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


def model_reliability(panel_labels: list[str]) -> dict[tuple[str, str], float]:
    """按 (label, dimension) 聚合历史采纳率（reliability）。

    返回 {(label, dimension): 0~1}。accepted=1/partial=0.5/rejected=0，Beta(2,2) 拉普拉斯
    (score+2)/(n+4)。每 (label,dim) 样本 <5 → 不进结果（调用方 .get(key,1.0) 兜底）。
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
        for r in rows:
            n = int(r["n"] or 0)
            if n < _MIN_SAMPLE:
                continue  # 小样本：不进 out，调用方 .get(key,1.0) 兜底 1.0
            score = float(r["score"] or 0.0)
            out[(r["label"], r["dimension"])] = round((score + _ALPHA) / (n + _ALPHA + _BETA), 3)
    except Exception as e:  # noqa: BLE001
        logger.warning("reviews_db model_reliability 失败（降级全 1.0 不加权）: %s", e)
        return {}
    return out


def invalidate_review_cache(params_hash: str) -> bool:
    """删除某 review 缓存，强制下次重算（reliability 变了）。返回是否删到。"""
    try:
        conn = _connect()
        cur = conn.execute("DELETE FROM reviews WHERE params_hash=?", (params_hash,))
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
