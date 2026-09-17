"""评测结果持久化。

为什么用 SQLite 单文件而不是接 Postgres：
  - 评测是本地/CI 行为，不该为了看个通过率先起三个容器
  - 单文件可以直接随仓库走，也能随手拷给别人复现
  - 真要多人协作时，换成服务端存储只是替换这一个文件
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from .models import CaseResult, EvalRun, Score, Step

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id       TEXT PRIMARY KEY,
    target_name  TEXT NOT NULL,
    dataset      TEXT NOT NULL,
    started_at   TEXT NOT NULL,
    total        INTEGER NOT NULL,
    passed       INTEGER NOT NULL,
    pass_rate    REAL NOT NULL,
    mean_score   REAL NOT NULL,
    p95_latency  REAL NOT NULL,
    total_cost   REAL NOT NULL DEFAULT 0,
    payload      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_runs_target ON runs(target_name, started_at DESC);

CREATE TABLE IF NOT EXISTS node_runs (
    run_id     TEXT NOT NULL,
    case_id    TEXT NOT NULL,
    node_name  TEXT NOT NULL,
    status     TEXT NOT NULL,
    elapsed_ms REAL NOT NULL,
    PRIMARY KEY (run_id, case_id, node_name)
);
CREATE INDEX IF NOT EXISTS idx_node_runs_node ON node_runs(node_name);
"""


class RunStore:
    """评测历史存储。"""

    def __init__(self, db_path: str | Path = "floweval.db"):
        self.db_path = Path(db_path)
        if self.db_path.parent and str(self.db_path.parent) not in {".", ""}:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    # ---------------------------------------------------------------- 写

    def save(self, run: EvalRun) -> None:
        payload = json.dumps(run.to_dict(), ensure_ascii=False)
        rows = [
            (
                run.run_id,
                case.case_id,
                step.name,
                step.status,
                step.elapsed_ms,
            )
            for case in run.results
            for step in case.steps
        ]
        with self._conn() as conn:
            conn.execute(
                """INSERT OR REPLACE INTO runs
                   (run_id, target_name, dataset, started_at, total, passed,
                    pass_rate, mean_score, p95_latency, total_cost, payload)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    run.run_id, run.target_name, run.dataset, run.started_at,
                    run.total, run.passed_count, run.pass_rate, run.mean_score,
                    run.latency_percentile(0.95), run.total_cost, payload,
                ),
            )
            conn.execute("DELETE FROM node_runs WHERE run_id = ?", (run.run_id,))
            if rows:
                conn.executemany(
                    "INSERT OR REPLACE INTO node_runs VALUES (?,?,?,?,?)", rows
                )

    def delete(self, run_id: str) -> bool:
        with self._conn() as conn:
            cur = conn.execute("DELETE FROM runs WHERE run_id = ?", (run_id,))
            conn.execute("DELETE FROM node_runs WHERE run_id = ?", (run_id,))
            return cur.rowcount > 0

    # ---------------------------------------------------------------- 读

    def list_runs(self, limit: int = 50, target: str | None = None) -> list[dict[str, Any]]:
        sql = """SELECT run_id, target_name, dataset, started_at, total, passed,
                        pass_rate, mean_score, p95_latency, total_cost
                 FROM runs"""
        params: list[Any] = []
        if target:
            sql += " WHERE target_name = ?"
            params.append(target)
        # rowid 兜底：时间戳撞车时仍能按插入顺序确定先后
        sql += " ORDER BY started_at DESC, rowid DESC LIMIT ?"
        params.append(limit)

        with self._conn() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]

    def get(self, run_id: str) -> EvalRun | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT payload FROM runs WHERE run_id = ?", (run_id,)
            ).fetchone()
        if row is None:
            return None
        return run_from_dict(json.loads(row["payload"]))

    def latest(self, target: str | None = None) -> EvalRun | None:
        runs = self.list_runs(limit=1, target=target)
        if not runs:
            return None
        return self.get(runs[0]["run_id"])

    def history(self, target: str | None = None, limit: int = 30) -> list[dict[str, Any]]:
        """趋势图用：按时间正序返回。"""
        runs = self.list_runs(limit=limit, target=target)
        return list(reversed(runs))

    def node_trend(self, limit: int = 10) -> dict[str, list[dict[str, Any]]]:
        """每个节点在最近若干次运行中的失败率趋势。"""
        recent = [r["run_id"] for r in self.list_runs(limit=limit)]
        if not recent:
            return {}
        placeholders = ",".join("?" * len(recent))
        sql = f"""
            SELECT run_id, node_name,
                   SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END) AS failed,
                   COUNT(*) AS calls,
                   AVG(elapsed_ms) AS avg_ms
            FROM node_runs WHERE run_id IN ({placeholders})
            GROUP BY run_id, node_name
        """
        with self._conn() as conn:
            rows = conn.execute(sql, recent).fetchall()

        trend: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            trend.setdefault(row["node_name"], []).append(
                {
                    "run_id": row["run_id"],
                    "fail_rate": round(row["failed"] / max(1, row["calls"]), 4),
                    "avg_ms": round(row["avg_ms"] or 0, 2),
                }
            )
        return trend

    # ---------------------------------------------------------------- 内部

    def _init_db(self) -> None:
        with self._conn() as conn:
            conn.executescript(SCHEMA)

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn


# ------------------------------------------------------------------ 反序列化


def run_from_dict(data: dict[str, Any]) -> EvalRun:
    """把 to_dict() 的结果还原成 EvalRun 对象。

    只还原"能被再次分析"的字段（scores/steps 保留完整），
    汇总指标由属性实时算，不落库，避免两份数据不一致。
    """
    results = [
        CaseResult(
            case_id=r["case_id"],
            category=r.get("category", "default"),
            input=r.get("input", ""),
            output=r.get("output", ""),
            expected=r.get("expected"),
            scores=[
                Score(
                    name=s["name"],
                    passed=s["passed"],
                    score=s.get("score", 0.0),
                    weight=s.get("weight", 1.0),
                    reason=s.get("reason", ""),
                    skipped=s.get("skipped", False),
                    detail=s.get("detail", {}),
                )
                for s in r.get("scores", [])
            ],
            steps=[
                Step(
                    name=s["name"],
                    status=s.get("status", "success"),
                    output=s.get("output", ""),
                    elapsed_ms=s.get("elapsed_ms", 0.0),
                    meta=s.get("meta", {}),
                )
                for s in r.get("steps", [])
            ],
            latency_ms=r.get("latency_ms", 0.0),
            tokens=r.get("tokens", 0),
            cost=r.get("cost", 0.0),
            error=r.get("error"),
        )
        for r in data.get("results", [])
    ]

    run = EvalRun(
        run_id=data.get("run_id", ""),
        target_name=data.get("target_name", "unknown"),
        dataset=data.get("dataset", "unknown"),
        started_at=data.get("started_at", ""),
        elapsed_ms=data.get("elapsed_ms", 0.0),
        results=results,
        config=data.get("config", {}),
        meta=data.get("meta", {}),
    )
    return run
