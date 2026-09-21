"""Default health-management domain pack.

This module owns the default health prompts, intent taxonomy, answer templates,
and tool catalog so all domain resources are loaded through DomainSpec.
"""

from __future__ import annotations

from importlib import import_module

from agent.core.domain import DomainSpec

_executor_prompts = import_module("agent.domains.health.executor_prompts")
_prompts = import_module("agent.domains.health.prompts")
_templates = import_module("agent.domains.health.templates")
_tools = import_module("agent.domains.health.tools")

EXECUTOR_SYSTEM_PROMPT = _executor_prompts.EXECUTOR_SYSTEM_PROMPT
SFT_PLANNER_SYSTEM_PROMPT = _prompts.SFT_PLANNER_SYSTEM_PROMPT
INTENT = _templates.INTENT
INTENT_TEMPLATES = _templates.INTENT_TEMPLATES
TOOL_CATALOG = _tools.TOOL_CATALOG
build_executor_user_prompt = _executor_prompts.build_executor_user_prompt
run_health_tool = _tools.run_health_tool
set_rag_context = _tools.set_rag_context
ExaApiError = _tools.ExaApiError

INTENT_ALIASES = {
    "食材食疗查询": "食疗咨询",
    "运动功法": "运动指导",
    "体质辨识咨询": "体质辨识",
    "症状调理建议": "症状调理",
    "情志调节指导": "情志调节",
}


DOMAIN = DomainSpec(
    name="health",
    display_name="司宁健康管理助手",
    has_intent=True,
    has_answer_template=True,
    has_clarification=True,
    planner_system_prompt=SFT_PLANNER_SYSTEM_PROMPT,
    executor_system_prompt=EXECUTOR_SYSTEM_PROMPT,
    executor_assembly_system_prompt=None,
    intent_taxonomy=dict(INTENT),
    intent_aliases=INTENT_ALIASES,
    answer_templates=dict(INTENT_TEMPLATES),
    tools=TOOL_CATALOG,
    executor_user_prompt_builder=build_executor_user_prompt,
    tool_runner=run_health_tool,
    sample_context_setter=set_rag_context,
    fatal_tool_error_types=(ExaApiError,),
    tool_selection={
        "intent_required_tools": {
            "食疗咨询": ["search_food_web"],
            "运动指导": ["search_exercise_web"],
            "体质辨识": ["search_tcm_web"],
            "症状调理": ["search_web", "search_safety_web"],
            "情志调节": ["search_emotion_web"],
        },
        "scenario_required_tools": {
            "write_file_plan": ["search_food_web", "search_exercise_web", "plan_write_file"],
            "plan_write_file": ["plan_write_file"],
            "bmi": ["calculate_bmi"],
            "water_intake": ["calculate_daily_water_intake"],
            "heart_rate": ["calculate_target_heart_rate"],
            "random": ["generate_random_number"],
            "datetime": ["get_current_datetime"],
        },
        "default_required_tools": ["search_web"],
        "generic_search_tools": ["search_rag", "search_web"],
        "plan_only_tools": ["plan_write_file"],
        "file_write_plan_tools": ["plan_write_file"],
        "file_write_tool": "write_file",
        "file_write_defaults": {
            "filename": "个性化健康管理方案.md",
            "file_type": "markdown",
            "write_purpose": "保存最终健康管理方案",
            "content_source": "executor_final_answer",
        },
        "tool_intent_hints": {
            "search_food_web": "食疗咨询",
            "search_exercise_web": "运动指导",
            "search_tcm_web": "体质辨识",
            "search_emotion_web": "情志调节",
            "search_safety_web": "症状调理",
        },
    },
    eval_config={
        "planner": {
            "requires_intent": True,
            "action_space": ["tool_call", "planning_finish", "ask_clarification"],
        },
        "executor": {
            "enabled_dimensions": [
                "relevance",
                "grounding",
                "helpfulness",
                "safety",
                "style",
                "template_adherence",
                "similarity_to_reference",
                "length_appropriateness",
                "reasoning_quality",
            ]
        },
    },
)