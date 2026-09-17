"""被测对象（EvalTarget）协议与注册表。

FlowEval 的核心只认这一个接口：

    class EvalTarget(Protocol):
        def run(self, case_input: str) -> TargetResult: ...

其它一切 —— HTTP 服务、OpenAI 兼容接口、agentflow 工作流、本地函数 ——
都是"适配器"：把各自的调用方式翻译成 TargetResult。

这么做的原因很实际：被测对象的形态是不受控的（别人的服务、别的框架），
而评测逻辑必须保持稳定。中间加一层协议，两边就解耦了。
新增一种被测对象 = 新增一个适配器文件，核心代码一行不用改。
"""

from __future__ import annotations

import time
from typing import Any, Callable, Protocol, runtime_checkable

from ..models import TargetResult


@runtime_checkable
class EvalTarget(Protocol):
    """被测对象协议。只要有 run(case_input) -> TargetResult 就满足。"""

    name: str

    def run(self, case_input: str) -> TargetResult: ...


# ------------------------------------------------------------------ 基类工具


class BaseTarget:
    """适配器基类：负责计时、异常兜底、命名。

    子类只需实现 `_call()` 返回 TargetResult（或纯字符串）。
    计时和异常捕获在这里统一做，避免每个适配器都写一遍 —— 也避免
    某个适配器忘了计时导致延迟数据缺失。
    """

    name: str = "base"

    def run(self, case_input: str) -> TargetResult:
        start = time.perf_counter()
        try:
            result = self._call(case_input)
            if isinstance(result, str):
                result = TargetResult(output=result)
        except Exception as exc:  # noqa: BLE001 - 被测对象的异常不该炸掉整轮评测
            result = TargetResult(
                output="",
                error=f"{type(exc).__name__}: {exc}",
            )
        elapsed = (time.perf_counter() - start) * 1000
        if result.latency_ms <= 0:
            result.latency_ms = elapsed
        return result

    def _call(self, case_input: str) -> TargetResult | str:
        raise NotImplementedError

    def __repr__(self) -> str:  # pragma: no cover - 调试用途
        return f"<{type(self).__name__} name={self.name!r}>"


class FunctionTarget(BaseTarget):
    """把一个普通 Python 函数/可调用包成被测对象。

    支持三种返回：
      - str                → 当作最终输出
      - TargetResult       → 原样使用
      - dict(output=..., steps=[...]) → 转成 TargetResult

    这是写 demo 和单测最快的入口。
    """

    def __init__(self, fn: Callable[[str], Any], name: str | None = None):
        self._fn = fn
        self.name = name or getattr(fn, "__name__", "function")

    def _call(self, case_input: str) -> TargetResult | str:
        out = self._fn(case_input)
        if isinstance(out, TargetResult):
            return out
        if isinstance(out, str):
            return out
        if isinstance(out, dict):
            return TargetResult(
                output=str(out.get("output", "")),
                steps=out.get("steps") or [],
                tokens=int(out.get("tokens", 0)),
                cost=float(out.get("cost", 0.0)),
                raw=out.get("raw"),
            )
        return str(out)


# ------------------------------------------------------------------ 注册表

_REGISTRY: dict[str, type[BaseTarget]] = {}


def register(name: str):
    """把适配器类注册进工厂，供 `floweval run --target <name>` 使用。"""

    def deco(cls: type[BaseTarget]) -> type[BaseTarget]:
        _REGISTRY[name] = cls
        cls.name = name
        return cls

    return deco


def available_targets() -> list[str]:
    return sorted(_REGISTRY)


def get_target_class(name: str) -> type[BaseTarget]:
    if name not in _REGISTRY:
        raise KeyError(
            f"未注册的被测对象类型: {name!r}，可用: {available_targets()}"
        )
    return _REGISTRY[name]
