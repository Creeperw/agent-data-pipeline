"""Randomly downsample rows of one intent from trajectory JSONL.

Typical use case:
- the trajectory dataset has too many ``症状调理`` rows;
- you want to remove a custom number of that intent;
- the removed rows should preserve the same tool-scenario mix as the source config.

This script works on trajectory JSONL rows that contain ``expected_intent`` and
``tool_sampling_mode``.
It is primarily designed for ``intent_tool_trajectories2.jsonl``.
"""

from __future__ import annotations

import argparse
import json
import random
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

try:
    from .config import (
        DistillConfig,
        INTENT_DOWNSAMPLE_INPUT_FILE,
        INTENT_DOWNSAMPLE_OUTPUT_FILE,
        INTENT_DOWNSAMPLE_RANDOM_SEED,
        INTENT_DOWNSAMPLE_REMOVED_FILE,
        INTENT_DOWNSAMPLE_TARGET_INTENT,
    )
except ImportError:  # Allows: python agent/downsample_intent_data.py
    from config import (  # type: ignore
        DistillConfig,
        INTENT_DOWNSAMPLE_INPUT_FILE,
        INTENT_DOWNSAMPLE_OUTPUT_FILE,
        INTENT_DOWNSAMPLE_RANDOM_SEED,
        INTENT_DOWNSAMPLE_REMOVED_FILE,
        INTENT_DOWNSAMPLE_TARGET_INTENT,
    )


VALID_INTENTS = {"食疗咨询", "运动指导", "体质辨识", "症状调理", "情志调节", "其他"}
INTENT_ALIASES = {
    "食材食疗查询": "食疗咨询",
    "运动功法": "运动指导",
    "体质辨识咨询": "体质辨识",
    "症状调理建议": "症状调理",
    "情志调节指导": "情志调节",
}


def normalize_intent(intent: Any) -> str | None:
    if intent is None:
        return None
    name = str(intent).strip()
    if not name:
        return None
    name = INTENT_ALIASES.get(name, name)
    return name if name in VALID_INTENTS else None


def clamp_ratio(value: float) -> float:
    return max(0.0, min(1.0, value))


def normalize_ratio_pair(first: float, second: float) -> tuple[float, float]:
    first = clamp_ratio(first)
    second = clamp_ratio(second)
    total = first + second
    if total > 1.0 and total > 0:
        first = first / total
        second = second / total
    return first, second


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            if line.strip():
                rows.append(json.loads(line))
    return rows


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")


def get_row_intent(row: dict[str, Any]) -> str | None:
    trajectory = row.get("trajectory")
    if isinstance(trajectory, list) and trajectory:
        for step in reversed(trajectory):
            planner_output = (step or {}).get("planner_output") or {}
            intent = normalize_intent(planner_output.get("intent"))
            if intent:
                return intent
    metadata = row.get("metadata") or {}
    if isinstance(metadata, dict):
        for key in ("intent", "answer_template_intent"):
            intent = normalize_intent(metadata.get(key))
            if intent:
                return intent
    for key in ("intent", "answer_template_intent", "expected_intent"):
        intent = normalize_intent(row.get(key))
        if intent:
            return intent
    prompt = str(row.get("prompt") or "")
    for candidate in VALID_INTENTS:
        if candidate in prompt:
            return candidate
    return None


def get_tool_sampling_mode(row: dict[str, Any]) -> str:
    top_level_mode = str(row.get("tool_sampling_mode") or "").strip()
    if top_level_mode:
        return top_level_mode
    metadata = row.get("metadata") or {}
    if isinstance(metadata, dict):
        mode = str(metadata.get("planner_tool_sampling_mode") or metadata.get("tool_sampling_mode") or "").strip()
        if mode:
            return mode
    return "(missing)"


def get_effective_tool_mode(row: dict[str, Any]) -> str:
    mode = get_tool_sampling_mode(row)
    return mode if mode in {"no_tools", "irrelevant_tools", "normal"} else "normal"


def count_by_intent(rows: list[dict[str, Any]]) -> Counter[str]:
    counter: Counter[str] = Counter()
    for row in rows:
        intent = get_row_intent(row) or "(missing)"
        counter[intent] += 1
    return counter


def compute_mode_quotas(remove_count: int, cfg: DistillConfig) -> dict[str, int]:
    no_ratio, irrelevant_ratio = normalize_ratio_pair(cfg.no_tool_ratio, cfg.irrelevant_tool_ratio)
    normal_ratio = max(0.0, 1.0 - no_ratio - irrelevant_ratio)
    ratios = {
        "no_tools": no_ratio,
        "irrelevant_tools": irrelevant_ratio,
        "normal": normal_ratio,
    }
    raw = {mode: remove_count * ratio for mode, ratio in ratios.items()}
    quotas = {mode: int(value) for mode, value in raw.items()}
    remaining = remove_count - sum(quotas.values())

    if remaining > 0:
        for _, mode in sorted(((raw[mode] - quotas[mode], mode) for mode in ratios), reverse=True):
            if remaining <= 0:
                break
            quotas[mode] += 1
            remaining -= 1

    return quotas


