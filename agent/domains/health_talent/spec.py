"""Health-management personalized talent-development domain pack."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from agent.core.domain import DomainSpec


INTENT_TAXONOMY = {
    "健康管理指导": "健康管理理论、慢病管理、生活方式干预、营养运动情志管理、健康风险评估等知识辅导。",
    "学情分析": "根据成绩、错题、实训表现、学习记录等分析学习基础、优势、薄弱点和学习风险。",
    "个性化培养路径": "根据学生基础、目标岗位和能力短板设计阶段性学习路径、课程安排和提升计划。",
    "技能培训": "围绕健康评估、健康档案、膳食调查、运动处方、健康宣教、沟通访谈等技能设计训练。",
    "实训任务设计": "为教师或培训者设计案例实训、情境模拟、项目任务、工作页和评价量规。",
    "评价反馈": "根据学生作业、实训记录、反思日志或案例回答进行能力评价和改进反馈。",
    "学习资源推荐": "根据目标和薄弱点推荐教材章节、课程资源、案例、练习题和拓展阅读。",
    "教学方案设计": "围绕健康管理课程、专题课或实训课设计教学目标、教学流程、课堂活动、作业与评价。",
    "其他": "信息不足、与人才培养弱相关或无法归入上述类别的问题。",
}


PLANNER_SYSTEM_PROMPT = """你是健康管理个性化人才培养系统中的规划模块。

当前任务是规划：根据用户输入、已执行轨迹、工具观测和当前可用 tools，判断用户问题的能力场景 intent，并产出下一步动作对象。

你服务的场景包括：健康管理知识辅导、学情分析、个性化培养路径、技能培训、实训任务设计、教学方案设计、评价反馈、学习资源推荐和职业能力发展。

intent 表示用户当前请求的主要培养/学习场景，只能使用以下标准名称之一：健康管理指导、学情分析、个性化培养路径、技能培训、实训任务设计、评价反馈、学习资源推荐、教学方案设计、其他。同一问题在多轮规划中保持同一个 intent。

【动作选择】
1. 如果现有信息足够支撑下游生成最终回复，选择 planning_finish。
2. 如果需要补充课程标准、岗位能力要求、学生学习记录、案例资源、评价量规、健康管理知识等信息，且当前 tools 中有合适工具，选择 tool_call。
3. 如果用户问题缺少关键背景，且工具无法补足，例如缺少学生年级、专业方向、已学课程、目标岗位、现有成绩或训练目标，选择 ask_clarification。
4. ask_clarification 是给最终回答模块的追问建议，不是直接面向用户发送的完整回复。
5. 当前 tools 为空时，不得选择 tool_call；优先判断是否可以 planning_finish，只有信息严重不足时才 ask_clarification。

【输出协议】
1. assistant content 必须是一个可被 json.loads 直接解析的 JSON object。
2. JSON 外不要输出 Markdown、解释、标题、列表或多余文本。
3. action=tool_call 时，content 输出 {"intent":"...","action":"tool_call"}，并通过 OpenAI 原生 tool_calls 字段发起工具调用。
4. action=ask_clarification 时，content 输出 {"intent":"...","action":"ask_clarification","ask":"..."}。
5. action=planning_finish 时，content 输出 {"intent":"...","action":"planning_finish","finish_reason":"..."}。
"""


EXECUTOR_SYSTEM_PROMPT = """你是健康管理个性化人才培养助手。

你面向健康管理、护理、公共卫生、康养服务、运动健康、营养指导等相关专业的学生、教师和培训机构，提供学习诊断、能力培养、技能训练、实训设计和健康管理知识辅导。

当前任务是执行阶段：基于用户问题、已识别 intent、外部参考信息和可选回答模板，生成面向用户的最终回复。

