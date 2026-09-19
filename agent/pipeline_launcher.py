"""Launch the full agent data pipeline end to end.

This is a small orchestration wrapper, not a new implementation of the data
pipeline. It simply starts the existing stages in order:

1. planner trajectory synthesis
2. empty tool-call intent repair
3. planner step-wise SFT conversion
4. planner final-prompt rendering
5. executor answer synthesis
6. planner/executor merge
7. final prompt deduplication
8. data statistics report

The script keeps the stage paths explicit so it is easy to run the whole chain
with one command and still inspect each intermediate artifact.
"""

from __future__ import annotations

import argparse
import shlex
import subprocess
import sys
from pathlib import Path

try:
    from .config import PROJECT_ROOT, safe_path_part
except ImportError:  # Allows: python agent/pipeline_launcher.py
    from config import PROJECT_ROOT, safe_path_part  # type: ignore


DEFAULT_DOMAIN = "health"
DEFAULT_PREFIX = "valid"
DEFAULT_STATS_FORMAT = "markdown"


def build_seed_path(domain: str, explicit_seed_path: Path | None) -> Path:
    if explicit_seed_path is not None:
        return explicit_seed_path.resolve()
    safe_domain = safe_path_part(domain, DEFAULT_DOMAIN)
    return PROJECT_ROOT / "agent" / "domains" / safe_domain / "seeds.jsonl"


def build_output_dir(domain: str, explicit_output_dir: Path | None) -> Path:
    if explicit_output_dir is not None:
        return explicit_output_dir.resolve()
    safe_domain = safe_path_part(domain, DEFAULT_DOMAIN)
    return PROJECT_ROOT / "agent" / "outputs" / safe_domain


def stage_path(output_dir: Path, prefix: str, stage: str) -> Path:
    return output_dir / f"{prefix}_{stage}.jsonl"


def run_stage(title: str, command: list[str]) -> None:
    pretty_command = " ".join(shlex.quote(part) for part in command)
    print(f"\n=== {title} ===")
    print(pretty_command)
    subprocess.run(command, cwd=PROJECT_ROOT, check=True)


def maybe_add(option_list: list[str], flag: str, value: int | None) -> None:
    if value is not None:
        option_list.extend([flag, str(value)])


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Launch the full agent data pipeline.")
    parser.add_argument("--domain", default=DEFAULT_DOMAIN, help="领域名，默认 health。")
    parser.add_argument("--seed-input", type=Path, default=None, help="seed JSONL 路径；不传则默认 agent/domains/<domain>/seeds.jsonl。")
    parser.add_argument("--output-dir", type=Path, default=None, help="输出目录；不传则默认 agent/outputs/<domain>。")
    parser.add_argument("--prefix", default=DEFAULT_PREFIX, help="输出文件名前缀，默认 valid。")
    parser.add_argument("--max-samples", type=int, default=None, help="传给合成阶段的样本上限；不传则使用各模块默认值。")
    parser.add_argument("--max-workers", type=int, default=None, help="传给合成阶段的并发数；不传则使用各模块默认值。")
    parser.add_argument("--stats-format", choices=["markdown", "json"], default=DEFAULT_STATS_FORMAT, help="统计报告输出格式。")
    parser.add_argument("--stats-output", type=Path, default=None, help="统计报告输出路径；不传则写到输出目录下的 <prefix>_data_stats.<ext>。")
    parser.add_argument("--force-rebuild", action="store_true", help="重建所有可重建的阶段输出。")
    return parser.parse_args()


