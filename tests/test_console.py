"""控制台相关的新逻辑：配置 schema 规范化 + 进度回调。"""

from __future__ import annotations

import pytest

from floweval.runner import EvalRunner
from floweval.targets.customer_service import CustomerServiceTarget
from floweval.targets.factory import TARGET_SCHEMAS, normalize_config


# ---------------------------------------------------------------- schema


def test_every_schema_field_has_required_keys():
    for kind, schema in TARGET_SCHEMAS.items():
        assert schema["label"], kind
        for f in schema["fields"]:
            assert f["key"] and f["label"] and f["type"], (kind, f)


def test_normalize_config_coerces_numbers():
    out = normalize_config("http", {"timeout": "15", "method": "POST"})
    assert out["timeout"] == 15


def test_normalize_config_parses_json_looking_fields():
    out = normalize_config("http", {"body_template": '{"q": "{input}"}'})
    assert out["body_template"] == {"q": "{input}"}


def test_normalize_config_rejects_bad_json():
    with pytest.raises(ValueError, match="body_template"):
        normalize_config("http", {"body_template": "{broken"})


def test_normalize_config_drops_empty_and_keeps_unknown():
    out = normalize_config(
        "openai",
        {"api_key": "", "temperature": "0", "custom_flag": True},
    )
    # 空 api_key 不进配置（走环境变量），temperature 转成数字，未知字段透传
    assert "api_key" not in out
    assert out["temperature"] == 0
    assert out["custom_flag"] is True


def test_normalize_config_unknown_kind_passthrough():
    assert normalize_config("nope", {"a": 1}) == {"a": 1}


def test_normalize_config_feeds_build_target():
    from floweval.targets.factory import build_target

    target = build_target("http", normalize_config("http", {"url": "http://x/y", "timeout": "5"}))
    assert target.timeout == 5


# ---------------------------------------------------------------- 进度回调


def test_runner_reports_progress_for_every_case():
    target = CustomerServiceTarget()
    raw = [
        {"id": f"c{i}", "input": "怎么退款", "must_contain": ["退款"]} for i in range(5)
    ]
    from floweval.dataset import load_cases_from

    cases = load_cases_from(raw)
    ticks: list[tuple[int, int]] = []
    run = EvalRunner(target, max_workers=3).run(
        cases, dataset="t", on_progress=lambda d, t: ticks.append((d, t))
    )
    assert run.total == 5
    # 每条用例恰好回调一次，最后一次是 (5, 5)
    assert len(ticks) == 5
    assert ticks[-1] == (5, 5)
    # done 单调递增
    dones = [d for d, _ in ticks]
    assert dones == sorted(dones)


def test_runner_works_without_progress_callback():
    from floweval.dataset import load_cases_from

    run = EvalRunner(CustomerServiceTarget()).run(
        load_cases_from([{"id": "c1", "input": "怎么退款", "must_contain": ["退款"]}]),
        dataset="t",
    )
    assert run.total == 1
