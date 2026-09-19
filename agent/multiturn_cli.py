"""Generate multi-turn synthesis data without changing the single-turn pipeline."""

from __future__ import annotations

import argparse
import asyncio
import json
from dataclasses import replace
from pathlib import Path
from typing import Any

from openai import AsyncOpenAI

try:
    from .config import (
        MULTI_TURN_COMPRESS_EVERY_TURNS,
        MULTI_TURN_EXECUTOR_FILE,
        MULTI_TURN_MAX_TURNS,
        MULTI_TURN_PLANNER_FILE,
        MULTI_TURN_RECENT_TURN_LIMIT,
        MULTI_TURN_SESSION_FILE,
        DistillConfig,
        multi_turn_compression_model_name,
        multi_turn_user_model_name,
        output_file_for_domain,
        resolve_distill_model,
    )
    from .core.domain import load_domain
    from .core.renderer import build_executor_messages as build_domain_executor_messages
    from .generator import (
        choose_tool_sampling_mode,
        get_client as get_planner_client,
        sample_tools_for_sample,
        get_tool_name,
        assign_sample_controls,
        filter_samples_by_expected_intents,
        load_jsonl,
        build_trajectory_for_sample,
        TOOL_FAILURE_ENABLED_KEY,
        TOOL_SAMPLING_MODE_KEY,
    )
    from .executor_generator import (
        build_executor_output_row,
        get_client as get_executor_client,
        extract_compressed_history_text,
        extract_history_turns,
        extract_recent_history_text,
    )
    from .multiturn import (
        MultiTurnSessionBuilder,
        build_history_compression_messages,
        build_user_simulator_messages,
    )
    from .multiturn.prompts import MULTI_TURN_HISTORY_COMPRESSION_PROMPT, MULTI_TURN_USER_SIMULATOR_PROMPT
    from .core.conversation import build_history_context_text, render_dialogue_turns, strip_think_text
except ImportError:  # Allows: python agent/multiturn_cli.py
    from config import (
        MULTI_TURN_COMPRESS_EVERY_TURNS,
        MULTI_TURN_EXECUTOR_FILE,
        MULTI_TURN_MAX_TURNS,
        MULTI_TURN_PLANNER_FILE,
        MULTI_TURN_RECENT_TURN_LIMIT,
        MULTI_TURN_SESSION_FILE,
        DistillConfig,
        multi_turn_compression_model_name,
        multi_turn_user_model_name,
        output_file_for_domain,
        resolve_distill_model,
    )
    from agent.core.domain import load_domain
    from agent.core.renderer import build_executor_messages as build_domain_executor_messages
    from agent.generator import (
        choose_tool_sampling_mode,
        get_client as get_planner_client,
        sample_tools_for_sample,
        get_tool_name,
        assign_sample_controls,
        filter_samples_by_expected_intents,
        load_jsonl,
        build_trajectory_for_sample,
        TOOL_FAILURE_ENABLED_KEY,
        TOOL_SAMPLING_MODE_KEY,
    )
    from agent.executor_generator import (
        build_executor_output_row,
        get_client as get_executor_client,
        extract_compressed_history_text,
        extract_history_turns,
        extract_recent_history_text,
    )
    from agent.multiturn import (
        MultiTurnSessionBuilder,
        build_history_compression_messages,
        build_user_simulator_messages,
    )
    from agent.multiturn.prompts import MULTI_TURN_HISTORY_COMPRESSION_PROMPT, MULTI_TURN_USER_SIMULATOR_PROMPT
    from agent.core.conversation import build_history_context_text, render_dialogue_turns, strip_think_text


def _safe_json_loads(text: str) -> dict[str, Any] | None:
    text = (text or "").strip()
    if not text:
        return None
    try:
        data = json.loads(text)
        return data if isinstance(data, dict) else None
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start != -1 and end != -1 and end > start:
            try:
                data = json.loads(text[start : end + 1])
                return data if isinstance(data, dict) else None
            except json.JSONDecodeError:
                return None
    return None


async def generate_compressed_history(
    client: AsyncOpenAI,
    history_text: str,
    next_user_prompt: str,
    model_name: str,
) -> str:
    messages = build_history_compression_messages(history_text, next_user_prompt)
    completion = await client.chat.completions.create(
        model=model_name,
        messages=messages,
        temperature=0.2,
        top_p=0.8,
        max_tokens=800,
    )
    content = completion.choices[0].message.content or ""
    return strip_think_text(content)


