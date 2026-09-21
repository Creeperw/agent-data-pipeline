"""把多个构建任务的同一类产物整合成一份。

分任务构建时每个任务各占一个目录（``outputs/<领域>/<任务>/``），要让最终数据集
覆盖全部任务，就得把它们拼起来。但直接 ``cat`` 会把同一个 seed 在多个任务里生成
的重复样本一起带进来，所以这里按样本内容去重后再拼接，并打印一份「哪个任务贡献
了多少条」的账，方便回头看整合结果是不是符合预期。

设计要点：

- **内容去重**：同一份 prompt/messages 在不同任务里就是同一条样本，留着只会让
  训练集里同一道题出现多次。默认按内容哈希去重，也可显式按 ``id``。
- **断点续跑**：默认不清空输出，已写过的样本会被识别出来并跳过，所以反复点
  「整合」不会把文件撑成两倍；要重来就加 ``--force-rebuild``。
- **只读输入**：输入文件一律不动，整合结果永远写进输出文件。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import time
from pathlib import Path
from typing import Any, Iterable

# 内容去重时优先看的字段，按「这份数据里它最能代表一条样本」排序。
CONTENT_FIELDS = ("messages", "prompt", "conversations", "text", "query")


def _dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def signature(row: Any, dedup_by: str) -> str:
    """一条样本的去重签名。

    ``auto``：先用 messages / prompt 这类正文字段（同一道题换个 id 也算重复），
    认不出字段时退回整行；``id``：按 id，适合「id 才是身份」的数据。
    """
    if dedup_by == "id":
        if isinstance(row, dict) and row.get("id") not in (None, ""):
            return f"id:{row['id']}"
        return f"raw:{hashlib.sha1(_dump(row).encode('utf-8')).hexdigest()}"

    if isinstance(row, dict):
        for field in CONTENT_FIELDS:
            if row.get(field) not in (None, "", [], {}):
                payload = _dump(row[field])
                return f"body:{hashlib.sha1(payload.encode('utf-8')).hexdigest()}"
    return f"raw:{hashlib.sha1(_dump(row).encode('utf-8')).hexdigest()}"


def iter_rows(path: Path) -> Iterable[tuple[int, Any]]:
    """逐行产出 ``(行号, 解析结果)``；空行跳过，坏行按 ``None`` 产出并计入统计。"""
    with path.open("r", encoding="utf-8") as handle:
        for index, line in enumerate(handle, start=1):
            text = line.strip()
            if not text:
                continue
            try:
                yield index, json.loads(text)
            except json.JSONDecodeError:
                yield index, None


def existing_signatures(output: Path, dedup_by: str) -> tuple[set[str], int]:
    """输出文件里已经写过的样本签名，用于断点续跑。"""
    seen: set[str] = set()
    rows = 0
    if not output.is_file():
        return seen, rows
    for _, row in iter_rows(output):
        rows += 1
        if row is not None:
            seen.add(signature(row, dedup_by))
    return seen, rows


def write_row(handle: Any, row: Any) -> None:
    handle.write(json.dumps(row, ensure_ascii=False))
    handle.write("\n")


def merge(
    inputs: list[Path],
    output: Path,
    dedup_by: str = "auto",
    force_rebuild: bool = False,
    max_rows: int | None = None,
) -> dict[str, Any]:
    """把 ``inputs`` 依次并进 ``output``，返回一份可打印的统计。"""
    if not inputs:
        raise ValueError("至少要给一个输入文件。")

    if force_rebuild and output.exists():
        output.write_text("", encoding="utf-8")

    seen, already = existing_signatures(output, dedup_by)
    started = time.time()

    sources: list[dict[str, Any]] = []
    total_read = 0
    total_written = 0
    total_invalid = 0
    total_dup = 0

    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("a", encoding="utf-8") as handle:
        for path in inputs:
            read = 0
            written = 0
            invalid = 0
            skipped_dup = 0
            if not path.is_file():
                sources.append(
                    {
                        "path": str(path),
                        "read": 0,
                        "written": 0,
                        "invalid": 0,
                        "duplicated": 0,
                        "missing": True,
                    }
                )
                print(f"⚠️ 输入不存在，跳过：{path}")
                continue
            for _, row in iter_rows(path):
                read += 1
                if row is None:
                    invalid += 1
                    continue
                if dedup_by != "none":
                    key = signature(row, dedup_by)
                    if key in seen:
                        skipped_dup += 1
                        continue
                    seen.add(key)
                write_row(handle, row)
                written += 1
                if max_rows is not None and read >= max_rows:
                    print(f"⚠️ 达到 --max-rows={max_rows}，{path} 只读了前 {read} 行")
                    break
            handle.flush()
            sources.append(
                {
                    "path": str(path),
                    "read": read,
                    "written": written,
                    "invalid": invalid,
                    "duplicated": skipped_dup,
                    "missing": False,
                }
            )
            total_read += read
            total_written += written
            total_invalid += invalid
            total_dup += skipped_dup
            print(f"➕ {path}: 读 {read:,} 条，新增 {written:,} 条，去重 {skipped_dup:,} 条")

    total_after = already + total_written
    report = {
        "inputs": [str(path) for path in inputs],
        "output": str(output),
        "dedupBy": dedup_by,
        "forceRebuild": force_rebuild,
        "sources": sources,
        "read": total_read,
        "written": total_written,
        "duplicated": total_dup,
        "invalid": total_invalid,
        "before": already,
        "after": total_after,
        "elapsed": round(time.time() - started, 2),
    }

    print("=" * 72)
    print("🧩 任务产物整合统计")
    print("=" * 72)
    for item in sources:
        mark = "（缺失）" if item["missing"] else ""
        print(f"{Path(item['path']).name:40s} 读 {item['read']:>8,} 新增 {item['written']:>8,} 去重 {item['duplicated']:>7,} {mark}")
    print("-" * 72)
    print(f"整合前已有   : {already:,}")
    print(f"本次新增     : {total_written:,}")
    print(f"内容重复跳过 : {total_dup:,}")
    print(f"坏行跳过     : {total_invalid:,}")
    print(f"整合后总数   : {total_after:,}")
    print(f"输出文件     : {output}")
    if total_written == 0:
        print("（没有新样本需要整合：输入里的样本要么已经在输出里，要么输入是空的）")
    print("=" * 72)
    return report


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="把多个构建任务的同一类产物合并成一份（按内容去重）。"
    )
    parser.add_argument(
        "--input",
        type=Path,
        action="append",
        required=True,
        help="输入 JSONL，可重复传多次；按传入顺序拼接。",
    )
    parser.add_argument("--output", type=Path, required=True, help="整合后的输出 JSONL。")
    parser.add_argument(
        "--dedup-by",
        choices=["auto", "id", "none"],
        default="auto",
        help="auto=按样本正文去重（默认），id=按 id 去重，none=不去重直接拼。",
    )
    parser.add_argument(
        "--force-rebuild",
        action="store_true",
        help="先清空输出文件再重来，而不是把已有内容当作已整合过的样本跳过。",
    )
    parser.add_argument("--report", type=Path, default=None, help="把统计写成 JSON，供界面读取。")
    parser.add_argument("--max-rows", type=int, default=None, help="每个输入最多读多少行，调试用。")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    report = merge(
        inputs=list(args.input),
        output=args.output,
        dedup_by=args.dedup_by,
        force_rebuild=args.force_rebuild,
        max_rows=args.max_rows,
    )
    if args.report is not None:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        print(f"📄 统计已写入：{args.report}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
