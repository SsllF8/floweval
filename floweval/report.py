"""报告渲染：终端文本 + Markdown。

终端报告的目标不是好看，是**三秒内说清三件事**：
  1. 总体能不能发
  2. 挂了的用例是谁
  3. 锅在哪个节点（节点级归因，FlowEval 的核心价值）
"""

from __future__ import annotations

from typing import Any

from .gate import GateResult
from .models import EvalRun
from .regression import RegressionReport

# 终端里用 ASCII 表格线，避免 Windows 控制台编码炸掉
SEP = "─" * 78


def _pad(text: str, width: int) -> str:
    """按显示宽度补齐（中日韩字符按 2 格算）。"""
    size = sum(2 if ord(ch) > 0x2E80 else 1 for ch in text)
    return text + " " * max(0, width - size)


def _truncate(text: str, limit: int) -> str:
    text = text.replace("\n", " ").strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def render_summary(run: EvalRun) -> str:
    lines: list[str] = []
    s = run.to_dict()["summary"]
    lines.append(SEP)
    lines.append(f"FlowEval 评测报告  ·  {run.target_name}")
    lines.append(SEP)
    lines.append(f"run_id    {run.run_id}")
    lines.append(f"数据集    {run.dataset}  ({run.total} 条)")
    lines.append(f"时间      {run.started_at}   耗时 {run.elapsed_ms:.0f}ms")
    lines.append("")
    lines.append(
        f"通过率    {s['pass_rate']:.1%}  ({s['passed']}/{s['total']} 通过, {s['failed']} 失败)"
    )
    lines.append(f"平均分    {s['mean_score']:.3f}")
    lines.append(f"P95 延迟  {s['p95_latency_ms']:.0f} ms")
    lines.append(f"成本      {s['total_cost']:.6f}  ({s['total_tokens']} tokens)")
    return "\n".join(lines)


def render_categories(run: EvalRun) -> str:
    data = run.by_category()
    if not data:
        return ""
    lines = ["", "按类别", SEP]
    lines.append(f"{_pad('类别', 20)}{_pad('用例', 8)}{_pad('通过率', 12)}平均分")
    for cat, stat in data.items():
        rate = "{:.1%}".format(stat["pass_rate"])
        lines.append(
            f"{_pad(cat, 20)}{_pad(str(stat['total']), 8)}"
            f"{_pad(rate, 12)}"
            f"{stat['mean_score']:.3f}"
        )
    return "\n".join(lines)


def render_failures(run: EvalRun, limit: int = 10) -> str:
    failed = [r for r in run.results if not r.passed]
    if not failed:
        return "\n全部通过。"

    shown = failed[:limit]
    lines = ["", f"失败用例（{len(failed)} 条，显示前 {len(shown)} 条）", SEP]
    for r in shown:
        lines.append(f"[{r.case_id}] ({r.category}) {_truncate(r.input, 60)}")
        if r.error:
            lines.append(f"    执行错误: {_truncate(r.error, 70)}")
        else:
            names = ", ".join(r.failed_scorers)
            lines.append(f"    未通过: {names}")
        lines.append(f"    归因节点: {r.blame_name()}")
        lines.append(f"    实际输出: {_truncate(r.output, 70)}")
        if r.expected:
            lines.append(f"    期望输出: {_truncate(r.expected, 70)}")
        lines.append("")
    if len(failed) > limit:
        lines.append(f"... 还有 {len(failed) - limit} 条失败用例")
    return "\n".join(lines).rstrip()


def render_attribution(run: EvalRun) -> str:
    """节点级归因分布 —— 整份报告里最该看的一段。"""
    dist = run.blame_distribution()
    lines = ["", "失败归因（按节点）", SEP]
    if not dist:
        lines.append("无失败用例，无需归因。")
        return "\n".join(lines)

    total_fail = sum(dist.values())
    for node, count in dist.items():
        bar = "█" * max(1, round(count / max(1, max(dist.values())) * 28))
        lines.append(f"{_pad(node, 18)}{_pad(str(count), 6)}{_pad(f'{count/total_fail:.0%}', 8)}{bar}")
    lines.append("")
    lines.append(
        "归因规则：节点执行失败取第一个失败节点；节点都成功时，"
        "取最后一个真正改变了输出的节点（原样透传的审查节点不背锅）。"
    )
    return "\n".join(lines)


def render_nodes(run: EvalRun) -> str:
    stats = run.node_stats()
    if not stats:
        return ""
    lines = ["", "节点耗时 / 失败率", SEP]
    lines.append(f"{_pad('节点', 18)}{_pad('调用', 8)}{_pad('平均ms', 10)}{_pad('最大ms', 10)}失败率")
    for name, s in stats.items():
        lines.append(
            f"{_pad(name, 18)}{_pad(str(s['calls']), 8)}{_pad(str(s['avg_ms']), 10)}"
            f"{_pad(str(s['max_ms']), 10)}{s['fail_rate']:.1%}"
        )
    return "\n".join(lines)


def render_gate(gate: GateResult) -> str:
    lines = ["", "上线判定", SEP]
    verdict = "PASS  可以发布" if gate.ok else "FAIL  不允许发布"
    lines.append(verdict)
    lines.append("")
    for rule in gate.rules:
        mark = "✓" if rule.passed else "✗"
        lines.append(f"  {mark} {_pad(rule.name, 20)}{rule.message}")
    return "\n".join(lines)


