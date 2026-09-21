"""Tools for the first-stage agent environment.

Search tools include a local RAG-summary retriever and real Exa-backed wrappers.
``write_file`` is kept for execution-stage use and is not exposed to the planner
tool list.

The generic calculators (BMI, water intake, target heart rate, random numbers,
current datetime) are defined once in the shared ``agent/tools/`` library; this
domain opens them up through ``agent/domains/health/tools.json``.
"""

from __future__ import annotations

import contextvars
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from agent.config import AGENT_ROOT, HEALTH_PLAN_DIR, PROJECT_ROOT
from .prompts import TOOL_CATALOG


class ExaApiError(RuntimeError):
    """Raised when a real Exa API call fails (HTTP error / network failure).

    This is intentionally separate from the simulated tool-failure injection
    used to synthesise training data. When this exception is raised the
    generation pipeline should halt instead of silently writing degraded
    "搜索失败" content into trajectories.
    """

    def __init__(self, message: str, *, query: str | None = None, original: BaseException | None = None) -> None:
        super().__init__(message)
        self.query = query
        self.original = original


@dataclass(frozen=True)
class ToolResult:
    name: str
    content: str


_RAG_CONTEXT: contextvars.ContextVar[dict[str, Any]] = contextvars.ContextVar("rag_context", default={})


def set_rag_context(sample: dict[str, Any] | None) -> None:
    """Set the current sample used by ``search_rag``.

    The planner may generate any retrieval query, but this distillation
    environment uses the current user question as the index key and returns the
    sample's precomputed ``rag_summary``.
    """
    _RAG_CONTEXT.set(dict(sample or {}))


