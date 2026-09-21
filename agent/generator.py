"""Build intent-to-tool-call planning trajectories with multi-round tool use.

This script is the first-stage agent environment for SFT data distillation:
- input has no dialogue history;
- the teacher planner recognizes intent;
- the teacher planner may call tools for multiple rounds;
- after enough observations, the planner must output ``planning_finish``.

模型接入（API 端点、教师模型）由运行时配置提供，缺值会在启动时直接报错。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import random
import re
import sys
from pathlib import Path
from typing import Any

from openai import AsyncOpenAI
from tqdm import tqdm

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

try:
    from .config import DistillConfig, output_file_for_domain, resolve_base_url, resolve_distill_model
    from .core.domain import DomainSpec, load_domain
    from .core.conversation import build_history_context_text, render_dialogue_turns, split_history_turns
except ImportError:  # Allows: python agent/generator.py
    from config import DistillConfig, output_file_for_domain, resolve_base_url, resolve_distill_model
    from agent.core.domain import DomainSpec, load_domain
    from agent.core.conversation import build_history_context_text, render_dialogue_turns, split_history_turns


write_lock = asyncio.Lock()

TOOL_SAMPLING_MODE_KEY = "__tool_sampling_mode"
TOOL_FAILURE_ENABLED_KEY = "__tool_failure_enabled"
INTENT_REFERENCE_KEY = "__intent_reference"

def normalize_intent_for_domain(intent: Any, domain: DomainSpec | None = None) -> str | None:
    if domain is None or not domain.has_intent:
        return None
    return domain.resolve_intent(intent)


def get_client(config: DistillConfig) -> AsyncOpenAI:
    return AsyncOpenAI(
        api_key=config.api_key or os.getenv("DEEPSEEK_API_KEY"),
        base_url=config.base_url or resolve_base_url(),
        timeout=float(os.getenv("AGENT_DISTILL_HTTP_TIMEOUT", "120")),
        max_retries=0,
    )


# Per-tool-call timeout (seconds) for synchronous tool execution. Exa / network
# tools occasionally hang; wrapping them with ``asyncio.wait_for`` prevents the
# whole event loop from being blocked indefinitely.
TOOL_CALL_TIMEOUT_SECONDS = float(os.getenv("AGENT_DISTILL_TOOL_TIMEOUT", "45"))
RETRY_BACKOFF_CAP_SECONDS = float(os.getenv("AGENT_DISTILL_RETRY_BACKOFF_CAP", "30"))


async def run_domain_tool_async(domain: DomainSpec, sample: dict[str, Any], name: str, arguments: dict[str, Any]) -> Any:
    """Run a tool through the current domain runtime with a hard timeout."""

    return await asyncio.wait_for(
        asyncio.to_thread(domain.run_tool, name, arguments, sample),
        timeout=TOOL_CALL_TIMEOUT_SECONDS,
    )


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if not path.exists():
        return rows
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            rows.append(json.loads(line))
    return rows


def normalize_expected_intents(expected_intents: Any, domain: DomainSpec) -> tuple[str, ...]:
    if expected_intents is None:
        return ()
    if isinstance(expected_intents, str):
        expected_intents = [expected_intents]
    if not domain.has_intent:
        return ()
    normalized: list[str] = []
    invalid: list[str] = []
    for intent in expected_intents:
        if intent is None:
            continue
        raw_name = str(intent).strip()
        if not raw_name:
            continue
        normalized_intent = domain.resolve_intent(raw_name, fallback_to_default=False)
        if normalized_intent and normalized_intent not in normalized:
            normalized.append(normalized_intent)
        elif not normalized_intent:
            invalid.append(raw_name)
    if invalid:
        valid_names = ", ".join(domain.intent_names()) or "无"
        raise ValueError(f"无效 expected_intent：{', '.join(invalid)}。领域 {domain.name} 可选：{valid_names}，或使用该领域已定义别名。")
    return tuple(normalized)


def canonicalize_intent_for_filter(intent: Any, domain: DomainSpec) -> str | None:
    return domain.resolve_intent(intent, fallback_to_default=False)


def sample_matches_expected_intents(sample: dict[str, Any], expected_intents: tuple[str, ...], domain: DomainSpec) -> bool:
    if not expected_intents:
        return True
    sample_intent = canonicalize_intent_for_filter(sample.get("expected_intent"), domain)
    return bool(sample_intent and sample_intent in expected_intents)


def get_processed_ids(output_file: Path) -> set[str]:
    processed_ids: set[str] = set()
    if not output_file.exists():
        return processed_ids
    with output_file.open("r", encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                continue
            data_id = data.get("id")
            if data_id:
                processed_ids.add(str(data_id))
    return processed_ids


def extract_json_object(text: str) -> dict[str, Any]:
    """Parse a JSON object from a teacher response."""
    text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, flags=re.S)
    if fenced:
        return json.loads(fenced.group(1))

    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        return json.loads(text[start : end + 1])

    raise ValueError(f"无法解析 JSON：{text[:300]}")


def normalize_action(action: dict[str, Any], domain: DomainSpec | None = None) -> dict[str, Any]:
    if domain is None or domain.has_intent:
        intent = normalize_intent_for_domain(action.get("intent"), domain)
        if not intent:
            raise ValueError("intent 缺失")
        action["intent"] = intent
    action.setdefault("tool_call", None)
    action.setdefault("tool_calls", [])
    action.setdefault("ask", None)
    action.setdefault("finish_reason", None)

    if action.get("action") == "tool_call":
        raw_tool_calls = action.get("tool_calls") or []
        if isinstance(raw_tool_calls, dict):
            raw_tool_calls = [raw_tool_calls]
        if not raw_tool_calls and action.get("tool_call"):
            raw_tool_calls = [action["tool_call"]]
        if not isinstance(raw_tool_calls, list) or not raw_tool_calls:
            raise ValueError("tool_calls 缺失")

        normalized_tool_calls = []
        for tool_call in raw_tool_calls:
            if not isinstance(tool_call, dict):
                raise ValueError("tool_call 必须是对象")
            tool_call = dict(tool_call)
            tool_call.setdefault("arguments", {})
            if "name" not in tool_call or not tool_call.get("name"):
                raise ValueError("tool_call.name 缺失")
            if not isinstance(tool_call.get("arguments"), dict):
                tool_call["arguments"] = {"query": str(tool_call.get("arguments"))}
            normalized_tool_calls.append(tool_call)

        action["tool_calls"] = normalized_tool_calls
        action["tool_call"] = normalized_tool_calls[0]
    elif action.get("action") not in {"ask_clarification", "planning_finish"}:
        raise ValueError(f"未知 action：{action.get('action')}")
    else:
        action["tool_call"] = None
        action["tool_calls"] = []

    return action


def parse_tool_call_arguments(arguments: Any) -> dict[str, Any]:
    if isinstance(arguments, dict):
        return arguments
    if isinstance(arguments, str):
        try:
            parsed = json.loads(arguments)
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            return {"query": arguments}
    return {"query": str(arguments)}


def serialize_tool_arguments(arguments: Any) -> str:
    if isinstance(arguments, str):
        return arguments
    return json.dumps(arguments or {}, ensure_ascii=False)


def get_tool_name(tool: dict[str, Any]) -> str:
    return str((tool.get("function") or {}).get("name", ""))


def parse_tool_failure_targets(targets: str | None) -> set[str]:
    if not targets:
        return set()
    return {item.strip() for item in targets.split(",") if item.strip()}


def get_tool_failure_budget(config: DistillConfig, failure_enabled: bool = False) -> int:
    if not failure_enabled or clamp_ratio(config.tool_failure_ratio) <= 0:
        return 0
    return max(0, int(config.tool_failure_max_calls))


def filter_failure_eligible_tool_calls(tool_calls: list[dict[str, Any]], config: DistillConfig) -> list[dict[str, Any]]:
    targets = parse_tool_failure_targets(config.tool_failure_targets)
    if not targets:
        return tool_calls
    return [tool_call for tool_call in tool_calls if tool_call.get("name") in targets]


def choose_failed_tool_call_ids(
    tool_calls: list[dict[str, Any]],
    config: DistillConfig,
    remaining_failure_budget: int,
    rng: random.Random | None = None,
) -> set[str]:
    if remaining_failure_budget <= 0:
        return set()
    eligible_tool_calls = filter_failure_eligible_tool_calls(tool_calls, config)
    if not eligible_tool_calls:
        return set()
    rng = rng or random
    shuffled_tool_calls = list(eligible_tool_calls)
    rng.shuffle(shuffled_tool_calls)
    fail_count = min(remaining_failure_budget, len(shuffled_tool_calls))
    return {str(tool_call.get("id")) for tool_call in shuffled_tool_calls[:fail_count] if tool_call.get("id")}


def make_tool_failure_result(tool_name: str, rng: random.Random | None = None) -> dict[str, Any]:
    rng = rng or random
    reasons = [
        "请求超时，未能获取有效结果。",
        "外部服务暂时不可用。",
        "返回内容为空或格式异常。",
        "检索服务繁忙，请稍后重试。",
        "网络连接异常，工具未完成执行。",
    ]
    return {
        "name": tool_name,
        "content": f"error：工具调用失败。失败工具：{tool_name}。原因：{rng.choice(reasons)}",
        "error": True,
        "failed_tool": tool_name,
    }


def tool_catalog_by_name(tools: list[dict[str, Any]] | None = None) -> dict[str, dict[str, Any]]:
    tool_list = tools or []
    return {get_tool_name(tool): tool for tool in tool_list if get_tool_name(tool)}


def clamp_ratio(value: float) -> float:
    return max(0.0, min(1.0, value))


def normalize_ratio_pair(first: float, second: float) -> tuple[float, float]:
    first = clamp_ratio(first)
    second = clamp_ratio(second)
    total = first + second
    if total > 1.0:
        first = first / total
        second = second / total
    return first, second


def quota_count(total: int, ratio: float) -> int:
    if total <= 0:
        return 0
    return max(0, min(total, int(total * clamp_ratio(ratio) + 0.5)))


def scenario_tool_sampling_mode(sample: dict[str, Any]) -> str | None:
    scenario = str(sample.get("scenario", ""))
    if "no_tool" in scenario:
        return "no_tools"
    if "irrelevant_tool" in scenario or "irrelevant_tools" in scenario:
        return "irrelevant_tools"
    return None


def assign_sample_controls(samples: list[dict[str, Any]], config: DistillConfig, rng: random.Random | None = None) -> None:
    """Assign dataset-level quotas for tool exposure and failure injection."""
    rng = rng or random.Random()
    total = len(samples)
    no_tool_ratio, irrelevant_tool_ratio = normalize_ratio_pair(config.no_tool_ratio, config.irrelevant_tool_ratio)
    target_no_tool_count = quota_count(total, no_tool_ratio)
    target_irrelevant_tool_count = quota_count(total, irrelevant_tool_ratio)

    unassigned: list[dict[str, Any]] = []
    explicit_no_tool_count = 0
    explicit_irrelevant_tool_count = 0
    for sample in samples:
        mode = scenario_tool_sampling_mode(sample)
        if mode == "no_tools":
            sample[TOOL_SAMPLING_MODE_KEY] = mode
            explicit_no_tool_count += 1
        elif mode == "irrelevant_tools":
            sample[TOOL_SAMPLING_MODE_KEY] = mode
            explicit_irrelevant_tool_count += 1
        else:
            unassigned.append(sample)

    rng.shuffle(unassigned)
    remaining_no_tool_count = min(max(0, target_no_tool_count - explicit_no_tool_count), len(unassigned))
    for sample in unassigned[:remaining_no_tool_count]:
        sample[TOOL_SAMPLING_MODE_KEY] = "no_tools"

    remaining = unassigned[remaining_no_tool_count:]
    remaining_irrelevant_tool_count = min(
        max(0, target_irrelevant_tool_count - explicit_irrelevant_tool_count),
        len(remaining),
    )
    for sample in remaining[:remaining_irrelevant_tool_count]:
        sample[TOOL_SAMPLING_MODE_KEY] = "irrelevant_tools"
    for sample in remaining[remaining_irrelevant_tool_count:]:
        sample[TOOL_SAMPLING_MODE_KEY] = "normal"

    failure_candidates = [sample for sample in samples if sample.get(TOOL_SAMPLING_MODE_KEY) != "no_tools"]
    target_failure_count = min(len(failure_candidates), quota_count(total, config.tool_failure_ratio))
    rng.shuffle(failure_candidates)
    failure_sample_ids = {id(sample) for sample in failure_candidates[:target_failure_count]}
    for sample in samples:
        sample[TOOL_FAILURE_ENABLED_KEY] = id(sample) in failure_sample_ids


def filter_samples_by_expected_intents(
    samples: list[dict[str, Any]],
    expected_intents: tuple[str, ...],
    domain: DomainSpec,
) -> list[dict[str, Any]]:
    if not expected_intents:
        return samples
    filtered = [sample for sample in samples if sample_matches_expected_intents(sample, expected_intents, domain)]
    return filtered


def choose_tool_sampling_mode(sample: dict[str, Any], config: DistillConfig) -> str:
    assigned_mode = sample.get(TOOL_SAMPLING_MODE_KEY)
    if assigned_mode in {"no_tools", "irrelevant_tools", "normal"}:
        return str(assigned_mode)
    return scenario_tool_sampling_mode(sample) or "normal"


def get_required_tool_names(
    sample: dict[str, Any],
    catalog: dict[str, dict[str, Any]],
    domain: DomainSpec | None = None,
) -> list[str]:
    if domain is not None:
        return domain.required_tool_names(sample, catalog)

    required_names: list[str] = []
    for field in ("required_tools", "reference_tool_names", "expected_tool_names"):
        value = sample.get(field)
        if isinstance(value, str):
            value = [value]
        if isinstance(value, list):
            for item in value:
                name = str(item or "").strip()
                if name in catalog and name not in required_names:
                    required_names.append(name)
    return required_names


def is_plan_only_tool_allowed(name: str, required_names: set[str], domain: DomainSpec | None = None) -> bool:
    if domain is not None:
        return domain.is_plan_only_tool_allowed(name, required_names)
    return True


def sample_irrelevant_tools(
    sample: dict[str, Any],
    catalog: dict[str, dict[str, Any]],
    max_tool_count: int | None = None,
    rng: random.Random | None = None,
    domain: DomainSpec | None = None,
) -> list[dict[str, Any]]:
    rng = rng or random
    required_names = set(get_required_tool_names(sample, catalog, domain=domain))
    expected_intent = normalize_intent_for_domain(sample.get("expected_intent"), domain)
    related_names = domain.related_tool_names(sample, catalog) if domain is not None else set(required_names)
    generic_search_tools = set((domain.tool_selection.get("generic_search_tools") if domain is not None else None) or [])

    irrelevant_names = [
        name
        for name in catalog
        if name not in related_names
        and name not in generic_search_tools
        and is_plan_only_tool_allowed(name, required_names, domain=domain)
    ]
    if not irrelevant_names:
        irrelevant_names = [
            name
            for name in catalog
            if name not in required_names and is_plan_only_tool_allowed(name, required_names, domain=domain)
        ]
    if not irrelevant_names:
        return []

    max_count = len(irrelevant_names)
    if max_tool_count is not None and max_tool_count > 0:
        max_count = min(max_count, max_tool_count)
    tool_count = rng.randint(1, max(1, max_count))
    rng.shuffle(irrelevant_names)
    return [catalog[name] for name in irrelevant_names[:tool_count]]


def get_fixed_tool_names(sample: dict[str, Any]) -> list[str]:
    for field in ("fixed_tool_names", "selected_tool_names", "available_tool_names", "tool_names"):
        value = sample.get(field)
        if isinstance(value, str):
            value = [value]
        if isinstance(value, list):
            names = [str(name).strip() for name in value if str(name).strip()]
            if names:
                return names
    return []


def sample_tools_for_sample(
    sample: dict[str, Any],
    max_tool_count: int | None = None,
    mode: str = "normal",
    rng: random.Random | None = None,
    tools: list[dict[str, Any]] | None = None,
    domain: DomainSpec | None = None,
) -> list[dict[str, Any]]:
    """Randomly expose a subset of tools for one trajectory.

    This improves tool-use generalization while keeping required tools available
    for the current intent/scenario. At least one search tool is kept so the
    planner can still retrieve external information.
    """
    rng = rng or random
    catalog = tool_catalog_by_name(tools)
    all_names = list(catalog)
    if not all_names:
        return tools or []

    fixed_tool_names = [name for name in get_fixed_tool_names(sample) if name in catalog]
    if fixed_tool_names:
        selected_names = []
        for name in fixed_tool_names:
            if name in catalog and name not in selected_names:
                selected_names.append(name)
        return [catalog[name] for name in selected_names]

    if mode == "no_tools":
        return []
    if mode == "irrelevant_tools":
        return sample_irrelevant_tools(sample, catalog, max_tool_count=max_tool_count, rng=rng, domain=domain)

    required_names = get_required_tool_names(sample, catalog, domain=domain)

    min_count = max(1, len(required_names))
    max_count = len(all_names)
    if max_tool_count is not None and max_tool_count > 0:
        max_count = min(max_count, max_tool_count)
    max_count = max(min_count, max_count)
    requested_count = sample.get("tool_count")
    if requested_count is not None:
        try:
            tool_count = int(requested_count)
        except (TypeError, ValueError):
            tool_count = rng.randint(min_count, max_count)
        tool_count = max(min_count, min(max_count, tool_count))
    else:
        tool_count = rng.randint(min_count, max_count)

    required_name_set = set(required_names)
    optional_names = [
        name
        for name in all_names
        if name not in required_name_set and is_plan_only_tool_allowed(name, required_name_set, domain=domain)
    ]
    rng.shuffle(optional_names)
    selected_names = required_names + optional_names[: max(0, tool_count - len(required_names))]
    rng.shuffle(selected_names)
    return [catalog[name] for name in selected_names]


def filter_tool_calls_to_available(action: dict[str, Any], available_tools: list[dict[str, Any]]) -> dict[str, Any]:
    if action.get("action") != "tool_call":
        return action

    available_names = {get_tool_name(tool) for tool in available_tools}
    tool_calls = [tool_call for tool_call in get_planner_tool_calls(action) if tool_call.get("name") in available_names]
    if not tool_calls:
        return fallback_finish_action(action.get("intent"), "当前可用工具不足以继续调用，结束规划。")
    action["tool_calls"] = tool_calls
    action["tool_call"] = tool_calls[0]
    return action


def tool_call_to_internal(tool_call: Any) -> dict[str, Any] | None:
    if tool_call is None:
        return None

    function = getattr(tool_call, "function", None)
    if function is not None:
        return {
            "id": getattr(tool_call, "id", None),
            "name": getattr(function, "name", None),
            "arguments": parse_tool_call_arguments(getattr(function, "arguments", {})),
        }

    if isinstance(tool_call, dict):
        function = tool_call.get("function") or {}
        return {
            "id": tool_call.get("id"),
            "name": function.get("name"),
            "arguments": parse_tool_call_arguments(function.get("arguments", {})),
        }

    return None


def tool_calls_to_internal(tool_calls: Any) -> list[dict[str, Any]]:
    internal_tool_calls = []
    for tool_call in tool_calls or []:
        internal_tool_call = tool_call_to_internal(tool_call)
        if internal_tool_call and internal_tool_call.get("name"):
            internal_tool_calls.append(internal_tool_call)
    return internal_tool_calls


def normalize_tool_call_action(action: dict[str, Any], tool_calls: Any, domain: DomainSpec | None = None) -> dict[str, Any]:
    if not tool_calls:
        return action

    internal_tool_calls = tool_calls_to_internal(tool_calls)
    if not internal_tool_calls:
        return action

    action["action"] = "tool_call"
    action["tool_calls"] = internal_tool_calls
    action["tool_call"] = internal_tool_calls[0]
    action["ask"] = None
    action["finish_reason"] = None
    return normalize_action(action, domain=domain)


def should_skip_intent_match(text: str, start: int) -> bool:
    prefix = text[max(0, start - 8) : start]
    return any(marker in prefix for marker in ("不", "非", "不是", "并非", "不属于", "不归为", "不能归为"))


def infer_intent_from_text(text: str | None, domain: DomainSpec | None = None) -> str | None:
    if not text:
        return None
    intent_names = domain.intent_names() if domain is not None and domain.has_intent else []
    if not intent_names:
        return None
    intent_pattern = "|".join(re.escape(intent) for intent in intent_names)
    decision_patterns = [
        rf"(?:整体意图|用户意图|意图|intent|类别|分类)\s*(?:是|为|应为|应该是|判定为|判断为|归为|归类为|属于|:|：)\s*[“\"'「『]?({intent_pattern})",
        rf"(?:归为|归类为|判定为|判断为|属于|应该是|应为|算是|视为)\s*[“\"'「『]?({intent_pattern})[”\"'」』]?\s*(?:类别|类|意图)?",
        rf"[“\"'「『]?({intent_pattern})[”\"'」』]?\s*(?:类别|类|意图)",
        rf"({intent_pattern})\s*(?:更合适|更准确|更贴切)",
    ]
    matches: list[tuple[int, str]] = []
    for pattern in decision_patterns:
        for match in re.finditer(pattern, text, flags=re.I):
            if should_skip_intent_match(text, match.start()):
                continue
            matches.append((match.start(), normalize_intent_for_domain(match.group(1), domain) or (domain.default_intent() if domain else "")))
    if matches:
        return sorted(matches, key=lambda item: item[0])[-1][1]

    if domain is not None:
        for alias, canonical in domain.intent_aliases.items():
            if alias in text:
                return domain.resolve_intent(canonical, fallback_to_default=False)
    return None


def infer_intent_from_tool_calls(tool_calls: Any, domain: DomainSpec | None = None) -> str | None:
    inferred_intents = []
    for tool_call in tool_calls_to_internal(tool_calls):
        intent = domain.tool_intent_hint(str(tool_call.get("name", ""))) if domain is not None else None
        if intent and intent not in inferred_intents:
            inferred_intents.append(intent)
    if len(inferred_intents) == 1:
        return inferred_intents[0]
    return None


def infer_intent(
    action: dict[str, Any],
    content: str,
    reasoning_content: str | None,
    tool_calls: Any,
    sample: dict[str, Any] | None = None,
    domain: DomainSpec | None = None,
) -> str:
    if domain is not None and not domain.has_intent:
        return ""
    explicit_intent = normalize_intent_for_domain(action.get("intent"), domain)
    reasoning_intent = infer_intent_from_text(reasoning_content, domain=domain)
    content_intent = infer_intent_from_text(content, domain=domain)

    for candidate in (
        explicit_intent,
        reasoning_intent,
        content_intent,
        normalize_intent_for_domain(infer_intent_from_tool_calls(tool_calls, domain=domain), domain),
        normalize_intent_for_domain((sample or {}).get("expected_intent"), domain),
    ):
        if candidate:
            return candidate
    return (domain.default_intent() if domain is not None else None) or ""


def get_intent_diagnostics(
    action: dict[str, Any],
    content: str,
    reasoning_content: str | None,
    sample: dict[str, Any] | None = None,
    domain: DomainSpec | None = None,
) -> dict[str, Any]:
    content_intent = normalize_intent_for_domain(action.get("intent"), domain)
    reasoning_intent = infer_intent_from_text(reasoning_content, domain=domain)
    expected_intent = normalize_intent_for_domain((sample or {}).get("expected_intent"), domain)
    return {
        "content_intent": content_intent,
        "reasoning_intent": reasoning_intent,
        "expected_intent": expected_intent,
        "reasoning_content_conflict": bool(content_intent and reasoning_intent and content_intent != reasoning_intent),
        "expected_intent_conflict": bool(content_intent and expected_intent and content_intent != expected_intent),
    }


def validate_reference_intent(action: dict[str, Any], diagnostics: dict[str, Any]) -> None:
    if diagnostics.get("reasoning_content_conflict"):
        raise ValueError(
            f"intent 与 reasoning 不一致：content={diagnostics.get('content_intent')}，reasoning={diagnostics.get('reasoning_intent')}"
        )


def parse_planner_action(
    content: str,
    tool_calls: Any,
    reasoning_content: str | None = None,
    sample: dict[str, Any] | None = None,
    domain: DomainSpec | None = None,
) -> dict[str, Any]:
    if tool_calls:
        try:
            action = normalize_action(extract_json_object(content), domain=domain) if content.strip() else {}
        except Exception:
            action = {}
        if domain is None or domain.has_intent:
            action["intent"] = infer_intent(action, content, reasoning_content, tool_calls, sample, domain=domain)
        action.setdefault("action", "tool_call")
        action.setdefault("tool_call", None)
        action.setdefault("ask", None)
        action.setdefault("finish_reason", None)
        action = normalize_tool_call_action(action, tool_calls, domain=domain)
        action["intent_diagnostics"] = get_intent_diagnostics(action, content, reasoning_content, sample, domain=domain)
        if domain is None or domain.has_intent:
            validate_reference_intent(action, action["intent_diagnostics"])
        return action

    if content.strip():
        action = normalize_action(extract_json_object(content), domain=domain)
        if domain is None or domain.has_intent:
            action["intent"] = infer_intent(action, content, reasoning_content, tool_calls, sample, domain=domain)
        action = normalize_action(action, domain=domain)
        action["intent_diagnostics"] = get_intent_diagnostics(action, content, reasoning_content, sample, domain=domain)
        if domain is None or domain.has_intent:
            validate_reference_intent(action, action["intent_diagnostics"])
        return action

    raise ValueError("assistant 输出既没有 tool_calls，也没有可解析的 JSON")


def compose_raw_text(reasoning_content: str | None, content: str | None) -> str:
    reasoning = (reasoning_content or "").strip()
    final_content = (content or "").strip()
    if reasoning and final_content:
        return f"<think>\n{reasoning}\n</think>\n{final_content}"
    if reasoning:
        return f"<think>\n{reasoning}\n</think>"
    return final_content


def make_trajectory_snapshot(trajectory: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return a compact JSON-serializable trajectory snapshot.

    Do not store the live ``trajectory`` list inside a step. Otherwise the step
    becomes part of the same list it references, causing ``json.dumps`` to raise
    ``ValueError: Circular reference detected``.
    """
    return [
        {
            "step": item.get("step"),
            "planner_output": item.get("planner_output"),
            "observations": item.get("observations") or ([item.get("observation")] if item.get("observation") else []),
        }
        for item in trajectory
    ]


