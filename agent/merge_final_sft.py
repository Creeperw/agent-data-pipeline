"""Merge planner final prompts and executor-answer messages into one prompt JSONL.

Output format is exactly aligned with ``convert_to_final.py``:

    {"id": "...", "prompt": "...", "metadata": {...}}

So the merged file can be used directly by ``SFT_TRAIN2.py``.
"""

from __future__ import annotations

import argparse
import copy
import json
from pathlib import Path
from typing import Any

from transformers import AutoTokenizer

try:
    from .config import MERGE_EXECUTOR_INPUT_FILE, MERGE_PLANNER_FINAL_FILE, MERGED_FINAL_PROMPT_FILE, QWEN_BASE_MODEL_PATH
    from .core.domain import DomainSpec, load_domain
except ImportError:  # Allows: python agent/merge_final_sft.py
    from config import MERGE_EXECUTOR_INPUT_FILE, MERGE_PLANNER_FINAL_FILE, MERGED_FINAL_PROMPT_FILE, QWEN_BASE_MODEL_PATH
    from agent.core.domain import DomainSpec, load_domain


DEFAULT_MODEL_PATH = QWEN_BASE_MODEL_PATH
DEFAULT_PLANNER_FINAL = MERGE_PLANNER_FINAL_FILE
DEFAULT_EXECUTOR_INPUT = MERGE_EXECUTOR_INPUT_FILE
DEFAULT_OUTPUT = MERGED_FINAL_PROMPT_FILE

MERGED_METADATA_DEFAULTS: dict[str, Any] = {
    "merge_source": "",
    "source": "",
    "source_id": "",
    "step": 0,
    "action": "",
    "include_reasoning": False,
    "planner_overridden": False,
    "tool_names": "",
    "has_tool_error": False,
    "has_file_write_plan": False,
    "file_written": False,
    "include_tool_errors": False,
    "answer_template_intent": "",
    "max_external_chars": 0,
    "planner_tool_sampling_mode": "",
    "intent": "",
    "has_clarification_ask": False,
    "domain": "",
    "executor_assembly_system_prompt_source": "",
}


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


def make_row_id(row: dict[str, Any], line_no: int, prefix: str) -> str:
    row_id = row.get("id")
    return str(row_id) if row_id else f"{prefix}_line_{line_no}"


def _as_str(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, list):
        return ",".join(str(item) for item in value)
    if isinstance(value, dict):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def _as_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def merge_metadata(metadata: Any, merge_source: str, extra: dict[str, Any] | None = None) -> dict[str, Any]:
    """Return a fixed scalar metadata schema for every merged row.

    ``datasets.load_dataset('json')`` cannot safely infer heterogeneous nested
    metadata structs. Keep exactly the same scalar keys in planner and executor
    rows so the merged JSONL remains Arrow-friendly.
    """
    raw = dict(metadata) if isinstance(metadata, dict) else {}
    raw["merge_source"] = merge_source
    if extra:
        raw.update(extra)

    result = dict(MERGED_METADATA_DEFAULTS)
    for key in result:
        value = raw.get(key, result[key])
        if isinstance(result[key], bool):
            result[key] = bool(value)
        elif isinstance(result[key], int):
            result[key] = _as_int(value)
        else:
            result[key] = _as_str(value)
    return result


def write_row(f_out, row_id: str, prompt: str, metadata: dict[str, Any]) -> None:
    f_out.write(
        json.dumps(
            {
                "id": row_id,
                "prompt": prompt,
                "metadata": metadata,
            },
            ensure_ascii=False,
        )
        + "\n"
    )


def append_planner_final(
    planner_final_path: Path,
    f_out,
    processed_ids: set[str],
    max_samples: int | None = None,
) -> tuple[int, int]:
    added = 0
    skipped = 0
    with planner_final_path.open("r", encoding="utf-8") as f_in:
        for line_no, line in enumerate(f_in, start=1):
            if max_samples is not None and added >= max_samples:
                break
            if not line.strip():
                continue
            row = json.loads(line)
            row_id = make_row_id(row, line_no, "planner_final")
            if row_id in processed_ids:
                skipped += 1
                continue
            prompt = str(row.get("prompt") or "")
            if not prompt:
                skipped += 1
                continue
            metadata = merge_metadata(row.get("metadata", {}), "planner_final")
            write_row(f_out, row_id, prompt, metadata)
            processed_ids.add(row_id)
            added += 1
    return added, skipped


def resolve_row_domain(row: dict[str, Any]) -> DomainSpec:
    metadata = row.get("metadata") or {}
    domain_name = row.get("domain")
    if not domain_name and isinstance(metadata, dict):
        domain_name = metadata.get("domain")
    try:
        return load_domain(str(domain_name or "health"))
    except Exception:
        return load_domain("health")


