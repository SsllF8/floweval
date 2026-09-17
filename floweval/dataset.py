"""评测数据集加载。

支持三种格式，按扩展名自动识别：
  - .jsonl  一行一条用例（推荐，最适合评测集，diff 友好）
  - .json   数组，或 {"cases": [...]}
  - .yaml/.yml  数组，或 {"cases": [...]}

设计原则：**字段宽松**。用户多写的字段一律进 metadata，不报错；
少写的字段用默认值补。评测工具不该在加载阶段就把人卡住。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable

from .models import Case


class DatasetError(ValueError):
    """数据集格式错误。"""


def load_cases(path: str | Path) -> list[Case]:
    p = Path(path)
    if not p.exists():
        raise DatasetError(f"数据集文件不存在: {p}")

    suffix = p.suffix.lower()
    if suffix == ".jsonl":
        raw: Any = [
            json.loads(line)
            for line in p.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.strip().startswith("#")
        ]
    elif suffix == ".json":
        raw = json.loads(p.read_text(encoding="utf-8"))
    elif suffix in {".yaml", ".yml"}:
        raw = _load_yaml(p)
    else:
        raise DatasetError(f"不支持的数据集格式: {suffix}（支持 .jsonl / .json / .yaml）")

    items = _extract_list(raw)
    cases = [Case.from_dict(item) for item in items]

    _validate(cases)
    return cases


def load_cases_from(data: Iterable[dict[str, Any]]) -> list[Case]:
    cases = [Case.from_dict(item) for item in data]
    _validate(cases)
    return cases


def _load_yaml(path: Path) -> Any:
    try:
        import yaml  # type: ignore  # noqa: PLC0415
    except ImportError as exc:  # pragma: no cover
        raise DatasetError(
            "读取 YAML 需要 pyyaml：pip install pyyaml（或改用 .jsonl 格式）"
        ) from exc
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _extract_list(raw: Any) -> list[dict[str, Any]]:
    if isinstance(raw, dict):
        raw = raw.get("cases") or raw.get("data") or []
    if not isinstance(raw, list):
        raise DatasetError(f"数据集必须是数组或含 cases 字段的对象，收到: {type(raw).__name__}")
    for item in raw:
        if not isinstance(item, dict):
            raise DatasetError(f"每条用例必须是对象，收到: {type(item).__name__}")
    return raw


def _validate(cases: list[Case]) -> None:
    if not cases:
        raise DatasetError("数据集为空")
    seen: set[str] = set()
    for case in cases:
        if not case.input.strip():
            raise DatasetError(f"用例 {case.id} 的 input 为空")
        if case.id in seen:
            raise DatasetError(f"用例 id 重复: {case.id}")
        seen.add(case.id)


def save_cases(cases: list[Case], path: str | Path) -> None:
    """导出为 jsonl。"""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    lines = [json.dumps(c.to_dict(), ensure_ascii=False) for c in cases]
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