def get_planner_tool_calls(planner_output: dict[str, Any]) -> list[dict[str, Any]]:
    tool_calls = planner_output.get("tool_calls") or []
    if isinstance(tool_calls, dict):
        tool_calls = [tool_calls]
    if not tool_calls and planner_output.get("tool_call"):
        tool_calls = [planner_output["tool_call"]]
    return [tool_call for tool_call in tool_calls if isinstance(tool_call, dict)]


def make_tool_call_message(planner_output: dict[str, Any], content: str | None = None) -> dict[str, Any]:
    tool_calls = get_planner_tool_calls(planner_output)
    return {
        "role": "assistant",
        "content": content or json.dumps(
            {"intent": planner_output.get("intent"), "action": "tool_call"},
            ensure_ascii=False,
        ),
        "tool_calls": [
            {
                "id": tool_call.get("id") or f"call_{planner_output.get('intent', 'tool')}_{index}",
                "type": "function",
                "function": {
                    "name": tool_call.get("name"),
                    "arguments": serialize_tool_arguments(tool_call.get("arguments") or {}),
                },
            }
            for index, tool_call in enumerate(tool_calls, start=1)
        ],
    }


def make_tool_message(observation: dict[str, Any] | None) -> dict[str, Any] | None:
    if not observation:
        return None
    return {
        "role": "tool",
        "tool_call_id": observation.get("tool_call_id"),
        "content": observation.get("content", ""),
    }


