"""Filter merged prompt samples using reviewer decisions keyed by explicit IDs."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

try:
    from .config import MERGED_FINAL_PROMPT_FILE, REVIEWED_FINAL_PROMPT_FILE, REVIEW_REJECTED_FILE
except ImportError:  # pragma: no cover
    from config import MERGED_FINAL_PROMPT_FILE, REVIEWED_FINAL_PROMPT_FILE, REVIEW_REJECTED_FILE


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as stream:
        for line in stream:
            if line.strip():
                rows.append(json.loads(line))
    return rows


def read_reviews(path: Path) -> dict[str, dict[str, Any]]:
    reviews: dict[str, dict[str, Any]] = {}
    for row in load_jsonl(path):
        row_id = row.get("source_sample_id") or row.get("source_id")
        if row_id is not None:
            reviews[str(row_id)] = row
    return reviews


def filter_reviewed_files(
    merged_input: Path,
    reviews_input: Path,
    approved_output: Path,
    rejected_output: Path,
    force_rebuild: bool = False,
) -> dict[str, int]:
    # 这是一个确定性筛选阶段：每次根据完整 merged/review 输入重建，避免重复追加。
    # ``force_rebuild`` 保留在 CLI 中用于表达用户意图，行为上不覆盖原始 merged 文件。
    for path in (approved_output, rejected_output):
        if path.exists():
            path.write_text("", encoding="utf-8")
    approved_output.parent.mkdir(parents=True, exist_ok=True)
    rejected_output.parent.mkdir(parents=True, exist_ok=True)

    reviews = read_reviews(reviews_input)
    approved: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    unmatched = 0
    for row in load_jsonl(merged_input):
        row_id = row.get("id")
        review = reviews.get(str(row_id)) if row_id is not None else None
        if review is None:
            metadata = row.get("metadata") or {}
            if isinstance(metadata, dict) and metadata.get("merge_source") == "planner_final":
                approved.append(row)
                continue
            unmatched += 1
            continue
        if review.get("decision") == "pass":
            approved.append(row)
        else:
            rejected.append({"sample": row, "review": review})

    with approved_output.open("a", encoding="utf-8") as stream:
        for row in approved:
            stream.write(json.dumps(row, ensure_ascii=False) + "\n")
    with rejected_output.open("a", encoding="utf-8") as stream:
        for row in rejected:
            stream.write(json.dumps(row, ensure_ascii=False) + "\n")

    result = {
        "reviews": len(reviews),
        "approved": len(approved),
        "rejected": len(rejected),
        "unmatched": unmatched,
    }
    print(
        f"审核筛选完成：通过 {result['approved']} 条，拒绝 {result['rejected']} 条，"
        f"未匹配 {result['unmatched']} 条。"
    )
    return result


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Filter merged SFT prompts by reviewer decisions.")
    parser.add_argument("--merged-input", type=Path, default=MERGED_FINAL_PROMPT_FILE)
    parser.add_argument("--reviews-input", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=REVIEWED_FINAL_PROMPT_FILE)
    parser.add_argument("--rejected-output", type=Path, default=REVIEW_REJECTED_FILE)
    parser.add_argument("--force-rebuild", action="store_true")
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    filter_reviewed_files(
        merged_input=args.merged_input,
        reviews_input=args.reviews_input,
        approved_output=args.output,
        rejected_output=args.rejected_output,
        force_rebuild=args.force_rebuild,
    )