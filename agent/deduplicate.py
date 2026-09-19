"""Deduplicate agent data files with n-gram Jaccard / optional MinHash LSH.

The module is inspired by the standalone ``MinHash.py`` script, but adapted for
this agent environment and multiple data formats.

Supported input formats are detected automatically:
- seed rows: ``user_query`` / ``expected_intent``
- planner trajectories: ``user_query`` + final planner answer text
- planner/executor message SFT: concatenated message content
- final prompt JSONL: ``prompt``

For merged final prompt data, the recommended default is to deduplicate by
``qa`` (last user turn + final assistant answer) instead of the whole prompt, so
different tool schemas or metadata do not prevent near-duplicate synthetic
samples from being removed.

Default mode uses MinHash LSH like the standalone ``MinHash.py`` script.
``--method auto`` prefers MinHash when ``datasketch`` is installed and falls
back to exact Jaccard otherwise.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

try:
    from tqdm import tqdm
except Exception:  # pragma: no cover - tqdm is optional for this module
    tqdm = None

try:
    from .config import DEDUP_DUPLICATES_LOG_FILE, DEDUP_FINAL_PROMPT_FILE, MERGED_FINAL_PROMPT_FILE
except ImportError:  # Allows: python agent/deduplicate.py
    from config import DEDUP_DUPLICATES_LOG_FILE, DEDUP_FINAL_PROMPT_FILE, MERGED_FINAL_PROMPT_FILE


DEFAULT_INPUT = MERGED_FINAL_PROMPT_FILE
DEFAULT_OUTPUT = DEDUP_FINAL_PROMPT_FILE
DEFAULT_DUPLICATES_LOG = DEDUP_DUPLICATES_LOG_FILE

ASSISTANT_MARKER = "<|im_start|>assistant\n"
USER_MARKER = "<|im_start|>user\n"
IM_END_MARKER = "<|im_end|>"


class UnionFind:
    def __init__(self) -> None:
        self.parent: dict[str, str] = {}

    def add(self, item: str) -> None:
        if item not in self.parent:
            self.parent[item] = item

    def find(self, item: str) -> str:
        parent = self.parent[item]
        if parent == item:
            return item
        self.parent[item] = self.find(parent)
        return self.parent[item]

    def union(self, left: str, right: str) -> None:
        root_left = self.find(left)
        root_right = self.find(right)
        if root_left != root_right:
            self.parent[root_left] = root_right


@dataclass
class DedupConfig:
    input_path: Path
    output_path: Path
    duplicates_log_path: Path
    text_field: str | None = None
    dedup_key: str = "auto"
    threshold: float = 0.85
    ngram: int = 3
    method: str = "auto"
    num_perm: int = 128
    min_chars: int = 3
    keep: str = "longest"
    in_place: bool = False
    no_backup: bool = False
    dry_run: bool = False
    max_rows: int | None = None


@dataclass
class DedupStats:
    input_rows: int = 0
    invalid_lines: int = 0
    short_text_rows: int = 0
    indexed_rows: int = 0
    clusters: int = 0
    duplicate_clusters: int = 0
    duplicates_removed: int = 0
    output_rows: int = 0
    exact_candidate_pairs: int = 0
    exact_matched_pairs: int = 0


def read_jsonl(path: Path, max_rows: int | None = None) -> tuple[list[dict[str, Any]], int]:
    rows: list[dict[str, Any]] = []
    invalid_lines = 0
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            if max_rows is not None and len(rows) >= max_rows:
                break
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                invalid_lines += 1
                continue
            if isinstance(row, dict):
                rows.append(row)
            else:
                invalid_lines += 1
    return rows, invalid_lines


def write_jsonl(path: Path, rows: Iterable[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
        f.flush()


def get_planner_tool_calls(planner_output: dict[str, Any]) -> list[dict[str, Any]]:
    tool_calls = planner_output.get("tool_calls") or []
    if isinstance(tool_calls, dict):
        tool_calls = [tool_calls]
    if not tool_calls and planner_output.get("tool_call"):
        tool_calls = [planner_output["tool_call"]]
    return [tool_call for tool_call in tool_calls if isinstance(tool_call, dict)]


def detect_row_type(row: dict[str, Any]) -> str:
    if "trajectory" in row:
        return "planner_trajectory"
    if "prompt" in row:
        return "final_prompt"
    if "messages" in row:
        if "executor_raw_output" in row or str(row.get("id", "")).endswith("_executor"):
            return "executor_answer_sft"
        return "message_sft"
    if "user_query" in row:
        return "seed"
    return "unknown"


def extract_last_chat_turn(prompt: str, marker: str) -> str:
    marker_pos = prompt.rfind(marker)
    if marker_pos < 0:
        return ""
    start = marker_pos + len(marker)
    end = prompt.find(IM_END_MARKER, start)
    if end < 0:
        end = len(prompt)
    return prompt[start:end].strip()


def strip_think(text: str) -> str:
    return re.sub(r"<think>.*?</think>", "", text, flags=re.S).strip()


def extract_final_prompt_text(row: dict[str, Any], dedup_key: str = "auto") -> str:
    prompt = str(row.get("prompt") or "")
    metadata = row.get("metadata") or {}
    merge_source = str(metadata.get("merge_source") or "") if isinstance(metadata, dict) else ""
    key = dedup_key
    if key == "auto":
        key = "qa" if merge_source == "executor_answer" else "assistant"

    user_text = extract_last_chat_turn(prompt, USER_MARKER)
    assistant_text = strip_think(extract_last_chat_turn(prompt, ASSISTANT_MARKER))
    if key == "prompt":
        return prompt
    if key == "user":
        return user_text or prompt
    if key == "assistant":
        return assistant_text or prompt
    if key == "qa":
        return "\n".join(part for part in [user_text, assistant_text] if part) or prompt
    return prompt


def get_final_trajectory_text(row: dict[str, Any]) -> str:
    trajectory = row.get("trajectory") or []
    if not trajectory:
        return ""
    final_step = trajectory[-1] if isinstance(trajectory[-1], dict) else {}
    planner_output = final_step.get("planner_output") or {}
    parts = [
        str(planner_output.get("intent") or ""),
        str(planner_output.get("action") or ""),
        str(planner_output.get("ask") or ""),
        str(planner_output.get("finish_reason") or ""),
    ]
    for tool_call in get_planner_tool_calls(planner_output):
        parts.append(str(tool_call.get("name") or ""))
        parts.append(json.dumps(tool_call.get("arguments") or {}, ensure_ascii=False))
    return "\n".join(part for part in parts if part)


def extract_messages_text(row: dict[str, Any]) -> str:
    messages = row.get("messages") or []
    parts: list[str] = []
    if not isinstance(messages, list):
        return ""
    for message in messages:
        if not isinstance(message, dict):
            continue
        role = str(message.get("role") or "")
        content = str(message.get("content") or "")
        if role in {"system", "tool"}:
            continue
        parts.append(content)
    return "\n".join(parts)


def extract_text(row: dict[str, Any], text_field: str | None = None, dedup_key: str = "auto") -> str:
    if text_field:
        value = row.get(text_field)
        if isinstance(value, str):
            return value
        if value is not None:
            return json.dumps(value, ensure_ascii=False)
        return ""

    row_type = detect_row_type(row)
    if row_type == "seed":
        return str(row.get("user_query") or "")
    if row_type == "planner_trajectory":
        return "\n".join([str(row.get("user_query") or ""), get_final_trajectory_text(row)])
    if row_type in {"message_sft", "executor_answer_sft"}:
        return extract_messages_text(row)
    if row_type == "final_prompt":
        return extract_final_prompt_text(row, dedup_key=dedup_key)
    return json.dumps(row, ensure_ascii=False, sort_keys=True)


def normalize_text(text: str) -> str:
    # Preserve Chinese, English letters and digits; remove punctuation and spaces.
    text = text.lower()
    return "".join(re.findall(r"[\u4e00-\u9fffA-Za-z0-9]+", text))


def make_ngrams(text: str, ngram: int) -> set[str]:
    clean_text = normalize_text(text)
    if len(clean_text) < ngram:
        return set()
    return {clean_text[index : index + ngram] for index in range(len(clean_text) - ngram + 1)}


def jaccard(left: set[str], right: set[str]) -> float:
    if not left or not right:
        return 0.0
    intersection = len(left & right)
    if intersection == 0:
        return 0.0
    return intersection / len(left | right)


def progress(iterable, **kwargs):
    if tqdm is None:
        return iterable
    return tqdm(iterable, **kwargs)


def choose_best_member(members: list[str], doc_store: dict[str, dict[str, Any]], keep: str) -> str:
    if keep == "first":
        return min(members, key=lambda doc_id: doc_store[doc_id]["index"])
    if keep == "last":
        return max(members, key=lambda doc_id: doc_store[doc_id]["index"])
    # longest: use normalized text length first, then original index for stable tie-break.
    return max(members, key=lambda doc_id: (doc_store[doc_id]["length"], -doc_store[doc_id]["index"]))


def group_exact(rows: list[dict[str, Any]], config: DedupConfig, stats: DedupStats) -> tuple[dict[str, list[str]], dict[str, dict[str, Any]]]:
    uf = UnionFind()
    doc_store: dict[str, dict[str, Any]] = {}
    buckets: dict[str, list[str]] = defaultdict(list)

    for index, row in enumerate(progress(rows, desc="Indexing exact n-grams"), start=1):
        text = extract_text(row, config.text_field, dedup_key=config.dedup_key)
        ngrams = make_ngrams(text, config.ngram)
        if len("".join(ngrams)) < config.min_chars or not ngrams:
            stats.short_text_rows += 1
            doc_id = str(index)
            doc_store[doc_id] = {"index": index, "row": row, "text": text, "ngrams": ngrams, "length": len(text), "short": True}
            continue

        doc_id = str(index)
        uf.add(doc_id)
        doc_store[doc_id] = {"index": index, "row": row, "text": text, "ngrams": ngrams, "length": len(text), "short": False}
        stats.indexed_rows += 1

        candidate_ids: set[str] = set()
        for ngram in ngrams:
            candidate_ids.update(buckets.get(ngram, []))
        for candidate_id in candidate_ids:
            stats.exact_candidate_pairs += 1
            score = jaccard(ngrams, doc_store[candidate_id]["ngrams"])
            if score >= config.threshold:
                uf.union(doc_id, candidate_id)
                stats.exact_matched_pairs += 1
        for ngram in ngrams:
            buckets[ngram].append(doc_id)

    clusters: dict[str, list[str]] = defaultdict(list)
    for doc_id, doc in doc_store.items():
        if doc.get("short"):
            clusters[f"short_{doc_id}"].append(doc_id)
            continue
        root = uf.find(doc_id)
        clusters[root].append(doc_id)
    return clusters, doc_store


def build_minhash(ngrams: set[str], num_perm: int):
    try:
        from datasketch import MinHash
    except Exception as exc:  # pragma: no cover - optional dependency
        raise RuntimeError("使用 --method minhash 需要安装 datasketch：pip install datasketch") from exc
    minhash = MinHash(num_perm=num_perm)
    for item in ngrams:
        minhash.update(item.encode("utf-8"))
    return minhash


def has_datasketch() -> bool:
    try:
        import datasketch  # noqa: F401
    except Exception:
        return False
    return True


def resolve_method(method: str) -> str:
    if method == "auto":
        return "minhash" if has_datasketch() else "exact"
    return method


def group_minhash(rows: list[dict[str, Any]], config: DedupConfig, stats: DedupStats) -> tuple[dict[str, list[str]], dict[str, dict[str, Any]]]:
    try:
        from datasketch import MinHashLSH
    except Exception as exc:  # pragma: no cover - optional dependency
        raise RuntimeError("使用 --method minhash 需要安装 datasketch：pip install datasketch") from exc

    lsh = MinHashLSH(threshold=config.threshold, num_perm=config.num_perm)
    uf = UnionFind()
    doc_store: dict[str, dict[str, Any]] = {}

    for index, row in enumerate(progress(rows, desc="Indexing MinHash LSH"), start=1):
        text = extract_text(row, config.text_field, dedup_key=config.dedup_key)
        ngrams = make_ngrams(text, config.ngram)
        doc_id = str(index)
        if len("".join(ngrams)) < config.min_chars or not ngrams:
            stats.short_text_rows += 1
            doc_store[doc_id] = {"index": index, "row": row, "text": text, "ngrams": ngrams, "length": len(text), "short": True}
            continue

        minhash = build_minhash(ngrams, config.num_perm)
        uf.add(doc_id)
        doc_store[doc_id] = {"index": index, "row": row, "text": text, "ngrams": ngrams, "length": len(text), "short": False}
        stats.indexed_rows += 1

        similar_ids = lsh.query(minhash)
        for similar_id in similar_ids:
            uf.union(doc_id, similar_id)
        lsh.insert(doc_id, minhash)

    clusters: dict[str, list[str]] = defaultdict(list)
    for doc_id, doc in doc_store.items():
        if doc.get("short"):
            clusters[f"short_{doc_id}"].append(doc_id)
            continue
        clusters[uf.find(doc_id)].append(doc_id)
    return clusters, doc_store


def build_duplicate_log_entry(members: list[str], best_doc_id: str, doc_store: dict[str, dict[str, Any]], config: DedupConfig) -> dict[str, Any]:
    removed_members = [doc_id for doc_id in members if doc_id != best_doc_id]
    kept = doc_store[best_doc_id]
    return {
        "cluster_size": len(members),
        "threshold": config.threshold,
        "ngram": config.ngram,
        "method": resolve_method(config.method),
        "requested_method": config.method,
        "text_field": config.text_field,
        "dedup_key": config.dedup_key,
        "kept_record": {
            "index": kept["index"],
            "length": kept["length"],
            "text_preview": str(kept["text"])[:300],
            "data": kept["row"],
        },
        "removed_records": [
            {
                "index": doc_store[doc_id]["index"],
                "length": doc_store[doc_id]["length"],
                "text_preview": str(doc_store[doc_id]["text"])[:300],
                "data": doc_store[doc_id]["row"],
            }
            for doc_id in removed_members
        ],
    }


def deduplicate_rows(rows: list[dict[str, Any]], config: DedupConfig) -> tuple[list[dict[str, Any]], list[dict[str, Any]], DedupStats]:
    stats = DedupStats(input_rows=len(rows))
    actual_method = resolve_method(config.method)
    if actual_method == "minhash":
        clusters, doc_store = group_minhash(rows, config, stats)
    else:
        clusters, doc_store = group_exact(rows, config, stats)

    stats.clusters = len(clusters)
    kept_rows_with_index: list[tuple[int, dict[str, Any]]] = []
    duplicate_logs: list[dict[str, Any]] = []

    for members in progress(list(clusters.values()), desc="Selecting kept records"):
        if len(members) > 1:
            stats.duplicate_clusters += 1
            stats.duplicates_removed += len(members) - 1
        best_doc_id = choose_best_member(members, doc_store, config.keep)
        kept_rows_with_index.append((doc_store[best_doc_id]["index"], doc_store[best_doc_id]["row"]))
        if len(members) > 1:
            duplicate_logs.append(build_duplicate_log_entry(members, best_doc_id, doc_store, config))

    kept_rows = [row for _, row in sorted(kept_rows_with_index, key=lambda item: item[0])]
    stats.output_rows = len(kept_rows)
    return kept_rows, duplicate_logs, stats


def build_backup_path(path: Path) -> Path:
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return path.with_name(f"{path.name}.bak_{timestamp}")


def run_dedup(config: DedupConfig) -> DedupStats:
    if not config.input_path.exists():
        raise FileNotFoundError(f"输入文件不存在：{config.input_path}")
    rows, invalid_lines = read_jsonl(config.input_path, max_rows=config.max_rows)
    kept_rows, duplicate_logs, stats = deduplicate_rows(rows, config)
    stats.invalid_lines = invalid_lines

    if config.dry_run:
        print_report(config, stats)
        return stats

    output_path = config.input_path.with_name(f".{config.input_path.name}.tmp_dedup") if config.in_place else config.output_path
    write_jsonl(output_path, kept_rows)
    write_jsonl(config.duplicates_log_path, duplicate_logs)

    if config.in_place:
        if not config.no_backup:
            backup_path = build_backup_path(config.input_path)
            shutil.copy2(config.input_path, backup_path)
            print(f"🗂️ 已备份原文件：{backup_path}")
        output_path.replace(config.input_path)

    print_report(config, stats)
    print(f"📁 去重后数据：{config.input_path if config.in_place else config.output_path}")
    print(f"🔍 重复簇日志：{config.duplicates_log_path}")
    return stats


def print_report(config: DedupConfig, stats: DedupStats) -> None:
    duplicate_ratio = stats.duplicates_removed / stats.input_rows * 100 if stats.input_rows else 0.0
    output_path = config.input_path if config.in_place else config.output_path
    actual_method = resolve_method(config.method)
    print("\n" + "=" * 72)
    print("🎉 Agent 数据去重统计")
    print("=" * 72)
    print(f"输入文件       : {config.input_path}")
    print(f"输出文件       : {output_path}")
    print(f"方法/阈值      : {actual_method} / Jaccard >= {config.threshold}（requested={config.method}）")
    print(f"N-Gram/保留策略 : {config.ngram} / {config.keep}")
    print(f"去重键         : {config.dedup_key}（text_field={config.text_field or 'auto'}）")
    print("-" * 72)
    print(f"输入样本       : {stats.input_rows:,}")
    print(f"无效 JSON 行   : {stats.invalid_lines:,}")
    print(f"可索引样本     : {stats.indexed_rows:,}")
    print(f"短文本保留     : {stats.short_text_rows:,}")
    print(f"独立簇数量     : {stats.clusters:,}")
    print(f"重复簇数量     : {stats.duplicate_clusters:,}")
    print(f"移除重复       : {stats.duplicates_removed:,} ({duplicate_ratio:.2f}%)")
    print(f"最终保留       : {stats.output_rows:,}")
    if actual_method == "exact":
        print(f"候选比较       : {stats.exact_candidate_pairs:,}")
        print(f"命中相似       : {stats.exact_matched_pairs:,}")
    print("=" * 72)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Deduplicate agent JSONL data with n-gram Jaccard / MinHash LSH.")
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT, help="输入 JSONL。")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT, help="去重后输出 JSONL。")
    parser.add_argument("--duplicates-log", type=Path, default=DEFAULT_DUPLICATES_LOG, help="重复簇抽检日志 JSONL。")
    parser.add_argument("--text-field", type=str, default=None, help="强制指定用于去重的字段；不填则按数据格式自动抽取文本。")
    parser.add_argument(
        "--dedup-key",
        choices=["auto", "prompt", "user", "assistant", "qa"],
        default="auto",
        help="最终 prompt 格式的去重依据：auto 默认 executor 用 qa、planner 用 assistant；也可指定整段 prompt/user/assistant/qa。",
    )
    parser.add_argument("--threshold", type=float, default=0.9, help="Jaccard 相似阈值。")
    parser.add_argument("--ngram", type=int, default=3, help="字符 n-gram 大小。")
    parser.add_argument("--method", choices=["auto", "exact", "minhash"], default="auto", help="默认 auto：优先 MinHash LSH；exact 仅回退，数据大时会明显变慢。")
    parser.add_argument("--num-perm", type=int, default=128, help="MinHash permutation 数。")
    parser.add_argument("--min-chars", type=int, default=3, help="归一化文本低于该长度时不参与相似聚类，直接保留。")
    parser.add_argument("--keep", choices=["longest", "first", "last"], default="longest", help="重复簇保留策略。")
    parser.add_argument("--in-place", action="store_true", help="直接覆盖输入文件；默认先备份。")
    parser.add_argument("--no-backup", action="store_true", help="与 --in-place 同用时不备份。")
    parser.add_argument("--dry-run", action="store_true", help="只统计，不写输出。")
    parser.add_argument("--max-rows", type=int, default=None, help="最多读取多少行，用于调试。")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    config = DedupConfig(
        input_path=args.input,
        output_path=args.output,
        duplicates_log_path=args.duplicates_log,
        text_field=args.text_field,
        dedup_key=args.dedup_key,
        threshold=args.threshold,
        ngram=args.ngram,
        method=args.method,
        num_perm=args.num_perm,
        min_chars=args.min_chars,
        keep=args.keep,
        in_place=args.in_place,
        no_backup=args.no_backup,
        dry_run=args.dry_run,
        max_rows=args.max_rows,
    )
    run_dedup(config)


if __name__ == "__main__":
    main()
