"""Render SFT message samples into final prompt JSONL with resume support."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from transformers import AutoTokenizer

try:
    from .config import CONVERT_TO_FINAL_INPUT_FILE, PLANNER_FINAL_PROMPT_FILE, QWEN_BASE_MODEL_PATH
except ImportError:  # Allows: python agent/convert_to_final.py
    from config import CONVERT_TO_FINAL_INPUT_FILE, PLANNER_FINAL_PROMPT_FILE, QWEN_BASE_MODEL_PATH


DEFAULT_MODEL_PATH = QWEN_BASE_MODEL_PATH
DEFAULT_INPUT = CONVERT_TO_FINAL_INPUT_FILE
DEFAULT_OUTPUT = PLANNER_FINAL_PROMPT_FILE


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


def make_row_id(row: dict[str, Any], line_no: int) -> str:
    return str(row.get("id") or f"line_{line_no}")


def render_prompt(tokenizer: AutoTokenizer, row: dict[str, Any]) -> str:
    return tokenizer.apply_chat_template(
        row.get("messages") or [],
        tools=row.get("tools") or [],
        tokenize=False,
        add_generation_prompt=False,
    )


def convert_file(input_path: Path, output_path: Path, model_path: Path, force_rebuild: bool = False) -> int:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if force_rebuild and output_path.exists():
        output_path.write_text("", encoding="utf-8")

    processed_ids = get_processed_ids(output_path)
    tokenizer = AutoTokenizer.from_pretrained(str(model_path), trust_remote_code=True)

    count = 0
    skipped = 0
    with input_path.open("r", encoding="utf-8") as f_in, output_path.open("a", encoding="utf-8") as f_out:
        for line_no, line in enumerate(f_in, start=1):
            if not line.strip():
                continue
            row = json.loads(line)
            row_id = make_row_id(row, line_no)
            if row_id in processed_ids:
                skipped += 1
                continue

            prompt = render_prompt(tokenizer, row)
            f_out.write(
                json.dumps(
                    {
                        "id": row_id,
                        "prompt": prompt,
                        "metadata": row.get("metadata", {}),
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )
            processed_ids.add(row_id)
            count += 1

    if skipped:
        print(f"↪️ 已跳过 {skipped} 条已渲染 prompt 样本。")
    return count


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Render SFT messages into apply_chat_template prompt JSONL.")
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--model-path", type=Path, default=DEFAULT_MODEL_PATH)
    parser.add_argument("--force-rebuild", action="store_true", help="清空输出文件并重新渲染全部样本。")
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    total = convert_file(args.input, args.output, args.model_path, force_rebuild=args.force_rebuild)
    print(f"✅ 新增渲染 {total} 条 prompt 样本：{args.output}")
