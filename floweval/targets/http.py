"""HTTP 适配器 —— 评测任意 HTTP 服务。

零第三方依赖（用标准库 urllib），保证 FlowEval 装上就能跑。
如果被测服务返回 JSON，用 `output_path` 指出最终文本在哪，例如：
    output_path = "choices.0.message.content"
    output_path = "data.answer"
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from typing import Any

from ..models import TargetResult
from .base import BaseTarget, register


def dig(data: Any, path: str, default: Any = None) -> Any:
    """按 a.b.0.c 形式从嵌套结构里取值。"""
    cur = data
    for part in path.split("."):
        if cur is None:
            return default
        if isinstance(cur, list):
            try:
                cur = cur[int(part)]
            except (ValueError, IndexError):
                return default
        elif isinstance(cur, dict):
            if part not in cur:
                return default
            cur = cur[part]
        else:
            return default
    return cur


@register("http")
class HTTPTarget(BaseTarget):
    """把一次 HTTP 调用包成被测对象。

    body_template 里用 `{input}` 占位，会被替换成用例输入，例如：
        {"query": "{input}"}
    """

    name = "http"

    def __init__(
        self,
        url: str,
        *,
        method: str = "POST",
        body_template: dict[str, Any] | str | None = None,
        headers: dict[str, str] | None = None,
        output_path: str | None = None,
        timeout: float = 30.0,
        name: str | None = None,
    ):
        if not url.lower().startswith(("http://", "https://")):
            raise ValueError(f"只支持 http/https，收到: {url!r}")
        self.url = url
        self.method = method.upper()
        self.body_template = body_template if body_template is not None else {"input": "{input}"}
        self.headers = {"Content-Type": "application/json", **(headers or {})}
        self.output_path = output_path
        self.timeout = timeout
        self.name = name or f"http:{url}"

    def _call(self, case_input: str) -> TargetResult:
        payload = self._render(case_input)
        if isinstance(payload, (dict, list)):
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        else:
            data = str(payload).encode("utf-8")

        req = urllib.request.Request(  # noqa: S310 - URL 已在构造时校验
            self.url, data=data, headers=self.headers, method=self.method
        )
        start = time.perf_counter()
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:  # noqa: S310
                raw_text = resp.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:500]
            raise RuntimeError(f"HTTP {exc.code}: {detail}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"请求失败: {exc.reason}") from exc
        elapsed_ms = (time.perf_counter() - start) * 1000

        output = raw_text
        parsed: Any = None
        try:
            parsed = json.loads(raw_text)
        except json.JSONDecodeError:
            parsed = None

        if parsed is not None:
            if self.output_path:
                found = dig(parsed, self.output_path)
                output = "" if found is None else str(found)
            else:
                output = json.dumps(parsed, ensure_ascii=False)

        return TargetResult(
            output=output,
            latency_ms=round(elapsed_ms, 2),
            raw=parsed if parsed is not None else raw_text,
        )

    def _render(self, case_input: str) -> Any:
        if isinstance(self.body_template, str):
            return self.body_template.replace("{input}", case_input)
        return _deep_format(self.body_template, case_input)


def _deep_format(obj: Any, value: str) -> Any:
    if isinstance(obj, dict):
        return {k: _deep_format(v, value) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_deep_format(v, value) for v in obj]
    if isinstance(obj, str):
        return obj.replace("{input}", value)
    return obj
