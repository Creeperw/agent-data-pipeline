"""Configuration for the intent-to-tool-call distillation environment.

模型接入信息（API 端点、教师模型名）不在这里写死：它们属于运行环境，由使用者
在控制台的运行时配置里填写（落到 ``agent/.env``）。缺值时 :func:`require_env`
直接报错并说明去哪儿填，而不是悄悄回落到某个脚本里的常量 —— 那样换个网关或
模型就得改代码，界面上也看不出真正生效的是什么。
"""

from __future__ import annotations

import os
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from dotenv import load_dotenv

IS_FROZEN = bool(getattr(sys, "frozen", False))
PACKAGE_ROOT = Path(__file__).resolve().parent
RESOURCE_PROJECT_ROOT = PACKAGE_ROOT.parent
if IS_FROZEN:
    RESOURCE_PROJECT_ROOT = Path(
        os.getenv("AGENT_RESOURCE_ROOT") or getattr(sys, "_MEIPASS", PACKAGE_ROOT.parent)
    )
    RESOURCE_PACKAGE_ROOT = RESOURCE_PROJECT_ROOT / "agent"
else:
    RESOURCE_PACKAGE_ROOT = PACKAGE_ROOT


def _default_user_data_root() -> Path:
    """Return the writable per-user data directory used by packaged builds."""

    configured = (os.getenv("AGENT_USER_DATA_DIR") or "").strip()
    if configured:
        return Path(configured).expanduser()
    if os.name == "nt":
        return Path(os.getenv("APPDATA") or Path.home()) / "AgentDataPipeline"
    return Path(os.getenv("XDG_DATA_HOME") or (Path.home() / ".local" / "share")) / "AgentDataPipeline"


USER_DATA_ROOT = _default_user_data_root()

# In source mode the repository remains both the code and data root. In a
# frozen build bundled files are read-only, so all editable assets move to the
# per-user directory while the bundled package stays under _MEIPASS.
PROJECT_ROOT = USER_DATA_ROOT if IS_FROZEN else RESOURCE_PROJECT_ROOT
AGENT_ROOT = USER_DATA_ROOT if IS_FROZEN else PACKAGE_ROOT
RESOURCE_AGENT_ROOT = RESOURCE_PACKAGE_ROOT

# Load the writable user's configuration first. ``override=False`` preserves
# values explicitly supplied by the process environment.
load_dotenv(AGENT_ROOT / ".env")
load_dotenv()
AGENT_DATA_DIR = AGENT_ROOT / "data"
AGENT_OUTPUTS_DIR = AGENT_ROOT / "outputs"
MODELS_DIR = PROJECT_ROOT / "MODELS"
SAVES_DIR = PROJECT_ROOT / "saves"


def initialize_user_data() -> None:
    """Create the writable data tree for a frozen distribution.

    Existing user files are never overwritten. This makes upgrading the
    executable safe: roles, tools, seeds, credentials and generated outputs
    survive replacement of the program directory.
    """

    if not IS_FROZEN:
        return

    AGENT_ROOT.mkdir(parents=True, exist_ok=True)
    for directory in (AGENT_DATA_DIR, AGENT_OUTPUTS_DIR, AGENT_ROOT / "domains", AGENT_ROOT / "tools"):
        directory.mkdir(parents=True, exist_ok=True)

    resource_domains = RESOURCE_AGENT_ROOT / "domains"
    target_domains = AGENT_ROOT / "domains"
    if resource_domains.is_dir():
        for source in resource_domains.rglob("*"):
            relative = source.relative_to(resource_domains)
            if any(part in {"__pycache__", ".history"} for part in relative.parts):
                continue
            if source.suffix in {".pyc", ".pyo"}:
                continue
            target = target_domains / relative
            if source.is_dir():
                target.mkdir(parents=True, exist_ok=True)
            elif source.is_file() and not target.exists():
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, target)

    resource_tools = RESOURCE_AGENT_ROOT / "tools"
    target_tools = AGENT_ROOT / "tools"
    if resource_tools.is_dir():
        for source in resource_tools.glob("*.py"):
            target = target_tools / source.name
            if not target.exists():
                shutil.copy2(source, target)

    example = RESOURCE_AGENT_ROOT / ".env.example"
    env_file = AGENT_ROOT / ".env"
    if example.is_file() and not env_file.exists():
        shutil.copy2(example, env_file)


