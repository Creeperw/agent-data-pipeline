"""随机整数生成（用于需要随机性的教学或抽样场景）。"""

from __future__ import annotations

import random
from typing import Any

SPEC = {
    "type": "function",
    "function": {
        "name": "generate_random_number",
        "description": "生成指定范围内的随机整数，用于用户明确要求的随机选择、抽签、随机编号或随机分组。",
        "parameters": {
            "type": "object",
            "properties": {
                "min_value": {"type": "integer", "description": "最小值"},
                "max_value": {"type": "integer", "description": "最大值"},
                "purpose": {"type": "string", "description": "随机数用途"},
            },
            "required": ["min_value", "max_value"],
        },
    },
}


def _integer(value: Any, default: int) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def run(arguments: dict[str, Any], sample: dict[str, Any] | None = None) -> str:
    payload = arguments or {}
    min_value = _integer(payload.get("min_value"), 1)
    max_value = _integer(payload.get("max_value"), 100)
    if min_value > max_value:
        min_value, max_value = max_value, min_value

    value = random.randint(min_value, max_value)
    purpose = str(payload.get("purpose") or "").strip()
    purpose_text = f"用途：{purpose}；" if purpose else ""
    return f"{purpose_text}随机整数={value}，范围=[{min_value}, {max_value}]。"
