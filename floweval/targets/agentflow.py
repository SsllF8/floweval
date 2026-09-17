"""agentflow 适配器 —— 把 agentflow 工作流翻译成 FlowEval 的 TargetResult。

FlowEval 核心不依赖 agentflow：这里用**路径注入 + 延迟导入**，
agentflow 不在时本模块依然可以被导入，只是运行时报错并给出明确提示。

一个真实的坑，写在最前面：
    agentflow 支持断点续跑，同一个 run_id 重跑会**跳过已成功的节点**。
    这在评测场景是灾难 —— 第二条用例会直接复用第一条的结果。
    所以适配器默认每条用例生成新的 run_id（见 `unique_run_id`）。
"""

from __future__ import annotations

import json
import os
import sys
import uuid
from pathlib import Path
from typing import Any

from ..models import Step, TargetResult
from .base import BaseTarget, register

# 通过环境变量指定 agentflow 项目目录（避免在源码里写死任何机器路径）
DEFAULT_AGENTFLOW_PATH = os.environ.get("AGENTFLOW_PATH")


def _import_agentflow(path: str | None = None):
    """动态导入 agentflow 的 core 包。

    不写成顶层 `import core` 的原因：agentflow 是另一个独立项目，
    不该变成 FlowEval 的硬依赖。
    """
    resolved = path or DEFAULT_AGENTFLOW_PATH
    if not resolved:
        raise ImportError(
            "未找到 agentflow 项目目录。两种方式指定：\n"
            "  1. 设置环境变量 AGENTFLOW_PATH 指向 agentflow 项目根\n"
            "  2. AgentflowTarget(workflow, agentflow_path=r'...') 传参"
        )
    base = Path(resolved).resolve()
    if not base.exists():
        raise ImportError(f"找不到 agentflow 目录: {base}")
    if str(base) not in sys.path:
        sys.path.insert(0, str(base))
    import core  # type: ignore  # noqa: PLC0415

    return core


@register("agentflow")
class AgentflowTarget(BaseTarget):
    """评测一个 agentflow 工作流。

    用法：
        target = AgentflowTarget.from_file("workflow.json")
        result = target.run("用户输入")

    映射关系：
        RunResult.node_records → TargetResult.steps   （节点级归因的数据来源）
        RunResult.output       → TargetResult.output
        NodeRunRecord.elapsed(s) → Step.elapsed_ms    （单位换算别忘）
    """

    name = "agentflow"

    def __init__(
        self,
        workflow: dict[str, Any] | str | Path,
        *,
        agentflow_path: str | None = None,
        name: str | None = None,
        max_workers: int = 4,
        parallel: bool = True,
        unique_run_id: bool = True,
        output_node: str | None = None,
    ):
        self._core = _import_agentflow(agentflow_path)
        resolved = agentflow_path or DEFAULT_AGENTFLOW_PATH
        self.agentflow_path = str(Path(resolved).resolve()) if resolved else ""

        if isinstance(workflow, (str, Path)):
            workflow = json.loads(Path(workflow).read_text(encoding="utf-8"))

        self.workflow_def = self._core.WorkflowDef.from_dict(workflow)
        self.workflow_def.validate()
        self.name = name or f"agentflow:{self.workflow_def.name}"

        # 优先用工作流里用户给节点起的名字（"转大写"），
        # 而不是节点类的通用名（"数据转换"）—— 归因报告要能一眼看懂。
        self._node_names: dict[str, str] = {
            n.id: n.name for n in self.workflow_def.nodes if n.name
        }

        self.max_workers = max_workers
        self.parallel = parallel
        self.unique_run_id = unique_run_id
        self.output_node = output_node

        self.engine = self._core.WorkflowEngine(
            self.workflow_def,
            store=self._core.InMemoryStore(),
            max_workers=max_workers,
            parallel=parallel,
        )

    # ---------------- 构造快捷方式

    @classmethod
    def from_file(cls, path: str | Path, **kwargs: object) -> "AgentflowTarget":
        return cls(Path(path), **kwargs)  # type: ignore[arg-type]

    # ---------------- 适配

    def _call(self, case_input: str) -> TargetResult:
        run_id = uuid.uuid4().hex[:12] if self.unique_run_id else None
        result = self.engine.run(case_input, run_id=run_id, resume=False)

        steps = [
            Step(
                name=self._node_names.get(rec.node_id) or rec.node_name or rec.node_id,
                status=self._map_status(rec.status),
                output=rec.output or "",
                elapsed_ms=round(rec.elapsed * 1000, 2),  # 秒 → 毫秒
                meta={
                    "node_id": rec.node_id,
                    "node_type": rec.node_type,
                    "builtin_name": rec.node_name,
                    "level": rec.level,
                    **({"error": rec.error} if rec.error else {}),
                },
            )
            for rec in result.node_records
        ]

        output = self._pick_output(result)
        return TargetResult(
            output=output,
            steps=steps,
            error=result.error or None,
            latency_ms=round(sum(s.elapsed_ms for s in steps), 2),
            raw=result,
        )

    # ---------------- 工具

    def _pick_output(self, result: Any) -> str:
        """默认用引擎的最终输出，也可以指定某个节点的输出作为评测对象。"""
        if not self.output_node:
            return result.output or ""
        for rec in result.node_records:
            if rec.node_id == self.output_node or rec.node_name == self.output_node:
                return rec.output or ""
        raise ValueError(
            f"找不到节点 {self.output_node!r}，可用节点: "
            f"{[r.node_id for r in result.node_records]}"
        )

    @staticmethod
    def _map_status(status: str) -> str:
        """agentflow 的 skipped 在评测语义下不算失败，保留原值供归因使用。"""
        return status if status in {"success", "failed", "skipped"} else "failed"

    def describe(self) -> str:
        return self.engine.describe()
