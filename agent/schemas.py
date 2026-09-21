"""Shared schema definitions for agent distillation data.

The main model is trained as the planning/execution agent. This first stage only
builds data from intent recognition to one or more tool calls, and the final
planner action must be ``planning_finish``.
"""

from __future__ import annotations

from typing import Any, Literal, TypedDict


IntentName = Literal["食疗咨询", "运动指导", "体质辨识", "症状调理", "情志调节", "其他"]
ActionType = Literal["tool_call", "ask_clarification", "planning_finish"]
ReviewDecision = Literal["pass", "reject"]


class ToolCall(TypedDict):
    name: str
    arguments: dict[str, Any]


class PlanningAction(TypedDict, total=False):
    action: ActionType
    intent: IntentName
    tool_call: ToolCall
    final_plan: str
    finish_reason: str
    ask: str


class ToolObservation(TypedDict):
    name: str
    content: str


class TrajectoryStep(TypedDict, total=False):
    step: int
    planner_input: dict[str, Any]
    planner_output: PlanningAction
    observation: ToolObservation | None


class DistillSample(TypedDict, total=False):
    id: str
    user_query: str
    expected_intent: IntentName
    scenario: str
    constraints: list[str]
    trajectory: list[TrajectoryStep]


class ReviewIssue(TypedDict, total=False):
    """One objectively describable issue found by the reviewer."""

    type: str
    message: str
    evidence: str


class ReviewResult(TypedDict):
    """The machine-readable contract emitted by the reviewer role."""

    decision: ReviewDecision
    score: int
    issues: list[ReviewIssue]
    suggestions: list[str]


def validate_review_payload(payload: Any) -> ReviewResult:
    """Validate a reviewer response without interpreting natural language.

    The caller must parse JSON before calling this function.  Only protocol
    fields are inspected; issue messages and suggestions remain opaque text.
    """

    if not isinstance(payload, dict):
        raise ValueError("审核结果必须是 JSON object")

    decision = payload.get("decision")
    if decision not in {"pass", "reject"}:
        raise ValueError("审核结果 decision 必须是 pass 或 reject")

    score = payload.get("score")
    if isinstance(score, bool) or not isinstance(score, int) or not 1 <= score <= 5:
        raise ValueError("审核结果 score 必须是 1 到 5 的整数")

    issues = payload.get("issues")
    if not isinstance(issues, list):
        raise ValueError("审核结果 issues 必须是数组")
    normalized_issues: list[ReviewIssue] = []
    for index, issue in enumerate(issues):
        if not isinstance(issue, dict):
            raise ValueError(f"审核结果 issues[{index}] 必须是 object")
        issue_type = issue.get("type")
        message = issue.get("message")
        if not isinstance(issue_type, str) or not issue_type.strip():
            raise ValueError(f"审核结果 issues[{index}].type 必须是非空字符串")
        if not isinstance(message, str) or not message.strip():
            raise ValueError(f"审核结果 issues[{index}].message 必须是非空字符串")
        normalized: ReviewIssue = {"type": issue_type.strip(), "message": message.strip()}
        evidence = issue.get("evidence")
        if evidence is not None:
            if not isinstance(evidence, str):
                raise ValueError(f"审核结果 issues[{index}].evidence 必须是字符串")
            normalized["evidence"] = evidence.strip()
        normalized_issues.append(normalized)

    suggestions = payload.get("suggestions")
    if not isinstance(suggestions, list) or any(not isinstance(item, str) for item in suggestions):
        raise ValueError("审核结果 suggestions 必须是字符串数组")

    return {
        "decision": decision,
        "score": score,
        "issues": normalized_issues,
        "suggestions": [item.strip() for item in suggestions],
    }
