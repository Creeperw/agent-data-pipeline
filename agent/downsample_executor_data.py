"""Randomly downsample one intent from executor answer JSONL.

Thin wrapper around ``agent.downsample_intent_data`` with executor-specific
default paths. Use this for ``executor_trajectories2_sft.jsonl``-style files
that already carry ``intent`` and ``metadata.planner_tool_sampling_mode``.
"""

from __future__ import annotations

try:
    from .config import (
        EXECUTOR_DOWNSAMPLE_INPUT_FILE,
        EXECUTOR_DOWNSAMPLE_OUTPUT_FILE,
        EXECUTOR_DOWNSAMPLE_REMOVED_FILE,
        INTENT_DOWNSAMPLE_TARGET_INTENT,
    )
    from .downsample_intent_data import main, parse_args
except ImportError:  # Allows: python agent/downsample_executor_data.py
    from config import (  # type: ignore
        EXECUTOR_DOWNSAMPLE_INPUT_FILE,
        EXECUTOR_DOWNSAMPLE_OUTPUT_FILE,
        EXECUTOR_DOWNSAMPLE_REMOVED_FILE,
        INTENT_DOWNSAMPLE_TARGET_INTENT,
    )
    from downsample_intent_data import main, parse_args  # type: ignore


if __name__ == "__main__":
    args = parse_args(
        description="Randomly downsample one intent from executor answer JSONL.",
        default_input=EXECUTOR_DOWNSAMPLE_INPUT_FILE,
        default_output=EXECUTOR_DOWNSAMPLE_OUTPUT_FILE,
        default_removed=EXECUTOR_DOWNSAMPLE_REMOVED_FILE,
        default_target=INTENT_DOWNSAMPLE_TARGET_INTENT,
    )
    main(args, banner="✅ 执行阶段意图下采样完成")