【回答要求】
1. 先识别用户身份和目标：学生、教师、培训者、课程负责人或学习者本人。
2. 围绕 intent 作答，避免偏离到纯健康咨询或纯教学理论。
3. 优先使用外部参考信息；外部信息不足时，可以结合通用健康管理教育和职业能力培养常识，但要保持谨慎。
4. 回答应体现个性化：结合学习基础、目标岗位、能力短板、训练阶段、可用时间和评价标准。
5. 对学生类请求，要给出清晰、可执行的学习或训练步骤。
6. 对教师类请求，要给出教学组织、任务设计、评价方式和反馈要点。
7. 涉及健康管理知识时，应保持科学、规范，不给出临床诊断、处方或治疗承诺。
8. 涉及学生评价时，语气要建设性，指出问题的同时给出改进路径。
9. 只输出最终回复，不输出规划动作，不调用工具，不输出 JSON，不提及 tool_call、planning_finish 或下游模块。
"""


ANSWER_TEMPLATES = {
    "健康管理指导": "请先解释健康管理概念，再说明岗位应用、易混点、简短案例和学习建议。",
    "学情分析": "请概括学习状态，分析优势与薄弱点，判断原因，给出分阶段改进建议和可观察评价指标。",
    "个性化培养路径": "请明确培养目标，分析差距，设计基础巩固、技能训练、综合实训和评价提升四阶段路径。",
    "技能培训": "请明确核心技能、岗位应用场景、技能步骤、训练任务、练习周期、常见错误和评价标准。",
    "实训任务设计": "请给出实训主题、适用对象、情境案例、学生任务、操作步骤、成果提交物、评价量规和教师指导要点。",
    "评价反馈": "请先肯定已有表现，再按知识理解、技能操作、案例分析、沟通表达和职业素养评价，并给出改进建议。",
    "学习资源推荐": "请明确学习目标，判断待补足内容，推荐资源类型、使用方法、学习顺序和效果检查方式。",
    "教学方案设计": "请先明确教学目标、学生基础、课时长度和教学重点，再给出教学流程、导入、讲授、练习、互动、评价和课后延伸建议。",
    "其他": "请先澄清用户目标，能回答则给出稳妥建议，信息不足时提出关键补充问题。",
}


def _tool(name: str, description: str, properties: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {"type": "object", "properties": properties, "required": required},
        },
    }


TOOLS = [
    _tool("search_health_management_knowledge", "检索健康管理、慢病管理、生活方式干预、营养运动情志调节等知识。", {"query": {"type": "string", "description": "检索关键词"}}, ["query"]),
    _tool("search_curriculum_standard", "检索课程标准、人才培养方案、岗位能力要求和教学目标。", {"query": {"type": "string", "description": "课程或岗位能力检索关键词"}}, ["query"]),
    _tool("analyze_learning_profile", "根据学生成绩、错题、实训记录和学习表现分析学情。", {"student_profile": {"type": "string", "description": "学生学习画像或学习记录"}, "target_competency": {"type": "string", "description": "目标能力"}}, ["student_profile"]),
    _tool("recommend_training_tasks", "根据能力短板推荐技能训练任务、实训项目或练习活动。", {"skill_gap": {"type": "string", "description": "能力短板"}, "training_stage": {"type": "string", "description": "训练阶段"}}, ["skill_gap"]),
    _tool("generate_assessment_rubric", "生成实训任务、案例分析或技能操作的评价量规。", {"task": {"type": "string", "description": "实训或评价任务"}, "competencies": {"type": "string", "description": "评价能力点"}}, ["task"]),
    _tool("search_learning_resources", "检索教材章节、课程资源、案例材料、练习题或拓展阅读。", {"query": {"type": "string", "description": "资源检索关键词"}}, ["query"]),
]


@dataclass(frozen=True)
class DomainToolResult:
    name: str
    content: str


def run_health_talent_tool(name: str, arguments: dict[str, Any], sample: dict[str, Any] | None = None) -> DomainToolResult:
    sample = sample or {}
    arguments = arguments if isinstance(arguments, dict) else {}
    if name == "search_health_management_knowledge":
        query = arguments.get("query") or sample.get("target_competency") or sample.get("user_query") or "健康管理"
        return DomainToolResult(name, f"健康管理知识参考：{query} 涉及健康评估、生活方式干预、风险沟通和随访管理，教学中应强调科学性、可操作性和安全边界。")
    if name == "search_curriculum_standard":
        query = arguments.get("query") or sample.get("target_competency") or "健康管理岗位能力"
        return DomainToolResult(name, f"课程/岗位能力参考：{query} 通常包含知识理解、技能操作、案例分析、沟通服务、职业素养和持续改进能力。")
    if name == "analyze_learning_profile":
        profile = arguments.get("student_profile") or sample.get("student_profile") or "未提供详细学情"
        target = arguments.get("target_competency") or sample.get("target_competency") or "目标能力未明确"
        return DomainToolResult(name, f"学情分析：学生画像为“{profile}”。围绕“{target}”，建议关注基础知识、案例迁移、实操熟练度、沟通表达和反思改进能力。")
    if name == "recommend_training_tasks":
        gap = arguments.get("skill_gap") or sample.get("target_competency") or "综合能力短板"
        stage = arguments.get("training_stage") or "基础到综合应用阶段"
        return DomainToolResult(name, f"训练任务建议：针对“{gap}”，在{stage}可安排示范讲解、分步练习、案例模拟、同伴互评和复盘改进。")
    if name == "generate_assessment_rubric":
        task = arguments.get("task") or sample.get("user_query") or "实训任务"
        comps = arguments.get("competencies") or sample.get("target_competency") or "综合职业能力"
        return DomainToolResult(name, f"评价量规参考：任务“{task}”可从知识准确性、操作规范性、方案完整性、沟通表达、风险意识和反思改进评价；核心能力为“{comps}”。")
    if name == "search_learning_resources":
        query = arguments.get("query") or sample.get("target_competency") or "健康管理学习资源"
        return DomainToolResult(name, f"学习资源建议：围绕“{query}”可使用教材章节、微课、案例库、岗位标准、实训工作页和自测题，并按基础知识、技能练习、综合案例顺序学习。")
    return DomainToolResult(name, f"工具 {name} 未在 health_talent 领域注册。")


DOMAIN = DomainSpec(
    name="health_talent",
    display_name="健康管理个性化人才培养助手",
    has_intent=True,
    has_answer_template=True,
    has_clarification=True,
    planner_system_prompt=PLANNER_SYSTEM_PROMPT,
    executor_system_prompt=EXECUTOR_SYSTEM_PROMPT,
    executor_assembly_system_prompt=None,
    intent_taxonomy=INTENT_TAXONOMY,
    answer_templates=ANSWER_TEMPLATES,
    tools=TOOLS,
    tool_runner=run_health_talent_tool,
    tool_selection={
        "intent_required_tools": {
            "健康管理指导": ["search_health_management_knowledge"],
            "学情分析": ["analyze_learning_profile"],
            "个性化培养路径": ["search_curriculum_standard", "recommend_training_tasks"],
            "技能培训": ["recommend_training_tasks"],
            "实训任务设计": ["search_health_management_knowledge", "generate_assessment_rubric"],
            "评价反馈": ["generate_assessment_rubric"],
            "学习资源推荐": ["search_curriculum_standard", "search_learning_resources"],
            "教学方案设计": ["search_curriculum_standard", "search_learning_resources", "generate_assessment_rubric"],
        },
        "scenario_required_tools": {
            "learning_diagnosis": ["analyze_learning_profile"],
            "skill_training": ["recommend_training_tasks"],
            "practical_training_design": ["generate_assessment_rubric"],
            "assessment_feedback": ["generate_assessment_rubric"],
            "personalized_learning_path": ["search_curriculum_standard", "recommend_training_tasks"],
            "teaching_plan_design": ["search_curriculum_standard", "search_learning_resources", "generate_assessment_rubric"],
        },
        "default_required_tools": ["search_health_management_knowledge"],
        "generic_search_tools": ["search_health_management_knowledge", "search_curriculum_standard", "search_learning_resources"],
        "plan_only_tools": [],
    },
)