def make_tool_messages(observations: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    messages = []
    for observation in observations or []:
        tool_message = make_tool_message(observation)
        if tool_message:
            messages.append(tool_message)
    return messages


def planner_content_payload(planner_output: dict[str, Any]) -> dict[str, Any]:
    payload = {
        "intent": planner_output.get("intent"),
        "action": planner_output.get("action"),
    }
    if planner_output.get("action") == "ask_clarification":
        payload["ask"] = planner_output.get("ask")
    elif planner_output.get("action") == "planning_finish":
        payload["finish_reason"] = planner_output.get("finish_reason")
    return payload


def _is_empty_raw_content_tool_call_step(step: dict[str, Any], domain: DomainSpec | None = None) -> bool:
    planner_output = step.get("planner_output") or {}
    raw_output = step.get("planner_raw_output") or {}
    current_intent = normalize_intent_for_domain(planner_output.get("intent"), domain)
    raw_content = str(raw_output.get("raw_content") or "").strip()
    return planner_output.get("action") == "tool_call" and not raw_content and current_intent in {None, "其他"}


def _update_intent_diagnostics(diagnostics: dict[str, Any] | None, intent: str, domain: DomainSpec | None = None) -> None:
    if not isinstance(diagnostics, dict):
        return
    diagnostics["content_intent"] = intent
    reasoning_intent = normalize_intent_for_domain(diagnostics.get("reasoning_intent"), domain)
    expected_intent = normalize_intent_for_domain(diagnostics.get("expected_intent"), domain)
    diagnostics["reasoning_content_conflict"] = bool(reasoning_intent and reasoning_intent != intent)
    diagnostics["expected_intent_conflict"] = bool(expected_intent and expected_intent != intent)


def patch_empty_tool_call_intents_from_final(trajectory: list[dict[str, Any]], domain: DomainSpec | None = None) -> int:
    """Backfill empty-content tool-call step intents from the final step intent.

    Some OpenAI-compatible responses put the real action in ``tool_calls`` and
    leave assistant ``content`` empty. Those steps should be treated as missing
    intent, not as the teacher explicitly choosing ``其他``. After the final
    planning step has a stable intent, use it to repair only those empty-content
    tool-call steps.
    """
    if not trajectory:
        return 0
    final_output = trajectory[-1].get("planner_output") or {}
    final_intent = normalize_intent_for_domain(final_output.get("intent"), domain)
    if not final_intent or final_intent == "其他":
        return 0

    changed = 0
    for step in trajectory[:-1]:
        if not _is_empty_raw_content_tool_call_step(step, domain=domain):
            continue
        planner_output = step.get("planner_output") or {}
        raw_output = step.get("planner_raw_output") or {}
        planner_output["intent"] = final_intent
        normalized_content = json.dumps(planner_content_payload(planner_output), ensure_ascii=False)
        raw_output["content"] = normalized_content
        raw_output["normalized_content"] = normalized_content
        raw_output["text"] = compose_raw_text(raw_output.get("reasoning_content"), normalized_content)
        _update_intent_diagnostics(step.get("intent_diagnostics"), final_intent, domain=domain)
        _update_intent_diagnostics(raw_output.get("intent_diagnostics"), final_intent, domain=domain)
        changed += 1
    return changed


def make_assistant_message_from_step(step: dict[str, Any]) -> dict[str, Any]:
    planner_output = step.get("planner_output", {})
    content = json.dumps(planner_content_payload(planner_output), ensure_ascii=False)
    if planner_output.get("action") == "tool_call":
        message = make_tool_call_message(planner_output, content=content)
    else:
        message = {"role": "assistant", "content": content}

    reasoning_content = (step.get("planner_raw_output") or {}).get("reasoning_content")
    if reasoning_content:
        message["reasoning_content"] = reasoning_content
    return message


def build_history_messages(trajectory: list[dict[str, Any]]) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = []
    for step in trajectory:
        messages.append(make_assistant_message_from_step(step))
        observations = step.get("observations") or ([step.get("observation")] if step.get("observation") else [])
        messages.extend(make_tool_messages(observations))
    return messages


def extract_history_turns(sample: dict[str, Any]) -> list[dict[str, Any]]:
    history = sample.get("history") or sample.get("dialogue_history") or sample.get("conversation_history") or []
    if isinstance(history, dict):
        history = [history]
    if not isinstance(history, list):
        return []
    return [item for item in history if isinstance(item, dict)]


def extract_compressed_history_text(sample: dict[str, Any]) -> str | None:
    for key in ("compressed_history", "compressed_history_text", "history_summary", "summary_history"):
        value = str(sample.get(key) or "").strip()
        if value:
            return value
    return None


def build_history_context_from_sample(sample: dict[str, Any], recent_turn_limit: int = 3) -> str | None:
    history_turns = extract_history_turns(sample)
    compressed_history_text = extract_compressed_history_text(sample)
    if not history_turns and not compressed_history_text:
        return None
    compressed_turns, recent_turns = split_history_turns(history_turns, recent_turn_limit=recent_turn_limit)
    if not compressed_history_text and compressed_turns:
        compressed_history_text = render_dialogue_turns(compressed_turns)
    recent_history_text = render_dialogue_turns(recent_turns)
    return build_history_context_text(compressed_history_text, recent_history_text)


def build_planner_messages(
    sample: dict[str, Any],
    trajectory: list[dict[str, Any]],
    max_tool_rounds: int,
    available_tools: list[dict[str, Any]],
    domain: DomainSpec | None = None,
) -> list[dict[str, Any]]:
    user_content = str(sample.get("user_query", ""))
    intent_reference = sample.get(INTENT_REFERENCE_KEY)
    if intent_reference:
        user_content = f"{user_content}\n\n【参考意图】\n{intent_reference}\n请优先按该参考意图保持整条规划轨迹的 intent 一致；该字段只用于当前数据校正，真实场景不会提供。"
    history_context = build_history_context_from_sample(sample)
    if history_context:
        user_content = f"{user_content}\n\n{history_context}"
    if domain is None:
        raise ValueError("build_planner_messages requires a domain")
    return [
        {"role": "system", "content": domain.planner_system_prompt},
        {"role": "user", "content": user_content},
        *build_history_messages(trajectory),
    ]


def build_multiturn_session_history(
    dialogue_turns: list[dict[str, Any]],
    recent_turn_limit: int,
    compressed_history_text: str | None = None,
) -> str | None:
    compressed_turns, recent_turns = split_history_turns(dialogue_turns, recent_turn_limit=recent_turn_limit)
    if not compressed_history_text and compressed_turns:
        compressed_history_text = render_dialogue_turns(compressed_turns)
    recent_history_text = render_dialogue_turns(recent_turns)
    return build_history_context_text(compressed_history_text, recent_history_text)


def freeze_session_tool_state(sample: dict[str, Any], available_tools: list[dict[str, Any]]) -> dict[str, Any]:
    """Freeze one sampled tool set for the whole multi-turn session."""

    tool_names = [get_tool_name(tool) for tool in available_tools if get_tool_name(tool)]
    frozen_sample = dict(sample)
    frozen_sample.setdefault("fixed_tool_names", tool_names)
    frozen_sample.setdefault("session_tool_names", tool_names)
    return frozen_sample


async def call_planner_with_retry(
    client: AsyncOpenAI,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]],
    model_name: str,
    max_retries: int,
    sample: dict[str, Any] | None = None,
    domain: DomainSpec | None = None,
) -> dict[str, Any] | None:
    for attempt in range(max_retries):
        try:
            completion = await client.chat.completions.create(
                model=model_name,
                messages=messages,
                tools=tools,
                tool_choice="auto",
                reasoning_effort="high",
                extra_body={"thinking": {"type": "enabled"}},
                temperature=0.5,
                top_p=0.8,
            )
            message = completion.choices[0].message
            reasoning_content = getattr(message, "reasoning_content", None)
            content = message.content or ""
            parsed_action = parse_planner_action(content, getattr(message, "tool_calls", None), reasoning_content, sample, domain=domain)
            normalized_content = json.dumps(planner_content_payload(parsed_action), ensure_ascii=False)
            return {
                "parsed_action": parsed_action,
                "raw_output": {
                    "reasoning_content": reasoning_content,
                    "raw_content": content,
                    "content": normalized_content,
                    "normalized_content": normalized_content,
                    "tool_calls": tool_calls_to_internal(getattr(message, "tool_calls", None)),
                    "intent_diagnostics": parsed_action.get("intent_diagnostics"),
                    "text": compose_raw_text(reasoning_content, normalized_content),
                },
            }
        except Exception as e:
            if attempt < max_retries - 1:
                await asyncio.sleep(min(2**attempt, RETRY_BACKOFF_CAP_SECONDS) + 1)
            else:
                print(f"\n[失败] planner 达到最大重试次数：{e}")
                return None
    return None


