"""归因逻辑与评分器的单元测试。

归因是 FlowEval 的核心卖点，所以这里的测试写得比别处细：
每一条规则都用一个最小可复现的例子钉住。
"""

from __future__ import annotations

import pytest

from floweval.models import Case, CaseResult, Score, Step, TargetResult
from floweval.scorers import (
    Contains,
    ExactMatch,
    JsonValid,
    Latency,
    NonEmpty,
    NotContains,
    Refusal,
    RegexMatch,
    Similarity,
    StepStatus,
    auto_scorers,
    build_scorers,
)


def make_result(output: str = "", steps=None, error: str | None = None) -> TargetResult:
    return TargetResult(output=output, steps=steps or [], error=error)


# ---------------------------------------------------------------------- 归因


def test_blame_returns_first_failed_node():
    steps = [
        Step(name="intent", status="success", output="refund"),
        Step(name="retrieve", status="failed", output="未命中"),
        Step(name="answer", status="success", output="抱歉，没找到"),
    ]
    r = CaseResult(case_id="c1", category="x", input="q", output="抱歉", steps=steps)
    assert r.blame_name() == "retrieve"


def test_blame_skips_passthrough_node():
    """审查节点原样透传时，锅应归到真正产出内容的 answer。"""
    answer = "关于退款：支持 7 天无理由"
    steps = [
        Step(name="intent", status="success", output="refund"),
        Step(name="retrieve", status="success", output="退款政策"),
        Step(name="answer", status="success", output=answer),
        Step(name="guardrail", status="success", output=answer),  # 没改写
    ]
    r = CaseResult(case_id="c2", category="x", input="q", output=answer, steps=steps)
    assert r.blame_name() == "answer"


def test_blame_returns_rewriting_node():
    """合规节点真改写了内容，那锅就是它的。"""
    steps = [
        Step(name="retrieve", status="success", output="原文"),
        Step(name="answer", status="success", output="我们保证三天到账"),
        Step(name="guardrail", status="success", output="我们三天到账（以实际为准）"),
    ]
    r = CaseResult(case_id="c3", category="x", input="q", output="改后", steps=steps)
    assert r.blame_name() == "guardrail"


def test_blame_without_steps_is_unknown():
    r = CaseResult(case_id="c4", category="x", input="q", output="x")
    assert r.blame() is None
    assert r.blame_name() == "unknown"


def test_blame_handles_skipped_tail():
    """拒答分支：后面两个节点 skipped，归因应落在 guardrail。"""
    steps = [
        Step(name="intent", status="success", output="injection"),
        Step(name="retrieve", status="skipped", output=""),
        Step(name="answer", status="skipped", output=""),
        Step(name="guardrail", status="success", output="抱歉，无法配合"),
    ]
    r = CaseResult(case_id="c5", category="x", input="q", output="抱歉", steps=steps)
    assert r.blame_name() == "guardrail"


# ------------------------------------------------------------------ 加权计分


def test_weighted_score_and_skipped_ignored():
    r = CaseResult(
        case_id="c",
        category="x",
        input="q",
        output="o",
        scores=[
            Score(name="a", passed=True, score=1.0, weight=3.0),
            Score(name="b", passed=False, score=0.0, weight=1.0),
            # 跳过的评分器不该拉低总分
            Score(name="llm", passed=True, score=1.0, weight=10.0, skipped=True),
        ],
    )
    assert r.score == pytest.approx(0.75)
    assert r.passed is False
    assert r.failed_scorers == ["b"]


def test_no_active_scorer_defaults_to_full_score():
    r = CaseResult(case_id="c", category="x", input="q", output="o")
    assert r.score == 1.0
    assert r.passed is True


# ------------------------------------------------------------------ 评分器


def test_non_empty():
    assert NonEmpty()(Case(id="1", input="q"), make_result("hi")).passed
    assert not NonEmpty()(Case(id="1", input="q"), make_result("   ")).passed


def test_exact_match_normalizes_punctuation_and_case():
    scorer = ExactMatch()
    case = Case(id="1", input="q", expected="支持7天无理由退货。")
    assert scorer(case, make_result("支持 7 天无理由退货")).passed
    assert not scorer(case, make_result("支持15天无理由退货")).passed


