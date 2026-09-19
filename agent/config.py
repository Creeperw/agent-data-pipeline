"""Configuration for the intent-to-tool-call distillation environment.

模型接入信息（API 端点、教师模型名）不在这里写死：它们属于运行环境，由使用者
在控制台的运行时配置里填写（落到 ``agent/.env``）。缺值时 :func:`require_env`
直接报错并说明去哪儿填，而不是悄悄回落到某个脚本里的常量 —— 那样换个网关或
模型就得改代码，界面上也看不出真正生效的是什么。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

PROJECT_ROOT = Path(__file__).resolve().parents[1]
AGENT_ROOT = Path(__file__).resolve().parent
AGENT_DATA_DIR = AGENT_ROOT / "data"
AGENT_OUTPUTS_DIR = AGENT_ROOT / "outputs"
MODELS_DIR = PROJECT_ROOT / "MODELS"
SAVES_DIR = PROJECT_ROOT / "saves"

def require_env(key: str, label: str) -> str:
    """取一个必须由使用者显式配置的值；缺了就直接报错。

    报错信息里带上键名和界面上的入口，让问题在启动阶段就暴露，而不是跑到一半
    才因为空字符串炸在 HTTP 层。
    """
    value = (os.getenv(key) or "").strip()
    if value:
        return value
    raise RuntimeError(
        f"缺少必需的配置「{label}」（{key}）。"
        "请在控制台顶栏的齿轮 → 模型接入 里填写，或直接写进 agent/.env。"
    )


def resolve_base_url() -> str:
    """OpenAI 兼容接口的 base URL。"""
    return require_env("DEEPSEEK_BASE_URL", "API 端点")


def resolve_distill_model() -> str:
    """教师模型：阶段 1 按 seed 合成 planner 轨迹用的那个。"""
    return require_env("AGENT_DISTILL_MODEL", "教师模型 · planner 轨迹")


def resolve_executor_model() -> str:
    """executor 阶段用的模型；没单独配就跟随教师模型。"""
    return (os.getenv("AGENT_EXECUTOR_MODEL") or "").strip() or resolve_distill_model()


def multi_turn_user_model_name() -> str:
    """多轮里扮演「用户」的模型；没单独配就跟随教师模型。"""
    return (os.getenv("AGENT_MULTI_TURN_USER_MODEL") or "").strip() or resolve_distill_model()


def multi_turn_compression_model_name() -> str:
    """多轮里压缩历史的模型；没单独配就跟随多轮用户模型。"""
    return (os.getenv("AGENT_MULTI_TURN_COMPRESSION_MODEL") or "").strip() or multi_turn_user_model_name()


def path_from_env(env_name: str, default: Path) -> Path:
    """Read a path from env; relative env values are resolved from project root."""
    value = os.getenv(env_name)
    if not value:
        return default
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    return path


def safe_path_part(value: str | None, fallback: str) -> str:
    """Return a conservative folder/file-name component."""
    text = (value or "").strip() or fallback
    cleaned = "".join(ch if ch.isalnum() or ch in {"_", "-", "."} else "_" for ch in text)
    return cleaned.strip("._-") or fallback


OUTPUT_DOMAIN_NAME = safe_path_part(os.getenv("AGENT_OUTPUT_DOMAIN") or os.getenv("AGENT_DOMAIN"), "health")
OUTPUT_FILE_PREFIX = safe_path_part(os.getenv("AGENT_OUTPUT_PREFIX"), "valid")
AGENT_DOMAIN_OUTPUTS_DIR = path_from_env("AGENT_DOMAIN_OUTPUTS_DIR", AGENT_OUTPUTS_DIR / OUTPUT_DOMAIN_NAME)


def output_dir_for_domain(domain_name: str | None = None) -> Path:
    """Default output folder for one domain."""
    domain = safe_path_part(domain_name, OUTPUT_DOMAIN_NAME)
    if domain == OUTPUT_DOMAIN_NAME:
        return AGENT_DOMAIN_OUTPUTS_DIR
    return AGENT_OUTPUTS_DIR / domain


def output_file_for_domain(stage: str, domain_name: str | None = None, prefix: str | None = None) -> Path:
    """Build a default output path from one domain folder and one shared prefix.

    Change ``AGENT_DOMAIN`` / ``AGENT_OUTPUT_DOMAIN`` to switch folders.
    Change ``AGENT_OUTPUT_PREFIX`` once to rename all stage files for a run.
    """
    file_prefix = safe_path_part(prefix, OUTPUT_FILE_PREFIX)
    file_stage = safe_path_part(stage, "data")
    return output_dir_for_domain(domain_name) / f"{file_prefix}_{file_stage}.jsonl"


# ---------------------------------------------------------------------------
# Centralized file/path defaults
# ---------------------------------------------------------------------------

# RAG summary generation.
RAG_SOURCE_FILE = path_from_env("AGENT_RAG_SOURCE_FILE", AGENT_DATA_DIR / "valid.jsonl")
RAG_SUMMARY_FILE = path_from_env("AGENT_RAG_SUMMARY_FILE", AGENT_DATA_DIR / "valid_summary.jsonl")
RAG_CHECKPOINT_FILE = path_from_env("AGENT_RAG_CHECKPOINT_FILE", Path(str(RAG_SUMMARY_FILE) + ".checkpoint"))

# Planner trajectory generation.
PLANNER_INPUT_FILE = path_from_env("AGENT_PLANNER_INPUT_FILE", AGENT_DATA_DIR / "generated_tizhi_data_with_id2.jsonl")
PLANNER_TRAJECTORY_FILE = path_from_env("AGENT_PLANNER_TRAJECTORY_FILE", output_file_for_domain("planner_trajectories"))

# Planner SFT conversion and final prompt rendering.
CONVERT_TO_SFT_INPUT_FILE = path_from_env("AGENT_CONVERT_TO_SFT_INPUT_FILE", PLANNER_TRAJECTORY_FILE)
PLANNER_STEP_SFT_FILE = path_from_env("AGENT_PLANNER_STEP_SFT_FILE", output_file_for_domain("planner_step_sft"))
CONVERT_TO_FINAL_INPUT_FILE = path_from_env("AGENT_CONVERT_TO_FINAL_INPUT_FILE", PLANNER_STEP_SFT_FILE)
PLANNER_FINAL_PROMPT_FILE = path_from_env("AGENT_PLANNER_FINAL_PROMPT_FILE", output_file_for_domain("planner_final_prompt"))

# Executor answer generation.
EXECUTOR_INPUT_FILE = path_from_env("AGENT_EXECUTOR_INPUT_FILE", PLANNER_TRAJECTORY_FILE)
EXECUTOR_ANSWER_FILE = path_from_env("AGENT_EXECUTOR_ANSWER_FILE", output_file_for_domain("executor_answer_sft"))
EXECUTOR_EXPECTED_INTENTS = tuple(
    intent.strip()
    for intent in os.getenv("AGENT_EXECUTOR_EXPECTED_INTENTS", "").split(",")
    if intent.strip()
)

# Merge and deduplicate final training prompts.
MERGE_PLANNER_FINAL_FILE = path_from_env("AGENT_MERGE_PLANNER_FINAL_FILE", PLANNER_FINAL_PROMPT_FILE)
MERGE_EXECUTOR_INPUT_FILE = path_from_env("AGENT_MERGE_EXECUTOR_INPUT_FILE", EXECUTOR_ANSWER_FILE)
MERGED_FINAL_PROMPT_FILE = path_from_env("AGENT_MERGED_FINAL_PROMPT_FILE", output_file_for_domain("merged_final_prompt"))
DEDUP_FINAL_PROMPT_FILE = path_from_env("AGENT_DEDUP_FINAL_PROMPT_FILE", output_file_for_domain("dedup_final_prompt"))
DEDUP_DUPLICATES_LOG_FILE = path_from_env("AGENT_DEDUP_DUPLICATES_LOG_FILE", output_file_for_domain("duplicates"))

# Intent downsampling for trajectory data.
INTENT_DOWNSAMPLE_INPUT_FILE = path_from_env("AGENT_INTENT_DOWNSAMPLE_INPUT_FILE", PLANNER_TRAJECTORY_FILE)
INTENT_DOWNSAMPLE_OUTPUT_FILE = path_from_env("AGENT_INTENT_DOWNSAMPLE_OUTPUT_FILE", output_file_for_domain("intent_downsampled"))
INTENT_DOWNSAMPLE_REMOVED_FILE = path_from_env("AGENT_INTENT_DOWNSAMPLE_REMOVED_FILE", output_file_for_domain("intent_downsampled_removed"))
INTENT_DOWNSAMPLE_TARGET_INTENT = os.getenv("AGENT_INTENT_DOWNSAMPLE_TARGET_INTENT", "")
INTENT_DOWNSAMPLE_RANDOM_SEED = int(os.getenv("AGENT_INTENT_DOWNSAMPLE_RANDOM_SEED", "42"))

# Intent downsampling for executor answer JSONL.
EXECUTOR_DOWNSAMPLE_INPUT_FILE = path_from_env("AGENT_EXECUTOR_DOWNSAMPLE_INPUT_FILE", EXECUTOR_ANSWER_FILE)
EXECUTOR_DOWNSAMPLE_OUTPUT_FILE = path_from_env("AGENT_EXECUTOR_DOWNSAMPLE_OUTPUT_FILE", output_file_for_domain("executor_downsampled"))
EXECUTOR_DOWNSAMPLE_REMOVED_FILE = path_from_env("AGENT_EXECUTOR_DOWNSAMPLE_REMOVED_FILE", output_file_for_domain("executor_downsampled_removed"))

# File-writing tool output.
HEALTH_PLAN_DIR = path_from_env("AGENT_HEALTH_PLAN_DIR", output_dir_for_domain() / "health_plans")

# Multi-turn session outputs.
MULTI_TURN_SESSION_FILE = path_from_env("AGENT_MULTI_TURN_SESSION_FILE", output_file_for_domain("multiturn_sessions"))
MULTI_TURN_PLANNER_FILE = path_from_env("AGENT_MULTI_TURN_PLANNER_FILE", output_file_for_domain("multiturn_planner_trajectories"))
MULTI_TURN_EXECUTOR_FILE = path_from_env("AGENT_MULTI_TURN_EXECUTOR_FILE", output_file_for_domain("multiturn_executor_answers"))
MULTI_TURN_MAX_TURNS = int(os.getenv("AGENT_MULTI_TURN_MAX_TURNS", "3"))
MULTI_TURN_RECENT_TURN_LIMIT = int(os.getenv("AGENT_MULTI_TURN_RECENT_TURN_LIMIT", "3"))
MULTI_TURN_COMPRESS_EVERY_TURNS = int(os.getenv("AGENT_MULTI_TURN_COMPRESS_EVERY_TURNS", "2"))

# Model / tokenizer / training output.
QWEN_BASE_MODEL_PATH = path_from_env("AGENT_QWEN_BASE_MODEL_PATH", MODELS_DIR / "Qwen3.5-0.8B-Base")
SFT_TRAIN_PROMPT_FILE = path_from_env("AGENT_SFT_TRAIN_PROMPT_FILE", MERGED_FINAL_PROMPT_FILE)
SFT_TRAIN_OUTPUT_DIR = path_from_env("AGENT_SFT_TRAIN_OUTPUT_DIR", SAVES_DIR / "qwen_agent_planner_lora")

# Default files included in statistics reports.
DATA_STATS_DEFAULT_FILES = [
    CONVERT_TO_SFT_INPUT_FILE,
    PLANNER_STEP_SFT_FILE,
    PLANNER_FINAL_PROMPT_FILE,
    MERGE_EXECUTOR_INPUT_FILE,
    MERGED_FINAL_PROMPT_FILE,
    DEDUP_FINAL_PROMPT_FILE,
]


@dataclass(frozen=True)
class DistillConfig:
    """Runtime options for building planning/tool-call trajectories."""

    input_file: Path = PLANNER_INPUT_FILE
    output_file: Path = PLANNER_TRAJECTORY_FILE
    domain_name: str = os.getenv("AGENT_DOMAIN", "health")
    # 留空表示「还没配」，由 resolve_distill_model() / resolve_base_url() 在
    # 真正发起调用时报错。这里不放默认值：默认值藏进脚本，使用者就无从判断
    # 请求实际打到了哪儿。
    model_name: str = os.getenv("AGENT_DISTILL_MODEL", "")
    api_key: str | None = os.getenv("DEEPSEEK_API_KEY")
    base_url: str = os.getenv("DEEPSEEK_BASE_URL", "")
    max_samples: int = int(os.getenv("AGENT_DISTILL_MAX_SAMPLES", "15000"))
    max_workers: int = int(os.getenv("AGENT_DISTILL_MAX_WORKERS", "50"))
    max_tool_rounds: int = int(os.getenv("AGENT_DISTILL_MAX_TOOL_ROUNDS", "3"))
    max_tool_count: int = int(os.getenv("AGENT_DISTILL_MAX_TOOL_COUNT", "4"))
    no_tool_ratio: float = float(os.getenv("AGENT_DISTILL_NO_TOOL_RATIO", os.getenv("AGENT_DISTILL_NO_TOOL_PROBABILITY", "0.3")))
    irrelevant_tool_ratio: float = float(os.getenv("AGENT_DISTILL_IRRELEVANT_TOOL_RATIO", os.getenv("AGENT_DISTILL_IRRELEVANT_TOOL_PROBABILITY", "0.1")))
    tool_failure_ratio: float = float(os.getenv("AGENT_DISTILL_TOOL_FAILURE_RATIO", os.getenv("AGENT_DISTILL_TOOL_FAILURE_PROBABILITY", "0.1")))
    tool_failure_max_calls: int = int(os.getenv("AGENT_DISTILL_TOOL_FAILURE_MAX_CALLS", "1"))
    tool_failure_targets: str = os.getenv("AGENT_DISTILL_TOOL_FAILURE_TARGETS", "")
    recent_history_turn_limit: int = int(os.getenv("AGENT_DISTILL_RECENT_HISTORY_TURN_LIMIT", "3"))
    multi_turn_enabled: bool = os.getenv("AGENT_DISTILL_MULTI_TURN_ENABLED", "0") == "1"
    expected_intents: tuple[str, ...] = tuple(
        intent.strip()
        for intent in os.getenv("AGENT_DISTILL_EXPECTED_INTENTS", "").split(",")
        if intent.strip()
    )
    max_retries: int = int(os.getenv("AGENT_DISTILL_MAX_RETRIES", "5"))
    force_rebuild: bool = os.getenv("AGENT_DISTILL_FORCE_REBUILD", "0") == "1"