async def generate_next_user_query(
    client: AsyncOpenAI,
    session_context_text: str,
    model_name: str,
) -> dict[str, Any]:
    messages = build_user_simulator_messages(session_context_text)
    completion = await client.chat.completions.create(
        model=model_name,
        messages=messages,
        temperature=0.7,
        top_p=0.9,
        max_tokens=400,
    )
    content = completion.choices[0].message.content or ""
    parsed = _safe_json_loads(content)
    if parsed is None:
        return {"continue": False, "user_query": "", "reason": "无法解析用户模拟器输出"}
    parsed.setdefault("continue", False)
    parsed.setdefault("user_query", "")
    parsed.setdefault("reason", "")
    return parsed


def build_multiturn_session_output(session: MultiTurnSessionBuilder, sample: dict[str, Any], compressed_history_text: str | None) -> dict[str, Any]:
    record = session.build()
    history_turns = record.history_turns()
    return {
        "session_id": record.session_id,
        "domain": record.domain,
        "source_id": sample.get("id"),
        "user_query": sample.get("user_query"),
        "history_turns": history_turns,
        "history_context_text": record.history_context_text(),
        "compressed_history_text": compressed_history_text or record.compressed_history_text,
        "fixed_tool_names": record.fixed_tool_names,
        "tool_failure_enabled": record.tool_failure_enabled,
        "tool_failure_targets": record.tool_failure_targets,
        "failure_turn_indexes": record.failure_turn_indexes,
        "recent_turn_limit": record.recent_turn_limit,
        "compress_every_turns": record.compress_every_turns,
        "last_compressed_turn_index": record.last_compressed_turn_index,
        "next_user_prompt": record.next_user_prompt,
        "metadata": sample.get("metadata"),
    }


def build_user_simulator_context(session: MultiTurnSessionBuilder, sample: dict[str, Any], executor_output: dict[str, Any]) -> str:
    record = session.build()
    return (
        "请根据下面信息决定是否继续生成下一轮用户追问。\n\n"
        f"【会话主题】\n{sample.get('user_query', '')}\n\n"
        f"【当前领域】\n{record.domain}\n\n"
        f"【近期历史对话】\n{render_dialogue_turns(record.history_turns()) or '无'}\n\n"
        f"【最近助手回复】\n{strip_think_text((executor_output.get('executor_raw_output') or {}).get('text') or '')}"
    )


def make_turn_sample(
    base_sample: dict[str, Any],
    session: MultiTurnSessionBuilder,
    user_query: str,
    turn_index: int,
) -> dict[str, Any]:
    record = session.build()
    turn_sample = dict(base_sample)
    turn_sample["id"] = f"{base_sample.get('id')}_mt_turn_{turn_index}"
    turn_sample["user_query"] = user_query
    turn_sample["history"] = record.history_turns()
    turn_sample["compressed_history_text"] = record.compressed_history_text
    turn_sample["fixed_tool_names"] = record.fixed_tool_names
    turn_sample["session_tool_names"] = record.fixed_tool_names
    turn_sample[TOOL_SAMPLING_MODE_KEY] = base_sample.get(TOOL_SAMPLING_MODE_KEY, "normal")
    turn_sample[TOOL_FAILURE_ENABLED_KEY] = turn_index in set(record.failure_turn_indexes)
    turn_sample["conversation_id"] = record.session_id
    turn_sample["turn_index"] = turn_index
    return turn_sample


def fallback_next_user_query(sample: dict[str, Any], turn_index: int) -> str:
    intent = str(sample.get("expected_intent") or sample.get("intent") or "").strip()
    if turn_index == 1:
        if "技能培训" in intent:
            return "能不能给我一个具体的课堂练习案例，让学生可以直接照着做？"
        if "学情分析" in intent:
            return "如果我要根据这个分析结果给学生安排补救训练，下一步该怎么做？"
        if "教学方案设计" in intent:
            return "能不能把这个教学流程再细化成一节课可以直接使用的安排？"
        return "能不能再给一个更具体、可直接执行的示例？"
    if turn_index == 2:
        return "如果换一个类似但不完全相同的场景，应该怎么调整这个方案？"
    return "还有哪些容易出错的地方需要提前提醒学生？"


async def maybe_compress_history(
    session: MultiTurnSessionBuilder,
    client: AsyncOpenAI,
    model_name: str,
    next_user_prompt: str,
) -> None:
    record = session.build()
    turns = record.history_turns()
    if len(turns) <= record.recent_turn_limit:
        return
    old_turns = turns[: -record.recent_turn_limit]
    history_text = render_dialogue_turns(old_turns)
    if not history_text:
        return
    compressed = await generate_compressed_history(client, history_text, next_user_prompt, model_name)
    record.set_compressed_history(compressed, source="model")


