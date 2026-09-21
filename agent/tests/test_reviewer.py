from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent.filter_reviewed_sft import filter_reviewed_files
from agent.reviewer_generator import parse_json_object, sample_id
from agent.schemas import validate_review_payload


def test_validate_review_payload_accepts_pass() -> None:
    result = validate_review_payload(
        {"decision": "pass", "score": 5, "issues": [], "suggestions": []}
    )
    assert result["decision"] == "pass"


def test_validate_review_payload_rejects_invalid_protocol() -> None:
    with pytest.raises(ValueError):
        validate_review_payload(
            {"decision": "通过", "score": 5, "issues": [], "suggestions": []}
        )
    with pytest.raises(ValueError):
        validate_review_payload(
            {"decision": "reject", "score": 6, "issues": [], "suggestions": []}
        )


def test_parse_json_object_only_allows_json_or_json_fence() -> None:
    assert parse_json_object('{"decision":"pass"}') == {"decision": "pass"}
    assert parse_json_object('```json\n{"decision":"pass"}\n```') == {"decision": "pass"}
    with pytest.raises(ValueError):
        parse_json_object("回答很好，建议通过")


def test_filter_reviewed_files_matches_explicit_ids(tmp_path: Path) -> None:
    merged = tmp_path / "merged.jsonl"
    reviews = tmp_path / "reviews.jsonl"
    approved = tmp_path / "approved.jsonl"
    rejected = tmp_path / "rejected.jsonl"
    merged.write_text(
        "\n".join(
            [
                json.dumps({"id": "row-a", "prompt": "A", "metadata": {"merge_source": "executor_answer"}}),
                json.dumps({"id": "row-b", "prompt": "B"}),
                json.dumps({"id": "planner-a", "prompt": "P", "metadata": {"merge_source": "planner_final"}}),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    reviews.write_text(
        "\n".join(
            [
                json.dumps({"id": "row-a_reviewer", "source_sample_id": "row-a", "decision": "pass"}),
                json.dumps({"id": "row-b_reviewer", "source_sample_id": "row-b", "decision": "reject"}),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    result = filter_reviewed_files(merged, reviews, approved, rejected)

    assert result == {"reviews": 2, "approved": 2, "rejected": 1, "unmatched": 0}
    assert [json.loads(line)["id"] for line in approved.read_text(encoding="utf-8").splitlines()] == ["row-a", "planner-a"]
    assert json.loads(rejected.read_text(encoding="utf-8"))["sample"]["id"] == "row-b"


def test_sample_id_requires_source_id() -> None:
    with pytest.raises(ValueError):
        sample_id({})