def render_regression(report: RegressionReport) -> str:
    lines = ["", "回归对比", SEP]
    lines.append(
        f"基线 {report.baseline_run_id}  →  当前 {report.current_run_id}"
    )
    lines.append(
        f"退化 {report.regression_count} 条 / 改进 {report.improvement_count} 条 "
        f"(净 {report.net:+d})"
    )
    if report.new_cases:
        lines.append(f"新增用例 {len(report.new_cases)} 条: {', '.join(report.new_cases[:8])}")
    if report.removed_cases:
        lines.append(f"移除用例 {len(report.removed_cases)} 条: {', '.join(report.removed_cases[:8])}")

    if report.regressions:
        blame = report.blame_distribution()
        lines.append("")
        lines.append(f"退化归因: {blame}")
        lines.append("")
        for d in report.regressions[:8]:
            lines.append(f"  ↓ [{d.case_id}] ({d.category}) {_truncate(d.input, 52)}")
            lines.append(f"      分数 {d.baseline_score:.2f} → {d.current_score:.2f}  "
                         f"归因 {d.baseline_blame or '-'} → {d.current_blame or '-'}")
    return "\n".join(lines)


def render_text(
    run: EvalRun,
    *,
    gate: GateResult | None = None,
    regression: RegressionReport | None = None,
    show_nodes: bool = True,
) -> str:
    parts = [render_summary(run), render_categories(run)]
    if show_nodes:
        parts.append(render_nodes(run))
    parts.append(render_attribution(run))
    parts.append(render_failures(run))
    if regression is not None:
        parts.append(render_regression(regression))
    if gate is not None:
        parts.append(render_gate(gate))
    parts.append(SEP)
    return "\n".join(parts)


# ------------------------------------------------------------------ Markdown


def render_markdown(
    run: EvalRun,
    *,
    gate: GateResult | None = None,
    regression: RegressionReport | None = None,
) -> str:
    s = run.to_dict()["summary"]
    lines = [
        f"# FlowEval 评测报告 — {run.target_name}",
        "",
        f"- run_id: `{run.run_id}`",
        f"- 数据集: `{run.dataset}`（{run.total} 条）",
        f"- 时间: {run.started_at}",
        f"- 通过率: **{s['pass_rate']:.1%}**（{s['passed']}/{s['total']}）",
        f"- 平均分: {s['mean_score']:.3f}",
        f"- P95 延迟: {s['p95_latency_ms']:.0f} ms",
        f"- 成本: {s['total_cost']:.6f}",
        "",
        "## 失败归因",
        "",
        "| 节点 | 失败数 | 占比 |",
        "|---|---|---|",
    ]
    dist = run.blame_distribution()
    total_fail = sum(dist.values()) or 1
    for node, count in dist.items():
        lines.append(f"| {node} | {count} | {count/total_fail:.0%} |")
    if not dist:
        lines.append("| — | 0 | 0% |")

    lines += ["", "## 分类表现", "", "| 类别 | 用例 | 通过率 | 平均分 |", "|---|---|---|---|"]
    for cat, stat in run.by_category().items():
        lines.append(
            f"| {cat} | {stat['total']} | {stat['pass_rate']:.1%} | {stat['mean_score']:.3f} |"
        )

    failed = [r for r in run.results if not r.passed]
    if failed:
        lines += ["", "## 失败用例", "", "| 用例 | 类别 | 归因节点 | 未通过评分器 |", "|---|---|---|---|"]
        for r in failed:
            lines.append(
                f"| {r.case_id} | {r.category} | {r.blame_name()} | "
                f"{', '.join(r.failed_scorers) or r.error or '-'} |"
            )

    if regression is not None:
        lines += [
            "", "## 回归对比", "",
            f"- 退化 {regression.regression_count} 条，改进 {regression.improvement_count} 条",
        ]
        for d in regression.regressions:
            lines.append(
                f"- `{d.case_id}` {d.baseline_score:.2f} → {d.current_score:.2f} "
                f"（归因 {d.current_blame}）"
            )

    if gate is not None:
        lines += ["", "## 上线判定", ""]
        lines.append("**PASS**" if gate.ok else "**FAIL**")
        lines.append("")
        lines.append("| 规则 | 结果 | 说明 |")
        lines.append("|---|---|---|")
        for rule in gate.rules:
            lines.append(
                f"| {rule.name} | {'✓' if rule.passed else '✗'} | {rule.message} |"
            )

    return "\n".join(lines) + "\n"


def build_dashboard_payload(
    run: EvalRun,
    *,
    gate: GateResult | None = None,
    regression: RegressionReport | None = None,
    history: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Web 看板吃的数据结构。

    看板不做计算，只做渲染 —— 所有指标在这里算好，
    保证终端报告和网页看到的数字是同一份。
    """
    payload: dict[str, Any] = run.to_dict()
    payload["gate"] = gate.to_dict() if gate else None
    payload["regression"] = regression.to_dict() if regression else None
    payload["history"] = history or []
    return payload
