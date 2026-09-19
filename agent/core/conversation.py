"""Conversation history helpers for multi-turn synthesis."""

from __future__ import annotations

import re
from typing import Any


_THINK_PATTERN = re.compile(r"<think>.*?</think>", flags=re.S)


def _normalize_text(value: Any) -> str:
    return "\n".join(line.rstrip() for line in str(value or "").strip().splitlines()).strip()


def strip_think_text(value: Any) -> str:
    """Remove assistant reasoning traces from a conversation text block."""

    text = _normalize_text(value)
    if not text:
        return ""
    return _THINK_PATTERN.sub("", text).strip()


def _turn_user_text(turn: dict[str, Any]) -> str:
    for key in ("user", "user_query", "user_content", "content"):
        value = _normalize_text(turn.get(key))
        if value:
            return value
    return ""


def _turn_assistant_text(turn: dict[str, Any]) -> str:
    for key in ("assistant", "assistant_answer", "assistant_content", "response", "answer"):
        value = _normalize_text(turn.get(key))
        if value:
            return value
    return ""


def render_dialogue_turns(turns: list[dict[str, Any]], max_turns: int | None = None) -> str:
    """Render natural dialogue turns as a plain text history block.

    Only user/assistant natural language is kept here. Internal planner JSON,
    tool calls, and reasoning traces must be filtered out by the caller before
    turning them into this representation.
    """

    if max_turns is not None and max_turns > 0:
        turns = turns[-max_turns:]
    lines: list[str] = []
    for turn in turns:
        if not isinstance(turn, dict):
            continue
        user_text = _turn_user_text(turn)
        assistant_text = strip_think_text(_turn_assistant_text(turn))
        if user_text:
            lines.append(f"用户：{user_text}")
        if assistant_text:
            lines.append(f"助手：{assistant_text}")
    return "\n".join(lines).strip()


def build_history_context_text(
    compressed_history_text: str | None = None,
    recent_history_text: str | None = None,
) -> str:
    """Compose the optional history blocks used by planner/executor prompts."""

    sections: list[str] = []
    compressed_history_text = _normalize_text(compressed_history_text)
    recent_history_text = _normalize_text(recent_history_text)
    if compressed_history_text:
        sections.append(f"【压缩历史对话】\n{compressed_history_text}")
    if recent_history_text:
        sections.append(f"【近期历史对话】\n{recent_history_text}")
    return "\n\n".join(sections).strip()


def split_history_turns(turns: list[dict[str, Any]], recent_turn_limit: int = 3) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Split dialogue turns into compressed and recent portions."""

    if recent_turn_limit <= 0 or len(turns) <= recent_turn_limit:
        return [], turns
    return turns[:-recent_turn_limit], turns[-recent_turn_limit:]
