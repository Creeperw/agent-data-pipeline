"""Comprehensive statistics for agent distillation JSONL data files.

Supported formats are detected automatically:
- seed/raw rows: user_query + expected_intent
- planner trajectories: trajectory + planner_output/planner_raw_output
- planner step-wise SFT: messages + tools + metadata.step/action
- executor answer SFT: messages + executor_raw_output / external_info
- final prompt JSONL: prompt + metadata
- merged final prompt JSONL: prompt + metadata.merge_source

Examples:

    python -m agent.data_stats agent/outputs/intent_tool_trajectories2.jsonl
    python -m agent.data_stats agent/outputs/executor_answer_sft2.jsonl --format json
    python -m agent.data_stats agent/outputs/*.jsonl --output agent/outputs/stats.md
"""

from __future__ import annotations

import argparse
import json
import math
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from statistics import mean
from typing import Any, Iterable

try:
    from .config import DATA_STATS_DEFAULT_FILES
except ImportError:  # Allows: python agent/data_stats.py
    from config import DATA_STATS_DEFAULT_FILES


DEFAULT_PATHS = DATA_STATS_DEFAULT_FILES

SEARCH_TOOL_NAMES = {
    "search_rag",
    "search_web",
    "search_food_web",
    "search_exercise_web",
    "search_tcm_web",
    "search_emotion_web",
    "search_safety_web",
}

VALID_ACTIONS = {"tool_call", "planning_finish", "ask_clarification"}
VALID_INTENTS = {"食疗咨询", "运动指导", "体质辨识", "症状调理", "情志调节", "其他"}


@dataclass
class JsonlReadResult:
    rows: list[dict[str, Any]] = field(default_factory=list)
    total_lines: int = 0
    blank_lines: int = 0
    invalid_lines: int = 0


@dataclass
class LengthStats:
    count: int
    min: int
    max: int
    avg: float
    p50: int
    p90: int
    p95: int
    p99: int


@dataclass
class FileStats:
    path: str
    file_type: str
    total_lines: int
    json_rows: int
    blank_lines: int
    invalid_lines: int
    duplicate_ids: int
    stats: dict[str, Any]


class MarkdownReport:
    def __init__(self) -> None:
        self.lines: list[str] = []

    def h1(self, text: str) -> None:
        self.lines.append(f"# {text}\n")

    def h2(self, text: str) -> None:
        self.lines.append(f"\n## {text}\n")

    def h3(self, text: str) -> None:
        self.lines.append(f"\n### {text}\n")

    def paragraph(self, text: str) -> None:
        self.lines.append(f"{text}\n")

    def bullets(self, items: Iterable[str]) -> None:
        items = list(items)
        if not items:
            self.lines.append("（无）\n")
            return
        for item in items:
            self.lines.append(f"- {item}")
        self.lines.append("")

    def table(self, headers: list[str], rows: Iterable[Iterable[Any]]) -> None:
        rows = list(rows)
        if not rows:
            self.lines.append("（无）\n")
            return
        self.lines.append("| " + " | ".join(headers) + " |")
        self.lines.append("| " + " | ".join("---" for _ in headers) + " |")
        for row in rows:
            self.lines.append("| " + " | ".join(format_cell(value) for value in row) + " |")
        self.lines.append("")

    def text(self) -> str:
        return "\n".join(self.lines).rstrip() + "\n"


def format_cell(value: Any) -> str:
    if isinstance(value, float):
        return f"{value:.2f}"
    if isinstance(value, bool):
        return "是" if value else "否"
    text = str(value)
    return text.replace("\n", "<br>").replace("|", "\\|")


def pct(part: int | float, total: int | float) -> float:
    if not total:
        return 0.0
    return float(part) / float(total) * 100


def fmt_pct(value: float | int | None, digits: int = 2) -> str:
    if value is None:
        return "0.00%"
    return f"{float(value):.{digits}f}%"


def meter(value: float | int, max_value: float | int = 100.0, width: int = 18) -> str:
    try:
        value_f = float(value)
        max_f = float(max_value)
    except (TypeError, ValueError):
        return "░" * width
    if max_f <= 0:
        return "░" * width
    ratio = min(max(value_f / max_f, 0.0), 1.0)
    filled = int(round(ratio * width))
    filled = min(max(filled, 0), width)
    return "█" * filled + "░" * (width - filled)


def top_n(counter_dict: dict[str, int], limit: int = 5) -> dict[str, int]:
    return dict(list(counter_dict.items())[:limit])


def safe_len(value: Any) -> int:
    return len(str(value or ""))


def percentile(values: list[int], q: float) -> int:
    if not values:
        return 0
    values = sorted(values)
    if len(values) == 1:
        return values[0]
    index = math.ceil((q / 100) * len(values)) - 1
    index = min(max(index, 0), len(values) - 1)
    return values[index]


def length_stats(values: list[int]) -> dict[str, Any]:
    if not values:
        return {
            "count": 0,
            "min": 0,
            "max": 0,
            "avg": 0.0,
            "p50": 0,
            "p90": 0,
            "p95": 0,
            "p99": 0,
        }
    return {
        "count": len(values),
        "min": min(values),
        "max": max(values),
        "avg": mean(values),
        "p50": percentile(values, 50),
        "p90": percentile(values, 90),
        "p95": percentile(values, 95),
        "p99": percentile(values, 99),
    }


def counter_to_dict(counter: Counter[Any]) -> dict[str, int]:
    return {str(key): int(value) for key, value in counter.most_common()}


def top_items(counter: Counter[Any], limit: int = 20) -> list[tuple[str, int]]:
    return [(str(key), int(value)) for key, value in counter.most_common(limit)]


def read_jsonl(path: Path, max_rows: int | None = None) -> JsonlReadResult:
    result = JsonlReadResult()
    if not path.exists():
        return result
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            result.total_lines += 1
            if not line.strip():
                result.blank_lines += 1
                continue
            if max_rows is not None and len(result.rows) >= max_rows:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                result.invalid_lines += 1
                continue
            if isinstance(row, dict):
                result.rows.append(row)
            else:
                result.invalid_lines += 1
    return result


def count_duplicate_ids(rows: list[dict[str, Any]]) -> int:
    ids = [str(row.get("id")) for row in rows if row.get("id") is not None]
    counts = Counter(ids)
    return sum(count - 1 for count in counts.values() if count > 1)


def detect_file_type(rows: list[dict[str, Any]], path: Path) -> str:
    if not rows:
        return "empty_or_unknown"
    sample = rows[0]
    if "trajectory" in sample:
        return "planner_trajectory"
    if "executor_raw_output" in sample or ("external_info" in sample and "messages" in sample and str(sample.get("id", "")).endswith("_executor")):
        return "executor_answer_sft"
    if "prompt" in sample:
        merge_sources = {
            str((row.get("metadata") or {}).get("merge_source") or "")
            for row in rows
            if isinstance(row.get("metadata"), dict)
        }
        if "executor_answer" in merge_sources or ("planner_final" in merge_sources and "executor_answer" in merge_sources) or "merged" in path.name:
            return "merged_final_prompt"
        return "final_prompt"
    if "messages" in sample:
        metadata = sample.get("metadata") or {}
        if isinstance(metadata, dict) and ("step" in metadata or "action" in metadata):
            return "planner_step_sft"
        return "message_sft"
    if "user_query" in sample and "expected_intent" in sample:
        return "seed"
    return "unknown"


