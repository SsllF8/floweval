"""被测对象适配器集合。

新增一种被测对象 = 在这里加一个文件 + 一行 import，核心代码不用动。
"""

from .base import (
    BaseTarget,
    EvalTarget,
    FunctionTarget,
    available_targets,
    get_target_class,
    register,
)
from .customer_service import (
    FALLBACK_ANSWER,
    KB_V1,
    KB_V2,
    AgentConfig,
    CustomerServiceTarget,
    make_target,
)
from .http import HTTPTarget, dig
from .openai_compat import OpenAIChatTarget

__all__ = [
    "BaseTarget",
    "EvalTarget",
    "FunctionTarget",
    "register",
    "available_targets",
    "get_target_class",
    "CustomerServiceTarget",
    "AgentConfig",
    "make_target",
    "KB_V1",
    "KB_V2",
    "FALLBACK_ANSWER",
    "HTTPTarget",
    "dig",
    "OpenAIChatTarget",
]


def _lazy_agentflow():
    """agentflow 适配器按需导入：没装 agentflow 时不影响其它适配器。"""
    try:
        from . import agentflow as _mod  # noqa: PLC0415

        return _mod.AgentflowTarget
    except Exception:  # noqa: BLE001 - agentflow 不在或缺失依赖
        return None


AgentflowTarget = _lazy_agentflow()
if AgentflowTarget is not None:
    __all__.append("AgentflowTarget")