async def process_one_session(
    sample: dict[str, Any],
    planner_client: AsyncOpenAI,
    executor_client: AsyncOpenAI,
    compression_client: AsyncOpenAI,
    config: DistillConfig,
    domain,
    max_turns: int,
    min_turns: int,
    compress_every_turns: int,
) -> dict[str, Any] | None:
    session = MultiTurnSessionBuilder(
        domain=domain.name,
        recent_turn_limit=config.recent_history_turn_limit,
        compress_every_turns=compress_every_turns,
    )
    # 模型名在这里一次性解析：缺配置时立刻报错，不会跑到一半才炸。
    user_model_name = multi_turn_user_model_name()
    compression_model_name = multi_turn_compression_model_name()
    planner_model_name = config.model_name or resolve_distill_model()
    tool_sampling_mode = choose_tool_sampling_mode(sample, config)
    sampled_tools = sample_tools_for_sample(
        sample,
        max_tool_count=config.max_tool_count,
        mode=tool_sampling_mode,
        tools=domain.tools,
        domain=domain,
    )
    fixed_tool_names = [get_tool_name(tool) for tool in sampled_tools if get_tool_name(tool)]
    failure_enabled = bool(sample.get(TOOL_FAILURE_ENABLED_KEY, False))
    failure_turn_indexes = [1] if failure_enabled else []
    session.with_tool_state(fixed_tool_names, tool_failure_enabled=failure_enabled, tool_failure_targets=[])
    session.with_failure_turn_indexes(failure_turn_indexes)
    history_turns = extract_history_turns(sample)
    if history_turns:
        session.record.user_turns = [{"user": turn.get("user") or turn.get("user_query") or ""} for turn in history_turns]
        session.record.assistant_turns = [{"assistant": strip_think_text(turn.get("assistant") or turn.get("assistant_answer") or turn.get("assistant_content") or turn.get("response") or turn.get("answer") or "")} for turn in history_turns]
    compressed_history_text = extract_compressed_history_text(sample)
    if not compressed_history_text and len(history_turns) > config.recent_history_turn_limit:
        history_text = render_dialogue_turns(history_turns[:-config.recent_history_turn_limit])
        compressed_history_text = await generate_compressed_history(
            compression_client,
            history_text,
            sample.get("user_query", ""),
            compression_model_name,
        )
    session.set_compressed_history(compressed_history_text, source="model" if compressed_history_text else None)
    planner_rows: list[dict[str, Any]] = []
    executor_rows: list[dict[str, Any]] = []
    current_user_query = str(sample.get("user_query") or "").strip()
    for turn_index in range(1, max_turns + 1):
        if not current_user_query:
            break
        turn_sample = make_turn_sample(sample, session, current_user_query, turn_index)
        planner_result = await build_multiturn_planner_row(turn_sample, planner_client, config, domain)
        if planner_result is None:
            break
        planner_result["conversation_id"] = session.build().session_id
        planner_result["turn_index"] = turn_index
        planner_rows.append(planner_result)
        executor_row = dict(planner_result)
        executor_row["history"] = session.build().history_turns()
        executor_row["compressed_history_text"] = session.build().compressed_history_text
        executor_output = await build_executor_output_row(
            row=executor_row,
            client=executor_client,
            model_name=planner_model_name,
            domain=domain,
            max_retries=config.max_retries,
            max_external_chars=6000,
            include_reasoning=True,
            include_tool_errors=True,
        )
        if executor_output is None:
            break
        executor_output["conversation_id"] = session.build().session_id
        executor_output["turn_index"] = turn_index
        executor_rows.append(executor_output)
        assistant_text = strip_think_text((executor_output.get("executor_raw_output") or {}).get("text") or "")
        session.append_turn(current_user_query, assistant_text)
        if turn_index >= max_turns:
            break
        record = session.build()
        if record.should_compress_history(turn_index):
            await maybe_compress_history(session, compression_client, compression_model_name, current_user_query)
            record.mark_compressed(turn_index, source="model")
        user_sim = await generate_next_user_query(
            executor_client,
            build_user_simulator_context(session, sample, executor_output),
            user_model_name,
        )
        if not user_sim.get("continue"):
            if turn_index >= min_turns:
                break
            current_user_query = fallback_next_user_query(sample, turn_index)
            session.set_next_user_prompt(current_user_query)
            continue
        current_user_query = str(user_sim.get("user_query") or "").strip()
        if not current_user_query and turn_index < min_turns:
            current_user_query = fallback_next_user_query(sample, turn_index)
        session.set_next_user_prompt(current_user_query)

    return {
        "session": build_multiturn_session_output(session, sample, compressed_history_text),
        "planner": planner_rows,
        "executor": executor_rows,
    }


