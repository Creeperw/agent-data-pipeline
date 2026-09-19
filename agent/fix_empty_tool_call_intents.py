"""Repair historical planner trajectories with empty-content tool-call intents.

Usage examples:

    python -m agent.fix_empty_tool_call_intents --dry-run
    python -m agent.fix_empty_tool_call_intents --in-place
    python -m agent.fix_empty_tool_call_intents --input agent/outputs/intent_tool_trajectories2.jsonl --output agent/outputs/intent_tool_trajectories2.fixed.jsonl

The script only changes steps matching all conditions:
- ``planner_output.action == "tool_call"``;
- ``planner_raw_output.raw_content`` is empty;
- current step intent is missing or ``其他``;
- final trajectory step has a non-``其他`` intent.
"""

from __future__ import annotations

import argparse
import json
import shutil
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

try:
    from .config import DistillConfig
    from .generator import patch_empty_tool_call_intents_from_final
except ImportError:  # Allows: python agent/fix_empty_tool_call_intents.py
    from config import DistillConfig
    from generator import patch_empty_tool_call_intents_from_final


@dataclass
class RepairStats:
    total_lines: int = 0
    json_rows: int = 0
    invalid_lines: int = 0
    changed_rows: int = 0
    changed_steps: int = 0


def default_output_path(input_path: Path) -> Path:
    suffix = input_path.suffix or ".jsonl"
    stem = input_path.name[: -len(suffix)] if input_path.name.endswith(suffix) else input_path.name
    return input_path.with_name(f"{stem}.intent_fixed{suffix}")


def repair_jsonl(input_path: Path, output_path: Path | None, dry_run: bool = False) -> RepairStats:
    stats = RepairStats()
    output_handle = None if dry_run or output_path is None else output_path.open("w", encoding="utf-8")
    try:
        with input_path.open("r", encoding="utf-8") as f_in:
            for line in f_in:
                stats.total_lines += 1
                stripped = line.strip()
                if not stripped:
                    if output_handle:
                        output_handle.write(line)
                    continue
                try:
                    row: dict[str, Any] = json.loads(stripped)
                except json.JSONDecodeError:
                    stats.invalid_lines += 1
                    if output_handle:
                        output_handle.write(line)
                    continue

                stats.json_rows += 1
                changed_steps = patch_empty_tool_call_intents_from_final(row.get("trajectory") or [])
                if changed_steps:
                    stats.changed_rows += 1
                    stats.changed_steps += changed_steps
                    metadata = row.setdefault("metadata", {})
                    if isinstance(metadata, dict):
                        metadata["empty_tool_call_intent_repaired"] = True
                        metadata["empty_tool_call_intent_repaired_steps"] = changed_steps
                    row["empty_tool_call_intent_backfill_count"] = (
                        int(row.get("empty_tool_call_intent_backfill_count") or 0) + changed_steps
                    )

                if output_handle:
                    output_handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    finally:
        if output_handle:
            output_handle.flush()
            output_handle.close()
    return stats


def build_backup_path(input_path: Path) -> Path:
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return input_path.with_name(f"{input_path.name}.bak_{timestamp}")


def parse_args() -> argparse.Namespace:
    default_input = DistillConfig().output_file
    parser = argparse.ArgumentParser(description="Repair empty-content tool-call step intents in trajectory JSONL.")
    parser.add_argument("--input", type=Path, default=default_input, help="原始轨迹 JSONL，默认读取 generator 输出文件。")
    parser.add_argument("--output", type=Path, default=None, help="修复后的输出路径；不指定且非 --in-place 时自动写 *.intent_fixed.jsonl。")
    parser.add_argument("--in-place", action="store_true", help="直接覆盖 input 文件；覆盖前默认生成 .bak_时间戳 备份。")
    parser.add_argument("--no-backup", action="store_true", help="与 --in-place 一起使用时不创建备份。")
    parser.add_argument("--dry-run", action="store_true", help="只统计会修复多少行，不写文件。")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    input_path = args.input.resolve()
    if not input_path.exists():
        raise FileNotFoundError(f"输入文件不存在：{input_path}")

    if args.dry_run:
        stats = repair_jsonl(input_path, output_path=None, dry_run=True)
        print(
            f"dry-run 完成：json_rows={stats.json_rows}，changed_rows={stats.changed_rows}，"
            f"changed_steps={stats.changed_steps}，invalid_lines={stats.invalid_lines}"
        )
        return

    if args.in_place:
        output_path = input_path.with_name(f".{input_path.name}.tmp_intent_fix")
    else:
        output_path = (args.output or default_output_path(input_path)).resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)

    stats = repair_jsonl(input_path, output_path=output_path, dry_run=False)

    if args.in_place:
        backup_path = None
        if not args.no_backup:
            backup_path = build_backup_path(input_path)
            shutil.copy2(input_path, backup_path)
        output_path.replace(input_path)
        backup_text = f"；备份：{backup_path}" if backup_path else ""
        target_text = f"已覆盖：{input_path}{backup_text}"
    else:
        target_text = f"输出：{output_path}"

    print(
        f"修复完成：json_rows={stats.json_rows}，changed_rows={stats.changed_rows}，"
        f"changed_steps={stats.changed_steps}，invalid_lines={stats.invalid_lines}。{target_text}"
    )


if __name__ == "__main__":
    main()