def get_tool_name(tool: dict[str, Any]) -> str:
    return str((tool.get("function") or {}).get("name") or tool.get("name") or "")


def get_planner_tool_calls(planner_output: dict[str, Any]) -> list[dict[str, Any]]:
    tool_calls = planner_output.get("tool_calls") or []
    if isinstance(tool_calls, dict):
        tool_calls = [tool_calls]
    if not tool_calls and planner_output.get("tool_call"):
        tool_calls = [planner_output["tool_call"]]
    return [tool_call for tool_call in tool_calls if isinstance(tool_call, dict)]


def get_step_observations(step: dict[str, Any]) -> list[dict[str, Any]]:
    observations = step.get("observations") or []
    if isinstance(observations, dict):
        observations = [observations]
    if not observations and step.get("observation"):
        observations = [step["observation"]]
    return [observation for observation in observations if isinstance(observation, dict)]


def message_roles(messages: list[dict[str, Any]]) -> Counter[str]:
    return Counter(str(message.get("role") or "") for message in messages if isinstance(message, dict))


def extract_prompt_sections(prompt: str) -> dict[str, bool]:
    return {
        "has_system": "<|im_start|>system" in prompt,
        "has_user": "<|im_start|>user" in prompt,
        "has_assistant": "<|im_start|>assistant" in prompt,
        "has_tool": "<|im_start|>tool" in prompt or "<|im_start|>function" in prompt,
        "has_think": "<think>" in prompt,
        "has_tool_schema": "# Tools" in prompt or "<tools>" in prompt,
    }


def infer_action_from_prompt(prompt: str) -> str:
    last_assistant = prompt.rfind("<|im_start|>assistant")
    text = prompt[last_assistant:] if last_assistant >= 0 else prompt
    if '"action": "tool_call"' in text or '"action":"tool_call"' in text:
        return "tool_call"
    if '"action": "planning_finish"' in text or '"action":"planning_finish"' in text:
        return "planning_finish"
    if '"action": "ask_clarification"' in text or '"action":"ask_clarification"' in text:
        return "ask_clarification"
    return "unknown"


def infer_intent_from_prompt(prompt: str) -> str:
    last_assistant = prompt.rfind("<|im_start|>assistant")
    text = prompt[last_assistant:] if last_assistant >= 0 else prompt
    end_pos = text.find("<|im_end|>")
    if end_pos >= 0:
        text = text[:end_pos]

    patterns = [
        r'"intent"\s*:\s*"([^"]+)"',
        r"'intent'\s*:\s*'([^']+)'",
        r"intent\s*[:：]\s*([^\s。；;，,\n]+)",
        r"意图\s*[:：]\s*([^\s。；;，,\n]+)",
    ]
    for pattern in patterns:
        match = re.search(pattern, text)
        if not match:
            continue
        intent = match.group(1).strip().strip('"“”').strip()
        intent = intent.rstrip("。.;；,，")
        if intent in VALID_INTENTS:
            return intent
    return "(missing)"


def base_stats(rows: list[dict[str, Any]]) -> dict[str, Any]:
    ids = [str(row.get("id")) for row in rows if row.get("id") is not None]
    return {
        "rows": len(rows),
        "ids": len(ids),
        "unique_ids": len(set(ids)),
        "missing_id": len(rows) - len(ids),
        "duplicate_ids": count_duplicate_ids(rows),
    }


def stats_seed(rows: list[dict[str, Any]]) -> dict[str, Any]:
    intents = Counter(str(row.get("expected_intent") or "") for row in rows)
    scenarios = Counter(str(row.get("scenario") or "") for row in rows)
    query_lengths = [safe_len(row.get("user_query")) for row in rows]
    rag_present = sum(bool(str(row.get("rag_summary") or "").strip()) for row in rows)
    return {
        **base_stats(rows),
        "expected_intent": counter_to_dict(intents),
        "scenario": counter_to_dict(scenarios),
        "query_length": length_stats(query_lengths),
        "rag_summary_present": rag_present,
        "rag_summary_present_pct": pct(rag_present, len(rows)),
    }