async def build_multiturn_planner_row(sample: dict[str, Any], client: AsyncOpenAI, config: DistillConfig, domain) -> dict[str, Any] | None:
    return await _build_single_turn_planner_row(sample, client, config, domain)


async def _build_single_turn_planner_row(sample: dict[str, Any], client: AsyncOpenAI, config: DistillConfig, domain) -> dict[str, Any] | None:
    from .generator import build_trajectory_for_sample

    return await build_trajectory_for_sample(sample, client, config, domain)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build multi-turn conversation synthesis data.")
    parser.add_argument("--input", type=Path, default=None)
    parser.add_argument("--output-session", type=Path, default=MULTI_TURN_SESSION_FILE)
    parser.add_argument("--output-planner", type=Path, default=MULTI_TURN_PLANNER_FILE)
    parser.add_argument("--output-executor", type=Path, default=MULTI_TURN_EXECUTOR_FILE)
    parser.add_argument("--domain", default="health")
    parser.add_argument("--max-samples", type=int, default=100)
    parser.add_argument("--max-turns", type=int, default=MULTI_TURN_MAX_TURNS)
    parser.add_argument("--min-turns", type=int, default=2)
    parser.add_argument("--recent-turn-limit", type=int, default=MULTI_TURN_RECENT_TURN_LIMIT)
    parser.add_argument("--compress-every-turns", type=int, default=MULTI_TURN_COMPRESS_EVERY_TURNS)
    parser.add_argument("--force-rebuild", action="store_true")
    return parser.parse_args()


def append_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")


async def run_multiturn(args: argparse.Namespace) -> None:
    base_config = DistillConfig()
    domain = load_domain(args.domain)
    input_file = args.input or base_config.input_file
    output_session = args.output_session
    output_planner = args.output_planner
    output_executor = args.output_executor
    if args.output_session == MULTI_TURN_SESSION_FILE:
        output_session = output_file_for_domain("multiturn_sessions", domain.name)
    if args.output_planner == MULTI_TURN_PLANNER_FILE:
        output_planner = output_file_for_domain("multiturn_planner_trajectories", domain.name)
    if args.output_executor == MULTI_TURN_EXECUTOR_FILE:
        output_executor = output_file_for_domain("multiturn_executor_answers", domain.name)

    config = replace(
        base_config,
        input_file=input_file,
        domain_name=domain.name,
        max_samples=args.max_samples,
        recent_history_turn_limit=args.recent_turn_limit,
        multi_turn_enabled=True,
    )
    config = replace(config, max_retries=base_config.max_retries)

    if args.force_rebuild:
        for path in (output_session, output_planner, output_executor):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("", encoding="utf-8")

    samples = load_jsonl(input_file)
    selected_samples = filter_samples_by_expected_intents(samples, config.expected_intents, domain) if domain.has_intent else samples
    pending = selected_samples[: args.max_samples]
    assign_sample_controls(pending, config)
    print(
        f"domain={domain.name}；读取 {len(samples)} 条 seed，命中意图过滤 {len(selected_samples)} 条，本次多轮处理 {len(pending)} 条。"
    )
    if not pending:
        return

    planner_client = get_planner_client(config)
    executor_client = get_executor_client(api_key=config.api_key, base_url=config.base_url)
    compression_client = executor_client

    session_rows: list[dict[str, Any]] = []
    planner_rows: list[dict[str, Any]] = []
    executor_rows: list[dict[str, Any]] = []
    for sample in pending:
        result = await process_one_session(
            sample=sample,
            planner_client=planner_client,
            executor_client=executor_client,
            compression_client=compression_client,
            config=config,
            domain=domain,
            max_turns=args.max_turns,
            min_turns=args.min_turns,
            compress_every_turns=args.compress_every_turns,
        )
        if result is None:
            continue
        session_rows.append(result["session"])
        planner_rows.extend(result["planner"])
        executor_rows.extend(result["executor"])

    append_jsonl(output_session, session_rows)
    append_jsonl(output_planner, planner_rows)
    append_jsonl(output_executor, executor_rows)
    print(f"✅ 多轮 session 写入 {len(session_rows)} 条：{output_session}")
    print(f"✅ 多轮 planner 写入 {len(planner_rows)} 条：{output_planner}")
    print(f"✅ 多轮 executor 写入 {len(executor_rows)} 条：{output_executor}")


def main() -> None:
    asyncio.run(run_multiturn(parse_args()))


if __name__ == "__main__":
    main()
