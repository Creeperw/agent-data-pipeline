"""Convert full trajectories into step-wise SFT samples.

Each planner step becomes one training example:
state + previous tool observations -> next planner action.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

try:
    from .config import CONVERT_TO_SFT_INPUT_FILE, PLANNER_STEP_SFT_FILE
    from .core.domain import DomainSpec, load_domain
except ImportError:
    from config import CONVERT_TO_SFT_INPUT_FILE, PLANNER_STEP_SFT_FILE
    from agent.core.domain import DomainSpec, load_domain


DEFAULT_INPUT = CONVERT_TO_SFT_INPUT_FILE
DEFAULT_OUTPUT = PLANNER_STEP_SFT_FILE

PLANNING_FORBIDDEN_TOOL_NAMES = {"write_file"}


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            if line.strip():
                rows.append(json.loads(line))
    return rows


def get_processed_ids(output_path: Path) -> set[str]:
    processed_ids: set[str] = set()
    if not output_path.exists():
        return processed_ids
    with output_path.open("r", encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            row_id = row.get("id")
            if row_id:
                processed_ids.add(str(row_id))
    return processed_ids


def compose_raw_text(reasoning_content: str | None, content: str | None) -> str:
    reasoning = (reasoning_content or "").strip()
    final_content = (content or "").strip()
    if reasoning and final_content:
        return f"<think>\n{reasoning}\n</think>\n{final_content}"
    if reasoning:
        return f"<think>\n{reasoning}\n</think>"
    return final_content


def normalize_tool_arguments(arguments: Any) -> dict[str, Any]:
    """Return tool arguments as a dict for tokenizer.apply_chat_template.

    Hugging Face/Qwen chat templates expect ``function.arguments`` to be a JSON
    object in the message list, not an already serialized JSON string.
    """
    if isinstance(arguments, dict):
        return arguments
    if isinstance(arguments, str):
        try:
            parsed = json.loads(arguments)
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            pass
        return {"query": arguments}
    return {"query": str(arguments)}


def planner_output_content_payload(planner_output: dict[str, Any]) -> dict[str, Any]:
    payload = {
        "intent": planner_output.get("intent"),
        "action": planner_output.get("action"),
    }
    if planner_output.get("action") == "ask_clarification":
        payload["ask"] = planner_output.get("ask")
    elif planner_output.get("action") == "planning_finish":
        payload["finish_reason"] = planner_output.get("finish_reason")
    return payload


def get_tool_name(tool: dict[str, Any]) -> str:
    return str((tool.get("function") or {}).get("name", ""))


def filter_planning_tools(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [tool for tool in tools if get_tool_name(tool) not in PLANNING_FORBIDDEN_TOOL_NAMES]


def get_row_domain(row: dict[str, Any]) -> DomainSpec:
    domain_name = str(row.get("domain") or "health")
    try:
        return load_domain(domain_name)
    except Exception:
        return load_domain("health")


def get_row_tools(row: dict[str, Any], domain: DomainSpec) -> list[dict[str, Any]]:
    """Return tools exactly exposed in the trajectory.

    ``tools=[]`` is meaningful for no-tool samples and must not fall back to the
    full catalog. Only older rows without a ``tools`` field use the current
    domain catalog.
    """
    if "tools" not in row:
        return domain.tools
    tools = row.get("tools")
    if not isinstance(tools, list):
        return []
    return tools


def has_forbidden_tool_call(planner_output: dict[str, Any]) -> bool:
    return any(tool_call.get("name") in PLANNING_FORBIDDEN_TOOL_NAMES for tool_call in get_planner_tool_calls(planner_output))


def make_user_content(row: dict[str, Any]) -> str:
    return str(row.get("user_query", ""))


def get_assistant_content(step: dict[str, Any], include_reasoning: bool) -> str:
    planner_output = step.get("planner_output", {})
    final_content = json.dumps(planner_output_content_payload(planner_output), ensure_ascii=False)
    if include_reasoning:
        raw_output = step.get("planner_raw_output") or {}
        return compose_raw_text(raw_output.get("reasoning_content"), final_content)
    return final_content


def get_planner_tool_calls(planner_output: dict[str, Any]) -> list[dict[str, Any]]:
    tool_calls = planner_output.get("tool_calls") or []
    if isinstance(tool_calls, dict):
        tool_calls = [tool_calls]
    if not tool_calls and planner_output.get("tool_call"):
        tool_calls = [planner_output["tool_call"]]
    return [tool_call for tool_call in tool_calls if isinstance(tool_call, dict)]


def make_tool_call_message(planner_output: dict[str, Any], assistant_content: str) -> dict[str, Any]:
    tool_calls = get_planner_tool_calls(planner_output)
    return {
        "role": "assistant",
        "content": assistant_content,
        "tool_calls": [
            {
                "type": "function",
                "function": {
                    "name": tool_call.get("name"),
                    "arguments": normalize_tool_arguments(tool_call.get("arguments") or {}),
                },
            }
            for tool_call in tool_calls
        ],
    }


def get_step_tool_call_id(step: dict[str, Any]) -> str:
    planner_output = step.get("planner_output", {})
    tool_call = planner_output.get("tool_call") or {}
    return tool_call.get("id") or f"call_{step.get('step', 'tool')}"


def make_tool_message(observation: dict[str, Any] | None) -> dict[str, Any] | None:
    if not observation:
        return None
    return {
        "role": "tool",
        "content": observation.get("content", ""),
    }


def get_step_observations(step: dict[str, Any]) -> list[dict[str, Any]]:
    observations = step.get("observations") or []
    if isinstance(observations, dict):
        observations = [observations]
    if not observations and step.get("observation"):
        observations = [step["observation"]]
    return [observation for observation in observations if isinstance(observation, dict)]


def make_tool_messages(step: dict[str, Any]) -> list[dict[str, Any]]:
    messages = []
    for observation in get_step_observations(step):
        tool_message = make_tool_message(observation)
        if tool_message:
            messages.append(tool_message)
    return messages


def make_history_messages(row: dict[str, Any], current_step: dict[str, Any], include_reasoning: bool, domain: DomainSpec) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": domain.planner_system_prompt},
        {"role": "user", "content": make_user_content(row)},
    ]

    for step in row.get("trajectory", []):
        if step is current_step:
            break
        planner_output = step.get("planner_output", {})
        assistant_content = get_assistant_content(step, include_reasoning)
        if planner_output.get("action") == "tool_call":
            messages.append(make_tool_call_message(planner_output, assistant_content))
            messages.extend(make_tool_messages(step))
        else:
            messages.append({"role": "assistant", "content": assistant_content})

    return messages


def convert_one_trajectory(row: dict[str, Any], include_reasoning: bool = True) -> list[dict[str, Any]]:
    samples: list[dict[str, Any]] = []
    domain = get_row_domain(row)

    for step in row.get("trajectory", []):
        planner_output = step.get("planner_output", {})
        if has_forbidden_tool_call(planner_output):
            break
        assistant_content = get_assistant_content(step, include_reasoning)
        messages = make_history_messages(row, step, include_reasoning, domain)
        if planner_output.get("action") == "tool_call":
            tool_call_message = make_tool_call_message(planner_output, assistant_content)
            if not messages or messages[-1] != tool_call_message:
                messages.append(tool_call_message)
        else:
            if not messages or messages[-1].get("content") != assistant_content:
                messages.append({"role": "assistant", "content": assistant_content})

        samples.append(
            {
                "id": f"{row.get('id')}_step_{step.get('step')}",
                "domain": domain.name,
                "tools": filter_planning_tools(get_row_tools(row, domain)),
                "messages": messages,
                "metadata": {
                    "domain": domain.name,
                    "source_id": row.get("id"),
                    "step": step.get("step"),
                    "action": planner_output.get("action"),
                    "include_reasoning": include_reasoning,
                    "planner_overridden": step.get("planner_overridden", False),
                },
            }
        )

    return samples


def convert_file(input_path: Path, output_path: Path, include_reasoning: bool = True, force_rebuild: bool = False) -> int:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if force_rebuild and output_path.exists():
        output_path.write_text("", encoding="utf-8")

    processed_ids = get_processed_ids(output_path)
    rows = load_jsonl(input_path)
    count = 0
    skipped = 0
    with output_path.open("a", encoding="utf-8") as f:
        for row in rows:
            for sample in convert_one_trajectory(row, include_reasoning=include_reasoning):
                sample_id = str(sample.get("id", ""))
                if sample_id and sample_id in processed_ids:
                    skipped += 1
                    continue
                f.write(json.dumps(sample, ensure_ascii=False) + "\n")
                if sample_id:
                    processed_ids.add(sample_id)
                count += 1
    if skipped:
        print(f"↪️ 已跳过 {skipped} 条已转换 SFT 样本。")
    return count


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Convert trajectories into SFT JSONL.")
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument(
        "--no-reasoning",
        action="store_true",
        help="只使用解析后的 JSON 作为 assistant 输出，不包含 <think> 思维过程。",
    )
    parser.add_argument("--force-rebuild", action="store_true", help="清空输出文件并重新转换全部样本。")
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    total = convert_file(args.input, args.output, include_reasoning=not args.no_reasoning, force_rebuild=args.force_rebuild)
    print(f"✅ 新增转换 {total} 条 step-wise SFT 样本：{args.output}")
