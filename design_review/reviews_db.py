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