def stats_planner_trajectory(rows: list[dict[str, Any]]) -> dict[str, Any]:
    intent_counter: Counter[str] = Counter()
    expected_intent_counter: Counter[str] = Counter()
    scenario_counter: Counter[str] = Counter()
    sampling_mode_counter: Counter[str] = Counter()
    steps_by_sampling_mode: Counter[str] = Counter()
    tool_calls_by_sampling_mode: Counter[str] = Counter()
    action_counter: Counter[str] = Counter()
    final_action_counter: Counter[str] = Counter()
    final_intent_counter: Counter[str] = Counter()
    tool_counter: Counter[str] = Counter()
    exposed_tool_counter: Counter[str] = Counter()
    observation_tool_counter: Counter[str] = Counter()
    trajectory_lengths: list[int] = []
    tool_call_steps_per_row: list[int] = []
    tool_calls_per_row: list[int] = []
    observation_chars: list[int] = []
    query_lengths: list[int] = []
    rows_with_tool_call = 0
    rows_with_search_tool_call = 0
    rows_with_no_tools = 0
    rows_with_tool_error = 0
    rows_with_injected_failure = 0
    rows_with_ask = 0
    rows_with_overridden = 0
    rows_with_raw_empty_tool_call = 0
    rows_with_forbidden_tool_call = 0
    rows_with_intent_reference = 0
    rows_with_reasoning_content_conflict = 0
    rows_with_expected_intent_conflict = 0
    rows_with_empty_backfill = 0
    malformed_steps = 0
    missing_final = 0

    for row in rows:
        trajectory = row.get("trajectory") or []
        if not isinstance(trajectory, list):
            trajectory = []
        trajectory_lengths.append(len(trajectory))
        query_lengths.append(safe_len(row.get("user_query")))
        scenario_counter[str(row.get("scenario") or "") or "(missing)"] += 1
        sampling_mode = str(row.get("tool_sampling_mode") or "") or "(missing)"
        sampling_mode_counter[sampling_mode] += 1
        steps_by_sampling_mode[sampling_mode] += len(trajectory)
        expected_intent_counter[str(row.get("expected_intent") or "") or "(missing)"] += 1
        rows_with_injected_failure += int(bool(row.get("tool_failure_enabled")) or int(row.get("tool_failure_count") or 0) > 0)
        rows_with_intent_reference += int(bool(row.get("intent_reference_used")))
        rows_with_empty_backfill += int(int(row.get("empty_tool_call_intent_backfill_count") or 0) > 0)

        exposed_tools = row.get("tools") or []
        if isinstance(exposed_tools, list):
            if not exposed_tools:
                rows_with_no_tools += 1
            for tool in exposed_tools:
                name = get_tool_name(tool)
                if name:
                    exposed_tool_counter[name] += 1

        row_tool_call_steps = 0
        row_tool_calls = 0
        row_has_search_tool_call = False
        row_has_tool_error = False
        row_has_raw_empty_tool_call = False
        row_has_forbidden = False
        row_has_reasoning_conflict = False
        row_has_expected_conflict = False
        for step in trajectory:
            if not isinstance(step, dict):
                malformed_steps += 1
                continue
            planner_output = step.get("planner_output") or {}
            action = str(planner_output.get("action") or "") or "(missing)"
            action_counter[action] += 1
            intent = str(planner_output.get("intent") or "") or "(missing)"
            intent_counter[intent] += 1
            diagnostics = step.get("intent_diagnostics") or {}
            if isinstance(diagnostics, dict):
                row_has_reasoning_conflict = row_has_reasoning_conflict or bool(diagnostics.get("reasoning_content_conflict"))
                row_has_expected_conflict = row_has_expected_conflict or bool(diagnostics.get("expected_intent_conflict"))

            if action == "tool_call":
                row_tool_call_steps += 1
                raw_output = step.get("planner_raw_output") or {}
                if not str(raw_output.get("raw_content") or "").strip():
                    row_has_raw_empty_tool_call = True
                tool_calls = get_planner_tool_calls(planner_output)
                row_tool_calls += len(tool_calls)
                tool_calls_by_sampling_mode[sampling_mode] += len(tool_calls)
                for tool_call in tool_calls:
                    name = str(tool_call.get("name") or "")
                    if name:
                        tool_counter[name] += 1
                        if name in SEARCH_TOOL_NAMES:
                            row_has_search_tool_call = True
                    if name == "write_file":
                        row_has_forbidden = True
            if action == "ask_clarification":
                rows_with_ask += 1
            if step.get("planner_overridden"):
                rows_with_overridden += 1
            for observation in get_step_observations(step):
                name = str(observation.get("name") or "")
                if name:
                    observation_tool_counter[name] += 1
                observation_chars.append(safe_len(observation.get("content")))
                if observation.get("error"):
                    row_has_tool_error = True

        if trajectory:
            final_output = (trajectory[-1].get("planner_output") or {}) if isinstance(trajectory[-1], dict) else {}
            final_action_counter[str(final_output.get("action") or "") or "(missing)"] += 1
            final_intent_counter[str(final_output.get("intent") or "") or "(missing)"] += 1
        else:
            missing_final += 1

        tool_call_steps_per_row.append(row_tool_call_steps)
        tool_calls_per_row.append(row_tool_calls)
        rows_with_tool_call += int(row_tool_calls > 0)
        rows_with_search_tool_call += int(row_has_search_tool_call)
        rows_with_tool_error += int(row_has_tool_error)
        rows_with_raw_empty_tool_call += int(row_has_raw_empty_tool_call)
        rows_with_forbidden_tool_call += int(row_has_forbidden)
        rows_with_reasoning_content_conflict += int(row_has_reasoning_conflict)
        rows_with_expected_intent_conflict += int(row_has_expected_conflict)

    total_steps = sum(trajectory_lengths)
    total_tool_calls = sum(tool_calls_per_row)
    return {
        **base_stats(rows),
        "scenario": counter_to_dict(scenario_counter),
        "tool_sampling_mode": counter_to_dict(sampling_mode_counter),
        "steps_by_sampling_mode": counter_to_dict(steps_by_sampling_mode),
        "tool_calls_by_sampling_mode": counter_to_dict(tool_calls_by_sampling_mode),
        "expected_intent": counter_to_dict(expected_intent_counter),
        "step_intent": counter_to_dict(intent_counter),
        "step_action": counter_to_dict(action_counter),
        "final_action": counter_to_dict(final_action_counter),
        "final_intent": counter_to_dict(final_intent_counter),
        "trajectory_length": length_stats(trajectory_lengths),
        "tool_call_steps_per_row": length_stats(tool_call_steps_per_row),
        "tool_calls_per_row": length_stats(tool_calls_per_row),
        "query_length": length_stats(query_lengths),
        "observation_chars": length_stats(observation_chars),
        "total_steps": total_steps,
        "total_tool_calls": total_tool_calls,
        "rows_with_tool_call": rows_with_tool_call,
        "rows_with_tool_call_pct": pct(rows_with_tool_call, len(rows)),
        "rows_with_search_tool_call": rows_with_search_tool_call,
        "rows_with_no_tools": rows_with_no_tools,
        "rows_with_no_tools_pct": pct(rows_with_no_tools, len(rows)),
        "rows_with_tool_error": rows_with_tool_error,
        "rows_with_injected_failure": rows_with_injected_failure,
        "rows_with_ask": rows_with_ask,
        "rows_with_overridden": rows_with_overridden,
        "rows_with_raw_empty_tool_call": rows_with_raw_empty_tool_call,
        "rows_with_forbidden_tool_call": rows_with_forbidden_tool_call,
        "rows_with_intent_reference": rows_with_intent_reference,
        "rows_with_reasoning_content_conflict": rows_with_reasoning_content_conflict,
        "rows_with_expected_intent_conflict": rows_with_expected_intent_conflict,
        "rows_with_empty_backfill": rows_with_empty_backfill,
        "malformed_steps": malformed_steps,
        "missing_final": missing_final,
        "exposed_tools": counter_to_dict(exposed_tool_counter),
        "tool_calls_by_name": counter_to_dict(tool_counter),
        "observations_by_name": counter_to_dict(observation_tool_counter),
        "estimated_stepwise_samples": total_steps,
        "special_trajectory_rows": sampling_mode_counter.get("no_tools", 0) + sampling_mode_counter.get("irrelevant_tools", 0),
        "special_trajectory_rows_pct": pct(sampling_mode_counter.get("no_tools", 0) + sampling_mode_counter.get("irrelevant_tools", 0), len(rows)),
        "special_stepwise_samples": steps_by_sampling_mode.get("no_tools", 0) + steps_by_sampling_mode.get("irrelevant_tools", 0),
        "special_stepwise_samples_pct": pct(steps_by_sampling_mode.get("no_tools", 0) + steps_by_sampling_mode.get("irrelevant_tools", 0), total_steps),
    }


