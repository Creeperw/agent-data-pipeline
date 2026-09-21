"""当前日期时间查询。

本地实现，不依赖任何外部服务；从 ``health`` 领域抽出来作为全局通用工具。
"""

from __future__ import annotations

import datetime as dt
from typing import Any

SPEC = {
    "type": "function",
    "function": {
        "name": "get_current_datetime",
        "description": "获取当前日期时间。仅当问题依赖当前日期、时间、星期或计划起始日期时调用。",
        "parameters": {
            "type": "object",
            "properties": {
                "timezone": {"type": "string", "description": "时区名称，例如 Asia/Shanghai"},
            },
            "required": [],
        },
    },
}


def run(arguments: dict[str, Any], sample: dict[str, Any] | None = None) -> str:
    timezone = str((arguments or {}).get("timezone") or "Asia/Shanghai")
    try:
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

        try:
            tz = ZoneInfo(timezone or "Asia/Shanghai")
            label = timezone or "Asia/Shanghai"
        except ZoneInfoNotFoundError:
            tz = dt.datetime.now().astimezone().tzinfo
            label = f"本地时区（请求时区 {timezone} 不可用）"
        now = dt.datetime.now(tz)
        return f"当前时间：{now.strftime('%Y-%m-%d %H:%M:%S %z')}；时区：{label}。"
    except Exception:  # noqa: BLE001 - 时区数据缺失时退回本地时间
        now = dt.datetime.now().astimezone()
        return f"当前时间：{now.strftime('%Y-%m-%d %H:%M:%S %z')}；时区：本地。"
