"""Build execution-stage final-answer SFT data from planner trajectories.

This module is intentionally isolated from the planner generator. It only reads
planner trajectory JSONL as upstream context and synthesizes final user-facing
answers with a teacher model.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any

from openai import AsyncOpenAI
from tqdm import tqdm

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

try:
    from .config import EXECUTOR_ANSWER_FILE, EXECUTOR_EXPECTED_INTENTS, EXECUTOR_INPUT_FILE, DistillConfig, output_file_for_domain, resolve_base_url, resolve_executor_model
    from .core.domain import DomainSpec, load_domain
    from .core.renderer import build_executor_messages as build_domain_executor_messages
    from .core.conversation import build_history_context_text, render_dialogue_turns, split_history_turns
except ImportError:  # Allows: python agent/executor_generator.py
    from config import EXECUTOR_ANSWER_FILE, EXECUTOR_EXPECTED_INTENTS, EXECUTOR_INPUT_FILE, DistillConfig, output_file_for_domain, resolve_base_url, resolve_executor_model
    from agent.core.domain import DomainSpec, load_domain
    from agent.core.renderer import build_executor_messages as build_domain_executor_messages
    from agent.core.conversation import build_history_context_text, render_dialogue_turns, split_history_turns


DEFAULT_INPUT = EXECUTOR_INPUT_FILE
DEFAULT_OUTPUT = EXECUTOR_ANSWER_FILE
DEFAULT_MAX_SAMPLES = int(os.getenv("AGENT_EXECUTOR_MAX_SAMPLES", "10000"))
DEFAULT_MAX_WORKERS = int(os.getenv("AGENT_EXECUTOR_MAX_WORKERS", "50"))
DEFAULT_MAX_RETRIES = int(os.getenv("AGENT_EXECUTOR_MAX_RETRIES", "5"))
DEFAULT_MAX_EXTERNAL_CHARS = int(os.getenv("AGENT_EXECUTOR_MAX_EXTERNAL_CHARS", "6000"))
DEFAULT_INCLUDE_REASONING = os.getenv("AGENT_EXECUTOR_INCLUDE_REASONING", "1") != "0"


def normalize_intent_for_domain(intent: Any, domain: DomainSpec | None = None) -> str | None:
    if domain is None or not domain.has_intent:
        return None
    return domain.resolve_intent(intent, fallback_to_default=False)


def normalize_expected_intents(values: Any, domain: DomainSpec) -> tuple[str, ...]:
    if values is None:
        return ()
    if isinstance(values, str):
        values = [values]
    if not domain.has_intent:
        return ()
    normalized: list[str] = []
    invalid: list[str] = []
    for value in values:
        if value is None:
            continue
        raw = str(value).strip()
        if not raw:
            continue
        canonical = domain.resolve_intent(raw, fallback_to_default=False)
        if canonical and canonical not in normalized:
            normalized.append(canonical)
        elif not canonical:
            invalid.append(raw)
    if invalid:
        valid_names = ", ".join(domain.intent_names()) or "无"
        raise ValueError(
            f"无效 expected_intent：{', '.join(invalid)}。领域 {domain.name} 可选：{valid_names}，或使用该领域已定义别名。"
        )
    return tuple(normalized)


write_lock = asyncio.Lock()

# Per-call timeouts to keep the asyncio event loop responsive even when the
# underlying HTTP transport or domain file-writing tool hangs.
HTTP_TIMEOUT_SECONDS = float(os.getenv("AGENT_EXECUTOR_HTTP_TIMEOUT", "120"))
TOOL_CALL_TIMEOUT_SECONDS = float(os.getenv("AGENT_EXECUTOR_TOOL_TIMEOUT", "30"))
RETRY_BACKOFF_CAP_SECONDS = float(os.getenv("AGENT_EXECUTOR_RETRY_BACKOFF_CAP", "30"))


def get_client(api_key: str | None = None, base_url: str | None = None) -> AsyncOpenAI:
    return AsyncOpenAI(
        api_key=api_key or os.getenv("DEEPSEEK_API_KEY"),
        base_url=base_url or resolve_base_url(),
        timeout=HTTP_TIMEOUT_SECONDS,
        max_retries=0,
    )


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if not path.exists():
        return rows
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            if line.strip():
                rows.append(json.loads(line))
    return rows


def get_processed_ids(output_file: Path) -> set[str]:
    processed_ids: set[str] = set()
    if not output_file.exists():
        return processed_ids
    with output_file.open("r", encoding="utf-8") as f:
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


def compose_answer_text(reasoning_content: str | None, content: str | None, include_reasoning: bool = True) -> str:
    answer = (content or "").strip()
    reasoning = (reasoning_content or "").strip()
    if include_reasoning and reasoning and answer:
        return f"<think>\n{reasoning}\n</think>\n{answer}"
    if include_reasoning and reasoning:
        return f"<think>\n{reasoning}\n</think>"
    return answer


def extract_intent(row: dict[str, Any], domain: DomainSpec) -> str | None:
    if not domain.has_intent:
        return None
    trajectory = row.get("trajectory") or []
    for step in reversed(trajectory):
        planner_output = step.get("planner_output") or {}
        intent = normalize_intent_for_domain(planner_output.get("intent"), domain)
        if intent:
            return intent
    for step in trajectory:
        planner_output = step.get("planner_output") or {}
        intent = normalize_intent_for_domain(planner_output.get("intent"), domain)
        if intent:
            return intent
    return normalize_intent_for_domain(row.get("expected_intent"), domain) or domain.default_intent()


def row_matches_expected_intents(row: dict[str, Any], expected_intents: tuple[str, ...], domain: DomainSpec) -> bool:
    if not expected_intents:
        return True
    intent = extract_intent(row, domain) or normalize_intent_for_domain(row.get("expected_intent"), domain)
    return bool(intent and intent in expected_intents)


def filter_rows_by_expected_intents(
    rows: list[dict[str, Any]],
    expected_intents: tuple[str, ...],
    domain: DomainSpec,
) -> list[dict[str, Any]]:
    if not expected_intents:
        return rows
    return [row for row in rows if row_matches_expected_intents(row, expected_intents, domain)]


def extract_clarification_ask(row: dict[str, Any]) -> str | None:
    for step in reversed(row.get("trajectory") or []):
        planner_output = step.get("planner_output") or {}
        if planner_output.get("action") != "ask_clarification":
            continue
        ask = str(planner_output.get("ask") or "").strip()
        if ask:
            return ask
    return None


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


def extract_history_turns(row: dict[str, Any]) -> list[dict[str, Any]]:
    history = row.get("history") or row.get("dialogue_history") or row.get("conversation_history") or []
    if isinstance(history, dict):
        history = [history]
    if not isinstance(history, list):
        return []
    turns: list[dict[str, Any]] = []
    for item in history:
        if isinstance(item, dict):
            turns.append(item)
    return turns


def extract_compressed_history_text(row: dict[str, Any]) -> str | None:
    for key in ("compressed_history", "compressed_history_text", "history_summary", "summary_history"):
        value = str(row.get(key) or "").strip()
        if value:
            return value
    return None


def extract_recent_history_text(row: dict[str, Any]) -> str | None:
    turns = extract_history_turns(row)
    if not turns:
        return None
    lines: list[str] = []
    for turn in turns:
        user_text = str(turn.get("user") or turn.get("user_query") or turn.get("content") or "").strip()
        assistant_text = str(turn.get("assistant") or turn.get("assistant_answer") or turn.get("assistant_content") or turn.get("response") or turn.get("answer") or "").strip()
        if user_text:
            lines.append(f"用户：{user_text}")
        if assistant_text:
            lines.append(f"助手：{assistant_text}")
    return "\n".join(lines).strip() or None


def get_fixed_tool_names(row: dict[str, Any]) -> list[str]:
    for field in ("fixed_tool_names", "selected_tool_names", "available_tool_names", "tool_names", "session_tool_names"):
        value = row.get(field)
        if isinstance(value, str):
            value = [value]
        if isinstance(value, list):
            names = [str(name).strip() for name in value if str(name).strip()]
            if names:
                return names
    return []


def get_step_observations(step: dict[str, Any]) -> list[dict[str, Any]]:
    observations = step.get("observations") or []
    if isinstance(observations, dict):
        observations = [observations]
    if not observations and step.get("observation"):
        observations = [step["observation"]]
    return [observation for observation in observations if isinstance(observation, dict)]


def format_observation(observation: dict[str, Any], index: int, include_errors: bool = True) -> str | None:
    name = str(observation.get("name") or f"tool_{index}")
    content = str(observation.get("content") or "").strip()
    if not content:
        return None
    if observation.get("error") and not include_errors:
        return None
    error_prefix = "（工具失败）" if observation.get("error") else ""
    return f"【{index}. {name}{error_prefix}】\n{content}"


def extract_external_info(row: dict[str, Any], max_chars: int = DEFAULT_MAX_EXTERNAL_CHARS, include_errors: bool = True) -> tuple[str, list[str], bool]:
    chunks: list[str] = []
    tool_names: list[str] = []
    has_tool_error = False
    index = 1
    for step in row.get("trajectory") or []:
        for observation in get_step_observations(step):
            if observation.get("error"):
                has_tool_error = True
            name = str(observation.get("name") or "")
            if name and name not in tool_names:
                tool_names.append(name)
            chunk = format_observation(observation, index, include_errors=include_errors)
            if chunk:
                chunks.append(chunk)
                index += 1

    external_info = "\n\n".join(chunks).strip() or "无外部补充信息。"
    if max_chars > 0 and len(external_info) > max_chars:
        external_info = external_info[:max_chars].rstrip() + "……"
    return external_info, tool_names, has_tool_error


def build_sample_id(row: dict[str, Any]) -> str:
    return f"{row.get('id')}_executor"


def get_planner_tool_calls(planner_output: dict[str, Any]) -> list[dict[str, Any]]:
    tool_calls = planner_output.get("tool_calls") or []
    if isinstance(tool_calls, dict):
        tool_calls = [tool_calls]
    if not tool_calls and planner_output.get("tool_call"):
        tool_calls = [planner_output["tool_call"]]
    return [tool_call for tool_call in tool_calls if isinstance(tool_call, dict)]


def extract_file_write_plan(row: dict[str, Any], domain: DomainSpec) -> dict[str, Any] | None:
    plan_tool_names = domain.file_write_plan_tool_names()
    if not plan_tool_names:
        return None
    for step in row.get("trajectory") or []:
        planner_output = step.get("planner_output") or {}
        for tool_call in get_planner_tool_calls(planner_output):
            if tool_call.get("name") not in plan_tool_names:
                continue
            arguments = tool_call.get("arguments") or {}
            if not isinstance(arguments, dict):
                arguments = {"filename": str(arguments)}
            return {
                "tool_name": str(tool_call.get("name") or ""),
                "filename": str(arguments.get("filename") or domain.file_write_default("filename", "final_answer.md")),
                "file_type": str(arguments.get("file_type") or domain.file_write_default("file_type", "markdown")),
                "write_purpose": str(arguments.get("write_purpose") or domain.file_write_default("write_purpose", "save final answer")),
                "content_source": str(arguments.get("content_source") or domain.file_write_default("content_source", "executor_final_answer")),
            }
    return None


def execute_file_write_plan(domain: DomainSpec, file_write_plan: dict[str, Any] | None, answer_text: str) -> dict[str, Any] | None:
    if not file_write_plan:
        return None
    write_tool_name = domain.file_write_tool_name()
    if not write_tool_name:
        return None
    result = domain.run_tool(
        write_tool_name,
        {
            "filename": file_write_plan.get("filename") or domain.file_write_default("filename", "final_answer.md"),
            "content": answer_text,
        },
        None,
    )
    return {
        "name": result.name,
        "content": result.content,
        "plan": file_write_plan,
    }


async def execute_file_write_plan_async(domain: DomainSpec, file_write_plan: dict[str, Any] | None, answer_text: str) -> dict[str, Any] | None:
    if not file_write_plan:
        return None
    try:
        return await asyncio.wait_for(
            asyncio.to_thread(execute_file_write_plan, domain, file_write_plan, answer_text),
            timeout=TOOL_CALL_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError:
        print(f"\n[警告] 写文件超时（>{TOOL_CALL_TIMEOUT_SECONDS:.0f}s）：{file_write_plan.get('filename')}")
        return None
    except Exception as exc:
        print(f"\n[警告] 写文件异常：{exc}")
        return None


async def call_executor_with_retry(
    client: AsyncOpenAI,
    messages: list[dict[str, str]],
    model_name: str,
    max_retries: int,
    include_reasoning: bool = True,
) -> dict[str, Any] | None:
    for attempt in range(max_retries):
        try:
            completion = await client.chat.completions.create(
                model=model_name,
                messages=messages,
                temperature=0.7,
                top_p=0.8,
                extra_body={"thinking": {"type": "enabled"}},
                
            )
            message = completion.choices[0].message
            reasoning_content = getattr(message, "reasoning_content", None)
            content = message.content or ""
            return {
                "reasoning_content": reasoning_content,
                "content": content,
                "text": compose_answer_text(reasoning_content, content, include_reasoning=include_reasoning),
            }
        except Exception as exc:
            if attempt < max_retries - 1:
                await asyncio.sleep(min(2**attempt, RETRY_BACKOFF_CAP_SECONDS) + 1)
            else:
                print(f"\n[失败] executor 达到最大重试次数：{exc}")
                return None
    return None


async def build_executor_output_row(
    row: dict[str, Any],
    client: AsyncOpenAI,
    model_name: str,
    domain: DomainSpec,
    max_retries: int,
    max_external_chars: int,
    include_reasoning: bool,
    include_tool_errors: bool,
) -> dict[str, Any] | None:
    user_query = str(row.get("user_query") or "").strip()
    if not user_query:
        return None
    intent = extract_intent(row, domain)
    external_info, tool_names, has_tool_error = extract_external_info(
        row,
        max_chars=max_external_chars,
        include_errors=include_tool_errors,
    )
    answer_template = domain.get_answer_template(intent) if domain.has_answer_template else None
    clarification_ask = extract_clarification_ask(row)
    compressed_history_text = extract_compressed_history_text(row)
    recent_history_text = extract_recent_history_text(row)
    messages = build_domain_executor_messages(
        domain,
        user_query=user_query,
        intent=intent,
        external_info=external_info,
        answer_template=answer_template,
        clarification_ask=clarification_ask,
        compressed_history_text=compressed_history_text,
        recent_history_text=recent_history_text,
    )
    answer = await call_executor_with_retry(
        client=client,
        messages=messages,
        model_name=model_name,
        max_retries=max_retries,
        include_reasoning=include_reasoning,
    )
    if answer is None:
        return None
    file_write_plan = extract_file_write_plan(row, domain)
    file_write_result = await execute_file_write_plan_async(domain, file_write_plan, answer["text"])

    return {
        "id": build_sample_id(row),
        "domain": domain.name,
        "source_id": row.get("id"),
        "user_query": user_query,
        "scenario": row.get("scenario"),
        "student_profile": row.get("student_profile"),
        "target_competency": row.get("target_competency"),
        "intent": intent,
        "answer_template": answer_template,
        "clarification_ask": clarification_ask,
        "compressed_history_text": compressed_history_text,
        "recent_history_text": recent_history_text,
        "external_info": external_info,
        "file_write_plan": file_write_plan,
        "file_write_result": file_write_result,
        "messages": [
            *messages,
            {"role": "assistant", "content": answer["text"]},
        ],
        "executor_raw_output": answer,
        "metadata": {
            "source": "planner_trajectory",
            "tool_names": tool_names,
            "fixed_tool_names": get_fixed_tool_names(row),
            "has_tool_error": has_tool_error,
            "has_file_write_plan": file_write_plan is not None,
            "file_written": file_write_result is not None,
            "include_tool_errors": include_tool_errors,
            "include_reasoning": include_reasoning,
            "answer_template_intent": intent if answer_template else None,
            "has_clarification_ask": clarification_ask is not None,
            "has_compressed_history": compressed_history_text is not None,
            "has_recent_history": recent_history_text is not None,
            "max_external_chars": max_external_chars,
            "planner_tool_sampling_mode": row.get("tool_sampling_mode"),
            "source_metadata": row.get("metadata"),
        },
    }


async def process_one(
    row: dict[str, Any],
    client: AsyncOpenAI,
    model_name: str,
    domain: DomainSpec,
    max_retries: int,
    max_external_chars: int,
    include_reasoning: bool,
    include_tool_errors: bool,
    sem: asyncio.Semaphore,
    f_out,
) -> bool:
    async with sem:
        output_row = await build_executor_output_row(
            row=row,
            client=client,
            model_name=model_name,
            domain=domain,
            max_retries=max_retries,
            max_external_chars=max_external_chars,
            include_reasoning=include_reasoning,
            include_tool_errors=include_tool_errors,
        )
        if output_row is None:
            return False
        async with write_lock:
            f_out.write(json.dumps(output_row, ensure_ascii=False) + "\n")
            f_out.flush()
        return True


async def process_dataset(
    input_file: Path,
    output_file: Path,
    model_name: str,
    domain: DomainSpec,
    max_samples: int,
    max_workers: int,
    max_retries: int,
    max_external_chars: int,
    include_reasoning: bool,
    include_tool_errors: bool,
    force_rebuild: bool,
    api_key: str | None = None,
    base_url: str | None = None,
    expected_intents: tuple[str, ...] = (),
) -> None:
    output_file.parent.mkdir(parents=True, exist_ok=True)
    if force_rebuild and output_file.exists():
        output_file.write_text("", encoding="utf-8")

    rows = load_jsonl(input_file)
    selected_rows = filter_rows_by_expected_intents(rows, expected_intents, domain) if domain.has_intent else rows
    processed_ids = get_processed_ids(output_file)
    pending = [row for row in selected_rows if build_sample_id(row) not in processed_ids]
    pending = pending[:max_samples]

    if expected_intents and domain.has_intent:
        print(f"🎯 仅合成指定意图：{', '.join(expected_intents)}")
    print(
        f"domain={domain.name}；读取 {len(rows)} 条规划轨迹，命中意图过滤 {len(selected_rows)} 条，已处理 {len(processed_ids)} 条，本次处理 {len(pending)} 条。"
    )
    if not pending:
        return

    client = get_client(api_key=api_key, base_url=base_url)
    sem = asyncio.Semaphore(max_workers)
    with output_file.open("a", encoding="utf-8") as f_out:
        tasks = [
            asyncio.create_task(
                process_one(
                    row=row,
                    client=client,
                    model_name=model_name,
                    domain=domain,
                    max_retries=max_retries,
                    max_external_chars=max_external_chars,
                    include_reasoning=include_reasoning,
                    include_tool_errors=include_tool_errors,
                    sem=sem,
                    f_out=f_out,
                )
            )
            for row in pending
        ]
        try:
            for task in tqdm(asyncio.as_completed(tasks), total=len(tasks), desc="构建执行阶段回答"):
                try:
                    await task
                except Exception as exc:
                    if not domain.is_fatal_tool_error(exc):
                        raise
                    print(
                        "\n[致命] 领域工具调用失败，已停止执行阶段生成："
                        f"{exc}\n"
                        f"  query: {getattr(exc, 'query', None)!r}"
                    )
                    for pending_task in tasks:
                        if not pending_task.done():
                            pending_task.cancel()
                    await asyncio.gather(*tasks, return_exceptions=True)
                    raise SystemExit(2) from exc
        finally:
            f_out.flush()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build execution-stage final-answer SFT data from planner trajectories.")
    parser.add_argument("--input", type=Path, default=None)
    parser.add_argument("--output", type=Path, default=None)
    parser.add_argument("--domain", default="health", help="领域包名称，默认 health。")
    parser.add_argument("--model-name", default=None, help="留空则用运行时配置里的执行模型。")
    parser.add_argument("--max-samples", type=int, default=DEFAULT_MAX_SAMPLES)
    parser.add_argument("--max-workers", type=int, default=DEFAULT_MAX_WORKERS)
    parser.add_argument("--max-retries", type=int, default=DEFAULT_MAX_RETRIES)
    parser.add_argument("--max-external-chars", type=int, default=DEFAULT_MAX_EXTERNAL_CHARS)
    parser.add_argument("--no-reasoning", action="store_true", help="assistant 内容只保存最终回答，不包含 <think>。")
    parser.add_argument("--exclude-tool-errors", action="store_true", help="提取外部信息时忽略失败工具返回。")
    parser.add_argument(
        "--expected-intent",
        action="append",
        dest="expected_intents",
        help="只合成指定领域意图，可重复传入；也可用环境变量 AGENT_EXECUTOR_EXPECTED_INTENTS=意图A,意图B。",
    )
    parser.add_argument("--force-rebuild", action="store_true")
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    domain = load_domain(args.domain)
    input_file = args.input or DEFAULT_INPUT
    output_file = args.output or DEFAULT_OUTPUT
    if args.input is None and args.domain and not os.getenv("AGENT_EXECUTOR_INPUT_FILE"):
        input_file = output_file_for_domain("planner_trajectories", domain.name)
    if args.output is None and args.domain and not os.getenv("AGENT_EXECUTOR_ANSWER_FILE"):
        output_file = output_file_for_domain("executor_answer_sft", domain.name)
    expected_intents = normalize_expected_intents(
        args.expected_intents if args.expected_intents is not None else (EXECUTOR_EXPECTED_INTENTS if domain.has_intent else ()),
        domain,
    )
    print(f"🚀 开始构建执行阶段最终回答数据... domain={domain.name}")
    asyncio.run(
        process_dataset(
            input_file=input_file,
            output_file=output_file,
            model_name=args.model_name or resolve_executor_model(),
            domain=domain,
            max_samples=args.max_samples,
            max_workers=args.max_workers,
            max_retries=args.max_retries,
            max_external_chars=args.max_external_chars,
            include_reasoning=not args.no_reasoning,
            include_tool_errors=not args.exclude_tool_errors,
            force_rebuild=args.force_rebuild,
            expected_intents=expected_intents,
        )
    )
    print("✅ 执行阶段数据生成结束！")