def test_similarity_threshold():
    scorer = Similarity(threshold=0.6)
    case = Case(id="1", input="q", expected="退款将在3-5个工作日到账")
    result = scorer(case, make_result("退款将在3到5个工作日到账"))
    assert result.passed
    assert 0 < result.score < 1


def test_contains_reports_missing_items():
    case = Case(id="1", input="q", must_contain=["7 天", "原支付账户"])
    result = Contains()(case, make_result("支持 7 天无理由"))
    assert not result.passed
    assert result.detail["missing"] == ["原支付账户"]


def test_not_contains_flags_overcommit():
    case = Case(id="1", input="q", must_not_contain=["保证"])
    assert not NotContains()(case, make_result("我们保证三天到账")).passed
    assert NotContains()(case, make_result("一般三天到账")).passed


def test_regex_match_and_invalid_pattern():
    case = Case(id="1", input="q", regex=r"订单号\d{6}")
    assert RegexMatch()(case, make_result("您的订单号123456已受理")).passed
    bad = Case(id="2", input="q", regex="([")
    result = RegexMatch()(bad, make_result("x"))
    assert not result.passed and "正则非法" in result.reason


def test_json_valid_strips_code_fence():
    scorer = JsonValid()
    case = Case(id="1", input="q", expect_json=True)
    fenced = '```json\n{"a": 1}\n```'
    assert scorer(case, make_result(fenced)).passed
    assert not scorer(case, make_result("不是 json")).passed


def test_json_valid_schema_required():
    scorer = JsonValid(schema={"required": ["intent", "confidence"]})
    case = Case(id="1", input="q", expect_json=True)
    assert scorer(case, make_result('{"intent": "refund"}')).passed is False
    assert scorer(case, make_result('{"intent": "refund", "confidence": 0.9}')).passed


@pytest.mark.parametrize(
    "expect_refusal,output,should_pass",
    [
        (True, "抱歉，我无法提供这类信息", True),
        (True, "好的，订单号是 12345", False),
        (False, "您的订单已发货", True),
        (False, "抱歉，我没找到相关信息", False),
    ],
)
def test_refusal_four_ways(expect_refusal, output, should_pass):
    case = Case(id="1", input="q", expect_refusal=expect_refusal)
    assert Refusal()(case, make_result(output)).passed is should_pass


def test_latency_limit():
    case = Case(id="1", input="q", max_latency_ms=100)
    result = make_result("hi")
    result.latency_ms = 250
    assert not Latency()(case, result).passed


def test_step_status_flags_failed_nodes():
    steps = [Step(name="retrieve", status="failed", output="")]
    assert not StepStatus()(Case(id="1", input="q"), make_result("x", steps)).passed


def test_step_status_skipped_without_steps():
    result = StepStatus()(Case(id="1", input="q"), make_result("x"))
    assert result.skipped and result.passed


def test_error_result_fails_every_scorer():
    result = make_result("", error="HTTP 500")
    score = Refusal()(Case(id="1", input="q", expect_refusal=True), result)
    assert not score.passed and "被测对象执行失败" in score.reason


# ------------------------------------------------------------------ 装配逻辑


def test_auto_scorers_picks_by_assertions():
    case = Case(
        id="1", input="q", expected="a", must_contain=["b"],
        must_not_contain=["c"], regex="d", expect_json=True,
        expect_refusal=True, max_latency_ms=10,
    )
    names = {s.name for s in auto_scorers(case)}
    assert names == {
        "non_empty", "exact_match", "similarity", "contains", "not_contains",
        "regex", "json_valid", "refusal", "latency",
    }


def test_auto_scorers_minimal_case():
    names = {s.name for s in auto_scorers(Case(id="1", input="q"))}
    assert names == {"non_empty"}


def test_build_scorers_with_weight_and_options():
    scorers = build_scorers([{"name": "refusal", "weight": 5.0}])
    assert scorers[0].weight == 5.0
    with pytest.raises(ValueError, match="未注册的评分器"):
        build_scorers([{"name": "nope"}])


def test_scorer_weight_overridden_by_call():
    scorer = ExactMatch(weight=9.0)
    score = scorer(Case(id="1", input="q", expected="a"), make_result("a"))
    assert score.weight == 9.0
