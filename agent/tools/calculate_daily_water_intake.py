"""每日饮水量估算。"""

from __future__ import annotations

from typing import Any

SPEC = {
    "type": "function",
    "function": {
        "name": "calculate_daily_water_intake",
        "description": "按体重和活动水平粗略估算每日饮水量。",
        "parameters": {
            "type": "object",
            "properties": {
                "weight_kg": {"type": "number", "description": "体重，单位千克"},
                "activity_level": {"type": "string", "description": "活动水平：low、normal、medium 或 high"},
            },
            "required": ["weight_kg"],
        },
    },
}


def _number(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def run(arguments: dict[str, Any], sample: dict[str, Any] | None = None) -> str:
    weight_kg = _number((arguments or {}).get("weight_kg"))
    if weight_kg <= 0:
        return "计算失败：weight_kg 必须为正数。"

    factor_map = {"low": 30, "normal": 35, "medium": 35, "high": 40}
    activity = str((arguments or {}).get("activity_level") or "normal").lower()
    factor = factor_map.get(activity, 35)
    water_ml = weight_kg * factor
    return f"按 {factor} ml/kg 估算，每日饮水量约 {water_ml:.0f} ml。需结合肾病、心衰、水肿等情况调整。"