def _get_field(obj: Any, name: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _extract_exa_items(response: Any) -> list[Any]:
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


def _format_search_results(results: list[Any], limit: int = 5) -> str:
    if not results:
        return "未检索到结果。"

    lines: list[str] = []
    for index, item in enumerate(results[:limit], start=1):
        title = str(_get_field(item, "title", "") or _get_field(item, "name", "") or "(无标题)").strip()
        url = str(_get_field(item, "url", "") or _get_field(item, "source_url", "") or "").strip()
        highlights = _get_field(item, "highlights", None) or []
        if isinstance(highlights, str):
            highlights_text = highlights
        else:
            try:
                highlights_text = "；".join(str(highlight).strip() for highlight in highlights if str(highlight).strip())
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


def _try_exa_search(query: str, num_results: int = 3) -> tuple[str | None, str | None]:
    api_key = os.getenv("EXA_API_KEY")
    if not api_key:
        return None, "未配置 EXA_API_KEY"

    try:
        from exa_py import Exa
    except Exception as exc:  # pragma: no cover - optional dependency
        return None, f"无法导入 exa_py: {exc}"

    client = Exa(api_key=api_key)
    try:
        response = client.search(
            query,
            type="auto",
            num_results=num_results,
            contents={"highlights": {"max_characters": 500}},
        )
    except ExaApiError:
        # Already a fatal API error – let it bubble up.
        raise
    except Exception as exc:  # pragma: no cover - depends on SDK/network
        # Real API call failed (HTTP error / network / SDK exception).
        # Halt the pipeline instead of returning a degraded "搜索失败" string,
        # so we don't keep burning teacher-model credits on broken samples.
        raise ExaApiError(
            f"Exa 调用失败：{exc}",
            query=query,
            original=exc,
        ) from exc

    results = _extract_exa_items(response)
    return _format_search_results(results), None


def _search_with_exa(tool_name: str, query: str, suffix: str = "", num_results: int = 1) -> ToolResult:
    enriched_query = " ".join(part for part in [query.strip(), suffix.strip()] if part).strip()
    content, error = _try_exa_search(enriched_query or "健康管理", num_results)
    if content is None:
        content = f"搜索失败：{error}"
    return ToolResult(tool_name, content)


def search_web(query: str) -> ToolResult:
    return _search_with_exa("search_web", query)


def search_rag(query: str) -> ToolResult:
    context = _RAG_CONTEXT.get()
    user_query = str(context.get("user_query", "")).strip()
    rag_summary = str(context.get("rag_summary", "")).strip()
    if rag_summary:
        return ToolResult(
            "search_rag",
            rag_summary,
        )
    return ToolResult("search_rag", f"未检索到相关内容。检索语句：{query}")


def search_food_web(query: str) -> ToolResult:
    return _search_with_exa("search_food_web", query, "食疗 饮食禁忌 营养 慢病饮食 健康建议")


def search_exercise_web(query: str) -> ToolResult:
    return _search_with_exa("search_exercise_web", query, "运动处方 功法 练习频率 禁忌 注意事项")


def search_tcm_web(query: str) -> ToolResult:
    return _search_with_exa("search_tcm_web", query, "中医体质 辨识 表现 调理 食疗 运动")


def search_emotion_web(query: str) -> ToolResult:
    return _search_with_exa("search_emotion_web", query, "情志调节 焦虑 睡眠 放松训练 心理健康")


def search_safety_web(query: str) -> ToolResult:
    return _search_with_exa("search_safety_web", query, "红旗症状 何时就医 风险 用药禁忌 医学建议")


def _safe_filename(filename: str) -> str:
    filename = Path(filename or "个性化健康管理方案.md").name.strip()
    filename = re.sub(r"[^\w\u4e00-\u9fff.\-]+", "_", filename)
    if not filename or filename in {".", ".."}:
        filename = "个性化健康管理方案.md"
    suffix = Path(filename).suffix.lower()
    if suffix not in {".md", ".txt"}:
        filename = f"{filename}.md"
    return filename


def _deduplicate_path(path: Path) -> Path:
    if not path.exists():
        return path
    stem = path.stem
    suffix = path.suffix
    for index in range(1, 10000):
        candidate = path.with_name(f"{stem}_{index}{suffix}")
        if not candidate.exists():
            return candidate
    return path.with_name(f"{stem}_latest{suffix}")


def write_file(filename: str, content: str) -> ToolResult:
    content = (content or "").strip()
    if not content:
        return ToolResult("write_file", "写入失败：content 为空，无法生成个性化健康管理方案文件。")

    HEALTH_PLAN_DIR.mkdir(parents=True, exist_ok=True)
    path = _deduplicate_path(HEALTH_PLAN_DIR / _safe_filename(filename))
    path.write_text(content + "\n", encoding="utf-8")
    try:
        display_path = path.relative_to(PROJECT_ROOT)
    except ValueError:
        display_path = path
    return ToolResult("write_file", f"已写入个性化健康管理方案文件：{display_path}\n字符数：{len(content)}")


def plan_write_file(filename: str, write_purpose: str, file_type: str = "markdown", content_source: str = "executor_final_answer") -> ToolResult:
    filename = _safe_filename(filename or "个性化健康管理方案.md")
    file_type = (file_type or "markdown").strip()
    write_purpose = (write_purpose or "保存最终健康管理方案").strip()
    content_source = (content_source or "executor_final_answer").strip()
    return ToolResult(
        "plan_write_file",
        f"已记录写文件需求：filename={filename}；file_type={file_type}；write_purpose={write_purpose}；content_source={content_source}。最终文件内容待执行阶段生成后写入。",
    )


def _query_from_arguments(arguments: dict[str, Any]) -> str:
    query = str(arguments.get("query", "")).strip()
    return query or "通用健康管理原则"


def _search_runner(func: Callable[[str], ToolResult]) -> Callable[[dict[str, Any]], ToolResult]:
    return lambda arguments: func(_query_from_arguments(arguments))


def _write_file_runner(arguments: dict[str, Any]) -> ToolResult:
    filename = str(arguments.get("filename") or arguments.get("path") or "个性化健康管理方案.md")
    content = str(arguments.get("content") or arguments.get("plan") or arguments.get("markdown") or "")
    return write_file(filename, content)


def _plan_write_file_runner(arguments: dict[str, Any]) -> ToolResult:
    return plan_write_file(
        filename=str(arguments.get("filename") or "个性化健康管理方案.md"),
        write_purpose=str(arguments.get("write_purpose") or "保存最终健康管理方案"),
        file_type=str(arguments.get("file_type") or "markdown"),
        content_source=str(arguments.get("content_source") or "executor_final_answer"),
    )


TOOL_REGISTRY: dict[str, Callable[[dict[str, Any]], ToolResult]] = {
    "search_rag": _search_runner(search_rag),
    "search_web": _search_runner(search_web),
    "search_food_web": _search_runner(search_food_web),
    "search_exercise_web": _search_runner(search_exercise_web),
    "search_tcm_web": _search_runner(search_tcm_web),
    "search_emotion_web": _search_runner(search_emotion_web),
    "search_safety_web": _search_runner(search_safety_web),
    "plan_write_file": _plan_write_file_runner,
    "write_file": _write_file_runner,
}


def run_tool(name: str, arguments: dict) -> ToolResult:
    if name not in TOOL_REGISTRY:
        return ToolResult(name, f"工具 {name} 不存在，无法返回结果。")
    if not isinstance(arguments, dict):
        arguments = {"query": str(arguments)}
    return TOOL_REGISTRY[name](arguments)


def run_health_tool(name: str, arguments: dict[str, Any], sample: dict[str, Any] | None = None) -> ToolResult:
    return run_tool(name, arguments)
