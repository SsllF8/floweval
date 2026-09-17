"""OpenAI 兼容接口适配器。

适用：DeepSeek / 通义 / 智谱 / vLLM / Ollama 等所有遵循
`POST /chat/completions` 协议的服务。

没有配 API key 时不报错，而是在 run() 时返回带 error 的 TargetResult ——
这样批量评测不会因为有两条用例没配置就整轮崩掉。
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any

from ..models import Step, TargetResult
from .base import BaseTarget, register
from .http import dig


@register("openai")
class OpenAIChatTarget(BaseTarget):
    """评测一个对话模型。"""

    name = "openai"

    def __init__(
        self,
        model: str,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        system_prompt: str | None = None,
        temperature: float = 0.0,
        max_tokens: int = 1024,
        timeout: float = 60.0,
        name: str | None = None,
    ):
        self.model = model
        self.api_key = api_key or os.environ.get("OPENAI_API_KEY") or os.environ.get("DEEPSEEK_API_KEY")
        self.base_url = (
            base_url
            or os.environ.get("OPENAI_BASE_URL")
            or "https://api.deepseek.com/v1"
        ).rstrip("/")
        self.system_prompt = system_prompt
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.timeout = timeout
        self.name = name or f"openai:{model}"

    @property
    def configured(self) -> bool:
        return bool(self.api_key)

    def _call(self, case_input: str) -> TargetResult:
        if not self.configured:
            return TargetResult(
                output="",
                error="未配置 API key（设置 OPENAI_API_KEY 或 DEEPSEEK_API_KEY）",
            )

        messages: list[dict[str, str]] = []
        if self.system_prompt:
            messages.append({"role": "system", "content": self.system_prompt})
        messages.append({"role": "user", "content": case_input})

        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
        }
        req = urllib.request.Request(  # noqa: S310
            f"{self.base_url}/chat/completions",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
            },
            method="POST",
        )

        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:  # noqa: S310
                raw = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:500]
            return TargetResult(output="", error=f"HTTP {exc.code}: {detail}")
        except Exception as exc:  # noqa: BLE001
            return TargetResult(output="", error=f"{type(exc).__name__}: {exc}")

        content = dig(raw, "choices.0.message.content", "") or ""
        usage = raw.get("usage") or {}
        cost = _estimate_cost(self.model, usage)

        # 单节点模型没有中间过程，但保留一个 step，保证归因逻辑不退化
        steps = [
            Step(
                name="llm_call",
                status="success",
                output=content,
                meta={"model": self.model, "finish_reason": dig(raw, "choices.0.finish_reason")},
            )
        ]
        return TargetResult(
            output=content,
            steps=steps,
            tokens=int(usage.get("total_tokens") or 0),
            cost=cost,
            raw=raw,
        )


# 每百万 token 单价（人民币估算，仅用于成本观测，不保证与账单一致）
_PRICE_PER_MTOK = {
    "deepseek-chat": (2.0, 8.0),
    "deepseek-reasoner": (4.0, 16.0),
    "gpt-4o-mini": (1.1, 4.4),
}


def _estimate_cost(model: str, usage: dict[str, Any]) -> float:
    pin, pout = _PRICE_PER_MTOK.get(model, (2.0, 8.0))
    tin = int(usage.get("prompt_tokens") or 0)
    tout = int(usage.get("completion_tokens") or 0)
    return round((tin * pin + tout * pout) / 1_000_000, 6)
