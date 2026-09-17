"""从配置构造被测对象。

把"配置字典 → 被测对象实例"这一步独立出来，CLI 和 Web 看板共用同一份逻辑，
避免两边各写一套解析导致行为不一致。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .base import BaseTarget, available_targets, get_target_class
from .customer_service import AgentConfig, CustomerServiceTarget, make_target
from .http import HTTPTarget
from .openai_compat import OpenAIChatTarget


def build_target(kind: str, config: dict[str, Any] | None = None) -> BaseTarget:
    """按类型名 + 配置构造被测对象。

    kind 支持：customer_service / http / openai / agentflow / 任何 register 过的名字
    """
    config = dict(config or {})

    if kind in {"customer_service", "cs", "demo"}:
        version = config.pop("version", None)
        if version is not None:
            return make_target(str(version), **config)
        return CustomerServiceTarget(AgentConfig(**config))

    if kind == "http":
        url = config.pop("url", None)
        if not url:
            raise ValueError("http 类型必须提供 url")
        return HTTPTarget(url, **config)

    if kind in {"openai", "llm", "chat"}:
        model = config.pop("model", None) or config.pop("model_name", None)
        if not model:
            raise ValueError("openai 类型必须提供 model")
        return OpenAIChatTarget(model, **config)

    if kind == "agentflow":
        from .agentflow import AgentflowTarget  # noqa: PLC0415 - 延迟导入

        workflow = config.pop("workflow", None)
        if not workflow:
            raise ValueError("agentflow 类型必须提供 workflow（工作流 JSON 路径或字典）")
        return AgentflowTarget(workflow, **config)

    cls = get_target_class(kind)
    return cls(**config)


def load_config(value: str | None) -> dict[str, Any]:
    """配置可以是内联 JSON，也可以是 JSON 文件路径。"""
    if not value:
        return {}
    path = Path(value)
    if path.exists() and path.is_file():
        return json.loads(path.read_text(encoding="utf-8"))
    try:
        data = json.loads(value)
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"--target-config 既不是存在的 JSON 文件，也不是合法 JSON: {value}"
        ) from exc
    if not isinstance(data, dict):
        raise ValueError(f"--target-config 必须是 JSON 对象: {value}")
    return data


__all__ = ["build_target", "load_config", "available_targets"]