def stats_message_sft(rows: list[dict[str, Any]]) -> dict[str, Any]:
    action_counter: Counter[str] = Counter()
    source_counter: Counter[str] = Counter()
    role_counter: Counter[str] = Counter()
    tool_counter: Counter[str] = Counter()
    exposed_tool_counter: Counter[str] = Counter()
    message_count_lengths: list[int] = []
    total_content_lengths: list[int] = []
    assistant_lengths: list[int] = []
    rows_with_tools = 0
    rows_with_tool_messages = 0
    rows_with_reasoning = 0
    rows_with_forbidden_tool = 0

    for row in rows:
        metadata = row.get("metadata") or {}
        if isinstance(metadata, dict):
            action_counter[str(metadata.get("action") or "") or "(missing)"] += 1
            source_counter[str(metadata.get("source") or metadata.get("source_id") or "") or "(missing)"] += 1
        messages = row.get("messages") or []
        if not isinstance(messages, list):
            messages = []
        message_count_lengths.append(len(messages))
        total_content_lengths.append(sum(safe_len(message.get("content")) for message in messages if isinstance(message, dict)))
        roles = message_roles(messages)
        role_counter.update(roles)
        rows_with_tool_messages += int(roles.get("tool", 0) > 0)
        for message in messages:
            if not isinstance(message, dict):
                continue
            content = str(message.get("content") or "")
            if message.get("role") == "assistant":
                assistant_lengths.append(len(content))
                if "<think>" in content:
                    rows_with_reasoning += 1
            for tool_call in message.get("tool_calls") or []:
                function = tool_call.get("function") or {}
                name = str(function.get("name") or "")
                if name:
                    tool_counter[name] += 1
                    if name == "write_file":
                        rows_with_forbidden_tool += 1
        tools = row.get("tools") or []
        if isinstance(tools, list):
            rows_with_tools += int(bool(tools))
            for tool in tools:
                name = get_tool_name(tool)
                if name:
                    exposed_tool_counter[name] += 1

    return {
        **base_stats(rows),
        "action": counter_to_dict(action_counter),
        "message_roles": counter_to_dict(role_counter),
        "source": counter_to_dict(source_counter),
        "message_count": length_stats(message_count_lengths),
        "message_content_chars": length_stats(total_content_lengths),
        "assistant_content_chars": length_stats(assistant_lengths),
        "rows_with_exposed_tools": rows_with_tools,
        "rows_with_exposed_tools_pct": pct(rows_with_tools, len(rows)),
        "rows_with_tool_messages": rows_with_tool_messages,
        "rows_with_tool_messages_pct": pct(rows_with_tool_messages, len(rows)),
        "rows_with_reasoning": rows_with_reasoning,
        "rows_with_reasoning_pct": pct(rows_with_reasoning, len(rows)),
        "rows_with_forbidden_tool_call": rows_with_forbidden_tool,
        "exposed_tools": counter_to_dict(exposed_tool_counter),
        "tool_calls_by_name": counter_to_dict(tool_counter),
    }


def stats_executor_answer(rows: list[dict[str, Any]]) -> dict[str, Any]:
    base = stats_message_sft(rows)
    intent_counter: Counter[str] = Counter()
    tool_name_counter: Counter[str] = Counter()
    sampling_mode_counter: Counter[str] = Counter()
    answer_lengths: list[int] = []
    external_lengths: list[int] = []
    rows_with_external = 0
    rows_with_tool_error = 0
    rows_with_file_write_plan = 0
    rows_file_written = 0
    rows_with_clarification_ask = 0

    for row in rows:
        intent_counter[str(row.get("intent") or "") or "(missing)"] += 1
        answer = row.get("executor_raw_output") or {}
        if isinstance(answer, dict):
            answer_lengths.append(safe_len(answer.get("text") or answer.get("content")))
        else:
            answer_lengths.append(0)
        external_info = str(row.get("external_info") or "")
        external_lengths.append(len(external_info))
        rows_with_external += int(bool(external_info.strip()) and external_info.strip() != "无外部补充信息。")
        rows_with_file_write_plan += int(row.get("file_write_plan") is not None)
        rows_file_written += int(row.get("file_write_result") is not None)
        rows_with_clarification_ask += int(row.get("clarification_ask") is not None)
        metadata = row.get("metadata") or {}
        if isinstance(metadata, dict):
            rows_with_tool_error += int(bool(metadata.get("has_tool_error")))
            sampling_mode_counter[str(metadata.get("planner_tool_sampling_mode") or "") or "(missing)"] += 1
            for name in metadata.get("tool_names") or []:
                tool_name_counter[str(name)] += 1

    base.update(
        {
            "intent": counter_to_dict(intent_counter),
            "planner_tool_sampling_mode": counter_to_dict(sampling_mode_counter),
            "tool_names": counter_to_dict(tool_name_counter),
            "answer_chars": length_stats(answer_lengths),
            "external_info_chars": length_stats(external_lengths),
            "rows_with_external_info": rows_with_external,
            "rows_with_external_info_pct": pct(rows_with_external, len(rows)),
            "rows_with_tool_error": rows_with_tool_error,
            "rows_with_file_write_plan": rows_with_file_write_plan,
            "rows_file_written": rows_file_written,
            "rows_with_clarification_ask": rows_with_clarification_ask,
            "rows_with_clarification_ask_pct": pct(rows_with_clarification_ask, len(rows)),
        }
    )
    return base


