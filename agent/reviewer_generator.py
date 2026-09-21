"""Generate strict JSON quality reviews for executor answer samples."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
from pathlib import Path
from typing import Any

from openai import AsyncOpenAI
from tqdm import tqdm

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

try:
    from .config import (
        EXECUTOR_ANSWER_FILE,
        REVIEW_RESULTS_FILE,
        resolve_base_url,
        resolve_reviewer_model,
    )
    from .core.domain import DomainSpec, load_domain
    from .core.renderer import build_reviewer_messages
    from .schemas import ReviewResult, validate_review_payload
except ImportError:  # pragma: no cover
    from config import EXECUTOR_ANSWER_FILE, REVIEW_RESULTS_FILE, resolve_base_url, resolve_reviewer_model
    from agent.core.domain import DomainSpec, load_domain
    from agent.core.renderer import build_reviewer_messages
    from agent.schemas import ReviewResult, validate_review_payload


DEFAULT_INPUT = EXECUTOR_ANSWER_FILE
DEFAULT_OUTPUT = REVIEW_RESULTS_FILE
DEFAULT_MAX_SAMPLES = int(os.getenv("AGENT_REVIEWER_MAX_SAMPLES", "10000"))
DEFAULT_MAX_WORKERS = int(os.getenv("AGENT_REVIEWER_MAX_WORKERS", "50"))
DEFAULT_MAX_RETRIES = int(os.getenv("AGENT_REVIEWER_MAX_RETRIES", "5"))
DEFAULT_MAX_ANSWER_CHARS = int(os.getenv("AGENT_REVIEWER_MAX_ANSWER_CHARS", "12000"))
DEFAULT_MIN_PASS_SCORE = int(os.getenv("AGENT_REVIEWER_MIN_PASS_SCORE", "3"))
HTTP_TIMEOUT_SECONDS = float(os.getenv("AGENT_REVIEWER_HTTP_TIMEOUT", "120"))
RETRY_BACKOFF_CAP_SECONDS = float(os.getenv("AGENT_REVIEWER_RETRY_BACKOFF_CAP", "30"))
write_lock = asyncio.Lock()


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if not path.exists():
        return rows
    with path.open("r", encoding="utf-8") as stream:
        for line in stream:
            if line.strip():
                rows.append(json.loads(line))
    return rows


def get_processed_ids(path: Path) -> set[str]:
    processed: set[str] = set()
    if not path.exists():
        return processed
    with path.open("r", encoding="utf-8") as stream:
        for line in stream:
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if row.get("id"):
                processed.add(str(row["id"]))
    return processed


def sample_id(row: dict[str, Any]) -> str:
    source_id = row.get("id") or row.get("source_id")
    if not source_id:
        raise ValueError("executor 样本缺少 id/source_id，无法安全关联审核结果")
    return f"{source_id}_reviewer"


def resolve_domain(row: dict[str, Any], fallback: DomainSpec) -> DomainSpec:
    metadata = row.get("metadata") or {}
    name = row.get("domain")
    if not name and isinstance(metadata, dict):
        name = metadata.get("domain")
    if not name or str(name) == fallback.name:
        return fallback
    return load_domain(str(name))


def extract_answer(row: dict[str, Any], max_chars: int) -> str:
    messages = row.get("messages") or []
    for message in reversed(messages):
        if isinstance(message, dict) and message.get("role") == "assistant":
            text = str(message.get("content") or "").strip()
            return text[:max_chars] if max_chars > 0 else text
    raw = row.get("executor_raw_output") or {}
    text = raw.get("text") if isinstance(raw, dict) else ""
    return str(text or "").strip()[:max_chars]


def extract_external_info(row: dict[str, Any], max_chars: int) -> str:
    value = str(row.get("external_info") or "无外部补充信息。")
    return value[:max_chars] if max_chars > 0 else value


def parse_json_object(text: str) -> dict[str, Any]:
    raw = str(text or "").strip()
    if not raw:
        raise ValueError("reviewer 返回空内容")
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        match = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", raw, flags=re.DOTALL | re.IGNORECASE)
        if not match:
            raise ValueError("reviewer 未返回可解析的 JSON object")
        payload = json.loads(match.group(1))
    if not isinstance(payload, dict):
        raise ValueError("reviewer 返回值不是 JSON object")
    return payload


def get_client(api_key: str | None = None, base_url: str | None = None) -> AsyncOpenAI:
    return AsyncOpenAI(
        api_key=api_key or os.getenv("DEEPSEEK_API_KEY"),
        base_url=base_url or resolve_base_url(),
        timeout=HTTP_TIMEOUT_SECONDS,
        max_retries=0,
    )


async def call_reviewer_with_retry(
    client: AsyncOpenAI,
    messages: list[dict[str, str]],
    model_name: str,
    max_retries: int,
) -> ReviewResult:
    last_error: Exception | None = None
    for attempt in range(max(1, max_retries)):
        try:
            completion = await client.chat.completions.create(
                model=model_name,
                messages=messages,
                temperature=0.1,
                top_p=0.9,
                response_format={"type": "json_object"},
            )
            content = completion.choices[0].message.content or ""
            return validate_review_payload(parse_json_object(content))
        except Exception as exc:  # API and protocol failures are retryable.
            last_error = exc
            if attempt < max_retries - 1:
                await asyncio.sleep(min(2**attempt, RETRY_BACKOFF_CAP_SECONDS) + 1)
    raise ValueError(f"reviewer 达到最大重试次数：{last_error}") from last_error


async def build_review_output_row(
    row: dict[str, Any],
    client: AsyncOpenAI,
    model_name: str,
    domain: DomainSpec,
    max_retries: int,
    max_answer_chars: int,
    min_pass_score: int,
) -> dict[str, Any]:
    active_domain = resolve_domain(row, domain)
    answer = extract_answer(row, max_answer_chars)
    if not answer:
        raise ValueError(f"样本 {row.get('id')!r} 缺少 executor 最终回答")
    messages = build_reviewer_messages(
        active_domain,
        user_query=str(row.get("user_query") or ""),
        answer=answer,
        intent=str(row.get("intent") or "").strip() or None,
        external_info=extract_external_info(row, max_answer_chars),
        scenario=str(row.get("scenario") or "").strip() or None,
        student_profile=str(row.get("student_profile") or "").strip() or None,
        target_competency=str(row.get("target_competency") or "").strip() or None,
    )
    result = await call_reviewer_with_retry(client, messages, model_name, max_retries)
    decision = result["decision"]
    if decision == "pass" and result["score"] < min_pass_score:
        decision = "reject"
    return {
        "id": sample_id(row),
        "source_id": row.get("id"),
        "source_sample_id": row.get("id"),
        "role": "reviewer",
        "source_role": "executor",
        "decision": decision,
        "score": result["score"],
        "issues": result["issues"],
        "suggestions": result["suggestions"],
        "reviewer_raw_output": result,
        "metadata": {
            "domain": active_domain.name,
            "user_query": row.get("user_query"),
            "min_pass_score": min_pass_score,
            "reviewed_answer_chars": len(answer),
        },
    }


async def process_one(
    row: dict[str, Any],
    client: AsyncOpenAI,
    model_name: str,
    domain: DomainSpec,
    max_retries: int,
    max_answer_chars: int,
    min_pass_score: int,
    sem: asyncio.Semaphore,
    output,
) -> bool:
    async with sem:
        result = await build_review_output_row(
            row, client, model_name, domain, max_retries, max_answer_chars, min_pass_score
        )
        async with write_lock:
            output.write(json.dumps(result, ensure_ascii=False) + "\n")
            output.flush()
        return True


async def process_dataset(
    input_file: Path,
    output_file: Path,
    model_name: str,
    domain: DomainSpec,
    max_samples: int,
    max_workers: int,
    max_retries: int,
    max_answer_chars: int,
    min_pass_score: int,
    force_rebuild: bool,
) -> None:
    if not 1 <= min_pass_score <= 5:
        raise ValueError("min_pass_score 必须在 1 到 5 之间")
    output_file.parent.mkdir(parents=True, exist_ok=True)
    if force_rebuild and output_file.exists():
        output_file.write_text("", encoding="utf-8")
    rows = load_jsonl(input_file)
    processed = get_processed_ids(output_file)
    pending = [row for row in rows if sample_id(row) not in processed][:max_samples]
    print(f"domain={domain.name}；读取 {len(rows)} 条 executor，已处理 {len(processed)} 条，本次审核 {len(pending)} 条。")
    if not pending:
        return
    client = get_client()
    sem = asyncio.Semaphore(max(1, max_workers))
    with output_file.open("a", encoding="utf-8") as output:
        tasks = [
            asyncio.create_task(
                process_one(
                    row, client, model_name, domain, max_retries, max_answer_chars,
                    min_pass_score, sem, output,
                )
            )
            for row in pending
        ]
        for task in tqdm(asyncio.as_completed(tasks), total=len(tasks), desc="生成审核结果"):
            await task


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Review executor answer JSONL with a strict JSON protocol.")
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--domain", default="health")
    parser.add_argument("--model-name", default=None)
    parser.add_argument("--max-samples", type=int, default=DEFAULT_MAX_SAMPLES)
    parser.add_argument("--max-workers", type=int, default=DEFAULT_MAX_WORKERS)
    parser.add_argument("--max-retries", type=int, default=DEFAULT_MAX_RETRIES)
    parser.add_argument("--max-answer-chars", type=int, default=DEFAULT_MAX_ANSWER_CHARS)
    parser.add_argument("--min-pass-score", type=int, default=DEFAULT_MIN_PASS_SCORE)
    parser.add_argument("--force-rebuild", action="store_true")
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    asyncio.run(
        process_dataset(
            input_file=args.input,
            output_file=args.output,
            model_name=args.model_name or resolve_reviewer_model(),
            domain=load_domain(args.domain),
            max_samples=args.max_samples,
            max_workers=args.max_workers,
            max_retries=args.max_retries,
            max_answer_chars=args.max_answer_chars,
            min_pass_score=args.min_pass_score,
            force_rebuild=args.force_rebuild,
        )
    )