def main() -> None:
    args = parse_args()

    domain = safe_path_part(args.domain, DEFAULT_DOMAIN)
    seed_input = build_seed_path(domain, args.seed_input)
    if not seed_input.exists():
        raise FileNotFoundError(f"seed 文件不存在：{seed_input}")

    output_dir = build_output_dir(domain, args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    prefix = safe_path_part(args.prefix, DEFAULT_PREFIX)
    planner_trajectory = stage_path(output_dir, prefix, "planner_trajectories")
    planner_step_sft = stage_path(output_dir, prefix, "planner_step_sft")
    planner_final_prompt = stage_path(output_dir, prefix, "planner_final_prompt")
    executor_answer = stage_path(output_dir, prefix, "executor_answer_sft")
    merged_final_prompt = stage_path(output_dir, prefix, "merged_final_prompt")
    dedup_final_prompt = stage_path(output_dir, prefix, "dedup_final_prompt")
    duplicates_log = stage_path(output_dir, prefix, "duplicates")
    stats_suffix = "json" if args.stats_format == "json" else "md"
    stats_output = args.stats_output.resolve() if args.stats_output else output_dir / f"{prefix}_data_stats.{stats_suffix}"

    synthesize_cmd = [
        sys.executable,
        "-m",
        "agent.generator",
        "--domain",
        domain,
        "--input",
        str(seed_input),
        "--output",
        str(planner_trajectory),
    ]
    maybe_add(synthesize_cmd, "--max-samples", args.max_samples)
    maybe_add(synthesize_cmd, "--max-workers", args.max_workers)
    if args.force_rebuild:
        synthesize_cmd.append("--force-rebuild")

    run_stage("1/8 planner trajectory synthesis", synthesize_cmd)

    clean_cmd = [
        sys.executable,
        "-m",
        "agent.fix_empty_tool_call_intents",
        "--input",
        str(planner_trajectory),
        "--in-place",
    ]
    run_stage("2/8 clean empty tool-call intents", clean_cmd)

    step_sft_cmd = [
        sys.executable,
        "-m",
        "agent.convert_to_sft",
        "--input",
        str(planner_trajectory),
        "--output",
        str(planner_step_sft),
    ]
    if args.force_rebuild:
        step_sft_cmd.append("--force-rebuild")
    run_stage("3/8 planner step-wise conversion", step_sft_cmd)

    final_prompt_cmd = [
        sys.executable,
        "-m",
        "agent.convert_to_final",
        "--input",
        str(planner_step_sft),
        "--output",
        str(planner_final_prompt),
    ]
    if args.force_rebuild:
        final_prompt_cmd.append("--force-rebuild")
    run_stage("4/8 planner final-prompt rendering", final_prompt_cmd)

    executor_cmd = [
        sys.executable,
        "-m",
        "agent.executor_generator",
        "--domain",
        domain,
        "--input",
        str(planner_trajectory),
        "--output",
        str(executor_answer),
    ]
    maybe_add(executor_cmd, "--max-samples", args.max_samples)
    maybe_add(executor_cmd, "--max-workers", args.max_workers)
    if args.force_rebuild:
        executor_cmd.append("--force-rebuild")
    run_stage("5/8 executor answer synthesis", executor_cmd)

    merge_cmd = [
        sys.executable,
        "-m",
        "agent.merge_final_sft",
        "--planner-final",
        str(planner_final_prompt),
        "--executor-input",
        str(executor_answer),
        "--output",
        str(merged_final_prompt),
    ]
    if args.force_rebuild:
        merge_cmd.append("--force-rebuild")
    run_stage("6/8 merge planner and executor prompts", merge_cmd)

    dedup_cmd = [
        sys.executable,
        "-m",
        "agent.deduplicate",
        "--input",
        str(merged_final_prompt),
        "--output",
        str(dedup_final_prompt),
        "--duplicates-log",
        str(duplicates_log),
    ]
    run_stage("7/8 deduplicate final prompts", dedup_cmd)

    stats_cmd = [
        sys.executable,
        "-m",
        "agent.data_stats",
        str(seed_input),
        str(planner_trajectory),
        str(planner_step_sft),
        str(planner_final_prompt),
        str(executor_answer),
        str(merged_final_prompt),
        str(dedup_final_prompt),
        "--format",
        args.stats_format,
        "--output",
        str(stats_output),
    ]
    run_stage("8/8 build data statistics report", stats_cmd)

    print("\n✅ 全流程启动完成。")
    print(f"  输出目录：{output_dir}")
    print(f"  最终去重文件：{dedup_final_prompt}")
    print(f"  统计报告：{stats_output}")


if __name__ == "__main__":
    main()