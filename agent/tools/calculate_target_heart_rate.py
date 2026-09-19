"""目标心率区间估算。"""

from __future__ import annotations

import math
from typing import Any

SPEC = {
    "type": "function",
    "function": {
        "name": "calculate_target_heart_rate",
        "description": "按年龄和运动强度估算目标心率区间。",
        "parameters": {
            "type": "object",
            "properties": {
                "age": {"type": "integer", "description": "年龄"},
                "intensity": {"type": "string", "description": "运动强度：low、moderate 或 high"},
            },
            "required": ["age"],
        },
    },
}


def _integer(value: Any, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def run(arguments: dict[str, Any], sample: dict[str, Any] | None = None) -> str:
    age = _integer((arguments or {}).get("age"))
    if age <= 0 or age > 120:
        return "计算失败：age 必须在 1-120 之间。"

    intensity = str((arguments or {}).get("intensity") or "moderate").lower()
    max_hr = 220 - age
    zones = {"low": (0.50, 0.60), "moderate": (0.60, 0.75), "high": (0.75, 0.85)}
    low, high = zones.get(intensity, zones["moderate"])
    return (
        f"估算最大心率 {max_hr} 次/分；{intensity} 强度目标心率约 "
        f"{math.floor(max_hr * low)}-{math.ceil(max_hr * high)} 次/分。"
    )
