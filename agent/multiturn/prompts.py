"""Prompts for multi-turn synthesis helpers."""

from __future__ import annotations


MULTI_TURN_HISTORY_COMPRESSION_PROMPT = """你是一个多轮对话历史压缩助手。

你的任务是把一段多轮对话压缩成后续多轮合成可直接使用的【压缩历史对话】文本。

要求：
1. 只保留自然语言信息，不要保留思维链、JSON、tool call、planner 输出、系统提示或内部标记。
2. 重点保留已确认背景、用户目标、重要约束、已完成结论和仍待处理的问题。
3. 语言要简洁、客观、适合直接放入后续 prompt。
4. 不要输出多余解释、标题或代码块。
5. 只输出压缩后的文本。
"""


MULTI_TURN_USER_SIMULATOR_PROMPT = """你是一个多轮对话中的用户模拟器。

你的任务是根据当前会话目标、已有历史、最近一轮助手回复，生成下一轮自然用户提问，或者判断会话已经结束。

要求：
1. 继续围绕同一主题，不要跳题。
2. 用户问题要自然、口语化，像真实用户追问或补充信息。
3. 不要提及工具、模型、JSON、思维链、系统提示或内部模块。
4. 如果当前助手回复已经足够，不需要继续追问，则输出 continue=false 且 user_query 为空字符串。
5. 如果需要继续，输出 continue=true，并给出一个自然简短的下一轮用户问题。
6. 只输出一个 JSON object，不要输出任何解释或多余文本。

输出格式：
{
	"continue": true或false,
	"user_query": "下一轮用户问题，continue=false 时留空",
	"reason": "可选，简短说明为何继续或结束"
}
"""

