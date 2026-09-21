"""通用网络搜索（Exa 后端）。

凭据从环境变量 ``EXA_API_KEY`` 读取，值写在 ``agent/.env``（控制台的「工具」页
可以增删改任意键，不用改代码）。不要把 key 写成本文件里的常量：那会跟着仓库一起
提交出去，而且换 key 得改代码、重启服务。

Exa 调用失败（欠费 / 断网 / key 失效）会抛 :class:`FatalToolError` 让整条流水线停
下来。否则失败会被当成一次普通工具观测写进训练样本，继续白烧教师模型额度。
"""

from __future__ import annotations

import os
from typing import Any

# 环境变量名。值写在 agent/.env，不要在这里写 key 本身。
ENV_API_KEY = "EXA_API_KEY"

# 每次搜索取几条结果
NUM_RESULTS = 3

SPEC = {
    "type": "function",
    "function": {
        "name": "search_web",
        "description": "通用网络搜索。需要最新信息、外部资料，或本地知识库未覆盖的内容时使用。",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "搜索关键词"},
            },
            "required": ["query"],
        },
    },
}


def _get_field(obj: Any, name: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _extract_items(response: Any) -> list[Any]:
    """Exa SDK 不同版本的返回结构不完全一致，这里做兼容提取。"""

    for field in ("results", "data", "items"):
        value = _get_field(response, field, None)
        if value is None:
            continue
        if isinstance(value, dict):
            return [value]
        try:
            return list(value)
        except TypeError:
            return [value]
    if isinstance(response, list):
        return response
    return []


def _format_results(results: list[Any], limit: int = 5) -> str:
    if not results:
        return "未检索到结果。"

    lines: list[str] = []
    for index, item in enumerate(results[:limit], start=1):
        title = str(
            _get_field(item, "title", "") or _get_field(item, "name", "") or "(无标题)"
        ).strip()
        url = str(
            _get_field(item, "url", "") or _get_field(item, "source_url", "") or ""
        ).strip()

        highlights = _get_field(item, "highlights", None) or []
        if isinstance(highlights, str):
            highlights_text = highlights
        else:
            try:
                highlights_text = "；".join(
                    str(highlight).strip()
                    for highlight in highlights
                    if str(highlight).strip()
                )
            except TypeError:
                highlights_text = str(highlights)

        snippet = (
            highlights_text
            or _get_field(item, "highlight", None)
            or _get_field(item, "text", None)
            or _get_field(item, "snippet", None)
            or _get_field(item, "content", None)
            or _get_field(item, "summary", None)
            or ""
        )
        snippet = " ".join(str(snippet).split()).strip()
        if len(snippet) > 300:
            snippet = snippet[:300].rstrip() + "…"

        parts = [f"{index}. {title}"]
        if url:
            parts.append(f"   URL: {url}")
        if snippet:
            parts.append(f"   摘要: {snippet}")
        lines.append("\n".join(parts))

    return "\n\n".join(lines)


def run(arguments: dict[str, Any], sample: dict[str, Any] | None = None) -> str:
    # 延迟导入：模块顶层导入会和 agent.tools 形成加载期循环依赖。
    from agent.tools import FatalToolError

    query = str((arguments or {}).get("query") or "").strip()
    if not query:
        return "搜索失败：缺少 query 参数。"

    api_key = (os.getenv(ENV_API_KEY) or "").strip()
    if not api_key:
        return f"搜索失败：未配置 {ENV_API_KEY}（在控制台「工具」页的工具凭据里填，或写进 agent/.env）。"

    try:
        from exa_py import Exa
    except Exception as exc:  # pragma: no cover - 取决于环境是否装了 exa_py
        return f"搜索失败：无法导入 exa_py：{exc}"

    client = Exa(api_key=api_key)
    try:
        response = client.search(
            query,
            type="auto",
            num_results=NUM_RESULTS,
            contents={"highlights": {"max_characters": 500}},
        )
    except Exception as exc:
        # 真实的 API / 网络故障：中断流水线，而不是返回降级的「搜索失败」文本。
        raise FatalToolError(f"Exa 调用失败：{exc}", query=query, original=exc) from exc

    return _format_results(_extract_items(response))