def build_keep_counts(
    rows: list[dict[str, Any]],
    target_intent: str,
    remove_count: int,
    seed: int,
    cfg: DistillConfig,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    target_rows = [row for row in rows if get_row_intent(row) == target_intent]

    if remove_count <= 0:
        return rows[:], []
    if remove_count >= len(target_rows):
        raise ValueError(f"要删除 {remove_count} 条 {target_intent}，但数据集中只有 {len(target_rows)} 条。")

    by_group: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in target_rows:
        by_group[get_effective_tool_mode(row)].append(row)

    planned_remove = compute_mode_quotas(remove_count, cfg)
    for mode, group_rows in by_group.items():
        if len(group_rows) < planned_remove.get(mode, 0):
            planned_remove[mode] = len(group_rows)

    allocated = sum(planned_remove.values())
    remaining = remove_count - allocated
    if remaining > 0:
        available_modes = [mode for mode in ("normal", "irrelevant_tools", "no_tools") if len(by_group.get(mode, [])) > planned_remove.get(mode, 0)]
        while remaining > 0 and available_modes:
            progressed = False
            for mode in available_modes:
                if remaining <= 0:
                    break
                if planned_remove.get(mode, 0) < len(by_group.get(mode, [])):
                    planned_remove[mode] = planned_remove.get(mode, 0) + 1
                    remaining -= 1
                    progressed = True
            available_modes = [mode for mode in available_modes if planned_remove.get(mode, 0) < len(by_group.get(mode, []))]
            if not progressed:
                break

    remove_ids: set[int] = set()
    rng = random.Random(seed)
    for mode in ("no_tools", "irrelevant_tools", "normal"):
        group_rows = by_group.get(mode, [])[:]
        rng.shuffle(group_rows)
        remove_n = min(planned_remove.get(mode, 0), len(group_rows))
        for row in group_rows[:remove_n]:
            remove_ids.add(id(row))

    merged_kept: list[dict[str, Any]] = []
    merged_removed: list[dict[str, Any]] = []
    for row in rows:
        if get_row_intent(row) != target_intent:
            merged_kept.append(row)
            continue
        if id(row) in remove_ids:
            merged_removed.append(row)
        else:
            merged_kept.append(row)
    return merged_kept, merged_removed


def parse_args(
    *,
    description: str = "Randomly downsample one intent while preserving tool-scenario mix.",
    default_input: Path = INTENT_DOWNSAMPLE_INPUT_FILE,
    default_output: Path = INTENT_DOWNSAMPLE_OUTPUT_FILE,
    default_removed: Path = INTENT_DOWNSAMPLE_REMOVED_FILE,
    default_target: str = INTENT_DOWNSAMPLE_TARGET_INTENT,
) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=description)
    parser.add_argument("--input", type=Path, default=default_input)
    parser.add_argument("--output", type=Path, default=default_output)
    parser.add_argument("--removed-output", type=Path, default=default_removed)
    parser.add_argument("--target-intent", default=default_target)
    parser.add_argument("--remove-count", type=int, required=True, help="要从目标意图中随机删除的条数。")
    parser.add_argument("--seed", type=int, default=INTENT_DOWNSAMPLE_RANDOM_SEED, help="随机种子。")
    return parser.parse_args()


def main(args: argparse.Namespace | None = None, banner: str = "✅ 轨迹意图下采样完成") -> None:
    if args is None:
        args = parse_args()
    cfg = DistillConfig()
    target_intent = normalize_intent(args.target_intent)
    if not target_intent:
        raise ValueError(f"无效 target_intent：{args.target_intent}。")

    rows = load_jsonl(args.input)
    intent_counter = count_by_intent(rows)
    if target_intent not in intent_counter:
        raise ValueError(f"数据中没有找到意图 {target_intent}。")

    merged_kept, merged_removed = build_keep_counts(rows, target_intent, args.remove_count, args.seed, cfg)

    removed_mode_counter = Counter(get_effective_tool_mode(row) for row in merged_removed)
    before_removed_mode_counter = Counter(get_effective_tool_mode(row) for row in rows if get_row_intent(row) == target_intent)
    desired_mode_counter = compute_mode_quotas(args.remove_count, cfg)

    write_jsonl(args.output, merged_kept)
    write_jsonl(args.removed_output, merged_removed)

    new_counter = count_by_intent(merged_kept)
    print(banner)
    print(f"输入文件：{args.input}")
    print(f"目标意图：{target_intent}")
    print(f"删除条数：{len(merged_removed)}")
    print(f"输出文件：{args.output}")
    print(f"移除样本文件：{args.removed_output}")
    print(f"删除前分布：{dict(intent_counter)}")
    print(f"删除后分布：{dict(new_counter)}")
    print(f"配置期望移除工具情景：{dict(desired_mode_counter)}")
    print(f"目标意图下采样前工具情景：{dict(before_removed_mode_counter)}")
    print(f"移除样本工具情景：{dict(removed_mode_counter)}")


if __name__ == "__main__":
    main()