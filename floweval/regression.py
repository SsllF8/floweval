"""回归对比 —— 回答"这次改动让哪些用例变坏了"。

单看一次评测的通过率没意义：85% 是及格还是事故？
只有跟上一版比，才知道是进步还是退步。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .models import EvalRun


@dataclass
class CaseDelta:
    """单条用例的前后差异。"""

    case_id: str
    category: str
    input: str
    baseline_passed: bool
    current_passed: bool
    baseline_score: float
    current_score: float
    delta: float
    baseline_blame: str | None = None
    current_blame: str | None = None
    baseline_output: str = ""
    current_output: str = ""

    @property
    def status(self) -> str:
        if self.baseline_passed and not self.current_passed:
            return "regression"
        if not self.baseline_passed and self.current_passed:
            return "improvement"
        return "unchanged"

    def to_dict(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "category": self.category,
            "input": self.input,
            "status": self.status,
            "baseline_passed": self.baseline_passed,
            "current_passed": self.current_passed,
            "baseline_score": round(self.baseline_score, 4),
            "current_score": round(self.current_score, 4),
            "delta": round(self.delta, 4),
            "baseline_blame": self.baseline_blame,
            "current_blame": self.current_blame,
            "baseline_output": self.baseline_output,
            "current_output": self.current_output,
        }


@dataclass
class RegressionReport:
    baseline_run_id: str
    current_run_id: str
    deltas: list[CaseDelta] = field(default_factory=list)
    new_cases: list[str] = field(default_factory=list)
    removed_cases: list[str] = field(default_factory=list)

    @property
    def regressions(self) -> list[CaseDelta]:
        return [d for d in self.deltas if d.status == "regression"]

    @property
    def improvements(self) -> list[CaseDelta]:
        return [d for d in self.deltas if d.status == "improvement"]

    @property
    def regression_count(self) -> int:
        return len(self.regressions)

    @property
    def improvement_count(self) -> int:
        return len(self.improvements)

    @property
    def net(self) -> int:
        return self.improvement_count - self.regression_count

    def blame_distribution(self) -> dict[str, int]:
        """退化用例按归因节点聚合 —— 直接告诉你"该去改哪个节点"。"""
        dist: dict[str, int] = {}
        for d in self.regressions:
            name = d.current_blame or "unknown"
            dist[name] = dist.get(name, 0) + 1
        return dict(sorted(dist.items(), key=lambda kv: -kv[1]))

    def to_dict(self) -> dict[str, Any]:
        return {
            "baseline_run_id": self.baseline_run_id,
            "current_run_id": self.current_run_id,
            "summary": {
                "regressions": self.regression_count,
                "improvements": self.improvement_count,
                "net": self.net,
                "new_cases": len(self.new_cases),
                "removed_cases": len(self.removed_cases),
            },
            "blame_distribution": self.blame_distribution(),
            "deltas": [d.to_dict() for d in self.deltas],
            "new_cases": self.new_cases,
            "removed_cases": self.removed_cases,
        }


def compare(baseline: EvalRun, current: EvalRun) -> RegressionReport:
    """对比两次评测。以 baseline 的用例集为基准，按 case_id 对齐。"""
    base_map = {r.case_id: r for r in baseline.results}
    curr_map = {r.case_id: r for r in current.results}

    report = RegressionReport(
        baseline_run_id=baseline.run_id,
        current_run_id=current.run_id,
        new_cases=[cid for cid in curr_map if cid not in base_map],
        removed_cases=[cid for cid in base_map if cid not in curr_map],
    )

    for cid, base in base_map.items():
        curr = curr_map.get(cid)
        if curr is None:
            continue
        report.deltas.append(
            CaseDelta(
                case_id=cid,
                category=curr.category,
                input=curr.input,
                baseline_passed=base.passed,
                current_passed=curr.passed,
                baseline_score=base.score,
                current_score=curr.score,
                delta=curr.score - base.score,
                baseline_blame=base.blame_name() if not base.passed else None,
                current_blame=curr.blame_name() if not curr.passed else None,
                baseline_output=base.output,
                current_output=curr.output,
            )
        )

    # 退化排前面 —— 报告里最该先看的
    order = {"regression": 0, "unchanged": 1, "improvement": 2}
    report.deltas.sort(key=lambda d: (order[d.status], d.delta))
    return report
