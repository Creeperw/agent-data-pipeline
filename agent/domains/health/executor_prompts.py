"""Prompt templates for the execution-stage answer agent."""

from __future__ import annotations

from .templates import INTENT_TEMPLATES


EXECUTOR_SYSTEM_PROMPT = """你是健康管理助手。你是一位经验丰富、温和耐心的健康管理顾问，专注于为用户提供科学、实用的饮食调理、运动养生、体质调理、症状管理和情志调节建议。

当前任务是执行阶段：基于用户问题、已识别意图和外部参考信息，生成面向用户的最终健康管理回复。
你会收到用户问题、已识别意图和外部参考信息。外部参考信息主要来自规划阶段的工具返回、本地知识库或网络检索结果。

【回答要求】
1. 先理解用户核心诉求，并围绕已识别意图作答。
2. 优先使用外部参考信息；外部信息不足时，可结合通用健康管理常识补充，但保持谨慎。
3. 回答要结构清晰、语言温和、具体可执行，避免机械复述参考资料。
4. 可以结合中医养生理论和现代营养学/运动科学常识，但表述要通俗，贴近生活。
5. 重点回答健康管理相关的内容，不要过多涉及临床或医疗领域的内容。超出健康管理范畴的问题，优先给出科学的生活方式建议，避免涉及诊断、用药、治疗等专业医疗内容。
6. 涉及疾病、手术、孕产、儿童、用药、严重症状或检查异常时，加入遵医嘱或及时就医提醒。
7. 给出符合实际的预期，鼓励循序渐进、长期坚持，不夸大效果，不制造焦虑。
8. 若用户模板和外部参考信息有冲突，优先遵循外部参考信息和安全原则；模板只用于约束回答风格、结构重点和表达方式。
9. 只输出面向用户的最终回答，不输出规划动作，不调用工具，不输出 JSON，不提及“工具调用”“planning_finish”“下游模块”。"""


def get_intent_template(intent: str) -> str:
    intent = (intent or "其他").strip() or "其他"
    return INTENT_TEMPLATES.get(intent) or INTENT_TEMPLATES.get("其他", "")


def build_executor_user_prompt(
    user_query: str,
    intent: str,
    external_info: str,
    answer_template: str | None = None,
    clarification_ask: str | None = None,
) -> str:
    external_info = external_info.strip() or "无外部补充信息。"
    intent = intent.strip() or "其他"
    answer_template = (answer_template if answer_template is not None else get_intent_template(intent)).strip()
    clarification_ask = (clarification_ask or "").strip()
    answer_guidance = answer_template or "无特定回答模板，请按通用健康管理回复要求作答。"
    if clarification_ask:
        answer_guidance = (
            f"{answer_guidance}\n\n"
            "【规划阶段追问建议】\n"
            f"{clarification_ask}\n"
            "请在最终回复中优先围绕该追问建议向用户补充询问信息；不要输出规划动作或 JSON。"
        )
    return f"""【用户问题】
{user_query.strip()}

【识别意图】
{intent}

【回答建议】
{answer_guidance}

【外部参考信息】
{external_info}
"""


def build_executor_messages(
    user_query: str,
    intent: str,
    external_info: str,
    answer_template: str | None = None,
    clarification_ask: str | None = None,
) -> list[dict[str, str]]:
    return [
        {"role": "system", "content": EXECUTOR_SYSTEM_PROMPT},
        {
            "role": "user",
            "content": build_executor_user_prompt(
                user_query,
                intent,
                external_info,
                answer_template,
                clarification_ask,
            ),
        },
    ]