def fallback_finish_action(intent: str | None, reason: str) -> dict[str, Any]:
    return {
        "intent": intent or "其他",
        "action": "planning_finish",
        "tool_call": None,
        "ask": None,
        "finish_reason": reason,
    }


def should_rebuild_with_intent_reference(result: dict[str, Any] | None) -> bool:
    if result is None:
        return False
    trajectory = result.get("trajectory") or []
    if not trajectory:
        return False
    diagnostics = trajectory[0].get("intent_diagnostics") or {}
    return bool(diagnostics.get("reasoning_content_conflict"))


async def build_trajectory_for_sample(
    sample: dict[str, Any],
    client: AsyncOpenAI,
    config: DistillConfig,
    domain: DomainSpec,
) -> dict[str, Any] | None:
    trajectory: list[dict[str, Any]] = []
    used_tool_rounds = 0
    model_name = config.model_name or resolve_distill_model()
    domain.prepare_sample_context(sample)
    history_turns = extract_history_turns(sample)
    recent_history_text = render_dialogue_turns(history_turns[-3:]) if history_turns else None
    compressed_history_text = extract_compressed_history_text(sample)
    if history_turns and not compressed_history_text and len(history_turns) > 3:
        compressed_history_text = render_dialogue_turns(history_turns[:-3])
    tool_sampling_mode = choose_tool_sampling_mode(sample, config)
    tool_failure_enabled = bool(sample.get(TOOL_FAILURE_ENABLED_KEY, False))
    remaining_failure_budget = get_tool_failure_budget(config, failure_enabled=tool_failure_enabled)
    injected_failure_count = 0
    available_tools = sample_tools_for_sample(
        sample,
        max_tool_count=config.max_tool_count,
        mode=tool_sampling_mode,
        tools=domain.tools,
        domain=domain,
    )

    # +1 lets the model emit planning_finish after the last observation.
    for step in range(1, config.max_tool_rounds + 2):
        planner_input = {
            "user_query": sample.get("user_query", ""),
            "history": None,
            "previous_steps": make_trajectory_snapshot(trajectory),
            "compressed_history_text": compressed_history_text,
            "recent_history_text": recent_history_text,
        }
        messages = build_planner_messages(sample, trajectory, config.max_tool_rounds, available_tools, domain=domain)
        planner_result = await call_planner_with_retry(
            client=client,
            messages=messages,
            tools=available_tools,
            model_name=model_name,
            max_retries=config.max_retries,
            sample=sample,
            domain=domain,
        )
        if planner_result is None:
            return None

        planner_output = planner_result["parsed_action"]
        planner_output = filter_tool_calls_to_available(planner_output, available_tools)
        intent_diagnostics = planner_output.pop("intent_diagnostics", None)
        planner_raw_output = planner_result["raw_output"]
        planner_overridden = False
        planner_override_reason = None

        observations = []
        if planner_output.get("action") == "tool_call":
            if used_tool_rounds >= config.max_tool_rounds:
                planner_overridden = True
                planner_override_reason = "已达到最大工具调用轮数，结束规划。"
                planner_output = fallback_finish_action(planner_output.get("intent"), planner_override_reason)
            else:
                tool_calls = get_planner_tool_calls(planner_output)
                for index, tool_call in enumerate(tool_calls, start=1):
                    tool_call_id = tool_call.get("id") or f"call_{step}_{index}"
                    tool_call["id"] = tool_call_id
                failed_tool_call_ids = choose_failed_tool_call_ids(tool_calls, config, remaining_failure_budget)
                for tool_call in tool_calls:
                    tool_call_id = tool_call["id"]
                    if str(tool_call_id) in failed_tool_call_ids:
                        failure_result = make_tool_failure_result(tool_call["name"])
                        observations.append({"tool_call_id": tool_call_id, **failure_result})
                        remaining_failure_budget -= 1
                        injected_failure_count += 1
                    else:
                        try:
                            tool_result = await run_domain_tool_async(
                                domain,
                                sample,
                                tool_call["name"],
                                tool_call.get("arguments", {}),
                            )
                        except asyncio.TimeoutError:
                            failure_result = make_tool_failure_result(tool_call["name"])
                            failure_result["content"] = (
                                f"error：工具调用超时（>{TOOL_CALL_TIMEOUT_SECONDS:.0f}s）。"
                                f"失败工具：{tool_call['name']}。"
                            )
                            observations.append({"tool_call_id": tool_call_id, **failure_result})
                            continue
                        except Exception as exc:
                            if domain.is_fatal_tool_error(exc):
                                raise
                            failure_result = make_tool_failure_result(tool_call["name"])
                            failure_result["content"] = (
                                f"error：工具调用异常。失败工具：{tool_call['name']}。原因：{exc}"
                            )
                            observations.append({"tool_call_id": tool_call_id, **failure_result})
                            continue
                        observations.append({"tool_call_id": tool_call_id, "name": tool_result.name, "content": tool_result.content})
                used_tool_rounds += 1

        trajectory.append(
            {
                "step": step,
                "planner_input": planner_input,
                "planner_raw_output": planner_raw_output,
                "planner_output": planner_output,
                "intent_diagnostics": intent_diagnostics,
                "planner_overridden": planner_overridden,
                "planner_override_reason": planner_override_reason,
                "observation": observations[0] if observations else None,
                "observations": observations,
            }
        )

        if planner_output.get("action") in {"planning_finish", "ask_clarification"}:
            break

    if not trajectory or trajectory[-1]["planner_output"].get("action") == "tool_call":
        last_intent = None
        if trajectory:
            last_intent = trajectory[-1]["planner_output"].get("intent")
        trajectory.append(
            {
                "step": len(trajectory) + 1,
                "planner_input": {
                    "user_query": sample.get("user_query", ""),
                    "history": None,
                    "previous_steps": make_trajectory_snapshot(trajectory),
                },
                "planner_raw_output": {"reasoning_content": None, "content": None, "text": ""},
                "planner_output": fallback_finish_action(last_intent, "工具信息已收集，输出 planning_finish。"),
                "planner_overridden": True,
                "planner_override_reason": "工具信息已收集，输出 planning_finish。",
                "observation": None,
            }
        )

    empty_tool_call_intent_backfill_count = patch_empty_tool_call_intents_from_final(trajectory, domain=domain)

    return {
        "id": sample.get("id"),
        "domain": domain.name,
        "user_query": sample.get("user_query"),
        "expected_intent": sample.get("expected_intent"),
        "scenario": sample.get("scenario", "intent_to_tool_call"),
        "student_profile": sample.get("student_profile"),
        "target_competency": sample.get("target_competency"),
        "metadata": sample.get("metadata"),
        "compressed_history_text": compressed_history_text,
        "recent_history_text": recent_history_text,
        "max_tool_rounds": config.max_tool_rounds,
        "max_tool_count": config.max_tool_count,
        "tool_sampling_mode": tool_sampling_mode,
        "tool_failure_ratio": config.tool_failure_ratio,
        "tool_failure_enabled": tool_failure_enabled,
        "tool_failure_max_calls": config.tool_failure_max_calls,
        "tool_failure_count": injected_failure_count,
        "tool_failure_targets": config.tool_failure_targets,
        "fixed_tool_names": get_fixed_tool_names(sample),
        "session_tool_names": sample.get("session_tool_names"),
        "empty_tool_call_intent_backfill_count": empty_tool_call_intent_backfill_count,
        "tools": available_tools,
        "tool_count": len(available_tools),
        "trajectory": trajectory,
    }


