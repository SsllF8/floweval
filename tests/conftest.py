"""pytest 全局配置。

agentflow 集成测试的探测逻辑：优先用户显式设置的环境变量，
否则按"兄弟项目"约定（floweval 与 agentflow 在同一父目录下）自动探测。
不写死任何机器路径，克隆两个项目到同级目录即可生效。
"""

import os
from pathlib import Path


def _detect_agentflow() -> None:
    if os.environ.get("AGENTFLOW_PATH"):
        return
    sibling = Path(__file__).resolve().parents[2] / "agentflow"
    if (sibling / "core").exists():
        os.environ["AGENTFLOW_PATH"] = str(sibling)


_detect_agentflow()
