"""本地知识库检索。

返回当前样本自带的 ``rag_summary``（由「RAG 摘要」阶段预先生成并写进种子文件）。
工具本身不做向量检索，也不依赖 planner 生成的 query —— 命中的始终是这条样本预置
的参考资料，这样蒸馏出的轨迹才可复现。
"""

from __future__ import annotations

from typing import Any

SPEC = {
    "type": "function",
    "function": {
        "name": "search_rag",
        "description": "检索本地知识库，补足当前问题相关的背景资料。",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "搜索关键词"},
            },
            "required": ["query"],
        },
    },
}


def run(arguments: dict[str, Any], sample: dict[str, Any] | None = None) -> str:
    query = str((arguments or {}).get("query") or "").strip()
    summary = str((sample or {}).get("rag_summary") or "").strip()
    if summary:
        return summary
    return f"未检索到相关内容。检索语句：{query}"