def stats_final_prompt(rows: list[dict[str, Any]]) -> dict[str, Any]:
    def first_nonempty(metadata: dict[str, Any], keys: list[str]) -> str:
        for key in keys:
            value = metadata.get(key)
            if isinstance(value, bool):
                return str(value)
            text = str(value or "").strip()
            if text:
                return text
        return "(missing)"

    metadata_source_counter: Counter[str] = Counter()
    action_counter: Counter[str] = Counter()
    inferred_action_counter: Counter[str] = Counter()
    intent_counter: Counter[str] = Counter()
    answer_template_intent_counter: Counter[str] = Counter()
    metadata_intent_raw_counter: Counter[str] = Counter()
    metadata_answer_template_intent_raw_counter: Counter[str] = Counter()
    planner_final_intent_counter: Counter[str] = Counter()
    executor_answer_intent_counter: Counter[str] = Counter()
    merge_source_counter: Counter[str] = Counter()
    planner_tool_mode_counter: Counter[str] = Counter()
    include_tool_errors_counter: Counter[str] = Counter()
    has_tool_error_counter: Counter[str] = Counter()
    has_clarification_ask_counter: Counter[str] = Counter()
    tool_names_counter: Counter[str] = Counter()
    prompt_lengths: list[int] = []
    assistant_turns: list[int] = []
    tool_sections = Counter()
    rows_with_reasoning = 0
    rows_with_tool_schema = 0
    rows_with_executor_answer = 0
    rows_with_planner_final = 0
    rows_with_nonempty_intent = 0
    rows_with_nonempty_answer_template_intent = 0
    rows_with_tool_error = 0
    rows_with_clarification_ask = 0
    rows_with_tool_names = 0

    for row in rows:
        prompt = str(row.get("prompt") or "")
        prompt_lengths.append(len(prompt))
        assistant_turns.append(prompt.count("<|im_start|>assistant"))
        sections = extract_prompt_sections(prompt)
        tool_sections.update({key: int(value) for key, value in sections.items()})
        rows_with_reasoning += int(sections["has_think"])
        rows_with_tool_schema += int(sections["has_tool_schema"])
        inferred_action_counter[infer_action_from_prompt(prompt)] += 1
        metadata = row.get("metadata") or {}
        if isinstance(metadata, dict):
            merge_source = str(metadata.get("merge_source") or "") or "(missing)"
            metadata_source_counter[str(metadata.get("source") or "") or "(missing)"] += 1
            merge_source_counter[merge_source] += 1
            action_counter[str(metadata.get("action") or "") or "(missing)"] += 1
            raw_intent = first_nonempty(metadata, ["intent"])
            raw_answer_template_intent = first_nonempty(metadata, ["answer_template_intent"])
            metadata_intent_raw_counter[raw_intent] += 1
            metadata_answer_template_intent_raw_counter[raw_answer_template_intent] += 1
            intent = first_nonempty(metadata, ["intent", "answer_template_intent"])
            answer_template_intent = first_nonempty(metadata, ["answer_template_intent", "intent"])
            inferred_intent = infer_intent_from_prompt(prompt)
            if intent == "(missing)" and inferred_intent != "(missing)":
                intent = inferred_intent
            if answer_template_intent == "(missing)" and inferred_intent != "(missing)":
                answer_template_intent = inferred_intent
            intent_counter[intent] += 1
            answer_template_intent_counter[answer_template_intent] += 1
            if merge_source == "planner_final":
                planner_final_intent_counter[intent] += 1
            elif merge_source == "executor_answer":
                executor_answer_intent_counter[intent] += 1
            planner_tool_mode_counter[str(metadata.get("planner_tool_sampling_mode") or "") or "(missing)"] += 1
            include_tool_errors_counter[str(bool(metadata.get("include_tool_errors")))] += 1
            has_tool_error = bool(metadata.get("has_tool_error"))
            has_clarification_ask = bool(metadata.get("has_clarification_ask"))
            has_tool_error_counter[str(has_tool_error)] += 1
            has_clarification_ask_counter[str(has_clarification_ask)] += 1
            rows_with_executor_answer += int(merge_source == "executor_answer")
            rows_with_planner_final += int(merge_source == "planner_final")
            rows_with_nonempty_intent += int(intent != "(missing)")
            rows_with_nonempty_answer_template_intent += int(answer_template_intent != "(missing)")
            rows_with_tool_error += int(has_tool_error)
            rows_with_clarification_ask += int(has_clarification_ask)
            tool_names = metadata.get("tool_names") or []
            if isinstance(tool_names, str):
                tool_names = [name.strip() for name in tool_names.split(",") if name.strip()]
            if isinstance(tool_names, list):
                rows_with_tool_names += int(bool(tool_names))
                for name in tool_names:
                    name_text = str(name or "").strip()
                    if name_text:
                        tool_names_counter[name_text] += 1

    return {
        **base_stats(rows),
        "metadata_source": counter_to_dict(metadata_source_counter),
        "metadata_action": counter_to_dict(action_counter),
        "metadata_intent": counter_to_dict(metadata_intent_raw_counter),
        "metadata_answer_template_intent": counter_to_dict(metadata_answer_template_intent_raw_counter),
        "effective_intent": counter_to_dict(intent_counter),
        "effective_answer_template_intent": counter_to_dict(answer_template_intent_counter),
        "planner_final_intent": counter_to_dict(planner_final_intent_counter),
        "executor_answer_intent": counter_to_dict(executor_answer_intent_counter),
        "merge_source": counter_to_dict(merge_source_counter),
        "planner_tool_sampling_mode": counter_to_dict(planner_tool_mode_counter),
        "include_tool_errors": counter_to_dict(include_tool_errors_counter),
        "has_tool_error": counter_to_dict(has_tool_error_counter),
        "has_clarification_ask": counter_to_dict(has_clarification_ask_counter),
        "tool_names": counter_to_dict(tool_names_counter),
        "inferred_final_action": counter_to_dict(inferred_action_counter),
        "prompt_chars": length_stats(prompt_lengths),
        "assistant_turns": length_stats(assistant_turns),
        "rows_with_reasoning": rows_with_reasoning,
        "rows_with_reasoning_pct": pct(rows_with_reasoning, len(rows)),
        "rows_with_tool_schema": rows_with_tool_schema,
        "rows_with_tool_schema_pct": pct(rows_with_tool_schema, len(rows)),
        "rows_with_executor_answer": rows_with_executor_answer,
        "rows_with_executor_answer_pct": pct(rows_with_executor_answer, len(rows)),
        "rows_with_planner_final": rows_with_planner_final,
        "rows_with_planner_final_pct": pct(rows_with_planner_final, len(rows)),
        "rows_with_nonempty_intent": rows_with_nonempty_intent,
        "rows_with_nonempty_intent_pct": pct(rows_with_nonempty_intent, len(rows)),
        "rows_with_nonempty_answer_template_intent": rows_with_nonempty_answer_template_intent,
        "rows_with_nonempty_answer_template_intent_pct": pct(rows_with_nonempty_answer_template_intent, len(rows)),
        "rows_with_tool_error": rows_with_tool_error,
        "rows_with_tool_error_pct": pct(rows_with_tool_error, len(rows)),
        "rows_with_clarification_ask": rows_with_clarification_ask,
        "rows_with_clarification_ask_pct": pct(rows_with_clarification_ask, len(rows)),
        "rows_with_tool_names": rows_with_tool_names,
        "rows_with_tool_names_pct": pct(rows_with_tool_names, len(rows)),
        "prompt_sections": counter_to_dict(tool_sections),
    }


def analyze_rows(rows: list[dict[str, Any]], file_type: str) -> dict[str, Any]:
    if file_type == "planner_trajectory":
        return stats_planner_trajectory(rows)
    if file_type == "executor_answer_sft":
        return stats_executor_answer(rows)
    if file_type in {"planner_step_sft", "message_sft"}:
        return stats_message_sft(rows)
    if file_type in {"final_prompt", "merged_final_prompt"}:
        return stats_final_prompt(rows)
    if file_type == "seed":
        return stats_seed(rows)
    return base_stats(rows)


def analyze_file(path: Path, max_rows: int | None = None) -> FileStats:
    read_result = read_jsonl(path, max_rows=max_rows)
    file_type = detect_file_type(read_result.rows, path)
    stats = analyze_rows(read_result.rows, file_type)
    return FileStats(
        path=str(path),
        file_type=file_type,
        total_lines=read_result.total_lines,
        json_rows=len(read_result.rows),
        blank_lines=read_result.blank_lines,
        invalid_lines=read_result.invalid_lines,
        duplicate_ids=count_duplicate_ids(read_result.rows),
        stats=stats,
    )


def length_table_rows(stats: dict[str, Any], keys: list[str]) -> list[list[Any]]:
    rows = []
    for key in keys:
        value = stats.get(key)
        if not isinstance(value, dict) or "count" not in value:
            continue
        rows.append([key, value["count"], value["min"], value["avg"], value["p50"], value["p90"], value["p95"], value["p99"], value["max"]])
    return rows


def counter_table(counter_dict: dict[str, int], total: int, limit: int = 20) -> list[list[Any]]:
    items = sorted(counter_dict.items(), key=lambda item: item[1], reverse=True)[:limit]
    return [[key, value, pct(value, total)] for key, value in items]