async def build_trajectory_with_optional_reference(
    sample: dict[str, Any],
    client: AsyncOpenAI,
    config: DistillConfig,
    domain: DomainSpec,
) -> dict[str, Any] | None:
    base_sample = dict(sample)
    base_sample.pop(INTENT_REFERENCE_KEY, None)
    result = await build_trajectory_for_sample(base_sample, client, config, domain)
    expected_intent = normalize_intent_for_domain(sample.get("expected_intent"), domain)
    if result is None or not domain.has_intent or not expected_intent or not should_rebuild_with_intent_reference(result):
        if result is not None:
            result["intent_reference_used"] = False
            result["intent_reference"] = None
            result["intent_reference_reason"] = None
        return result

    reference_sample = dict(sample)
    reference_sample[INTENT_REFERENCE_KEY] = expected_intent
    reference_result = await build_trajectory_for_sample(reference_sample, client, config, domain)
    if reference_result is None:
        return None
    reference_result["intent_reference_used"] = True
    reference_result["intent_reference"] = expected_intent
    reference_result["intent_reference_reason"] = "首步 content_intent 与 reasoning_intent 不一致，使用 expected_intent 作为离线参考意图重建轨迹。"
    reference_result["original_intent_diagnostics"] = (result.get("trajectory") or [{}])[0].get("intent_diagnostics")
    return reference_result


