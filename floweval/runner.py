"""评测执行器。

职责：并发跑用例、重试、把 TargetResult + 评分结果组装成 EvalRun。

两个容易踩的坑，这里都处理了：
1. **并发下的用例顺序**：ThreadPoolExecutor 返回顺序不确定，所以先按 index
   占位再回填，保证 results 顺序与数据集一致 —— 否则回归对比会对错位。
2. **重试要换 run_id**：被测对象若有缓存/断点续跑，重试必须视为新的一次运行，
   否则"重试"等于"复读同样的结果"。AgentflowTarget 已默认每次新 run_id。
"""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Sequence

from .models import Case, CaseResult, EvalRun, Step, TargetResult
from .scorers import Scorer, auto_scorers, build_scorers
from .targets.base import BaseTarget, EvalTarget


class EvalRunner:
    """跑一轮评测。"""

    def __init__(
        self,
        target: EvalTarget | BaseTarget,
        *,
        scorers: Sequence[Scorer] | None = None,
        max_workers: int = 4,
        retries: int = 0,
        retry_delay: float = 0.5,
        verbose: bool = False,
    ):
        if not hasattr(target, "run"):
            raise TypeError(f"被测对象必须实现 run(case_input) -> TargetResult: {target!r}")
        self.target = target
        self.default_scorers = list(scorers) if scorers else None
        self.max_workers = max(1, max_workers)
        self.retries = max(0, retries)
        self.retry_delay = retry_delay
        self.verbose = verbose

    # ---------------------------------------------------------------- 主入口

    def run(self, cases: Sequence[Case], *, dataset: str = "unknown") -> EvalRun:
        start = time.perf_counter()
        results: list[CaseResult | None] = [None] * len(cases)

        workers = min(self.max_workers, len(cases)) or 1
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {
                pool.submit(self._run_one, idx, case): idx
                for idx, case in enumerate(cases)
            }
            for future in as_completed(futures):
                idx = futures[future]
                results[idx] = future.result()

        run = EvalRun(
            target_name=getattr(self.target, "name", type(self.target).__name__),
            dataset=dataset,
            results=[r for r in results if r is not None],  # type: ignore[misc]
        )
        run.elapsed_ms = (time.perf_counter() - start) * 1000
        run.config = {
            "max_workers": self.max_workers,
            "retries": self.retries,
            "scorers": [s.name for s in (self.default_scorers or [])] or "auto",
        }
        return run

    # ---------------------------------------------------------------- 单条

    def _run_one(self, idx: int, case: Case) -> CaseResult:
        target_result = self._call_with_retry(case)

        scorers = self._resolve_scorers(case)
        scores = [scorer(case, target_result) for scorer in scorers]

        return CaseResult(
            case_id=case.id,
            category=case.category,
            input=case.input,
            output=target_result.output,
            expected=case.expected,
            scores=scores,
            steps=target_result.steps,
            latency_ms=target_result.latency_ms,
            tokens=target_result.tokens,
            cost=target_result.cost,
            error=target_result.error,
        )

    def _call_with_retry(self, case: Case) -> TargetResult:
        result = self.target.run(case.input)
        attempt = 0
        while result.error and attempt < self.retries:
            attempt += 1
            if self.retry_delay:
                time.sleep(self.retry_delay)
            result = self.target.run(case.input)
        return result

    def _resolve_scorers(self, case: Case) -> list[Scorer]:
        """用例里显式写了 scorers 就用它，否则按断言自动推断。"""
        if case.scorers:
            return build_scorers(case.scorers)
        if self.default_scorers:
            return list(self.default_scorers)
        return auto_scorers(case)


# ------------------------------------------------------------------ 便捷函数


def evaluate(
    target: EvalTarget | BaseTarget,
    cases: Sequence[Case],
    *,
    scorers: Sequence[Scorer] | None = None,
    max_workers: int = 4,
    dataset: str = "unknown",
) -> EvalRun:
    """一行跑完一轮评测。"""
    return EvalRunner(
        target, scorers=scorers, max_workers=max_workers
    ).run(cases, dataset=dataset)
