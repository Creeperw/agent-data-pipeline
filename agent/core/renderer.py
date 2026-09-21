"""Domain-agnostic prompt rendering helpers."""

from __future__ import annotations

from typing import Any

from .domain import DomainSpec


def build_planner_messages(domain: DomainSpec, user_query: str) -> list[dict[str, str]]:
    """Build planner messages while keeping the planner action protocol stable."""

    return [
        {"role": "system", "content": domain.planner_system_prompt},
        {"role": "user", "content": str(user_query or "")},
    ]


def _append_history_blocks(content: str, compressed_history_text: str | None = None, recent_history_text: str | None = None) -> str:
    blocks: list[str] = [str(content or "").strip()]
    if compressed_history_text:
        blocks.append(f"【压缩历史对话】\n{str(compressed_history_text).strip()}")
    if recent_history_text:
        blocks.append(f"【近期历史对话】\n{str(recent_history_text).strip()}")
    return "\n\n".join(block for block in blocks if block).strip()


def build_executor_messages(
    domain: DomainSpec,
    *,
    user_query: str,
    intent: str | None = None,
    external_info: str = "",
    answer_template: str | None = None,
    clarification_ask: str | None = None,
    compressed_history_text: str | None = None,
    recent_history_text: str | None = None,
) -> list[dict[str, str]]:
    """Build executor messages using only capabilities declared by domain."""

    return [
        {"role": "system", "content": domain.executor_system_prompt},
        {
            "role": "user",
            "content": build_executor_user_prompt_with_history(
                domain,
                user_query=user_query,
                intent=intent,
                external_info=external_info,
                answer_template=answer_template,
                clarification_ask=clarification_ask,
                compressed_history_text=compressed_history_text,
                recent_history_text=recent_history_text,
            ),
        },
    ]


def build_reviewer_messages(
    domain: DomainSpec,
    *,
    user_query: str,
    answer: str,
    intent: str | None = None,
    external_info: str = "",
    scenario: str | None = None,
    student_profile: str | None = None,
    target_competency: str | None = None,
) -> list[dict[str, str]]:
    """Build a reviewer request whose output is a strict JSON protocol."""

    sections = [f"【用户问题】\n{str(user_query or '').strip()}"]
    if intent:
        sections.append(f"【识别意图】\n{intent.strip()}")
    if scenario:
        sections.append(f"【场景】\n{str(scenario).strip()}")
    if student_profile:
        sections.append(f"【学生画像】\n{str(student_profile).strip()}")
    if target_competency:
        sections.append(f"【目标能力】\n{str(target_competency).strip()}")
    sections.append(f"【外部参考信息】\n{external_info.strip() or '无外部补充信息。'}")
    sections.append(f"【执行阶段回答】\n{str(answer or '').strip()}")
    return [
        {"role": "system", "content": domain.get_reviewer_system_prompt()},
        {"role": "user", "content": "\n\n".join(sections)},
    ]


def build_executor_user_prompt_with_history(
    domain: DomainSpec,
    *,
    user_query: str,
    intent: str | None = None,
    external_info: str = "",
    answer_template: str | None = None,
    clarification_ask: str | None = None,
    compressed_history_text: str | None = None,
    recent_history_text: str | None = None,
) -> str:
    base_prompt = domain.build_executor_user_prompt(
        user_query=user_query,
        intent=intent,
        external_info=external_info,
        answer_template=answer_template,
        clarification_ask=clarification_ask,
    )
    return _append_history_blocks(base_prompt, compressed_history_text, recent_history_text)


def available_tool_names(domain: DomainSpec) -> list[str]:
    names: list[str] = []
    for tool in domain.tools:
        function = tool.get("function") if isinstance(tool, dict) else None
        if isinstance(function, dict) and function.get("name"):
            names.append(str(function["name"]))
    return names