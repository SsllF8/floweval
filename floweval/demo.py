"""一键演示：跑两版智能客服，对比回归，给出上线判定。

这个 demo 想讲清楚一件事：
**"通过率从 100% 掉到 71%" 没用，"4 条退化里有 3 条锅在 retrieve 节点" 才有用。**

    python -m floweval.demo
"""

from __future__ import annotations

import json
from pathlib import Path

from .dataset import load_cases
from .gate import GateConfig, evaluate_gate
from .regression import compare
from .report import (
    render_attribution,
    render_gate,
    render_regression,
    render_summary,
    render_text,
)
from .runner import EvalRunner
from .storage import RunStore
from .targets.customer_service import make_target

EXAMPLES = Path(__file__).resolve().parent.parent / "examples"
DEFAULT_DATASET = EXAMPLES / "customer_service.jsonl"

LINE = "═" * 78


def run_demo(db_path: str = "floweval.db", quiet: bool = False, dataset: str | None = None) -> int:
    path = Path(dataset) if dataset else DEFAULT_DATASET
    if not path.exists():
        print(f"[error] 找不到示例数据集: {path}", file=sys.stderr)
        return 2

    cases = load_cases(path)
    print(LINE)
    print("FlowEval 演示：智能客服流水线 v1 → v2 的回归分析")
    print(LINE)
    print(f"数据集: {path.name}  ({len(cases)} 条用例)")
    print("被测对象: 四节点客服流水线  intent → retrieve → answer → guardrail")
    print("")
    print("v1 = 基线版本（知识库完整，top_k=3）")
    print("v2 = 疑似事故版本（知识库误删发票/保修，top_k 降到 1）")

    store = RunStore(db_path)
    runner_kwargs = {"max_workers": 4}

    # ---------------- v1 基线
    print("\n" + LINE)
    print("第 1 步：跑基线 v1")
    print(LINE)
    v1 = EvalRunner(make_target("v1"), **runner_kwargs).run(cases, dataset=str(path))
    store.save(v1)
    print(render_text(v1, show_nodes=False) if quiet else render_text(v1))

    # ---------------- v2 当前
    print("\n" + LINE)
    print("第 2 步：跑待发布版本 v2")
    print(LINE)
    v2 = EvalRunner(make_target("v2"), **runner_kwargs).run(cases, dataset=str(path))
    store.save(v2)

    print(render_summary(v2))
    print(render_attribution(v2))

    regression = compare(v1, v2)
    print(render_regression(regression))

    # ---------------- 上线判定
    print("\n" + LINE)
    print("第 3 步：上线判定")
    print(LINE)
    gate = evaluate_gate(
        v2,
        GateConfig(min_pass_rate=0.9, min_mean_score=0.85, require_no_regression=True),
        regression,
    )
    print(render_gate(gate))

    print("\n" + LINE)
    print("结论")
    print(LINE)
    if gate.ok:
        print("v2 通过全部门槛，可以发布。")
    else:
        print(f"v2 不允许发布：{len(gate.failed_rules)} 项未达标。")
        blame = regression.blame_distribution()
        if blame:
            top_node, top_count = next(iter(blame.items()))
            print(
                f"退化的 {regression.regression_count} 条用例中，"
                f"{top_count} 条归因到 [{top_node}] 节点 —— 优先排查这里。"
            )
        print("")
        print("复现命令：")
        print(f"  floweval compare {v1.run_id} {v2.run_id}")

    # ---------------- 落盘
    out_dir = Path("demo_output")
    out_dir.mkdir(exist_ok=True)
    (out_dir / "v1.json").write_text(
        json.dumps(v1.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (out_dir / "v2.json").write_text(
        json.dumps(v2.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (out_dir / "regression.json").write_text(
        json.dumps(regression.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"\n结果已写入 {out_dir.resolve()}")
    print(f"查看网页看板：floweval serve --db {db_path}")

    return 0 if gate.ok else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(run_demo())