async def process_one(
    sample: dict[str, Any],
    client: AsyncOpenAI,
    config: DistillConfig,
    domain: DomainSpec,
    sem: asyncio.Semaphore,
    f_out,
) -> bool:
    async with sem:
        try:
            result = await build_trajectory_with_optional_reference(sample, client, config, domain)
        except Exception as exc:
            if domain.is_fatal_tool_error(exc):
                # Real domain tool failure – propagate to halt the entire dataset run.
                raise
            raise
        if result is None:
            return False
        async with write_lock:
            f_out.write(json.dumps(result, ensure_ascii=False) + "\n")
            f_out.flush()
        return True


async def process_dataset(config: DistillConfig) -> None:
    domain = load_domain(config.domain_name)
    config.output_file.parent.mkdir(parents=True, exist_ok=True)
    if config.force_rebuild and config.output_file.exists():
        config.output_file.write_text("", encoding="utf-8")

    samples = load_jsonl(config.input_file)
    processed_ids = get_processed_ids(config.output_file)
    selected_samples = filter_samples_by_expected_intents(samples, config.expected_intents, domain) if domain.has_intent else samples
    pending = [s for s in selected_samples if str(s.get("id")) not in processed_ids]
    pending = pending[: config.max_samples]
    assign_sample_controls(pending, config)

    if config.expected_intents and domain.has_intent:
        print(f"🎯 仅合成指定意图：{', '.join(config.expected_intents)}")
    print(
        f"domain={domain.name}；读取 {len(samples)} 条 seed，命中意图过滤 {len(selected_samples)} 条，已处理 {len(processed_ids)} 条，本次处理 {len(pending)} 条。"
    )
    if not pending:
        return

    client = get_client(config)
    sem = asyncio.Semaphore(config.max_workers)

    with config.output_file.open("a", encoding="utf-8") as f_out:
        tasks = [
            asyncio.create_task(process_one(sample, client, config, domain, sem, f_out))
            for sample in pending
        ]
        try:
            for task in tqdm(asyncio.as_completed(tasks), total=len(tasks), desc="构建规划轨迹"):
                try:
                    await task
                except Exception as exc:
                    if not domain.is_fatal_tool_error(exc):
                        raise
                    # Cancel everything else and abort the pipeline cleanly.
                    print(
                        "\n[致命] 领域工具调用失败，已停止规划轨迹生成："
                        f"{exc}\n"
                        f"  query: {getattr(exc, 'query', None)!r}\n"
                        "  请检查对应工具配置、配额或网络后重新启动；已写入的样本可断点续跑。"
                    )
                    for pending_task in tasks:
                        if not pending_task.done():
                            pending_task.cancel()
                    # Drain cancellations so we don't leak warnings.
                    await asyncio.gather(*tasks, return_exceptions=True)
                    raise SystemExit(2) from exc
        finally:
            f_out.flush()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build intent-to-tool-call planning trajectories.")
    parser.add_argument("--input", type=Path, default=None, help="Seed JSONL path.")
    parser.add_argument("--output", type=Path, default=None, help="Output JSONL path.")
    parser.add_argument("--domain", default=None, help="领域包名称，默认读取 AGENT_DOMAIN 或 health。")
    parser.add_argument("--max-samples", type=int, default=None)
    parser.add_argument("--max-workers", type=int, default=None)
    parser.add_argument("--max-tool-rounds", type=int, default=None)
    parser.add_argument("--max-tool-count", type=int, default=None, help="每条样本随机暴露的最大工具数量；<=0 表示不限制。")
    parser.add_argument("--no-tool-ratio", "--no-tool-probability", dest="no_tool_ratio", type=float, default=None, help="无工具场景在本次待处理数据中的比例。")
    parser.add_argument("--irrelevant-tool-ratio", "--irrelevant-tool-probability", dest="irrelevant_tool_ratio", type=float, default=None, help="只暴露无关工具场景在本次待处理数据中的比例。")
    parser.add_argument("--tool-failure-ratio", "--tool-failure-probability", dest="tool_failure_ratio", type=float, default=None, help="工具失败样本在本次待处理数据中的比例。")
    parser.add_argument("--tool-failure-max-calls", type=int, default=None, help="每条失败样本最多注入失败的工具调用次数，默认 1。")
    parser.add_argument("--tool-failure-targets", type=str, default=None, help="只对指定工具注入失败，逗号分隔；为空表示所有工具可失败。")
    parser.add_argument(
        "--expected-intent",
        action="append",
        dest="expected_intents",
        help="只合成指定领域意图，可重复传入；也可用环境变量 AGENT_DISTILL_EXPECTED_INTENTS=意图A,意图B。",
    )
    parser.add_argument("--force-rebuild", action="store_true")
    return parser.parse_args()