def compact_counter_table(counter_dict: dict[str, int], total: int, limit: int = 8) -> list[list[Any]]:
    rows = []
    for key, value in sorted(counter_dict.items(), key=lambda item: item[1], reverse=True)[:limit]:
        percent = pct(value, total)
        rows.append([key, value, fmt_pct(percent), meter(percent)])
    return rows


def get_file_label(file_stats: FileStats) -> str:
    return f"{Path(file_stats.path).name}（{file_stats.file_type}）"


def build_highlights(file_stats: FileStats) -> list[str]:
    stats = file_stats.stats
    highlights: list[str] = []
    if file_stats.file_type == "planner_trajectory":
        highlights.extend(
            [
                f"轨迹 {stats.get('rows', file_stats.json_rows)} 条，预计 step-wise SFT {stats.get('estimated_stepwise_samples', 0)} 条，平均轨迹长度 {(stats.get('trajectory_length') or {}).get('avg', 0):.2f}。",
                f"特殊轨迹占 {fmt_pct(stats.get('special_trajectory_rows_pct'))}，但转换后特殊 step 只占 {fmt_pct(stats.get('special_stepwise_samples_pct'))}。",
                f"工具调用轨迹占 {fmt_pct(stats.get('rows_with_tool_call_pct'))}，无工具轨迹占 {fmt_pct(stats.get('rows_with_no_tools_pct'))}。",
                f"最终动作：planning_finish={(stats.get('final_action') or {}).get('planning_finish', 0)}，ask_clarification={(stats.get('final_action') or {}).get('ask_clarification', 0)}。",
                f"风险计数：重复 id={file_stats.duplicate_ids}，raw_content 空工具调用轨迹={stats.get('rows_with_raw_empty_tool_call', 0)}，expected intent 冲突={stats.get('rows_with_expected_intent_conflict', 0)}，forbidden tool={stats.get('rows_with_forbidden_tool_call', 0)}。",
            ]
        )
    elif file_stats.file_type == "executor_answer_sft":
        highlights.extend(
            [
                f"执行阶段样本 {stats.get('rows', file_stats.json_rows)} 条，外部参考覆盖 {fmt_pct(stats.get('rows_with_external_info_pct'))}，追问建议覆盖 {fmt_pct(stats.get('rows_with_clarification_ask_pct'))}。",
                f"平均回答长度 {(stats.get('answer_chars') or {}).get('avg', 0):.0f} 字，平均外部参考长度 {(stats.get('external_info_chars') or {}).get('avg', 0):.0f} 字。",
                f"工具错误样本={stats.get('rows_with_tool_error', 0)}，写文件计划={stats.get('rows_with_file_write_plan', 0)}，实际写文件={stats.get('rows_file_written', 0)}。",
            ]
        )
    elif file_stats.file_type in {"planner_step_sft", "message_sft"}:
        highlights.extend(
            [
                f"step-wise 样本 {stats.get('rows', file_stats.json_rows)} 条，含 reasoning {fmt_pct(stats.get('rows_with_reasoning_pct'))}，含工具消息 {fmt_pct(stats.get('rows_with_tool_messages_pct'))}。",
                f"平均 messages 数 {(stats.get('message_count') or {}).get('avg', 0):.2f}，平均 assistant 文本 {(stats.get('assistant_content_chars') or {}).get('avg', 0):.0f} 字。",
            ]
        )
    elif file_stats.file_type in {"final_prompt", "merged_final_prompt"}:
        highlights.extend(
            [
                f"最终 prompt 样本 {stats.get('rows', file_stats.json_rows)} 条，含 reasoning {fmt_pct(stats.get('rows_with_reasoning_pct'))}，含工具 schema {fmt_pct(stats.get('rows_with_tool_schema_pct'))}。",
                f"平均 prompt 长度 {(stats.get('prompt_chars') or {}).get('avg', 0):.0f} 字，平均 assistant 轮数 {(stats.get('assistant_turns') or {}).get('avg', 0):.2f}。",
                f"merge_source：planner_final={stats.get('merge_source', {}).get('planner_final', 0)}，executor_answer={stats.get('merge_source', {}).get('executor_answer', 0)}。",
                f"intent 覆盖率 {fmt_pct(stats.get('rows_with_nonempty_intent_pct'))}，answer_template_intent 覆盖率 {fmt_pct(stats.get('rows_with_nonempty_answer_template_intent_pct'))}。",
            ]
        )
    else:
        highlights.append(f"JSON 样本 {file_stats.json_rows} 条，重复 id={file_stats.duplicate_ids}，无效行={file_stats.invalid_lines}。")
    return highlights


def build_issues(file_stats: FileStats) -> list[tuple[str, str]]:
    stats = file_stats.stats
    issues: list[tuple[str, str]] = []
    if file_stats.invalid_lines:
        issues.append(("高", f"存在 {file_stats.invalid_lines} 行无效 JSON，会影响下游读取。"))
    if file_stats.duplicate_ids:
        issues.append(("中", f"存在 {file_stats.duplicate_ids} 个重复 id，断点续跑/去重可能跳样本。"))

    if file_stats.file_type == "planner_trajectory":
        special_step_pct = float(stats.get("special_stepwise_samples_pct") or 0)
        special_traj_pct = float(stats.get("special_trajectory_rows_pct") or 0)
        raw_empty = int(stats.get("rows_with_raw_empty_tool_call") or 0)
        expected_conflict = int(stats.get("rows_with_expected_intent_conflict") or 0)
        forbidden = int(stats.get("rows_with_forbidden_tool_call") or 0)
        final_actions = stats.get("final_action") or {}
        final_tool_call = int(final_actions.get("tool_call", 0))

        if special_step_pct < 25:
            issues.append(("中", f"特殊场景轨迹占 {fmt_pct(special_traj_pct)}，但 step-wise 只占 {fmt_pct(special_step_pct)}，无工具/无关工具训练信号可能偏弱。"))
        elif special_step_pct > 45:
            issues.append(("低", f"特殊场景 step-wise 占 {fmt_pct(special_step_pct)}，可能让模型偏保守。"))
        if raw_empty:
            issues.append(("中", f"仍有 {raw_empty} 条轨迹包含 raw_content 为空的工具调用 step；若已回填可忽略，否则会污染 intent。"))
        if expected_conflict:
            issues.append(("低", f"有 {expected_conflict} 条轨迹 content intent 与 expected_intent 不一致，需要抽样确认是否为标注或模型判断差异。"))
        if forbidden:
            issues.append(("高", f"存在 {forbidden} 条轨迹调用规划阶段禁用工具。"))
        if final_tool_call:
            issues.append(("高", f"存在 {final_tool_call} 条轨迹最终动作仍是 tool_call，规划未闭环。"))

    if file_stats.file_type == "executor_answer_sft":
        answer_avg = float((stats.get("answer_chars") or {}).get("avg", 0) or 0)
        if answer_avg < 200:
            issues.append(("低", f"平均回答长度 {answer_avg:.0f} 字，可能偏短。"))
        if stats.get("rows_with_external_info_pct", 0) < 50:
            issues.append(("中", f"外部参考覆盖率仅 {fmt_pct(stats.get('rows_with_external_info_pct'))}。"))

    if file_stats.file_type == "merged_final_prompt":
        missing_effective_intents = int((stats.get("effective_intent") or {}).get("(missing)", 0) or 0)
        if missing_effective_intents:
            issues.append(("中", f"仍有 {missing_effective_intents} 条样本无法从 metadata 或 prompt 推断意图。"))
        if stats.get("rows_with_tool_error", 0):
            issues.append(("低", f"包含 {stats.get('rows_with_tool_error', 0)} 条工具错误样本，确认是否是期望保留的鲁棒性数据。"))
        if stats.get("rows_with_clarification_ask", 0):
            issues.append(("低", f"包含 {stats.get('rows_with_clarification_ask', 0)} 条澄清追问样本，应确认训练目标包含 ask 场景。"))

    return issues


