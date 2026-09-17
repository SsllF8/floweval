"""FlowEval 命令行入口。

设计目标：一条命令跑完评测并给出能不能上线的结论，能直接挂 CI。

    floweval run -d cases.jsonl -t customer_service --gate --fail-on-gate
    # 退出码 0 = 可以发；1 = 不许发
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from .dataset import load_cases
from .gate import GateConfig, evaluate_gate
from .models import EvalRun
from .regression import RegressionReport, compare
from .report import build_dashboard_payload, render_markdown, render_text
from .runner import EvalRunner
from .storage import RunStore
from .targets.factory import available_targets, build_target, load_config

EXIT_OK = 0
EXIT_GATE_FAILED = 1
EXIT_ERROR = 2


# ------------------------------------------------------------------ 命令实现


def cmd_run(args: argparse.Namespace) -> int:
    try:
        cases = load_cases(args.dataset)
        target = build_target(args.target, load_config(args.target_config))
    except Exception as exc:  # noqa: BLE001 - CLI 边界，统一转成友好报错
        print(f"[error] {type(exc).__name__}: {exc}", file=sys.stderr)
        return EXIT_ERROR

    runner = EvalRunner(target, max_workers=args.workers, retries=args.retries)
    run = runner.run(cases, dataset=args.dataset)

    store = RunStore(args.db)
    store.save(run)

    # --- 回归对比
    regression: RegressionReport | None = None
    baseline: EvalRun | None = None
    if args.baseline:
        baseline = store.get(args.baseline)
        if baseline is None:
            print(f"[warn] 找不到基线 run_id={args.baseline}，跳过回归对比", file=sys.stderr)
        else:
            regression = compare(baseline, run)

    # --- 上线判定
    gate = None
    if args.gate:
        # 指定了基线却没设退化上限时，默认按"零退化"判定 ——
        # 你都指定基线了，目的就是防退化，没道理比完不算数。
        max_regressions = args.max_regressions
        if (
            max_regressions is None
            and not args.no_regression
            and regression is not None
        ):
            max_regressions = 0

        gate_cfg = GateConfig(
            min_pass_rate=args.min_pass_rate,
            min_mean_score=args.min_mean_score,
            max_p95_latency_ms=args.max_p95_latency,
            max_total_cost=args.max_cost,
            max_regressions=max_regressions,
            require_no_regression=args.no_regression,
        )
        gate = evaluate_gate(run, gate_cfg, regression)
        run.meta["gate"] = gate.to_dict()

    # --- 输出
    text = render_text(run, gate=gate, regression=regression)
    if args.quiet:
        text = render_text(run, gate=gate, regression=regression, show_nodes=False)
    print(text)

    if args.json:
        _write(args.json, json.dumps(run.to_dict(), ensure_ascii=False, indent=2))
    if args.markdown:
        _write(args.markdown, render_markdown(run, gate=gate, regression=regression))
    if args.html:
        payload = build_dashboard_payload(
            run, gate=gate, regression=regression, history=store.history(limit=30)
        )
        _write(args.html, render_dashboard_html(args.html, payload))

    print(f"\n结果已存入 {Path(args.db).resolve()}  (run_id={run.run_id})")

    if gate is not None and not gate.ok and args.fail_on_gate:
        return EXIT_GATE_FAILED
    return EXIT_OK


def cmd_compare(args: argparse.Namespace) -> int:
    store = RunStore(args.db)
    baseline = store.get(args.baseline)
    current = store.get(args.current)
    if baseline is None or current is None:
        missing = [
            name for name, run in (("baseline", baseline), ("current", current)) if run is None
        ]
        print(f"[error] 找不到运行记录: {', '.join(missing)}", file=sys.stderr)
        return EXIT_ERROR

    report = compare(baseline, current)
    from .report import render_regression  # noqa: PLC0415 - 避免循环

    print(f"基线  {baseline.run_id}  ({baseline.target_name}, {baseline.started_at})")
    print(f"当前  {current.run_id}  ({current.target_name}, {current.started_at})")
    print(render_regression(report))
    print(f"\n退化归因: {report.blame_distribution()}")

    if args.json:
        _write(args.json, json.dumps(report.to_dict(), ensure_ascii=False, indent=2))

    if args.fail_on_regression and report.regression_count > 0:
        return EXIT_GATE_FAILED
    return EXIT_OK


def cmd_list(args: argparse.Namespace) -> int:
    store = RunStore(args.db)
    runs = store.list_runs(limit=args.limit, target=args.target)
    if not runs:
        print("暂无评测记录。先跑一次：floweval run -d cases.jsonl -t customer_service")
        return EXIT_OK

    print(f"{'run_id':<28}{'被测对象':<24}{'通过率':<10}{'平均分':<10}时间")
    print("─" * 96)
    for r in runs:
        print(
            f"{r['run_id']:<28}{r['target_name'][:22]:<24}"
            f"{r['pass_rate']:.1%}{'':<6}{r['mean_score']:<10.3f}{r['started_at']}"
        )
    return EXIT_OK


def cmd_show(args: argparse.Namespace) -> int:
    store = RunStore(args.db)
    run = store.get(args.run_id)
    if run is None:
        print(f"[error] 找不到 run_id={args.run_id}", file=sys.stderr)
        return EXIT_ERROR
    print(render_text(run))
    if args.json:
        _write(args.json, json.dumps(run.to_dict(), ensure_ascii=False, indent=2))
    return EXIT_OK


def cmd_serve(args: argparse.Namespace) -> int:
    """启动 Web 控制台。

    和旧版只做静态展示的区别：这里起的是带 API 的服务，
    网页上可以直接选被测对象、填配置、传数据集、发起评测、看结果。
    """
    from .server import start_server  # noqa: PLC0415 - 只在 serve 时才需要

    try:
        start_server(
            host=args.host,
            port=args.port,
            db_path=args.db,
            datasets_dir=args.datasets_dir,
            open_browser=not args.no_browser,
        )
    except OSError as exc:
        print(f"[error] 端口 {args.port} 无法使用: {exc}", file=sys.stderr)
        return EXIT_ERROR
    return EXIT_OK


def cmd_demo(args: argparse.Namespace) -> int:
    """一键跑通：内置数据集 + 两个版本对比 + 上线判定。"""
    from .demo import run_demo  # noqa: PLC0415

    return run_demo(db_path=args.db, quiet=args.quiet)


def cmd_targets(args: argparse.Namespace) -> int:
    print("可用被测对象类型:")
    for name in available_targets():
        print(f"  - {name}")
    print("\n示例:")
    print("  floweval run -d cases.jsonl -t customer_service --target-config '{\"version\":\"v2\"}'")
    print("  floweval run -d cases.jsonl -t http --target-config '{\"url\":\"http://localhost:8000/ask\"}'")
    print("  floweval run -d cases.jsonl -t openai --target-config '{\"model\":\"deepseek-chat\"}'")
    return EXIT_OK


# ------------------------------------------------------------------ 参数解析


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="floweval",
        description="FlowEval — 面向多节点 Agent 流水线的评测与上线判定工具",
    )
    parser.add_argument("--version", action="version", version="FlowEval 0.1.0")
    sub = parser.add_subparsers(dest="command", required=True)

    # run
    p_run = sub.add_parser("run", help="跑一轮评测")
    p_run.add_argument("-d", "--dataset", required=True, help="数据集路径 (.jsonl/.json/.yaml)")
    p_run.add_argument("-t", "--target", default="customer_service", help="被测对象类型")
    p_run.add_argument("-c", "--target-config", help="被测对象配置（内联 JSON 或 JSON 文件路径）")
    p_run.add_argument("-w", "--workers", type=int, default=4, help="并发数")
    p_run.add_argument("--retries", type=int, default=0, help="失败重试次数")
    p_run.add_argument("--db", default="floweval.db", help="结果数据库路径")
    p_run.add_argument("-b", "--baseline", help="基线 run_id（用于回归对比）")
    p_run.add_argument("--gate", action="store_true", help="启用上线判定")
    p_run.add_argument("--min-pass-rate", type=float, default=0.9)
    p_run.add_argument("--min-mean-score", type=float, default=0.85)
    p_run.add_argument("--max-p95-latency", type=float, default=None)
    p_run.add_argument("--max-cost", type=float, default=None)
    p_run.add_argument("--max-regressions", type=int, default=None)
    p_run.add_argument("--no-regression", action="store_true", help="要求零退化")
    p_run.add_argument("--fail-on-gate", action="store_true", help="判定不通过时退出码为 1（CI 用）")
    p_run.add_argument("--json", help="结果 JSON 输出路径")
    p_run.add_argument("--markdown", help="Markdown 报告输出路径")
    p_run.add_argument("--html", help="单文件 Web 看板输出路径")
    p_run.add_argument("-q", "--quiet", action="store_true", help="精简输出")
    p_run.set_defaults(func=cmd_run)

    # compare
    p_cmp = sub.add_parser("compare", help="对比两次评测")
    p_cmp.add_argument("baseline", help="基线 run_id")
    p_cmp.add_argument("current", help="当前 run_id")
    p_cmp.add_argument("--db", default="floweval.db")
    p_cmp.add_argument("--json", help="差异 JSON 输出路径")
    p_cmp.add_argument("--fail-on-regression", action="store_true")
    p_cmp.set_defaults(func=cmd_compare)

    # list
    p_list = sub.add_parser("list", help="列出历史评测")
    p_list.add_argument("--db", default="floweval.db")
    p_list.add_argument("--limit", type=int, default=20)
    p_list.add_argument("--target", help="按被测对象过滤")
    p_list.set_defaults(func=cmd_list)

    # show
    p_show = sub.add_parser("show", help="查看某次评测详情")
    p_show.add_argument("run_id")
    p_show.add_argument("--db", default="floweval.db")
    p_show.add_argument("--json", help="结果 JSON 输出路径")
    p_show.set_defaults(func=cmd_show)

    # serve
    p_serve = sub.add_parser("serve", help="启动 Web 控制台（可视化跑评测）")
    p_serve.add_argument("--db", default="floweval.db")
    p_serve.add_argument("--host", default="127.0.0.1")
    p_serve.add_argument("--port", type=int, default=8765)
    p_serve.add_argument(
        "--datasets-dir", default="datasets", help="上传的数据集存放目录"
    )
    p_serve.add_argument("--no-browser", action="store_true")
    p_serve.set_defaults(func=cmd_serve)

    # demo
    p_demo = sub.add_parser("demo", help="一键跑通完整演示")
    p_demo.add_argument("--db", default="floweval.db")
    p_demo.add_argument("-q", "--quiet", action="store_true")
    p_demo.set_defaults(func=cmd_demo)

    # targets
    p_t = sub.add_parser("targets", help="列出可用的被测对象类型")
    p_t.set_defaults(func=cmd_targets)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except KeyboardInterrupt:  # pragma: no cover
        print("\n已中断", file=sys.stderr)
        return 130


def _write(path: str, content: str) -> None:
    p = Path(path)
    if p.parent and str(p.parent) not in {".", ""}:
        p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    print(f"已写入 {p.resolve()}")


def render_dashboard_html(path: str, payload: dict[str, Any]) -> str:
    """生成单文件看板：CSS/JS/数据全部内联，双击即可打开，无需起服务。"""
    web_dir = Path(__file__).resolve().parent.parent / "web"
    template = (web_dir / "index.html").read_text(encoding="utf-8")
    css = (web_dir / "styles.css").read_text(encoding="utf-8")
    js = (web_dir / "app.js").read_text(encoding="utf-8")

    # JSON 里若出现 </script> 会提前闭合标签，转义成正斜杠（JSON 中合法）
    inline = json.dumps(payload, ensure_ascii=False).replace("</", "<\\/")

    html = template.replace(
        '<link rel="stylesheet" href="styles.css">', f"<style>\n{css}\n</style>"
    )
    html = html.replace(
        '<script src="data/latest.json" type="application/json" id="floweval-data"></script>',
        f'<script type="application/json" id="floweval-data">{inline}</script>',
    )
    html = html.replace('<script src="app.js"></script>', f"<script>\n{js}\n</script>")
    return html


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