def render_executor_prompt(tokenizer: AutoTokenizer, row: dict[str, Any], domain: DomainSpec) -> str:
    messages = copy.deepcopy(row.get("messages") or [])
    if domain.executor_assembly_system_prompt is not None:
        assembly_prompt = domain.executor_assembly_system_prompt.strip()
        if messages and isinstance(messages[0], dict) and messages[0].get("role") == "system":
            messages[0]["content"] = assembly_prompt
        else:
            messages.insert(0, {"role": "system", "content": assembly_prompt})
    return tokenizer.apply_chat_template(
        messages,
        tools=row.get("tools") or [],
        tokenize=False,
        add_generation_prompt=False,
    )


def append_executor_answers(
    executor_input_path: Path,
    f_out,
    processed_ids: set[str],
    tokenizer: AutoTokenizer,
    max_samples: int | None = None,
) -> tuple[int, int]:
    added = 0
    skipped = 0
    with executor_input_path.open("r", encoding="utf-8") as f_in:
        for line_no, line in enumerate(f_in, start=1):
            if max_samples is not None and added >= max_samples:
                break
            if not line.strip():
                continue
            row = json.loads(line)
            row_id = make_row_id(row, line_no, "executor")
            if row_id in processed_ids:
                skipped += 1
                continue
            messages = row.get("messages") or []
            if not messages:
                skipped += 1
                continue
            domain = resolve_row_domain(row)
            prompt = render_executor_prompt(tokenizer, row, domain)
            metadata = merge_metadata(
                row.get("metadata", {}),
                "executor_answer",
                {
                    "domain": domain.name,
                    "source_id": row.get("source_id"),
                    "intent": row.get("intent"),
                    "has_clarification_ask": row.get("clarification_ask") is not None,
                    "executor_assembly_system_prompt_source": "domain" if domain.executor_assembly_system_prompt is not None else "executor_messages",
                },
            )
            write_row(f_out, row_id, prompt, metadata)
            processed_ids.add(row_id)
            added += 1
    return added, skipped


def merge_files(
    planner_final_path: Path,
    executor_input_path: Path,
    output_path: Path,
    model_path: Path,
    force_rebuild: bool = False,
    max_planner_samples: int | None = None,
    max_executor_samples: int | None = None,
) -> dict[str, int]:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if force_rebuild and output_path.exists():
        output_path.write_text("", encoding="utf-8")

    processed_ids = get_processed_ids(output_path)
    tokenizer = AutoTokenizer.from_pretrained(str(model_path), trust_remote_code=True)

    planner_added = planner_skipped = executor_added = executor_skipped = 0
    with output_path.open("a", encoding="utf-8") as f_out:
        if planner_final_path.exists():
            planner_added, planner_skipped = append_planner_final(
                planner_final_path,
                f_out,
                processed_ids,
                max_samples=max_planner_samples,
            )
        else:
            print(f"⚠️ planner final 文件不存在，已跳过：{planner_final_path}")

        if executor_input_path.exists():
            executor_added, executor_skipped = append_executor_answers(
                executor_input_path,
                f_out,
                processed_ids,
                tokenizer,
                max_samples=max_executor_samples,
            )
        else:
            print(f"⚠️ executor answer 文件不存在，已跳过：{executor_input_path}")

        f_out.flush()

    return {
        "planner_added": planner_added,
        "planner_skipped": planner_skipped,
        "executor_added": executor_added,
        "executor_skipped": executor_skipped,
        "total_added": planner_added + executor_added,
        "total_skipped": planner_skipped + executor_skipped,
    }


def parse_optional_positive_int(value: str | None) -> int | None:
    if value is None:
        return None
    number = int(value)
    return number if number >= 0 else None


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Merge planner final prompts and executor-answer messages into one prompt JSONL.")
    parser.add_argument("--planner-final", type=Path, default=DEFAULT_PLANNER_FINAL, help="convert_to_final.py 输出的 planner prompt JSONL。")
    parser.add_argument("--executor-input", type=Path, default=DEFAULT_EXECUTOR_INPUT, help="executor_generator.py 输出的 messages JSONL。")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT, help="合并后的 prompt JSONL，可直接给 SFT_TRAIN2.py 使用。")
    parser.add_argument("--model-path", type=Path, default=DEFAULT_MODEL_PATH, help="用于 apply_chat_template 的 tokenizer 路径。")
    parser.add_argument("--max-planner-samples", type=parse_optional_positive_int, default=None, help="最多合并多少条 planner prompt；不填表示全部。")
    parser.add_argument("--max-executor-samples", type=parse_optional_positive_int, default=None, help="最多渲染多少条 executor answer；不填表示全部。")
    parser.add_argument("--force-rebuild", action="store_true", help="清空输出文件并重新合并全部样本。")
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    stats = merge_files(
        planner_final_path=args.planner_final,
        executor_input_path=args.executor_input,
        output_path=args.output,
        model_path=args.model_path,
        force_rebuild=args.force_rebuild,
        max_planner_samples=args.max_planner_samples,
        max_executor_samples=args.max_executor_samples,
    )
    print(
        "✅ 合并完成："
        f"planner 新增 {stats['planner_added']} / 跳过 {stats['planner_skipped']}，"
        f"executor 新增 {stats['executor_added']} / 跳过 {stats['executor_skipped']}，"
        f"总新增 {stats['total_added']}。输出：{args.output}"
    )