def render_distribution(report: MarkdownReport, title: str, values: dict[str, int], limit: int = 8) -> None:
    if not values:
        return
    report.h3(title)
    report.table(["类别", "数量", "占比", "分布"], compact_counter_table(values, sum(values.values()), limit=limit))


def render_focused_file(report: MarkdownReport, file_stats: FileStats) -> None:
    stats = file_stats.stats
    report.h2(get_file_label(file_stats))
    report.h3("重点摘要")
    report.bullets(build_highlights(file_stats))

    issues = build_issues(file_stats)
    if issues:
        report.h3("需要关注")
        report.table(["级别", "问题"], issues)
    else:
        report.h3("需要关注")
        report.paragraph("未发现明显高优先级问题。")

    if file_stats.file_type == "planner_trajectory":
        report.h3("核心比例")
        report.table(
            ["指标", "值", "分布"],
            [
                ["特殊轨迹占比", fmt_pct(stats.get("special_trajectory_rows_pct")), meter(stats.get("special_trajectory_rows_pct") or 0)],
                ["特殊 step-wise 占比", fmt_pct(stats.get("special_stepwise_samples_pct")), meter(stats.get("special_stepwise_samples_pct") or 0)],
                ["工具调用轨迹占比", fmt_pct(stats.get("rows_with_tool_call_pct")), meter(stats.get("rows_with_tool_call_pct") or 0)],
                ["无工具轨迹占比", fmt_pct(stats.get("rows_with_no_tools_pct")), meter(stats.get("rows_with_no_tools_pct") or 0)],
                ["工具失败轨迹占比", fmt_pct(pct(stats.get("rows_with_tool_error", 0), stats.get("rows", file_stats.json_rows))), meter(pct(stats.get("rows_with_tool_error", 0), stats.get("rows", file_stats.json_rows)))],
                ["ask_clarification 占比", fmt_pct(pct(stats.get("rows_with_ask", 0), stats.get("total_steps", 0))), meter(pct(stats.get("rows_with_ask", 0), stats.get("total_steps", 0)))],
            ],
        )
        render_distribution(report, "轨迹模式分布", stats.get("tool_sampling_mode") or {})
        render_distribution(report, "step-wise 模式分布", stats.get("steps_by_sampling_mode") or {})
        render_distribution(report, "最终动作分布", stats.get("final_action") or {})
        render_distribution(report, "最终意图分布", stats.get("final_intent") or {})
        render_distribution(report, "工具调用 Top", top_n(stats.get("tool_calls_by_name") or {}, 10), limit=10)
    elif file_stats.file_type == "executor_answer_sft":
        report.h3("核心比例")
        report.table(
            ["指标", "值", "分布"],
            [
                ["外部参考覆盖率", fmt_pct(stats.get("rows_with_external_info_pct")), meter(stats.get("rows_with_external_info_pct") or 0)],
                ["追问建议覆盖率", fmt_pct(stats.get("rows_with_clarification_ask_pct")), meter(stats.get("rows_with_clarification_ask_pct") or 0)],
                ["工具错误样本占比", fmt_pct(pct(stats.get("rows_with_tool_error", 0), stats.get("rows", file_stats.json_rows))), meter(pct(stats.get("rows_with_tool_error", 0), stats.get("rows", file_stats.json_rows)))],
                ["写文件计划占比", fmt_pct(pct(stats.get("rows_with_file_write_plan", 0), stats.get("rows", file_stats.json_rows))), meter(pct(stats.get("rows_with_file_write_plan", 0), stats.get("rows", file_stats.json_rows)))],
            ],
        )
        render_distribution(report, "执行意图分布", stats.get("intent") or {})
        render_distribution(report, "来源规划模式分布", stats.get("planner_tool_sampling_mode") or {})
    elif file_stats.file_type in {"planner_step_sft", "message_sft"}:
        render_distribution(report, "训练动作分布", stats.get("action") or {})
        render_distribution(report, "消息角色分布", stats.get("message_roles") or {})
        render_distribution(report, "工具调用 Top", stats.get("tool_calls_by_name") or {})
    elif file_stats.file_type in {"final_prompt", "merged_final_prompt"}:
        render_distribution(report, "merge_source", stats.get("merge_source") or {})
        render_distribution(report, "有效意图分布", stats.get("effective_intent") or {})
        render_distribution(report, "planner_final 意图分布", stats.get("planner_final_intent") or {})
        render_distribution(report, "executor_answer 意图分布", stats.get("executor_answer_intent") or {})
        render_distribution(report, "有效 answer_template_intent 分布", stats.get("effective_answer_template_intent") or {})
        render_distribution(report, "metadata.action 分布", stats.get("metadata_action") or {})
        render_distribution(report, "planner_tool_sampling_mode 分布", stats.get("planner_tool_sampling_mode") or {})
        render_distribution(report, "include_tool_errors 分布", stats.get("include_tool_errors") or {})
        render_distribution(report, "has_tool_error 分布", stats.get("has_tool_error") or {})
        render_distribution(report, "has_clarification_ask 分布", stats.get("has_clarification_ask") or {})
        render_distribution(report, "工具名 Top", stats.get("tool_names") or {})
        render_distribution(report, "原始 metadata.intent 分布", stats.get("metadata_intent") or {})
        render_distribution(report, "metadata.source 字段", stats.get("metadata_source") or {})
        render_distribution(report, "最终动作推断", stats.get("inferred_final_action") or {})

    length_keys = [
        "trajectory_length",
        "tool_calls_per_row",
        "query_length",
        "answer_chars",
        "external_info_chars",
        "prompt_chars",
    ]
    rows = length_table_rows(stats, length_keys)
    if rows:
        report.h3("关键长度分布")
        report.table(["指标", "count", "min", "avg", "p50", "p90", "p95", "p99", "max"], rows)


