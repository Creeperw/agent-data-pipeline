"""Shared schema definitions for agent distillation data.

The main model is trained as the planning/execution agent. This first stage only
builds data from intent recognition to one or more tool calls, and the final
planner action must be ``planning_finish``.
"""

from __future__ import annotations

from typing import Any, Literal, TypedDict


IntentName = Literal["食疗咨询", "运动指导", "体质辨识", "症状调理", "情志调节", "其他"]
ActionType = Literal["tool_call", "ask_clarification", "planning_finish"]


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
