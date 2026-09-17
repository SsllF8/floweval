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


def _field(
    key: str,
    label: str,
    *,
    type_: str = "text",
    default: Any = "",
    required: bool = False,
    help_: str = "",
    options: list[Any] | None = None,
    placeholder: str = "",
) -> dict[str, Any]:
    """一个配置字段的声明。前端按 type 渲染控件。"""
    return {
        "key": key,
        "label": label,
        "type": type_,
        "default": default,
        "required": required,
        "help": help_,
        "options": options or [],
        "placeholder": placeholder,
    }


# 每种被测对象类型需要哪些配置。
#
# 为什么要有这层声明而不是让前端自己写死：
# 新增一种被测对象时，只要在这里加一份 schema，网页表单、CLI 提示、README
# 示例就都齐了 —— 不写死意味着不用改前端就能扩展。
TARGET_SCHEMAS: dict[str, dict[str, Any]] = {
    "customer_service": {
        "label": "内置客服 Agent",
        "description": "四节点客服流水线（意图→检索→生成→审查），内置可注入缺陷，零配置即可跑通。",
        "badge": "推荐上手",
        "fields": [
            _field(
                "version",
                "版本",
                type_="select",
                default="v1",
                options=["v1", "v2", "v3"],
                help_="v1 = 完整知识库（基线）；v2 = 误删发票/保修条目（事故版）；v3 = 检索 top_k 调小。",
            ),
        ],
    },
    "http": {
        "label": "HTTP 服务",
        "description": "任意 HTTP 接口。请求体用 {input} 占位，评测时替换成用例输入。",
        "fields": [
            _field("url", "接口地址", required=True, placeholder="http://localhost:8000/ask",
                   help_="必须 http:// 或 https:// 开头"),
            _field("method", "请求方法", type_="select", default="POST", options=["POST", "GET", "PUT"]),
            _field("body_template", "请求体模板", type_="textarea", default='{"query": "{input}"}',
                   help_="JSON 字符串，{input} 会被替换成用例输入"),
            _field("output_path", "输出取值路径", placeholder="choices.0.message.content",
                   help_="返回 JSON 时，指出最终文本在哪；留空则整段 JSON 作为输出"),
            _field("headers", "额外请求头", type_="textarea", placeholder='{"Authorization": "Bearer xxx"}',
                   help_="JSON 对象，可留空"),
            _field("timeout", "超时（秒）", type_="number", default=30),
        ],
    },
    "openai": {
        "label": "OpenAI 兼容模型",
        "description": "DeepSeek / 通义 / 智谱 / vLLM / Ollama 等所有 /chat/completions 协议的服务。",
        "fields": [
            _field("model", "模型名", required=True, default="deepseek-chat",
                   placeholder="deepseek-chat"),
            _field("base_url", "接口地址", default="https://api.deepseek.com/v1",
                   help_="以 /v1 结尾，程序会自动拼 /chat/completions"),
            _field("api_key", "API Key", type_="password",
                   help_="留空则读取环境变量 OPENAI_API_KEY / DEEPSEEK_API_KEY；没配 key 时用例会标为 error 而不是整轮崩掉"),
            _field("system_prompt", "System Prompt", type_="textarea", placeholder="你是一个客服助手…"),
            _field("temperature", "温度", type_="number", default=0,
                   help_="评测建议设 0，结果才可复现"),
            _field("max_tokens", "最大 token", type_="number", default=1024),
            _field("timeout", "超时（秒）", type_="number", default=60),
        ],
    },
    "agentflow": {
        "label": "agentflow 工作流",
        "description": "把 agentflow 的 node_records 映射成节点级归因数据。需先设置 AGENTFLOW_PATH。",
        "fields": [
            _field("workflow", "工作流", required=True, type_="textarea",
                   placeholder='工作流 JSON 文件路径，或直接粘贴 JSON',
                   help_="文件路径或 JSON 字典"),
            _field("agentflow_path", "agentflow 项目目录",
                   help_="留空则读环境变量 AGENTFLOW_PATH"),
            _field("output_node", "输出节点名", help_="留空取最后一个节点"),
            _field("parallel", "并行执行", type_="select", default="true", options=["true", "false"]),
            _field("max_workers", "并发数", type_="number", default=4),
        ],
    },
}


def _coerce(field: dict[str, Any], raw: Any) -> Any:
    """把表单里的字符串值转成目标类型。

    前端一律传字符串（除了 checkbox 传布尔），这里统一收口。
    """
    kind = field["type"]
    if raw is None or raw == "":
        return None if kind in {"number"} and field.get("default") in (None, "") else field["default"]
    if kind == "number":
        try:
            value = float(raw)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"字段 {field['key']} 需要数字，收到 {raw!r}") from exc
        return int(value) if value.is_integer() else value
    if kind == "select":
        value = str(raw)
        if value in {"true", "false"}:
            return value == "true"
        return value
    if kind in {"textarea", "text"}:
        text = str(raw).strip()
        # 看起来像 JSON 的字段（请求体、请求头、工作流）自动解析，
        # 否则 target 收到的是字符串，行为会和 CLI 传 JSON 时不一致。
        if field["key"] in {"body_template", "headers", "workflow"} and text.startswith(("{", "[")):
            try:
                return json.loads(text)
            except json.JSONDecodeError as exc:
                raise ValueError(f"字段 {field['key']} 不是合法 JSON: {exc}") from exc
        return text
    return raw


def normalize_config(kind: str, raw: dict[str, Any] | None) -> dict[str, Any]:
    """按 schema 把前端传来的表单值规范化成 build_target 能吃的配置。"""
    schema = TARGET_SCHEMAS.get(kind)
    if schema is None:
        return dict(raw or {})

    out: dict[str, Any] = {}
    for field in schema["fields"]:
        if field["key"] not in (raw or {}):
            continue
        value = _coerce(field, raw[field["key"]])
        if value is None or value == "":
            continue  # 空值交给被测对象自己的默认值
        out[field["key"]] = value

    # 未知字段原样透传，schema 没覆盖到的高级用法不至于被吞掉
    for key, value in (raw or {}).items():
        if key not in {f["key"] for f in schema["fields"]}:
            out[key] = value
    return out


__all__ = [
    "build_target",
    "load_config",
    "available_targets",
    "TARGET_SCHEMAS",
    "normalize_config",
]