def render_markdown_file(report: MarkdownReport, file_stats: FileStats) -> None:
    stats = file_stats.stats
    report.h2(Path(file_stats.path).name)
    report.table(
        ["字段", "值"],
        [
            ["路径", file_stats.path],
            ["识别类型", file_stats.file_type],
            ["总行数", file_stats.total_lines],
            ["JSON 行数", file_stats.json_rows],
            ["空行", file_stats.blank_lines],
            ["无效 JSON 行", file_stats.invalid_lines],
            ["重复 id 数", file_stats.duplicate_ids],
        ],
    )

    if not file_stats.json_rows:
        return

    common_counter_keys = [
        "scenario",
        "tool_sampling_mode",
        "expected_intent",
        "step_intent",
        "step_action",
        "final_action",
        "final_intent",
        "intent",
        "action",
        "metadata_source",
        "metadata_action",
        "merge_source",
        "effective_intent",
        "effective_answer_template_intent",
        "metadata_intent",
        "metadata_answer_template_intent",
        "planner_final_intent",
        "executor_answer_intent",
        "inferred_final_action",
        "planner_tool_sampling_mode",
        "include_tool_errors",
        "has_tool_error",
        "has_clarification_ask",
    ]
    for key in common_counter_keys:
        value = stats.get(key)
        if isinstance(value, dict) and value:
            report.h3(key)
            report.table(["值", "数量", "占比%"], counter_table(value, sum(value.values())))

    length_keys = [
        "trajectory_length",
        "tool_call_steps_per_row",
        "tool_calls_per_row",
        "query_length",
        "observation_chars",
        "message_count",
        "message_content_chars",
        "assistant_content_chars",
        "answer_chars",
        "external_info_chars",
        "prompt_chars",
        "assistant_turns",
    ]
    rows = length_table_rows(stats, length_keys)
    if rows:
        report.h3("长度 / 数量分布")
        report.table(["指标", "count", "min", "avg", "p50", "p90", "p95", "p99", "max"], rows)

    important_keys = [
        "total_steps",
        "total_tool_calls",
        "estimated_stepwise_samples",
        "rows_with_tool_call",
        "rows_with_tool_call_pct",
        "rows_with_no_tools",
        "rows_with_no_tools_pct",
        "special_trajectory_rows",
        "special_trajectory_rows_pct",
        "special_stepwise_samples",
        "special_stepwise_samples_pct",
        "rows_with_search_tool_call",
        "rows_with_tool_error",
        "rows_with_injected_failure",
        "rows_with_ask",
        "rows_with_overridden",
        "rows_with_raw_empty_tool_call",
        "rows_with_forbidden_tool_call",
        "rows_with_intent_reference",
        "rows_with_reasoning_content_conflict",
        "rows_with_expected_intent_conflict",
        "rows_with_empty_backfill",
        "rows_with_exposed_tools",
        "rows_with_exposed_tools_pct",
        "rows_with_tool_messages",
        "rows_with_tool_messages_pct",
        "rows_with_reasoning",
        "rows_with_reasoning_pct",
        "rows_with_external_info",
        "rows_with_external_info_pct",
        "rows_with_file_write_plan",
        "rows_file_written",
        "rows_with_clarification_ask",
        "rows_with_clarification_ask_pct",
        "rows_with_tool_schema",
        "rows_with_tool_schema_pct",
        "rows_with_executor_answer",
        "rows_with_executor_answer_pct",
        "rows_with_planner_final",
        "rows_with_planner_final_pct",
        "rows_with_nonempty_intent",
        "rows_with_nonempty_intent_pct",
        "rows_with_nonempty_answer_template_intent",
        "rows_with_nonempty_answer_template_intent_pct",
        "rows_with_tool_error_pct",
        "rows_with_tool_names",
        "rows_with_tool_names_pct",
        "malformed_steps",
        "missing_final",
    ]
    important_rows = [[key, stats[key]] for key in important_keys if key in stats]
    if important_rows:
        report.h3("关键指标")
        report.table(["指标", "值"], important_rows)

    tool_counter_keys = [
        "steps_by_sampling_mode",
        "tool_calls_by_sampling_mode",
        "exposed_tools",
        "tool_calls_by_name",
        "observations_by_name",
        "tool_names",
        "message_roles",
        "prompt_sections",
    ]
    for key in tool_counter_keys:
        value = stats.get(key)
        if isinstance(value, dict) and value:
            total = sum(value.values()) if value else file_stats.json_rows
            report.h3(key)
            report.table(["值", "数量", "占比%"], counter_table(value, total))


def render_markdown(files: list[FileStats]) -> str:
    report = MarkdownReport()
    report.h1("Agent 数据统计报告")
    report.paragraph("默认展示重点摘要、风险提示和关键比例。若需全量明细，可加 `--detail`。")
    report.table(
        ["文件", "类型", "JSON 行数", "无效行", "重复 id", "总步骤/样本"],
        [
            [
                Path(file_stats.path).name,
                file_stats.file_type,
                file_stats.json_rows,
                file_stats.invalid_lines,
                file_stats.duplicate_ids,
                file_stats.stats.get("total_steps") or file_stats.stats.get("rows") or file_stats.json_rows,
            ]
            for file_stats in files
        ],
    )
    for file_stats in files:
        render_focused_file(report, file_stats)
    return report.text()


def render_detail_markdown(files: list[FileStats]) -> str:
    report = MarkdownReport()
    report.h1("Agent 数据统计报告（全量明细）")
    report.table(
        ["文件", "类型", "JSON 行数", "无效行", "重复 id", "总步骤/样本"],
        [
            [
                Path(file_stats.path).name,
                file_stats.file_type,
                file_stats.json_rows,
                file_stats.invalid_lines,
                file_stats.duplicate_ids,
                file_stats.stats.get("total_steps") or file_stats.stats.get("rows") or file_stats.json_rows,
            ]
            for file_stats in files
        ],
    )
    for file_stats in files:
        render_markdown_file(report, file_stats)
    return report.text()


def expand_paths(paths: list[Path]) -> list[Path]:
    if paths:
        return paths
    return [path for path in DEFAULT_PATHS if path.exists()]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Comprehensive statistics for agent JSONL data files.")
    parser.add_argument("paths", nargs="*", type=Path, help="要统计的 JSONL 文件；不传则统计 agent/outputs 下的默认文件。")
    parser.add_argument("--format", choices=["markdown", "json"], default="markdown", help="输出格式。")
    parser.add_argument("--output", type=Path, default=None, help="报告输出路径；不传则打印到 stdout。")
    parser.add_argument("--max-rows", type=int, default=None, help="最多读取每个文件多少条 JSON 行；调试大文件时可用。")
    parser.add_argument("--detail", action="store_true", help="输出全量明细而非重点摘要。")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    paths = expand_paths(args.paths)
    if not paths:
        raise FileNotFoundError("没有找到可统计的 JSONL 文件。")

    file_stats = [analyze_file(path, max_rows=args.max_rows) for path in paths]
    if args.format == "json":
        output_text = json.dumps([file.__dict__ for file in file_stats], ensure_ascii=False, indent=2)
    else:
        output_text = render_detail_markdown(file_stats) if args.detail else render_markdown(file_stats)

    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(output_text, encoding="utf-8")
        print(f"✅ 统计报告已写入：{args.output}")
    else:
        print(output_text)


if __name__ == "__main__":
    main()
