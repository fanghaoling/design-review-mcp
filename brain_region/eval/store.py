"""评测 ledger 存储：SQLite 主 + JSONL 导出（对齐 reviews_db 模式 + GPT Strong Rec 1）。

SQLite 让尺子成为活资产：可 SELECT 聚合（on vs off 跨 run、某变体 p95 延迟）。
JSONL 仅 --export 人读/备份。append-only：每次 run 新 run_id，不覆盖历史。

表：eval_runs（每 run 一行）/ eval_case_records（每 task×variant 一行）/ eval_blind_judgements
（每 task×judge×variant 一行，per-judge）。
"""
from __future__ import annotations

import dataclasses
import json
import logging
import os
import sqlite3
from pathlib import Path

logger = logging.getLogger("brain_region.eval.store")


def _db_path() -> Path:
    root = os.environ.get("UNITY_PROJECT_ROOT", ".")
    p = Path(root) / ".brain-region" / "eval" / "eval.db"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(_db_path())
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=5000")
    except Exception:  # noqa: BLE001
        pass
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS eval_runs (
            run_id TEXT PRIMARY KEY,
            date TEXT,
            git_sha TEXT,
            variants TEXT,
            judge_models TEXT,
            rubric_hash TEXT,
            knowledge_hash TEXT,
            reviewer_hash TEXT,
            defaults_hash TEXT,
            n_tasks INTEGER,
            summary TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS eval_case_records (
            run_id TEXT NOT NULL,
            task_id TEXT NOT NULL,
            variant TEXT NOT NULL,
            report_summary TEXT,
            retrieved_case_ids TEXT,
            cost TEXT,
            latency_ms REAL,
            outputs_json TEXT,
            error TEXT,
            PRIMARY KEY (run_id, task_id, variant)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS eval_blind_judgements (
            run_id TEXT NOT NULL,
            task_id TEXT NOT NULL,
            judge_id TEXT NOT NULL,
            judge_model TEXT NOT NULL,
            rubric_hash TEXT,
            variant TEXT NOT NULL,
            blind INTEGER,
            scores TEXT,
            reason TEXT,
            judge_cost_usd REAL,
            PRIMARY KEY (run_id, task_id, judge_id, variant)
        )
        """
    )
    conn.commit()
    return conn


def _as_json(obj) -> str:
    return json.dumps(dataclasses.asdict(obj), ensure_ascii=False, default=str)


def record_run(entry) -> None:
    try:
        conn = _connect()
        conn.execute(
            "INSERT INTO eval_runs(run_id,date,git_sha,variants,judge_models,rubric_hash,"
            "knowledge_hash,reviewer_hash,defaults_hash,n_tasks,summary) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(run_id) DO UPDATE SET "
            "  date=excluded.date,git_sha=excluded.git_sha,variants=excluded.variants,"
            "  judge_models=excluded.judge_models,rubric_hash=excluded.rubric_hash,"
            "  knowledge_hash=excluded.knowledge_hash,reviewer_hash=excluded.reviewer_hash,"
            "  defaults_hash=excluded.defaults_hash,n_tasks=excluded.n_tasks,summary=excluded.summary",
            (
                entry.run_id, entry.date, entry.git_sha,
                json.dumps(entry.variants, ensure_ascii=False),
                json.dumps(entry.judge_models, ensure_ascii=False),
                entry.rubric_hash, entry.knowledge_hash, entry.reviewer_hash,
                entry.defaults_hash, entry.n_tasks,
                json.dumps(entry.summary, ensure_ascii=False, default=str),
            ),
        )
        conn.commit()
    except Exception as e:  # noqa: BLE001
        logger.warning("eval record_run 失败: %s", e)


def record_case(rec) -> None:
    try:
        conn = _connect()
        conn.execute(
            "INSERT INTO eval_case_records(run_id,task_id,variant,report_summary,"
            "retrieved_case_ids,cost,latency_ms,outputs_json,error) "
            "VALUES(?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(run_id,task_id,variant) DO UPDATE SET "
            "  report_summary=excluded.report_summary,retrieved_case_ids=excluded.retrieved_case_ids,"
            "  cost=excluded.cost,latency_ms=excluded.latency_ms,outputs_json=excluded.outputs_json,"
            "  error=excluded.error",
            (
                rec.run_id, rec.task_id, rec.variant,
                json.dumps(rec.report_summary, ensure_ascii=False, default=str),
                json.dumps(rec.retrieved_case_ids, ensure_ascii=False),
                json.dumps(rec.cost, ensure_ascii=False, default=str),
                rec.latency_ms, rec.outputs_json, rec.error,
            ),
        )
        conn.commit()
    except Exception as e:  # noqa: BLE001
        logger.warning("eval record_case 失败: %s", e)


def record_judgement(j) -> None:
    try:
        conn = _connect()
        conn.execute(
            "INSERT INTO eval_blind_judgements(run_id,task_id,judge_id,judge_model,rubric_hash,"
            "variant,blind,scores,reason,judge_cost_usd) "
            "VALUES(?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(run_id,task_id,judge_id,variant) DO UPDATE SET "
            "  judge_model=excluded.judge_model,rubric_hash=excluded.rubric_hash,blind=excluded.blind,"
            "  scores=excluded.scores,reason=excluded.reason,judge_cost_usd=excluded.judge_cost_usd",
            (
                j.run_id, j.task_id, j.judge_id, j.judge_model, j.rubric_hash,
                j.variant, 1 if j.blind else 0,
                json.dumps(j.scores, ensure_ascii=False, default=str),
                j.reason, j.judge_cost_usd,
            ),
        )
        conn.commit()
    except Exception as e:  # noqa: BLE001
        logger.warning("eval record_judgement 失败: %s", e)


def export_jsonl(run_id: str, path) -> int:
    """把一次 run 的所有记录导成 JSONL（人读/备份）。返回写入行数。"""
    conn = _connect()
    rows = []
    run = conn.execute("SELECT * FROM eval_runs WHERE run_id=?", (run_id,)).fetchone()
    if run:
        rows.append({"kind": "run", **dict(run)})
    for r in conn.execute("SELECT * FROM eval_case_records WHERE run_id=?", (run_id,)).fetchall():
        rows.append({"kind": "case", **dict(r)})
    for r in conn.execute("SELECT * FROM eval_blind_judgements WHERE run_id=?", (run_id,)).fetchall():
        rows.append({"kind": "judgement", **dict(r)})
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n", encoding="utf-8")
    return len(rows)
