"""Session-level orchestration for multi-turn synthesis."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

from ..core.conversation import build_history_context_text, render_dialogue_turns, split_history_turns, strip_think_text
from .prompts import MULTI_TURN_HISTORY_COMPRESSION_PROMPT, MULTI_TURN_USER_SIMULATOR_PROMPT


@dataclass
class MultiTurnSessionRecord:
    session_id: str = field(default_factory=lambda: str(uuid4()))
    domain: str = "health"
    user_turns: list[dict[str, Any]] = field(default_factory=list)
    assistant_turns: list[dict[str, Any]] = field(default_factory=list)
    fixed_tool_names: list[str] = field(default_factory=list)
    tool_failure_enabled: bool = False
    tool_failure_targets: list[str] = field(default_factory=list)
    failure_turn_indexes: list[int] = field(default_factory=list)
    recent_turn_limit: int = 3
    compress_every_turns: int = 2
    last_compressed_turn_index: int = 0
    compressed_history_text: str | None = None
    compressed_history_source: str | None = None
    next_user_prompt: str | None = None

    def append_turn(self, user_text: str, assistant_text: str | None = None) -> None:
        self.user_turns.append({"user": user_text})
        if assistant_text is not None:
            self.assistant_turns.append({"assistant": strip_think_text(assistant_text)})

    def history_turns(self) -> list[dict[str, Any]]:
        turns: list[dict[str, Any]] = []
        for idx, user_turn in enumerate(self.user_turns):
            turn: dict[str, Any] = {"user": user_turn.get("user", "")}
            if idx < len(self.assistant_turns):
                turn["assistant"] = self.assistant_turns[idx].get("assistant", "")
            turns.append(turn)
        return turns

    def history_context_text(self) -> str | None:
        turns = self.history_turns()
        compressed_turns, recent_turns = split_history_turns(turns, recent_turn_limit=self.recent_turn_limit)
        compressed_history_text = self.compressed_history_text
        if not compressed_history_text and compressed_turns:
            compressed_history_text = render_dialogue_turns(compressed_turns)
        recent_history_text = render_dialogue_turns(recent_turns)
        return build_history_context_text(compressed_history_text, recent_history_text)

    def compression_input_text(self) -> str:
        history_text = render_dialogue_turns(self.history_turns())
        next_user_prompt = self.next_user_prompt or "请把以上对话压缩成适合后续多轮合成使用的简短历史摘要。"
        return (
            "请根据下面的多轮对话，生成可直接放入【压缩历史对话】的内容。\n"
            "要求：\n"
            "1. 只保留自然语言信息，不要保留 <think>、JSON、tool call、planner 输出或执行细节。\n"
            "2. 重点保留已确认背景、用户目标、重要约束、已完成结论和未解决问题。\n"
            "3. 用简洁的摘要风格输出，不要分角色复述全部细节。\n\n"
            f"【当前会话】\n{history_text}\n\n"
            f"【下一轮用户草稿】\n{next_user_prompt}"
        )

    def set_failure_turn_indexes(self, turn_indexes: list[int]) -> None:
        self.failure_turn_indexes = list(dict.fromkeys(index for index in turn_indexes if index > 0))

    def set_compressed_history(self, compressed_history_text: str | None, source: str | None = None) -> None:
        self.compressed_history_text = compressed_history_text
        self.compressed_history_source = source

    def should_compress_history(self, current_turn_index: int) -> bool:
        if self.compress_every_turns <= 0:
            return False
        if current_turn_index <= 0:
            return False
        if current_turn_index <= self.recent_turn_limit:
            return False
        if current_turn_index - self.last_compressed_turn_index < self.compress_every_turns:
            return False
        return True

    def mark_compressed(self, current_turn_index: int, source: str | None = None) -> None:
        self.last_compressed_turn_index = current_turn_index
        self.compressed_history_source = source

    def set_next_user_prompt(self, prompt: str | None) -> None:
        self.next_user_prompt = prompt


class MultiTurnSessionBuilder:
    """Build a multi-turn session record without changing single-turn outputs."""

    def __init__(self, domain: str = "health", recent_turn_limit: int = 3, compress_every_turns: int = 2) -> None:
        self.record = MultiTurnSessionRecord(
            domain=domain,
            recent_turn_limit=recent_turn_limit,
            compress_every_turns=compress_every_turns,
        )

    def with_tool_state(self, fixed_tool_names: list[str], tool_failure_enabled: bool = False, tool_failure_targets: list[str] | None = None) -> "MultiTurnSessionBuilder":
        self.record.fixed_tool_names = list(dict.fromkeys(name for name in fixed_tool_names if name))
        self.record.tool_failure_enabled = tool_failure_enabled
        self.record.tool_failure_targets = list(dict.fromkeys(tool_failure_targets or []))
        return self

    def with_failure_turn_indexes(self, turn_indexes: list[int]) -> "MultiTurnSessionBuilder":
        self.record.set_failure_turn_indexes(turn_indexes)
        return self

    def set_compressed_history(self, compressed_history_text: str | None, source: str | None = None) -> "MultiTurnSessionBuilder":
        self.record.set_compressed_history(compressed_history_text, source=source)
        return self

    def set_next_user_prompt(self, prompt: str | None) -> "MultiTurnSessionBuilder":
        self.record.set_next_user_prompt(prompt)
        return self

    def append_turn(self, user_text: str, assistant_text: str | None = None) -> "MultiTurnSessionBuilder":
        self.record.append_turn(user_text, assistant_text)
        return self

    def build(self) -> MultiTurnSessionRecord:
        return self.record


def build_history_compression_messages(history_text: str, next_user_prompt: str | None = None) -> list[dict[str, str]]:
    prompt = next_user_prompt or "请把以上对话压缩成适合后续多轮合成使用的简短历史摘要。"
    return [
        {"role": "system", "content": MULTI_TURN_HISTORY_COMPRESSION_PROMPT},
        {"role": "user", "content": f"【当前会话】\n{history_text.strip()}\n\n【下一轮用户草稿】\n{prompt.strip()}"},
    ]


def build_user_simulator_messages(session_context_text: str) -> list[dict[str, str]]:
    return [
        {"role": "system", "content": MULTI_TURN_USER_SIMULATOR_PROMPT},
        {"role": "user", "content": session_context_text.strip()},
    ]