initialize_user_data()
if IS_FROZEN:
    load_dotenv(AGENT_ROOT / ".env", override=False)

if IS_FROZEN:
    # Add the writable domain package root to the regular package search path.
    # This lets a user-created ``domains/<name>/spec.py`` override the bundled
    # template without copying the whole executable.
    if str(AGENT_ROOT) not in sys.path:
        sys.path.insert(0, str(AGENT_ROOT))
    try:
        import agent.domains as _domains_package

        user_domains = str(AGENT_ROOT / "domains")
        if user_domains not in _domains_package.__path__:
            _domains_package.__path__.insert(0, user_domains)
    except Exception:  # pragma: no cover - defensive startup fallback
        pass

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


def resolve_reviewer_model() -> str:
    """Reviewer model; defaults to the executor model when not overridden."""
    return (os.getenv("AGENT_REVIEWER_MODEL") or "").strip() or resolve_executor_model()


def multi_turn_user_model_name() -> str:
    """多轮里扮演「用户」的模型；没单独配就跟随教师模型。"""
    return (os.getenv("AGENT_MULTI_TURN_USER_MODEL") or "").strip() or resolve_distill_model()


def multi_turn_compression_model_name() -> str:
    """多轮里压缩历史的模型；没单独配就跟随多轮用户模型。"""
    return (os.getenv("AGENT_MULTI_TURN_COMPRESSION_MODEL") or "").strip() or multi_turn_user_model_name()


def path_from_env(env_name: str, default: Path) -> Path:
    """Read a path from env; relative values follow the active data root."""
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

# 数据集种类：训练数据 / 测试数据。领域包可以在 ``DomainSpec.dataset_splits`` 里
# 覆盖或扩展，这里只是不带领域包上下文时的兜底。
DEFAULT_DATASET_SPLIT = "train"


def output_prefix_for(domain_name: str | None = None, split: str | None = None) -> str:
    """产物文件名的共同前缀，形如 ``<领域名>_<数据集>``。

    前缀里带上领域名，是为了让产物离开本项目目录后仍然认得出出处；带上数据集则
    让同一项目的训练集和测试集互不覆盖。全项目只在这里拼一次，界面、控制台和
    命令行都走它，免得改一处漏一处。
    """
    domain = safe_path_part(domain_name, OUTPUT_DOMAIN_NAME)
    dataset = safe_path_part(split, DEFAULT_DATASET_SPLIT)
    return f"{domain}_{dataset}"


OUTPUT_FILE_PREFIX = safe_path_part(os.getenv("AGENT_OUTPUT_PREFIX"), output_prefix_for())
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
    Change ``AGENT_OUTPUT_PREFIX`` once to rename all stage files for a run;
    otherwise the prefix is ``<domain>_<split>`` from ``output_prefix_for``.
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
REVIEW_RESULTS_FILE = path_from_env("AGENT_REVIEW_RESULTS_FILE", output_file_for_domain("review_results"))
REVIEW_REJECTED_FILE = path_from_env("AGENT_REVIEW_REJECTED_FILE", output_file_for_domain("review_rejected"))
EXECUTOR_EXPECTED_INTENTS = tuple(
    intent.strip()
    for intent in os.getenv("AGENT_EXECUTOR_EXPECTED_INTENTS", "").split(",")
    if intent.strip()
)

# Merge and deduplicate final training prompts.
MERGE_PLANNER_FINAL_FILE = path_from_env("AGENT_MERGE_PLANNER_FINAL_FILE", PLANNER_FINAL_PROMPT_FILE)
MERGE_EXECUTOR_INPUT_FILE = path_from_env("AGENT_MERGE_EXECUTOR_INPUT_FILE", EXECUTOR_ANSWER_FILE)
MERGED_FINAL_PROMPT_FILE = path_from_env("AGENT_MERGED_FINAL_PROMPT_FILE", output_file_for_domain("merged_final_prompt"))
REVIEWED_FINAL_PROMPT_FILE = path_from_env("AGENT_REVIEWED_FINAL_PROMPT_FILE", output_file_for_domain("reviewed_final_prompt"))
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
    REVIEW_RESULTS_FILE,
    REVIEW_REJECTED_FILE,
    MERGED_FINAL_PROMPT_FILE,
    REVIEWED_FINAL_PROMPT_FILE,
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
