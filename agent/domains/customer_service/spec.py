"""A minimal non-health domain pack without intent or answer templates.

This domain is intentionally simple. It demonstrates that the agent synthesis
core can work without health-management intents, answer templates, or medical
safety rules while preserving the shared planner action protocol:
``tool_call`` / ``planning_finish`` / ``ask_clarification``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from agent.core.domain import DomainSpec


@dataclass(frozen=True)
class DomainToolResult:
    name: str
    content: str


PLANNER_SYSTEM_PROMPT = """你是通用客服助手中的规划模块。

当前任务是规划：根据用户输入、已执行轨迹、工具观测和当前可用 tools，判断下一步动作。

规划阶段以“现有信息是否足够”为核心依据：
1. 信息足够直接回复时，选择 planning_finish。
2. 需要查询订单、物流、售后政策等信息，且当前 tools 中有合适工具时，选择 tool_call。
3. 用户问题缺少关键上下文且工具无法补足时，选择 ask_clarification。

本领域不要求输出 intent 字段。

【输出协议】
1. assistant content 保持为一个可被 json.loads 直接解析的 JSON object。
2. action=tool_call 时，content 输出 {"action":"tool_call"}，并通过 OpenAI 原生 tool_calls 字段发起工具调用。
3. action=ask_clarification 时，content 输出 {"action":"ask_clarification","ask":"..."}。
4. action=planning_finish 时，content 输出 {"action":"planning_finish","finish_reason":"..."}。
5. 当前 tools 为空时不调用任何工具。
6. 不要输出最终客服回复；最终回复由执行阶段生成。
"""


EXECUTOR_SYSTEM_PROMPT = """你是通用客服助手。你的任务是基于用户问题和外部参考信息，生成面向用户的最终客服回复。

【回答要求】
1. 先正面回应用户诉求，语气礼貌、清晰、负责。
2. 优先使用外部参考信息；外部信息不足时，不要编造订单状态、物流状态或政策细节。
3. 如信息不足，说明需要用户补充哪些关键信息。
4. 给出下一步可执行建议，例如查询订单、联系客服、提交售后申请等。
5. 只输出最终客服回复，不输出规划动作，不调用工具，不输出 JSON。
"""


def _lookup_tool(name: str, description: str, properties: dict, required: list[str]) -> dict:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": required,
            },
        },
    }


TOOLS = [
    _lookup_tool(
        "lookup_order_status",
        "查询订单当前状态、付款状态和发货状态。",
        {
            "order_id": {"type": "string", "description": "订单号"},
        },
        ["order_id"],
    ),
    _lookup_tool(
        "lookup_shipping_status",
        "查询物流状态、快递节点和预计送达时间。",
        {
            "tracking_number": {"type": "string", "description": "物流单号"},
        },
        ["tracking_number"],
    ),
    _lookup_tool(
        "search_refund_policy",
        "检索退换货、退款、售后政策。",
        {
            "query": {"type": "string", "description": "售后政策检索关键词"},
        },
        ["query"],
    ),
]


def run_customer_service_tool(name: str, arguments: dict[str, Any], sample: dict[str, Any] | None = None) -> DomainToolResult:
    sample = sample or {}
    arguments = arguments if isinstance(arguments, dict) else {}
    if name == "lookup_order_status":
        order_id = str(arguments.get("order_id") or sample.get("order_id") or "未知订单号")
        return DomainToolResult(
            name,
            f"订单 {order_id} 当前状态：仓库处理中，预计 24 小时内发货。如超过预计时间仍未发出，可联系人工客服催办。",
        )
    if name == "lookup_shipping_status":
        tracking_number = str(arguments.get("tracking_number") or sample.get("tracking_number") or "未知物流单号")
        return DomainToolResult(
            name,
            f"物流单号 {tracking_number} 当前节点：已揽收，正在发往下一分拨中心，预计 1-3 天内更新派送信息。",
        )
    if name == "search_refund_policy":
        query = str(arguments.get("query") or "退换货政策")
        return DomainToolResult(
            name,
            f"售后政策检索：{query}。一般情况下，未发货订单可申请取消；已发货订单需等待签收后按平台规则申请退换货。具体以订单页面展示政策为准。",
        )
    return DomainToolResult(name, f"工具 {name} 未在客服领域注册，无法返回结果。")


DOMAIN = DomainSpec(
    name="customer_service",
    display_name="通用客服助手",
    has_intent=False,
    has_answer_template=False,
    has_clarification=True,
    planner_system_prompt=PLANNER_SYSTEM_PROMPT,
    executor_system_prompt=EXECUTOR_SYSTEM_PROMPT,
    executor_assembly_system_prompt=None,
    tools=TOOLS,
    tool_runner=run_customer_service_tool,
    tool_selection={
        "scenario_required_tools": {
            "order_status": ["lookup_order_status"],
            "shipping_status": ["lookup_shipping_status"],
            "refund_policy": ["search_refund_policy"],
        },
        "default_required_tools": ["search_refund_policy"],
        "generic_search_tools": ["search_refund_policy"],
        "plan_only_tools": [],
    },
    eval_config={
        "planner": {
            "requires_intent": False,
            "action_space": ["tool_call", "planning_finish", "ask_clarification"],
        },
        "executor": {
            "enabled_dimensions": [
                "relevance",
                "grounding",
                "helpfulness",
                "safety_or_policy",
                "style",
                "similarity_to_reference",
                "length_appropriateness",
            ]
        },
    },
)