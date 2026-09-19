"""BMI 计算（中国成人参考分类）。"""

from __future__ import annotations

from typing import Any

SPEC = {
    "type": "function",
    "function": {
        "name": "calculate_bmi",
        "description": "根据身高和体重计算 BMI，并返回中国成人参考分类。",
        "parameters": {
            "type": "object",
            "properties": {
                "height_cm": {"type": "number", "description": "身高，单位厘米"},
                "weight_kg": {"type": "number", "description": "体重，单位千克"},
            },
            "required": ["height_cm", "weight_kg"],
        },
    },
}


def _number(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def run(arguments: dict[str, Any], sample: dict[str, Any] | None = None) -> str:
    height_cm = _number((arguments or {}).get("height_cm"))
    weight_kg = _number((arguments or {}).get("weight_kg"))
    if height_cm <= 0 or weight_kg <= 0:
        return "计算失败：height_cm 和 weight_kg 必须为正数。"

    height_m = height_cm / 100
    bmi = weight_kg / (height_m * height_m)
    if bmi < 18.5:
        category = "偏瘦"
    elif bmi < 24:
        category = "正常"
    elif bmi < 28:
        category = "超重"
    else:
        category = "肥胖"
    return f"BMI={bmi:.1f}，中国成人参考分类：{category}。"