def build_config_from_args(args: argparse.Namespace) -> DistillConfig:
    base = DistillConfig()
    domain_name = args.domain or base.domain_name
    domain = load_domain(domain_name)
    output_file = args.output or base.output_file
    if args.output is None and args.domain and not os.getenv("AGENT_PLANNER_TRAJECTORY_FILE"):
        output_file = output_file_for_domain("planner_trajectories", domain_name)
    expected_intents = normalize_expected_intents(
        args.expected_intents if args.expected_intents is not None else (base.expected_intents if domain.has_intent else ()),
        domain,
    )
    return DistillConfig(
        input_file=args.input or base.input_file,
        output_file=output_file,
        domain_name=domain_name,
        model_name=base.model_name or resolve_distill_model(),
        api_key=base.api_key,
        base_url=base.base_url or resolve_base_url(),
        max_samples=args.max_samples if args.max_samples is not None else base.max_samples,
        max_workers=args.max_workers if args.max_workers is not None else base.max_workers,
        max_tool_rounds=args.max_tool_rounds if args.max_tool_rounds is not None else base.max_tool_rounds,
        max_tool_count=args.max_tool_count if args.max_tool_count is not None else base.max_tool_count,
        no_tool_ratio=args.no_tool_ratio if args.no_tool_ratio is not None else base.no_tool_ratio,
        irrelevant_tool_ratio=args.irrelevant_tool_ratio if args.irrelevant_tool_ratio is not None else base.irrelevant_tool_ratio,
        tool_failure_ratio=args.tool_failure_ratio if args.tool_failure_ratio is not None else base.tool_failure_ratio,
        tool_failure_max_calls=args.tool_failure_max_calls if args.tool_failure_max_calls is not None else base.tool_failure_max_calls,
        tool_failure_targets=args.tool_failure_targets if args.tool_failure_targets is not None else base.tool_failure_targets,
        recent_history_turn_limit=base.recent_history_turn_limit,
        multi_turn_enabled=base.multi_turn_enabled,
        expected_intents=expected_intents,
        max_retries=base.max_retries,
        force_rebuild=args.force_rebuild or base.force_rebuild,
    )


if __name__ == "__main__":
    runtime_config = build_config_from_args(parse_args())
    print(f"🚀 开始构建规划轨迹... domain={runtime_config.domain_name}")
    asyncio.run(process_dataset(runtime_config))
    print("✅ 规划轨迹构建结束！")
