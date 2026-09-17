"""FlowEval 核心数据模型。

设计约束：
1. 所有结构都能 `to_dict()` 直接 JSON 序列化（Web 看板靠这个吃饭）。
2. `TargetResult` 是评测核心与被评测对象之间唯一的契约 —— 见 targets/base.py。
3. `steps` 是可选项但强烈建议提供：没有 steps 就没有节点级归因，
   FlowEval 相比通用评测工具的差异化也就没了。
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Iterable


def _now_id(prefix: str = "run") -> str:
    stamp = time.strftime("%Y%m%d-%H%M%S")
    return f"{prefix}-{stamp}-{uuid.uuid4().hex[:6]}"


def _now_iso() -> str:
    """本地时间，精确到毫秒。

    为什么要毫秒：started_at 是"哪次运行更新"的排序依据，
    只到秒的话同一秒内的多次评测分不出先后。
    """
    t = time.time()
    base = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(t))
    return f"{base}.{int((t % 1) * 1000):03d}"


# ---------------------------------------------------------------- 被测对象输出


@dataclass
class Step:
    """流水线中单个节点的执行记录 —— 归因的最小单位。"""

    name: str
    status: str = "success"  # success | failed | skipped
    output: str = ""
    elapsed_ms: float = 0.0
    meta: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "status": self.status,
            "output": self.output,
            "elapsed_ms": round(self.elapsed_ms, 2),
            "meta": self.meta,
        }


@dataclass
class TargetResult:
    """被测对象跑完一条用例后的结果。

    这是 FlowEval 的"标准接口"。任何被测对象 —— HTTP 服务、OpenAI 兼容接口、
    agentflow 工作流、本地 Python 函数 —— 只要能产出这个结构，就能被评测。

    output 是必填项，其余都是可选增强：
      - steps     : 节点级过程，有了它才有节点级归因
      - tokens/cost: 成本观测
      - latency_ms: 延迟观测
      - error     : 执行期异常（非 None 时该 case 直接判失败）
    """

    output: str
    steps: list[Step] = field(default_factory=list)
    tokens: int = 0
    cost: float = 0.0
    latency_ms: float = 0.0
    error: str | None = None
    raw: Any = None

    @property
    def ok(self) -> bool:
        return self.error is None

    def to_dict(self) -> dict[str, Any]:
        return {
            "output": self.output,
            "steps": [s.to_dict() for s in self.steps],
            "tokens": self.tokens,
            "cost": self.cost,
            "latency_ms": round(self.latency_ms, 2),
            "error": self.error,
        }


# -------------------------------------------------------------------- 测试用例


@dataclass
class Case:
    """一条评测用例。

    断言可以通过三种方式给出，从简到繁：
      - expected          : 期望输出（精确 / 语义匹配）
      - must_contain      : 输出必须包含的子串
      - must_not_contain  : 输出不得包含的子串
      - regex             : 输出需匹配的正则
      - expect_refusal    : 是否期望拒答（安全类用例）
      - scorers           : 显式指定评分器及权重，覆盖全局默认

    category 用于分组统计 —— 看板上"哪一类问题最容易翻车"就靠它。
    """

    id: str
    input: str
    expected: str | None = None
    must_contain: list[str] = field(default_factory=list)
    must_not_contain: list[str] = field(default_factory=list)
    regex: str | None = None
    expect_json: bool = False
    expect_refusal: bool | None = None
    category: str = "default"
    max_latency_ms: float | None = None
    scorers: list[dict[str, Any]] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "input": self.input,
            "expected": self.expected,
            "must_contain": self.must_contain,
            "must_not_contain": self.must_not_contain,
            "regex": self.regex,
            "expect_json": self.expect_json,
            "expect_refusal": self.expect_refusal,
            "category": self.category,
            "max_latency_ms": self.max_latency_ms,
            "scorers": self.scorers,
            "metadata": self.metadata,
        }

    @staticmethod
    def from_dict(data: dict[str, Any]) -> "Case":
        known = {
            "id", "input", "expected", "must_contain", "must_not_contain",
            "regex", "expect_json", "expect_refusal", "category",
            "max_latency_ms", "scorers", "metadata",
        }
        case = Case(
            id=str(data.get("id") or uuid.uuid4().hex[:8]),
            input=str(data.get("input", "")),
            expected=data.get("expected"),
            must_contain=list(data.get("must_contain") or []),
            must_not_contain=list(data.get("must_not_contain") or []),
            regex=data.get("regex"),
            expect_json=bool(data.get("expect_json", False)),
            expect_refusal=data.get("expect_refusal"),
            category=str(data.get("category", "default")),
            max_latency_ms=data.get("max_latency_ms"),
            scorers=list(data.get("scorers") or []),
            metadata=dict(data.get("metadata") or {}),
        )
        # 未识别的字段塞进 metadata，避免用户自定义字段被静默丢弃
        for key, value in data.items():
            if key not in known:
                case.metadata.setdefault(key, value)
        return case


# ---------------------------------------------------------------------- 评分


@dataclass
class Score:
    """单个评分器的结果。

    `skipped=True` 表示该评分器本次未实际生效（例如没配 LLM API key），
    不计入加权总分 —— 否则会污染通过率。
    """

    name: str
    passed: bool
    score: float = 0.0  # 0.0 ~ 1.0
    weight: float = 1.0
    reason: str = ""
    skipped: bool = False
    detail: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "passed": self.passed,
            "score": round(self.score, 4),
            "weight": self.weight,
            "reason": self.reason,
            "skipped": self.skipped,
            "detail": self.detail,
        }


@dataclass
class CaseResult:
    """一条用例的完整评测结果。"""

    case_id: str
    category: str
    input: str
    output: str
    expected: str | None = None
    scores: list[Score] = field(default_factory=list)
    steps: list[Step] = field(default_factory=list)
    latency_ms: float = 0.0
    tokens: int = 0
    cost: float = 0.0
    error: str | None = None

    # ---------------- 汇总

    @property
    def active_scores(self) -> list[Score]:
        return [s for s in self.scores if not s.skipped]

    @property
    def score(self) -> float:
        """加权平均分；没有生效的评分器时返回 1.0（无从扣分）。"""
        active = self.active_scores
        if not active:
            return 1.0
        total_w = sum(s.weight for s in active) or 1.0
        return sum(s.score * s.weight for s in active) / total_w

    @property
    def passed(self) -> bool:
        if self.error:
            return False
        return all(s.passed for s in self.active_scores)

    @property
    def failed_scorers(self) -> list[str]:
        return [s.name for s in self.active_scores if not s.passed]

    # ---------------- 归因

    def blame(self) -> Step | None:
        """把失败归因到具体节点。

        归因优先级：
          1. 有节点显式 failed   → 第一个失败的节点（执行链断在这里）
          2. 节点都成功但断言没过 → 从后往前找第一个**改变了输出**的节点
          3. 没有 steps           → None（无法归因，只能说"不知道"）

        第 2 条为什么不是"直接取最后一个节点"：流水线末尾常常挂着
        合规审查/格式化这类节点，它们多数时候原样透传上游输出。
        内容错了不该由它们背锅，所以要往前找到真正产出这段输出的节点；
        只有当它确实改写了内容（比如合规改写），锅才归它。

        这是启发式，不是事实：真实项目里答案错了可能是检索错了，
        也可能是生成错了。所以我们同时保留完整 steps 供人工复核，
        归因只负责指出"先看哪里"。
        """
        if not self.steps:
            return None
        for step in self.steps:
            if step.status == "failed":
                return step
        for i in range(len(self.steps) - 1, 0, -1):
            cur, prev = self.steps[i], self.steps[i - 1]
            if cur.status == "skipped":
                continue
            if cur.output and cur.output == prev.output:
                continue  # 原样透传，不是它的问题
            return cur
        return self.steps[-1]

    def blame_name(self) -> str:
        step = self.blame()
        return step.name if step else "unknown"

    # ---------------- 序列化

    def to_dict(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "category": self.category,
            "input": self.input,
            "output": self.output,
            "expected": self.expected,
            "score": round(self.score, 4),
            "passed": self.passed,
            "failed_scorers": self.failed_scorers,
            "blame": self.blame_name(),
            "latency_ms": round(self.latency_ms, 2),
            "tokens": self.tokens,
            "cost": self.cost,
            "error": self.error,
            "scores": [s.to_dict() for s in self.scores],
            "steps": [s.to_dict() for s in self.steps],
        }


# ------------------------------------------------------------------ 一次评测


@dataclass
class EvalRun:
    """一次完整评测。

    `target_name` / `dataset` / `git_commit` 等元信息用于回归对比时回答
    "这两次跑的到底是什么版本"。
    """

    run_id: str = field(default_factory=_now_id)
    target_name: str = "unknown"
    dataset: str = "unknown"
    started_at: str = field(default_factory=_now_iso)
    elapsed_ms: float = 0.0
    results: list[CaseResult] = field(default_factory=list)
    config: dict[str, Any] = field(default_factory=dict)
    meta: dict[str, Any] = field(default_factory=dict)

    # ---------------- 汇总指标

    @property
    def total(self) -> int:
        return len(self.results)

    @property
    def passed_count(self) -> int:
        return sum(1 for r in self.results if r.passed)

    @property
    def failed_count(self) -> int:
        return self.total - self.passed_count

    @property
    def pass_rate(self) -> float:
        return self.passed_count / self.total if self.total else 0.0

    @property
    def mean_score(self) -> float:
        return (
            sum(r.score for r in self.results) / self.total if self.total else 0.0
        )

    @property
    def total_cost(self) -> float:
        return sum(r.cost for r in self.results)

    @property
    def total_tokens(self) -> int:
        return sum(r.tokens for r in self.results)

    def latency_percentile(self, p: float = 0.95) -> float:
        if not self.results:
            return 0.0
        vals = sorted(r.latency_ms for r in self.results)
        idx = min(len(vals) - 1, int(round(p * (len(vals) - 1))))
        return vals[idx]

    def by_category(self) -> dict[str, dict[str, Any]]:
        buckets: dict[str, list[CaseResult]] = {}
        for r in self.results:
            buckets.setdefault(r.category, []).append(r)
        out: dict[str, dict[str, Any]] = {}
        for cat, items in sorted(buckets.items()):
            passed = sum(1 for r in items if r.passed)
            out[cat] = {
                "total": len(items),
                "passed": passed,
                "pass_rate": passed / len(items),
                "mean_score": sum(r.score for r in items) / len(items),
            }
        return out

    def blame_distribution(self) -> dict[str, int]:
        """失败用例按归因节点计数 —— 节点级归因的聚合视图。"""
        dist: dict[str, int] = {}
        for r in self.results:
            if r.passed:
                continue
            name = r.blame_name()
            dist[name] = dist.get(name, 0) + 1
        return dict(sorted(dist.items(), key=lambda kv: -kv[1]))

    def node_stats(self) -> dict[str, dict[str, Any]]:
        """每个节点的耗时/失败率统计（跨所有用例聚合）。"""
        stats: dict[str, dict[str, Any]] = {}
        for r in self.results:
            for step in r.steps:
                s = stats.setdefault(
                    step.name,
                    {"calls": 0, "failed": 0, "total_ms": 0.0, "max_ms": 0.0},
                )
                s["calls"] += 1
                s["failed"] += 1 if step.status == "failed" else 0
                s["total_ms"] += step.elapsed_ms
                s["max_ms"] = max(s["max_ms"], step.elapsed_ms)
        for name, s in stats.items():
            calls = s["calls"] or 1
            s["avg_ms"] = round(s["total_ms"] / calls, 2)
            s["max_ms"] = round(s["max_ms"], 2)
            s["fail_rate"] = round(s["failed"] / calls, 4)
            s.pop("total_ms")
        return stats

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "target_name": self.target_name,
            "dataset": self.dataset,
            "started_at": self.started_at,
            "elapsed_ms": round(self.elapsed_ms, 2),
            "config": self.config,
            "meta": self.meta,
            "summary": {
                "total": self.total,
                "passed": self.passed_count,
                "failed": self.failed_count,
                "pass_rate": round(self.pass_rate, 4),
                "mean_score": round(self.mean_score, 4),
                "p95_latency_ms": round(self.latency_percentile(0.95), 2),
                "total_tokens": self.total_tokens,
                "total_cost": round(self.total_cost, 6),
            },
            "by_category": self.by_category(),
            "blame_distribution": self.blame_distribution(),
            "node_stats": self.node_stats(),
            "results": [r.to_dict() for r in self.results],
        }


# ------------------------------------------------------------- 上线判定


@dataclass
class GateRule:
    """一条上线门槛规则。"""

    name: str
    passed: bool
    actual: Any
    threshold: Any
    message: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "passed": self.passed,
            "actual": self.actual,
            "threshold": self.threshold,
            "message": self.message,
        }


@dataclass
class GateResult:
    """上线判定结论。CI 里靠 `ok` 决定退出码。"""

    ok: bool
    rules: list[GateRule] = field(default_factory=list)
    regressions: list[dict[str, Any]] = field(default_factory=list)

    @property
    def failed_rules(self) -> list[GateRule]:
        return [r for r in self.rules if not r.passed]

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "rules": [r.to_dict() for r in self.rules],
            "regressions": self.regressions,
        }
