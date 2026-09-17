"""FlowEval — 面向多节点 Agent 流水线的评测与上线判定工具。

三句话说明它是什么：
  1. 给一批用例，跑你的 Agent，逐条打分；
  2. 失败时告诉你是哪个节点出的问题（节点级归因），不是只给个总分；
  3. 跟上一版对比，给出"能不能上线"的结论和退出码。

最快上手：

    from floweval import evaluate, make_target, load_cases
    from floweval.gate import GateConfig, evaluate_gate

    cases = load_cases("cases.jsonl")
    run = evaluate(make_target("v1"), cases)
    print(run.pass_rate, run.blame_distribution())

命令行：

    floweval run -d cases.jsonl -t customer_service --gate --fail-on-gate
    floweval compare <baseline_run_id> <current_run_id>
    floweval serve
"""

from .dataset import load_cases, load_cases_from, save_cases
from .gate import GateConfig, evaluate_gate
from .models import (
    Case,
    CaseResult,
    EvalRun,
    GateResult,
    Score,
    Step,
    TargetResult,
)
from .regression import compare
from .report import render_markdown, render_text
from .runner import EvalRunner, evaluate
from .storage import RunStore
from .targets.base import BaseTarget, EvalTarget, FunctionTarget
from .targets.customer_service import CustomerServiceTarget, make_target
from .targets.factory import build_target

__version__ = "0.1.0"

__all__ = [
    "__version__",
    # 模型
    "Case", "CaseResult", "EvalRun", "Score", "Step", "TargetResult", "GateResult",
    # 数据
    "load_cases", "load_cases_from", "save_cases",
    # 执行
    "evaluate", "EvalRunner",
    # 被测对象
    "EvalTarget", "BaseTarget", "FunctionTarget", "CustomerServiceTarget",
    "make_target", "build_target",
    # 判定与回归
    "GateConfig", "evaluate_gate", "compare",
    # 报告与存储
    "render_text", "render_markdown", "RunStore",
]
