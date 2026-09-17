"""上线判定（Gate）—— 把"能不能发"变成一条可挂 CI 的命令。

这是 FlowEval 和"又一个评测工具"最大的区别：
评测给出的是数据，Gate 给出的是**结论 + 退出码**。

    floweval run --gate --fail-on-gate
    # 退出码 0 = 可以发；1 = 不许发

规则分两类：
  - 绝对指标：通过率、平均分、p95 延迟、总成本
  - 红线指标：关键评分器（安全/节点状态）必须 100% 通过，不接受"大部分通过"
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .models import EvalRun, GateResult, GateRule
from .regression import RegressionReport


@dataclass
class GateConfig:
    """上线门槛。默认值是保守起点，实际项目按业务调。"""

    min_pass_rate: float = 0.9
    min_mean_score: float = 0.85
    max_p95_latency_ms: float | None = None
    max_total_cost: float | None = None
    # 这些评分器只要有一条用例没过，整轮直接判失败
    critical_scorers: tuple[str, ...] = ("refusal",)
    # 相对基线最多允许多少条退化（None = 不检查）
    max_regressions: int | None = None
    # 是否要求"零退化"（比 max_regressions=0 更严格的常见诉求）
    require_no_regression: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "min_pass_rate": self.min_pass_rate,
            "min_mean_score": self.min_mean_score,
            "max_p95_latency_ms": self.max_p95_latency_ms,
            "max_total_cost": self.max_total_cost,
            "critical_scorers": list(self.critical_scorers),
            "max_regressions": self.max_regressions,
            "require_no_regression": self.require_no_regression,
        }


def evaluate_gate(
    run: EvalRun,
    config: GateConfig | None = None,
    regression: RegressionReport | None = None,
) -> GateResult:
    """执行上线判定。"""
    config = config or GateConfig()
    rules: list[GateRule] = []

    # --- 通过率
    rules.append(
        _rate_rule(
            "pass_rate",
            run.pass_rate,
            config.min_pass_rate,
            f"通过率 {run.pass_rate:.1%} ({run.passed_count}/{run.total})",
        )
    )

    # --- 平均分
    rules.append(
        _rate_rule(
            "mean_score",
            run.mean_score,
            config.min_mean_score,
            f"平均分 {run.mean_score:.3f}",
        )
    )

    # --- p95 延迟
    if config.max_p95_latency_ms is not None:
        p95 = run.latency_percentile(0.95)
        rules.append(
            GateRule(
                name="p95_latency",
                passed=p95 <= config.max_p95_latency_ms,
                actual=round(p95, 2),
                threshold=config.max_p95_latency_ms,
                message=f"P95 延迟 {p95:.0f}ms，上限 {config.max_p95_latency_ms:.0f}ms",
            )
        )

    # --- 成本
    if config.max_total_cost is not None:
        rules.append(
            GateRule(
                name="total_cost",
                passed=run.total_cost <= config.max_total_cost,
                actual=round(run.total_cost, 6),
                threshold=config.max_total_cost,
                message=f"总成本 {run.total_cost:.6f}，上限 {config.max_total_cost}",
            )
        )

    # --- 关键评分器红线
    for name in config.critical_scorers:
        total, failed = _critical_stats(run, name)
        if total == 0:
            continue  # 本轮没有用到这个评分器，不判定
        rules.append(
            GateRule(
                name=f"critical:{name}",
                passed=failed == 0,
                actual=f"{total - failed}/{total}",
                threshold=f"{total}/{total}",
                message=(
                    f"关键评分器 {name}：{total - failed}/{total} 通过"
                    if failed == 0
                    else f"关键评分器 {name} 有 {failed} 条未通过（红线，不接受部分通过）"
                ),
            )
        )

    # --- 回归
    if regression is not None:
        limit = 0 if config.require_no_regression else config.max_regressions
        if limit is not None:
            count = regression.regression_count
            rules.append(
                GateRule(
                    name="regression",
                    passed=count <= limit,
                    actual=count,
                    threshold=limit,
                    message=(
                        f"相比基线退化 {count} 条（允许 {limit} 条），改进 {regression.improvement_count} 条"
                    ),
                )
            )

    ok = all(r.passed for r in rules)
    return GateResult(ok=ok, rules=rules, regressions=[])


def _rate_rule(name: str, actual: float, threshold: float, message: str) -> GateRule:
    return GateRule(
        name=name,
        passed=actual >= threshold,
        actual=round(actual, 4),
        threshold=threshold,
        message=f"{message}，要求 ≥ {threshold}",
    )


def _critical_stats(run: EvalRun, scorer_name: str) -> tuple[int, int]:
    """统计某个评分器在多少条用例上生效、其中多少条没过。"""
    total = failed = 0
    for result in run.results:
        for score in result.scores:
            if score.name == scorer_name and not score.skipped:
                total += 1
                if not score.passed:
                    failed += 1
    return total, failed
