"""Declarative registry of agent data-pipeline stages for the local web UI.

设计原则：

- 纯数据 + 少量命令构造函数，**不导入任何重依赖**（transformers / openai /
  datasketch），所以 UI 服务启动是秒级的。
- 每个阶段的命令与 ``pipeline_launcher.py`` 保持一致，UI 只负责拼参数、
  起子进程、收集日志，不改动现有脚本。
- 新增一个阶段只需在这里加一条 ``Stage`` 声明 + 一个 builder 函数，前端
  表单、前置条件检查、产物视图会自动跟上。
"""

from __future__ import annotations

import ast
import json
import os
import re
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Literal

try:  # 正常以 ``agent.ui.registry`` 导入
    from ..config import (
        AGENT_ROOT,
        DEFAULT_DATASET_SPLIT,
        OUTPUT_DOMAIN_NAME,
        PROJECT_ROOT,
        QWEN_BASE_MODEL_PATH,
        output_prefix_for,
        safe_path_part,
    )
    from ..core.domain import load_domain
except ImportError:  # pragma: no cover - 兼容 ``python agent/ui/server.py``
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from config import (  # type: ignore
        AGENT_ROOT,
        DEFAULT_DATASET_SPLIT,
        OUTPUT_DOMAIN_NAME,
        PROJECT_ROOT,
        QWEN_BASE_MODEL_PATH,
        output_prefix_for,
        safe_path_part,
    )
    from core.domain import load_domain  # type: ignore


ParamType = Literal["int", "float", "str", "bool", "choice", "path", "text"]


# ---------------------------------------------------------------------------
# 路径解析：产物 key -> 磁盘路径
# ---------------------------------------------------------------------------


def domain_output_dir(domain: str) -> Path:
    """``agent/outputs/<domain>``。"""
    return AGENT_ROOT / "outputs" / safe_path_part(domain, "health")


def seed_path(domain: str) -> Path:
    """``agent/domains/<domain>/seeds.jsonl``。"""
    return AGENT_ROOT / "domains" / safe_path_part(domain, "health") / "seeds.jsonl"


def stage_file(domain: str, prefix: str, stem: str, suffix: str = ".jsonl") -> Path:
    """``agent/outputs/<domain>/<prefix>_<stem><suffix>``，与 launcher 一致。

    前缀为空时回落到这个项目的默认前缀（``<领域名>_train`` 之类），而不是某个
    全局常量——否则新建一个领域时会去读另一个项目的产物。
    """
    return domain_output_dir(domain) / f"{safe_path_part(prefix, default_prefix(domain))}_{stem}{suffix}"


# 领域包声明的数据集：{领域名: (spec.py 的 mtime, {split: 名字})}。
# stage_file 每渲染一次产物列表就要解析前缀，不能每次都完整导入领域包（那会连带
# 合并全局工具、读一堆文件）。用 spec.py 的 mtime 当缓存键，用户在界面里改完
# spec.py 立刻能生效。
_SPLIT_CACHE: dict[str, tuple[float, dict[str, str]]] = {}
_FALLBACK_SPLITS: dict[str, str] = {"train": "训练数据", "valid": "测试数据"}


def _split_map(domain: str) -> dict[str, str]:
    """这个领域声明了哪几种数据集；读不到就用通用的 train/valid。"""
    spec_file = domain_dir(domain) / "spec.py"
    try:
        stamp = spec_file.stat().st_mtime
    except OSError:
        return dict(_FALLBACK_SPLITS)

    cached = _SPLIT_CACHE.get(domain)
    if cached is not None and cached[0] == stamp:
        return cached[1]

    try:
        splits = dict(load_domain(domain).dataset_splits or _FALLBACK_SPLITS)
    except Exception:  # noqa: BLE001 - spec.py 有语法错误时也要能打开界面
        splits = dict(_FALLBACK_SPLITS)
    if not splits:
        splits = dict(_FALLBACK_SPLITS)
    _SPLIT_CACHE[domain] = (stamp, splits)
    return splits


def default_prefix(domain: str, split: str | None = None) -> str:
    """这个项目的默认输出前缀，即 ``<领域名>_<split>``。

    不传 split 取领域包声明的第一个（基类默认是 ``train``）。
    """
    key = (split or "").strip()
    if not key:
        key = next(iter(_split_map(domain)), DEFAULT_DATASET_SPLIT)
    return output_prefix_for(domain, key)


def dataset_splits(domain: str) -> list[dict[str, str]]:
    """``[{"split": "train", "label": "训练数据", "prefix": "<领域>_train"}]``。

    界面据它渲染快捷按钮。前缀本身仍可手填，这里只是把常用的几种列出来。
    """
    return [
        {"split": key, "label": label, "prefix": output_prefix_for(domain, key)}
        for key, label in _split_map(domain).items()
    ]


@dataclass(frozen=True)
class Artifact:
    """一个可浏览的产物（JSONL / markdown / 目录）。"""

    key: str
    label: str
    kind: str = "jsonl"  # jsonl | markdown | json | directory
    description: str = ""


ARTIFACTS: tuple[Artifact, ...] = (
    Artifact("seeds", "seed 输入", "jsonl", "领域种子数据，也是 seed 管理页编辑的文件"),
    Artifact("rag_source", "RAG 原始语料", "jsonl", "rag_summary_data.py 的输入"),
    Artifact("rag_summary", "RAG 摘要", "jsonl", "rag_summary_data.py 的产物"),
    Artifact("planner_trajectories", "planner 轨迹", "jsonl", "阶段 1 产物，后续多数阶段的输入"),
    Artifact("planner_step_sft", "planner step-wise SFT", "jsonl", "阶段 3 产物"),
    Artifact("planner_final_prompt", "planner final prompt", "jsonl", "阶段 4 产物"),
    Artifact("executor_answer", "executor 回答 SFT", "jsonl", "阶段 5 产物"),
    Artifact("merged_final_prompt", "合并后的 final prompt", "jsonl", "阶段 6 产物"),
    Artifact("dedup_final_prompt", "去重后的 final prompt", "jsonl", "阶段 7 产物，可直接喂训练"),
    Artifact("duplicates", "重复簇日志", "jsonl", "去重阶段抽检用"),
    Artifact("data_stats_md", "统计报告 (markdown)", "markdown", "阶段 8 产物"),
    Artifact("data_stats_json", "统计报告 (json)", "json", "统计页图表用的结构化报告"),
    Artifact("multiturn_sessions", "多轮会话", "jsonl", "multiturn_cli 产物"),
    Artifact("multiturn_planner", "多轮 planner 轨迹", "jsonl", "multiturn_cli 产物"),
    Artifact("multiturn_executor", "多轮 executor 回答", "jsonl", "multiturn_cli 产物"),
    Artifact("identity_sft", "身份样本", "jsonl", "inject_identity 产物"),
    Artifact("no_intent_executor", "无意图 executor SFT", "jsonl", "build_executor_no_intent_sft 产物"),
    Artifact("intent_downsampled", "意图降采样输出", "jsonl", "downsample 产物"),
    Artifact("intent_downsampled_removed", "意图降采样剔除", "jsonl", "被删掉的样本，可复查"),
    Artifact("executor_downsampled", "executor 降采样输出", "jsonl", "downsample_executor 产物"),
    Artifact("executor_downsampled_removed", "executor 降采样剔除", "jsonl", "被删掉的样本，可复查"),
    Artifact("health_plans", "写文件工具产出", "directory", "工具调用落盘的 markdown"),
)

ARTIFACT_BY_KEY: dict[str, Artifact] = {item.key: item for item in ARTIFACTS}

# 与 domain/prefix 无关的固定路径产物。
_FIXED_PATHS: dict[str, Callable[[str], Path]] = {
    "seeds": seed_path,
    "rag_source": lambda _domain: AGENT_ROOT / "data" / "valid.jsonl",
    "rag_summary": lambda _domain: AGENT_ROOT / "data" / "valid_summary.jsonl",
    "health_plans": lambda domain: domain_output_dir(domain) / "health_plans",
}

# 形如 ``<prefix>_<stem><suffix>`` 的产物。
_PREFIXED_PATHS: dict[str, tuple[str, str]] = {
    "planner_trajectories": ("planner_trajectories", ".jsonl"),
    "planner_step_sft": ("planner_step_sft", ".jsonl"),
    "planner_final_prompt": ("planner_final_prompt", ".jsonl"),
    "executor_answer": ("executor_answer_sft", ".jsonl"),
    "merged_final_prompt": ("merged_final_prompt", ".jsonl"),
    "dedup_final_prompt": ("dedup_final_prompt", ".jsonl"),
    "duplicates": ("duplicates", ".jsonl"),
    "data_stats_md": ("data_stats", ".md"),
    "data_stats_json": ("data_stats", ".json"),
    "multiturn_sessions": ("multiturn_sessions", ".jsonl"),
    "multiturn_planner": ("multiturn_planner_trajectories", ".jsonl"),
    "multiturn_executor": ("multiturn_executor_answers", ".jsonl"),
    "identity_sft": ("identity_sft", ".jsonl"),
    "no_intent_executor": ("executor_no_intent_sft", ".jsonl"),
    "intent_downsampled": ("intent_downsampled", ".jsonl"),
    "intent_downsampled_removed": ("intent_downsampled_removed", ".jsonl"),
    "executor_downsampled": ("executor_downsampled", ".jsonl"),
    "executor_downsampled_removed": ("executor_downsampled_removed", ".jsonl"),
}


def artifact_path(key: str, domain: str, prefix: str) -> Path:
    """解析产物路径；未知 key 抛 KeyError。"""
    if key in _FIXED_PATHS:
        return _FIXED_PATHS[key](domain)
    stem, suffix = _PREFIXED_PATHS[key]
    return stage_file(domain, prefix, stem, suffix)


# ---------------------------------------------------------------------------
# 阶段与参数声明
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Param:
    """一个可在 UI 里填写的阶段参数。

    ``default=None`` 表示「留空 = 不传这个参数，由脚本按自己的配置取值」。
    那个值由 ``env`` / ``fallback`` 描述，并在界面上直接显示出来 —— 使用者关心
    的是「不填会怎样」，而不是默认值写在哪个文件里。

    ``env``：留空时脚本读取的环境变量名；``.env`` 或进程环境里设了就用那个值。
    ``fallback``：环境变量也没设时的兜底值。
    """

    key: str
    label: str
    type: ParamType = "str"
    default: Any = None
    help: str = ""
    choices: tuple[str, ...] = ()
    group: str = "常用"
    placeholder: str = ""
    env: str = ""
    fallback: Any = None

    def resolved_placeholder(self, env_values: dict[str, str]) -> str:
        """留空时实际会用的值。

        优先用显式 ``placeholder``（值本身不是数据、只是举例，或语义是「不限」
        这类非数值描述）；否则按 ``env`` → ``fallback`` 顺序解析。
        """
        if self.placeholder:
            return self.placeholder
        if self.env:
            value = env_values.get(self.env) or os.environ.get(self.env, "")
            if value:
                return value
        return "" if self.fallback is None else str(self.fallback)

    def to_json(self, env_values: dict[str, str] | None = None) -> dict[str, Any]:
        env_values = read_env_file() if env_values is None else env_values
        default = self.default
        if isinstance(default, Path):
            default = str(default)
        return {
            "key": self.key,
            "label": self.label,
            "type": self.type,
            "default": default,
            "help": self.help,
            "choices": list(self.choices),
            "group": self.group,
            "placeholder": self.resolved_placeholder(env_values),
        }


@dataclass(frozen=True)
class Stage:
    """一个可独立执行的流水线阶段。"""

    id: str
    title: str
    description: str
    module: str
    inputs: tuple[str, ...] = ()
    outputs: tuple[str, ...] = ()
    params: tuple[Param, ...] = ()
    requires: tuple[str, ...] = ()  # "api" / "model"
    mutates_input: bool = False  # 会就地修改输入文件（如 --in-place）
    strict_inputs: bool = True  # False = 输入可以部分缺失（如统计阶段有多少算多少）
    category: str = "main"  # main / side
    cost: str = "normal"  # light / normal / heavy，用于界面提示耗时

    def to_json(self, env_values: dict[str, str] | None = None) -> dict[str, Any]:
        env_values = read_env_file() if env_values is None else env_values
        return {
            "id": self.id,
            "title": self.title,
            "description": self.description,
            "module": self.module,
            "inputs": list(self.inputs),
            "outputs": list(self.outputs),
            "params": [item.to_json(env_values) for item in self.params],
            "requires": list(self.requires),
            "mutatesInput": self.mutates_input,
            "strictInputs": self.strict_inputs,
            "category": self.category,
            "cost": self.cost,
        }


# 复用的参数片段 ------------------------------------------------------------

MODEL_PATH_PARAM = Param(
    key="model_path",
    label="tokenizer 路径",
    type="path",
    help="用于 apply_chat_template 的本地 tokenizer；留空则用 config 里的 QWEN_BASE_MODEL_PATH。",
    group="高级",
    placeholder=str(QWEN_BASE_MODEL_PATH),
)

def max_samples_param(env: str, fallback: int | None, help_text: str) -> Param:
    """各阶段的「样本上限」留空时行为并不一致，所以按阶段各建一份。

    共享同一份定义会让界面显示一个对其中某些阶段并不成立的值 —— 之前统一写
    「不限」，但 planner / executor 阶段留空其实是有上限的。
    """
    return Param(
        key="max_samples",
        label="样本上限",
        type="int",
        help=help_text,
        group="常用",
        env=env,
        fallback=fallback,
        placeholder="不限" if fallback is None else "",
    )


def max_workers_param(env: str, fallback: int = 50) -> Param:
    """并发上限。

    多数阶段读的是 ``AGENT_DISTILL_MAX_WORKERS`` / ``AGENT_EXECUTOR_MAX_WORKERS``；
    少数阶段的环境变量就是 argparse 的字面默认值，此时 ``env`` 传空、只给 ``fallback``。
    """
    return Param(
        key="max_workers",
        label="并发数",
        type="int",
        help="同时发出的 API 请求数上限；调高会更快，但也更容易触发限流。",
        group="常用",
        env=env,
        fallback=fallback,
    )


FORCE_REBUILD_PARAM = Param(
    key="force_rebuild",
    label="强制重建",
    type="bool",
    default=False,
    help="清空输出文件后重跑，而不是按 id 断点续跑。",
    group="常用",
)


STAGES: tuple[Stage, ...] = (
    Stage(
        id="planner_trajectories",
        title="1. planner 轨迹合成",
        description="调用 teacher 模型按 seed 合成规划轨迹（意图识别 → 多轮工具调用 → planning_finish）。",
        module="agent.generator",
        inputs=("seeds",),
        outputs=("planner_trajectories",),
        requires=("api",),
        cost="heavy",
        params=(
            max_samples_param(
                "AGENT_DISTILL_MAX_SAMPLES",
                15000,
                "本次最多处理多少条 seed。",
            ),
            max_workers_param("AGENT_DISTILL_MAX_WORKERS"),
            Param(
                key="max_tool_rounds",
                label="最大工具轮数",
                type="int",
                help="单条样本最多几轮工具调用。",
                env="AGENT_DISTILL_MAX_TOOL_ROUNDS",
                fallback=3,
            ),
            Param(
                key="max_tool_count",
                label="最多暴露工具数",
                type="int",
                help="每条样本随机暴露的工具上限；<=0 表示不限制。",
                group="高级",
                env="AGENT_DISTILL_MAX_TOOL_COUNT",
                fallback=4,
            ),
            Param(
                key="no_tool_ratio",
                label="无工具样本比例",
                type="float",
                help="0~1，故意不给工具的场景占比。",
                group="采样策略",
                env="AGENT_DISTILL_NO_TOOL_RATIO",
                fallback=0.3,
            ),
            Param(
                key="irrelevant_tool_ratio",
                label="无关工具样本比例",
                type="float",
                help="0~1，只暴露无关工具的场景占比。",
                group="采样策略",
                env="AGENT_DISTILL_IRRELEVANT_TOOL_RATIO",
                fallback=0.1,
            ),
            Param(
                key="tool_failure_ratio",
                label="工具失败样本比例",
                type="float",
                help="0~1，注入工具报错的场景占比。",
                group="采样策略",
                env="AGENT_DISTILL_TOOL_FAILURE_RATIO",
                fallback=0.1,
            ),
            Param(
                key="tool_failure_max_calls",
                label="失败调用次数上限",
                type="int",
                help="每条失败样本最多注入几次工具失败。",
                group="采样策略",
                env="AGENT_DISTILL_TOOL_FAILURE_MAX_CALLS",
                fallback=1,
            ),
            Param(
                key="tool_failure_targets",
                label="指定失败工具",
                type="str",
                help="逗号分隔的工具名；留空表示所有工具都可能失败。",
                group="采样策略",
                env="AGENT_DISTILL_TOOL_FAILURE_TARGETS",
                fallback="不限",
            ),
            FORCE_REBUILD_PARAM,
        ),
    ),
    Stage(
        id="fix_intents",
        title="2. 修复空 tool-call intent",
        description="把 tool_call 步骤里缺失/空的 intent 补回，就地覆盖轨迹文件（会先备份）。",
        module="agent.fix_empty_tool_call_intents",
        inputs=("planner_trajectories",),
        outputs=("planner_trajectories",),
        mutates_input=True,
        cost="light",
    ),
    Stage(
        id="planner_step_sft",
        title="3. planner step-wise 转换",
        description="把完整轨迹切成逐步 SFT 样本（messages + tools + metadata）。",
        module="agent.convert_to_sft",
        inputs=("planner_trajectories",),
        outputs=("planner_step_sft",),
        cost="light",
        params=(FORCE_REBUILD_PARAM,),
    ),
    Stage(
        id="planner_final_prompt",
        title="4. planner final prompt 渲染",
        description="用本地 tokenizer 把 step-wise 样本渲染成最终训练 prompt。",
        module="agent.convert_to_final",
        inputs=("planner_step_sft",),
        outputs=("planner_final_prompt",),
        requires=("model",),
        cost="normal",
        params=(MODEL_PATH_PARAM, FORCE_REBUILD_PARAM),
    ),
    Stage(
        id="executor_answer",
        title="5. executor 回答合成",
        description="从轨迹里提取意图与工具结果，让 teacher 合成执行阶段的最终回答。",
        module="agent.executor_generator",
        inputs=("planner_trajectories",),
        outputs=("executor_answer",),
        requires=("api",),
        cost="heavy",
        params=(
            max_samples_param(
                "AGENT_EXECUTOR_MAX_SAMPLES",
                10000,
                "本次最多处理多少条轨迹。",
            ),
            max_workers_param("AGENT_EXECUTOR_MAX_WORKERS"),
            Param(
                key="max_retries",
                label="最大重试次数",
                type="int",
                help="单条样本 teacher 调用失败后的重试上限。",
                group="高级",
                env="AGENT_EXECUTOR_MAX_RETRIES",
                fallback=5,
            ),
            Param(
                key="max_external_chars",
                label="外部信息截断长度",
                type="int",
                help="工具结果拼进 prompt 时的字符上限。",
                group="高级",
                env="AGENT_EXECUTOR_MAX_EXTERNAL_CHARS",
                fallback=6000,
            ),
            Param(
                key="no_reasoning",
                label="不保留思维链",
                type="bool",
                default=False,
                help="勾选后 assistant 只保存最终回答，不含 <think>。",
            ),
            Param(
                key="exclude_tool_errors",
                label="忽略失败的工具返回",
                type="bool",
                default=False,
                help="提取外部信息时跳过报错的工具结果。",
            ),
            FORCE_REBUILD_PARAM,
        ),
    ),
    Stage(
        id="merged_final_prompt",
        title="6. planner/executor 合并",
        description="把 planner prompt 与 executor 回答合并成一份可训练的 prompt JSONL。",
        module="agent.merge_final_sft",
        inputs=("planner_final_prompt", "executor_answer"),
        outputs=("merged_final_prompt",),
        requires=("model",),
        cost="normal",
        params=(
            MODEL_PATH_PARAM,
            Param(
                key="max_planner_samples",
                label="planner 条数上限",
                type="int",
                help="最多合并多少条 planner prompt；留空表示全部。",
                placeholder="不限",
            ),
            Param(
                key="max_executor_samples",
                label="executor 条数上限",
                type="int",
                help="最多渲染多少条 executor 回答；留空表示全部。",
                placeholder="不限",
            ),
            FORCE_REBUILD_PARAM,
        ),
    ),
    Stage(
        id="dedup_final_prompt",
        title="7. final prompt 去重",
        description="MinHash LSH 近似去重，输出干净的训练集和重复簇日志。",
        module="agent.deduplicate",
        inputs=("merged_final_prompt",),
        outputs=("dedup_final_prompt", "duplicates"),
        cost="heavy",
        params=(
            Param(
                key="threshold",
                label="相似度阈值",
                type="float",
                help="Jaccard 阈值，越大越宽松。",
                placeholder="0.9",
            ),
            Param(
                key="ngram",
                label="n-gram 大小",
                type="int",
                help="字符 n-gram。",
                group="高级",
                placeholder="3",
            ),
            Param(
                key="method",
                label="算法",
                type="choice",
                choices=("auto", "exact", "minhash"),
                help="auto 优先 MinHash LSH，exact 仅在小数据上使用。",
                group="高级",
                placeholder="auto",
            ),
            Param(
                key="num_perm",
                label="permutation 数",
                type="int",
                help="MinHash 精度，越大越准也越慢。",
                group="高级",
                placeholder="128",
            ),
            Param(
                key="min_chars",
                label="最短参与长度",
                type="int",
                help="归一化后短于该长度的文本不参与相似聚类。",
                group="高级",
                placeholder="3",
            ),
            Param(
                key="keep",
                label="保留策略",
                type="choice",
                choices=("longest", "first", "last"),
                help="重复簇里保留哪一条。",
                group="高级",
                placeholder="longest",
            ),
        ),
    ),
    Stage(
        id="data_stats",
        title="8. 数据统计报告",
        description="对各个阶段产物做统一统计，输出 markdown 报告和结构化 JSON。",
        module="agent.data_stats",
        inputs=(
            "seeds",
            "planner_trajectories",
            "planner_step_sft",
            "planner_final_prompt",
            "executor_answer",
            "merged_final_prompt",
            "dedup_final_prompt",
        ),
        outputs=("data_stats_md", "data_stats_json"),
        cost="normal",
        strict_inputs=False,
        params=(
            Param(
                key="format",
                label="报告格式",
                type="choice",
                choices=("markdown", "json"),
                help="markdown 给人看；json 给统计页画图用。",
                placeholder="markdown",
            ),
            Param(
                key="detail",
                label="输出全量明细",
                type="bool",
                default=False,
                help="勾选后报告包含每条样本的细节，文件会明显变大。",
            ),
        ),
    ),
    # ------------------------------------------------------------------
    # 旁支脚本
    # ------------------------------------------------------------------
    Stage(
        id="rag_summary",
        title="RAG 摘要生成",
        description="把原始语料提炼成 RAG 参考知识。注意：该脚本路径硬编码，跟随 config 默认值，不受域名/前缀影响。",
        module="agent.rag_summary_data",
        inputs=("rag_source",),
        outputs=("rag_summary",),
        requires=("api",),
        category="side",
        cost="heavy",
    ),
    Stage(
        id="multiturn",
        title="多轮会话合成",
        description="在单轮流程之上包一层会话编排，产出会话、多轮 planner 轨迹和多轮 executor 回答。",
        module="agent.multiturn_cli",
        inputs=("seeds",),
        outputs=("multiturn_sessions", "multiturn_planner", "multiturn_executor"),
        requires=("api",),
        category="side",
        cost="heavy",
        params=(
            Param(
                key="max_samples",
                label="样本上限",
                type="int",
                help="最多合成多少个会话。",
                placeholder="100",
            ),
            Param(
                key="max_turns",
                label="最大轮数",
                type="int",
                env="AGENT_MULTI_TURN_MAX_TURNS",
                fallback=3,
            ),
            Param(
                key="min_turns",
                label="最小轮数",
                type="int",
                placeholder="2",
            ),
            Param(
                key="recent_turn_limit",
                label="近期历史轮数",
                type="int",
                help="拼进 prompt 的近期原文对话轮数。",
                group="高级",
                env="AGENT_MULTI_TURN_RECENT_TURN_LIMIT",
                fallback=3,
            ),
            Param(
                key="compress_every_turns",
                label="压缩间隔轮数",
                type="int",
                help="每隔几轮把较早对话压缩一次。",
                group="高级",
                env="AGENT_MULTI_TURN_COMPRESS_EVERY_TURNS",
                fallback=2,
            ),
            FORCE_REBUILD_PARAM,
        ),
    ),
    Stage(
        id="identity_sft",
        title="身份样本注入",
        description="合成“你叫什么/谁做的”这类身份问答样本，用于固化模型自我认知。",
        module="agent.inject_identity",
        inputs=(),
        outputs=("identity_sft",),
        requires=("api", "model"),
        category="side",
        cost="heavy",
        params=(
            Param(key="name", label="模型名字", fallback="健康管理助手"),
            Param(key="team", label="团队/公司", fallback="示例团队", group="高级"),
            Param(key="product", label="产品定位", fallback="健康管理 agent", group="高级"),
            Param(
                key="stage",
                label="生成阶段",
                type="choice",
                choices=("both", "executor", "planner"),
                placeholder="both",
            ),
            Param(key="executor_total", label="执行阶段条数", type="int", fallback=240),
            Param(key="planner_total", label="规划阶段条数", type="int", fallback=240),
            max_workers_param("AGENT_DISTILL_MAX_WORKERS"),
            Param(key="seed", label="随机种子", type="int", fallback=42, group="高级"),
            Param(
                key="dry_run",
                label="只试跑一条",
                type="bool",
                default=False,
                help="打印一条合成示例，不写文件。",
                group="高级",
            ),
            MODEL_PATH_PARAM,
        ),
    ),
    Stage(
        id="no_intent_executor",
        title="无意图 executor SFT",
        description="重新合成不带意图识别、不带回答模板的 executor 样本（从 executor 回答文件出发）。",
        module="agent.build_executor_no_intent_sft",
        inputs=("executor_answer",),
        outputs=("no_intent_executor",),
        requires=("api",),
        category="side",
        cost="heavy",
        params=(
            Param(
                key="synthesis_mode",
                label="合成方式",
                type="choice",
                choices=("api", "copy"),
                help="api = 重新调用 teacher；copy = 复制旧答案，仅调试用。",
                placeholder="api",
            ),
            max_samples_param(
                "",
                None,
                "本次最多处理多少条输入；留空表示不限。",
            ),
            max_workers_param("", 100),
            Param(key="max_attempts", label="最大尝试次数", type="int", placeholder="3", group="高级"),
            Param(
                key="require_external_info",
                label="只保留有外部信息的样本",
                type="bool",
                default=False,
                group="高级",
            ),
            Param(
                key="no_reasoning",
                label="不保留思维链",
                type="bool",
                default=False,
                group="高级",
            ),
            Param(key="shuffle", label="打乱顺序", type="bool", default=False, group="高级"),
            Param(key="seed", label="随机种子", type="int", placeholder="42", group="高级"),
            Param(
                key="print_one",
                label="打印第一条",
                type="bool",
                default=False,
                help="写入后打印第一条样本便于检查格式。",
                group="高级",
            ),
            FORCE_REBUILD_PARAM,
        ),
    ),
    Stage(
        id="downsample_intent",
        title="轨迹意图降采样",
        description="从轨迹数据里随机剔除某个意图的若干条，用来压平意图分布。",
        module="agent.downsample_intent_data",
        inputs=("planner_trajectories",),
        outputs=("intent_downsampled", "intent_downsampled_removed"),
        category="side",
        cost="light",
        params=(
            Param(
                key="target_intent",
                label="目标意图",
                type="str",
                help="要削弱的意图名，例如 食疗咨询。",
                env="AGENT_INTENT_DOWNSAMPLE_TARGET_INTENT",
                fallback="必填",
            ),
            Param(
                key="remove_count",
                label="删除条数",
                type="int",
                help="从目标意图里随机删掉多少条。",
                placeholder="必填",
            ),
            Param(
                key="seed",
                label="随机种子",
                type="int",
                group="高级",
                env="AGENT_INTENT_DOWNSAMPLE_RANDOM_SEED",
                fallback=42,
            ),
        ),
    ),
    Stage(
        id="downsample_executor",
        title="executor 意图降采样",
        description="同上，但作用于 executor 回答文件，保持工具场景配比。",
        module="agent.downsample_executor_data",
        inputs=("executor_answer",),
        outputs=("executor_downsampled", "executor_downsampled_removed"),
        category="side",
        cost="light",
        params=(
            Param(
                key="target_intent",
                label="目标意图",
                type="str",
                help="要削弱的意图名。",
                env="AGENT_INTENT_DOWNSAMPLE_TARGET_INTENT",
                fallback="必填",
            ),
            Param(key="remove_count", label="删除条数", type="int", placeholder="必填"),
            Param(
                key="seed",
                label="随机种子",
                type="int",
                group="高级",
                env="AGENT_INTENT_DOWNSAMPLE_RANDOM_SEED",
                fallback=42,
            ),
        ),
    ),
)

STAGE_BY_ID: dict[str, Stage] = {item.id: item for item in STAGES}

# 一条龙默认执行顺序（只含主链路）。
DEFAULT_PIPELINE: tuple[str, ...] = tuple(
    item.id for item in STAGES if item.category == "main"
)


# ---------------------------------------------------------------------------
# 命令构造
# ---------------------------------------------------------------------------


@dataclass
class RunContext:
    """执行一次作业所需的上下文。"""

    domain: str
    prefix: str
    python: str
    seed_input: Path
    output_dir: Path
    values: dict[str, dict[str, Any]] = field(default_factory=dict)

    def stage_values(self, stage_id: str) -> dict[str, Any]:
        return self.values.get(stage_id) or {}


def _opt(cmd: list[str], flag: str, value: Any) -> None:
    """只在 value 有值时追加 flag，与 pipeline_launcher.maybe_add 行为一致。"""
    if value is None or value == "":
        return
    cmd.extend([flag, str(value)])


def _flag(cmd: list[str], flag: str, enabled: Any) -> None:
    if enabled:
        cmd.append(flag)


def _paths(ctx: RunContext, *keys: str) -> list[Path]:
    return [artifact_path(key, ctx.domain, ctx.prefix) for key in keys]


def _build_planner_trajectories(ctx: RunContext, v: dict[str, Any]) -> list[str]:
    (trajectory,) = _paths(ctx, "planner_trajectories")
    cmd = [
        ctx.python, "-m", "agent.generator",
        "--domain", ctx.domain,
        "--input", str(ctx.seed_input),
        "--output", str(trajectory),
    ]
    _opt(cmd, "--max-samples", v.get("max_samples"))
    _opt(cmd, "--max-workers", v.get("max_workers"))
    _opt(cmd, "--max-tool-rounds", v.get("max_tool_rounds"))
    _opt(cmd, "--max-tool-count", v.get("max_tool_count"))
    _opt(cmd, "--no-tool-ratio", v.get("no_tool_ratio"))
    _opt(cmd, "--irrelevant-tool-ratio", v.get("irrelevant_tool_ratio"))
    _opt(cmd, "--tool-failure-ratio", v.get("tool_failure_ratio"))
    _opt(cmd, "--tool-failure-max-calls", v.get("tool_failure_max_calls"))
    _opt(cmd, "--tool-failure-targets", v.get("tool_failure_targets"))
    _flag(cmd, "--force-rebuild", v.get("force_rebuild"))
    return cmd


def _build_fix_intents(ctx: RunContext, v: dict[str, Any]) -> list[str]:
    (trajectory,) = _paths(ctx, "planner_trajectories")
    return [
        ctx.python, "-m", "agent.fix_empty_tool_call_intents",
        "--input", str(trajectory),
        "--in-place",
    ]


def _build_planner_step_sft(ctx: RunContext, v: dict[str, Any]) -> list[str]:
    trajectory, step_sft = _paths(ctx, "planner_trajectories", "planner_step_sft")
    cmd = [
        ctx.python, "-m", "agent.convert_to_sft",
        "--input", str(trajectory),
        "--output", str(step_sft),
    ]
    _flag(cmd, "--force-rebuild", v.get("force_rebuild"))
    return cmd


def _build_planner_final_prompt(ctx: RunContext, v: dict[str, Any]) -> list[str]:
    step_sft, final_prompt = _paths(ctx, "planner_step_sft", "planner_final_prompt")
    cmd = [
        ctx.python, "-m", "agent.convert_to_final",
        "--input", str(step_sft),
        "--output", str(final_prompt),
    ]
    _opt(cmd, "--model-path", v.get("model_path"))
    _flag(cmd, "--force-rebuild", v.get("force_rebuild"))
    return cmd


def _build_executor_answer(ctx: RunContext, v: dict[str, Any]) -> list[str]:
    trajectory, executor = _paths(ctx, "planner_trajectories", "executor_answer")
    cmd = [
        ctx.python, "-m", "agent.executor_generator",
        "--domain", ctx.domain,
        "--input", str(trajectory),
        "--output", str(executor),
    ]
    _opt(cmd, "--max-samples", v.get("max_samples"))
    _opt(cmd, "--max-workers", v.get("max_workers"))
    _opt(cmd, "--max-retries", v.get("max_retries"))
    _opt(cmd, "--max-external-chars", v.get("max_external_chars"))
    _flag(cmd, "--no-reasoning", v.get("no_reasoning"))
    _flag(cmd, "--exclude-tool-errors", v.get("exclude_tool_errors"))
    _flag(cmd, "--force-rebuild", v.get("force_rebuild"))
    return cmd


def _build_merged_final_prompt(ctx: RunContext, v: dict[str, Any]) -> list[str]:
    final_prompt, executor, merged = _paths(
        ctx, "planner_final_prompt", "executor_answer", "merged_final_prompt"
    )
    cmd = [
        ctx.python, "-m", "agent.merge_final_sft",
        "--planner-final", str(final_prompt),
        "--executor-input", str(executor),
        "--output", str(merged),
    ]
    _opt(cmd, "--model-path", v.get("model_path"))
    _opt(cmd, "--max-planner-samples", v.get("max_planner_samples"))
    _opt(cmd, "--max-executor-samples", v.get("max_executor_samples"))
    _flag(cmd, "--force-rebuild", v.get("force_rebuild"))
    return cmd


def _build_dedup_final_prompt(ctx: RunContext, v: dict[str, Any]) -> list[str]:
    merged, dedup, duplicates = _paths(
        ctx, "merged_final_prompt", "dedup_final_prompt", "duplicates"
    )
    cmd = [
        ctx.python, "-m", "agent.deduplicate",
        "--input", str(merged),
        "--output", str(dedup),
        "--duplicates-log", str(duplicates),
    ]
    _opt(cmd, "--threshold", v.get("threshold"))
    _opt(cmd, "--ngram", v.get("ngram"))
    _opt(cmd, "--method", v.get("method"))
    _opt(cmd, "--num-perm", v.get("num_perm"))
    _opt(cmd, "--min-chars", v.get("min_chars"))
    _opt(cmd, "--keep", v.get("keep"))
    return cmd


def _build_data_stats(ctx: RunContext, v: dict[str, Any]) -> list[str]:
    inputs = _paths(
        ctx,
        "seeds",
        "planner_trajectories",
        "planner_step_sft",
        "planner_final_prompt",
        "executor_answer",
        "merged_final_prompt",
        "dedup_final_prompt",
    )
    existing = [str(path) for path in inputs if path.exists()]
    if not existing:
        raise ValueError("没有任何已存在的输入文件，先跑前面的阶段再来统计。")

    (stats_md,) = _paths(ctx, "data_stats_md")
    stats_json = artifact_path("data_stats_json", ctx.domain, ctx.prefix)
    fmt = v.get("format") or "markdown"
    target = stats_json if fmt == "json" else stats_md
    cmd = [ctx.python, "-m", "agent.data_stats", *existing, "--format", fmt, "--output", str(target)]
    _flag(cmd, "--detail", v.get("detail"))
    return cmd


def _build_rag_summary(ctx: RunContext, v: dict[str, Any]) -> list[str]:
    return [ctx.python, "-m", "agent.rag_summary_data"]


def _build_multiturn(ctx: RunContext, v: dict[str, Any]) -> list[str]:
    session, planner, executor = _paths(
        ctx, "multiturn_sessions", "multiturn_planner", "multiturn_executor"
    )
    cmd = [
        ctx.python, "-m", "agent.multiturn_cli",
        "--domain", ctx.domain,
        "--input", str(ctx.seed_input),
        "--output-session", str(session),
        "--output-planner", str(planner),
        "--output-executor", str(executor),
    ]
    _opt(cmd, "--max-samples", v.get("max_samples"))
    _opt(cmd, "--max-turns", v.get("max_turns"))
    _opt(cmd, "--min-turns", v.get("min_turns"))
    _opt(cmd, "--recent-turn-limit", v.get("recent_turn_limit"))
    _opt(cmd, "--compress-every-turns", v.get("compress_every_turns"))
    _flag(cmd, "--force-rebuild", v.get("force_rebuild"))
    return cmd


def _build_identity_sft(ctx: RunContext, v: dict[str, Any]) -> list[str]:
    (identity,) = _paths(ctx, "identity_sft")
    cmd = [ctx.python, "-m", "agent.inject_identity", "--output", str(identity)]
    _opt(cmd, "--name", v.get("name"))
    _opt(cmd, "--team", v.get("team"))
    _opt(cmd, "--product", v.get("product"))
    _opt(cmd, "--stage", v.get("stage"))
    _opt(cmd, "--executor-total", v.get("executor_total"))
    _opt(cmd, "--planner-total", v.get("planner_total"))
    _opt(cmd, "--max-workers", v.get("max_workers"))
    _opt(cmd, "--seed", v.get("seed"))
    _opt(cmd, "--model-path", v.get("model_path"))
    _flag(cmd, "--dry-run", v.get("dry_run"))
    return cmd


def _build_no_intent_executor(ctx: RunContext, v: dict[str, Any]) -> list[str]:
    executor, target = _paths(ctx, "executor_answer", "no_intent_executor")
    cmd = [
        ctx.python, "-m", "agent.build_executor_no_intent_sft",
        "--input", str(executor),
        "--output", str(target),
    ]
    _opt(cmd, "--synthesis-mode", v.get("synthesis_mode"))
    _opt(cmd, "--max-samples", v.get("max_samples"))
    _opt(cmd, "--max-workers", v.get("max_workers"))
    _opt(cmd, "--max-attempts", v.get("max_attempts"))
    _opt(cmd, "--seed", v.get("seed"))
    _flag(cmd, "--require-external-info", v.get("require_external_info"))
    _flag(cmd, "--no-reasoning", v.get("no_reasoning"))
    _flag(cmd, "--shuffle", v.get("shuffle"))
    _flag(cmd, "--print-one", v.get("print_one"))
    _flag(cmd, "--force-rebuild", v.get("force_rebuild"))
    return cmd


def _build_downsample_intent(ctx: RunContext, v: dict[str, Any]) -> list[str]:
    source, output, removed = _paths(
        ctx, "planner_trajectories", "intent_downsampled", "intent_downsampled_removed"
    )
    cmd = [
        ctx.python, "-m", "agent.downsample_intent_data",
        "--input", str(source),
        "--output", str(output),
        "--removed-output", str(removed),
    ]
    _opt(cmd, "--target-intent", v.get("target_intent"))
    _opt(cmd, "--remove-count", v.get("remove_count"))
    _opt(cmd, "--seed", v.get("seed"))
    return cmd


def _build_downsample_executor(ctx: RunContext, v: dict[str, Any]) -> list[str]:
    source, output, removed = _paths(
        ctx, "executor_answer", "executor_downsampled", "executor_downsampled_removed"
    )
    cmd = [
        ctx.python, "-m", "agent.downsample_executor_data",
        "--input", str(source),
        "--output", str(output),
        "--removed-output", str(removed),
    ]
    _opt(cmd, "--target-intent", v.get("target_intent"))
    _opt(cmd, "--remove-count", v.get("remove_count"))
    _opt(cmd, "--seed", v.get("seed"))
    return cmd


_BUILDERS: dict[str, Callable[[RunContext, dict[str, Any]], list[str]]] = {
    "planner_trajectories": _build_planner_trajectories,
    "fix_intents": _build_fix_intents,
    "planner_step_sft": _build_planner_step_sft,
    "planner_final_prompt": _build_planner_final_prompt,
    "executor_answer": _build_executor_answer,
    "merged_final_prompt": _build_merged_final_prompt,
    "dedup_final_prompt": _build_dedup_final_prompt,
    "data_stats": _build_data_stats,
    "rag_summary": _build_rag_summary,
    "multiturn": _build_multiturn,
    "identity_sft": _build_identity_sft,
    "no_intent_executor": _build_no_intent_executor,
    "downsample_intent": _build_downsample_intent,
    "downsample_executor": _build_downsample_executor,
}


def build_command(stage_id: str, ctx: RunContext) -> list[str]:
    """构造某个阶段的完整命令行。"""
    builder = _BUILDERS.get(stage_id)
    if builder is None:
        raise KeyError(f"未注册的阶段：{stage_id}")
    return builder(ctx, ctx.stage_values(stage_id))


# ---------------------------------------------------------------------------
# 参数清洗
# ---------------------------------------------------------------------------


def coerce_params(stage: Stage, raw: dict[str, Any]) -> dict[str, Any]:
    """按声明类型清洗前端传来的参数；空字符串统一变成 None（= 不传该 flag）。"""
    cleaned: dict[str, Any] = {}
    for param in stage.params:
        value = raw.get(param.key, param.default)
        if param.type == "bool":
            cleaned[param.key] = bool(value)
            continue
        if value is None or (isinstance(value, str) and not value.strip()):
            cleaned[param.key] = None
            continue
        try:
            if param.type == "int":
                cleaned[param.key] = int(str(value).strip())
            elif param.type == "float":
                cleaned[param.key] = float(str(value).strip())
            else:
                cleaned[param.key] = str(value).strip()
        except (TypeError, ValueError) as exc:
            raise ValueError(f"参数「{param.label}」取值非法：{value!r}") from exc
    return cleaned


def find_domains() -> list[dict[str, Any]]:
    """扫描 ``agent/domains/*/``，返回可选领域及其 seed 状态。"""
    root = AGENT_ROOT / "domains"
    domains: list[dict[str, Any]] = []
    if not root.is_dir():
        return domains
    for child in sorted(root.iterdir()):
        if not child.is_dir() or child.name.startswith((".", "_")):
            continue
        seeds = child / "seeds.jsonl"
        has_seeds = seeds.is_file()
        try:
            seed_mtime = seeds.stat().st_mtime if has_seeds else 0.0
        except OSError:  # pragma: no cover - 文件被并发删除
            seed_mtime = 0.0
        domains.append(
            {
                "name": child.name,
                "hasSeeds": has_seeds,
                "seedPath": str(seeds),
                "seedLines": count_lines(seeds) if has_seeds else 0,
                "seedMtime": seed_mtime,
                # 每个项目自己的默认前缀与可选数据集。带上这两项，界面切换项目时
                # 不用再等一次请求就能把前缀换成新项目的。
                "defaultPrefix": default_prefix(child.name),
                "datasetSplits": dataset_splits(child.name),
            }
        )
    return domains


def default_domain(domains: list[dict[str, Any]]) -> str | None:
    """挑一个最可能被使用的领域作为 UI 初始值。

    规则：

    1. ``AGENT_DOMAIN`` / ``AGENT_OUTPUT_DOMAIN`` 指向的领域，且它确实有 seeds；
    2. 否则退回「最近修改过 seeds.jsonl」的领域 —— 通常就是当前在做的那个；
    3. 再否则取第一个领域。
    """
    if not domains:
        return None
    configured = next((item for item in domains if item["name"] == OUTPUT_DOMAIN_NAME), None)
    if configured and configured["hasSeeds"]:
        return configured["name"]
    seeded = [item for item in domains if item["hasSeeds"]]
    if seeded:
        newest = max(seeded, key=lambda item: item.get("seedMtime") or 0.0)
        return newest["name"]
    if configured:
        return configured["name"]
    return domains[0]["name"]


def count_lines(path: Path) -> int:
    """快速统计文件里的非空行数（只读字节流，不做 JSON 解析）。

    JSONL 文件末行常常没有换行符，直接数 ``\\n`` 会少算一条；这里按
    「非空行」口径计数，与前端逐行解析的结果保持一致。
    """
    try:
        total = 0
        tail = b""
        with path.open("rb") as handle:
            while chunk := handle.read(1 << 20):
                lines = (tail + chunk).split(b"\n")
                tail = lines.pop()  # 最后一段可能是被截断的半行
                total += sum(1 for line in lines if line.strip())
        if tail.strip():
            total += 1
        return total
    except OSError:
        return 0


# ---------------------------------------------------------------------------
# 领域（项目）管理
#
# 一个领域就是一个自包含的「项目」，磁盘上由两部分组成：
#
#   agent/domains/<name>/     代码包：spec.py（DomainSpec）+ seeds.jsonl + 可选 prompts/tools
#   agent/outputs/<name>/     该领域全部产物，与其他领域完全隔离
#
# 因此「新建领域」= 复制一份领域包（并改写 spec.py 里的 name/display_name），
# 而不是只建一个空目录 —— 否则新领域没有 spec.py，流水线跑不起来。
# ---------------------------------------------------------------------------

DOMAINS_DIR = AGENT_ROOT / "domains"
OUTPUTS_DIR = AGENT_ROOT / "outputs"
TRASH_DIR = AGENT_ROOT / ".trash"

# 目录名白名单：小写字母开头，只允许小写字母/数字/下划线，2-32 字符。
DOMAIN_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{1,31}$")

# 领域包里除 spec.py / seeds.jsonl 之外会被复制的内容。
# ``.history`` 是编辑器自动留的备份，复制领域时不要带过去。
_DOMAIN_IGNORE = shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo", ".DS_Store", ".history")

# 只读文件头部即可拿到显示名，避免为了读元信息而 import 整个 spec.py。
_SPEC_HEAD_BYTES = 16 * 1024
_SPEC_CALL_RE = re.compile(r"DOMAIN\s*=\s*DomainSpec\(")
_SPEC_DISPLAY_RE = re.compile(r"display_name\s*=\s*[\"']([^\"']*)[\"']")
_SPEC_NAME_RE = re.compile(r"\bname\s*=\s*[\"']([^\"']*)[\"']")


def domain_dir(name: str) -> Path:
    """``agent/domains/<name>``。"""
    return DOMAINS_DIR / safe_path_part(name, "health")


def validate_domain_name(name: str) -> str:
    """校验目录名；不合法就抛 ValueError（调用方转成 400）。"""
    cleaned = (name or "").strip()
    if not DOMAIN_NAME_RE.match(cleaned):
        raise ValueError(
            "领域名只能用小写字母、数字、下划线，且以字母开头（2-32 字符），例如 health_talent。"
        )
    if cleaned.startswith("_"):
        raise ValueError("领域名不能以下划线开头。")
    return cleaned


def read_spec_display_name(spec_file: Path) -> str | None:
    """从 spec.py 头部抠出 ``display_name``，失败返回 None。

    刻意不做 import：领域包可能拉起 openai 等重依赖，UI 服务必须保持轻量。
    """
    try:
        with spec_file.open("r", encoding="utf-8", errors="replace") as handle:
            head = handle.read(_SPEC_HEAD_BYTES)
    except OSError:
        return None
    match = _SPEC_CALL_RE.search(head)
    if match is None:
        return None
    display = _SPEC_DISPLAY_RE.search(head[match.end():])
    return display.group(1).strip() if display else None


def _dir_size(directory: Path) -> tuple[int, float, int]:
    """返回 ``(总字节, 最近修改时间, 文件数)``，不递归进符号链接。"""
    total = 0
    newest = 0.0
    count = 0
    if not directory.is_dir():
        return 0, 0.0, 0
    for item in directory.rglob("*"):
        try:
            if not item.is_file():
                continue
            stat = item.stat()
        except OSError:
            continue
        total += stat.st_size
        newest = max(newest, stat.st_mtime)
        count += 1
    return total, newest, count


def describe_domain(name: str) -> dict[str, Any]:
    """单个领域的完整画像：代码包状态 + 产物规模。"""
    directory = domain_dir(name)
    spec_file = directory / "spec.py"
    seeds = directory / "seeds.jsonl"
    out_dir = domain_output_dir(name)

    has_seeds = seeds.is_file()
    try:
        seed_stat = seeds.stat() if has_seeds else None
    except OSError:
        seed_stat = None
    out_bytes, out_mtime, out_files = _dir_size(out_dir)

    extras = sorted(
        item.name
        for item in directory.glob("*.py")
        if item.name not in {"__init__.py", "spec.py"}
    )
    return {
        "name": name,
        "displayName": read_spec_display_name(spec_file) or name,
        "path": str(directory),
        "specPath": str(spec_file),
        "hasSpec": spec_file.is_file(),
        "extraModules": extras,
        "hasSeeds": has_seeds,
        "seedPath": str(seeds),
        "seedLines": count_lines(seeds) if has_seeds else 0,
        "seedBytes": seed_stat.st_size if seed_stat else 0,
        "seedMtime": seed_stat.st_mtime if seed_stat else 0.0,
        "outputDir": str(out_dir),
        "artifactFiles": out_files,
        "artifactBytes": out_bytes,
        "artifactMtime": out_mtime,
        "active": name == OUTPUT_DOMAIN_NAME,
    }


def list_domains() -> list[dict[str, Any]]:
    """列出全部领域，按「最近有改动」优先排序。"""
    if not DOMAINS_DIR.is_dir():
        return []
    names = [
        child.name
        for child in sorted(DOMAINS_DIR.iterdir())
        if child.is_dir() and not child.name.startswith((".", "_"))
    ]
    domains = [describe_domain(name) for name in names]
    domains.sort(key=lambda item: max(item["seedMtime"], item["artifactMtime"]), reverse=True)
    return domains


def domain_templates() -> list[dict[str, str]]:
    """可用作模板的领域（必须带 spec.py，否则复制出来跑不起来）。"""
    templates: list[dict[str, str]] = []
    for item in list_domains():
        if item["hasSpec"]:
            templates.append(
                {
                    "name": item["name"],
                    "displayName": item["displayName"],
                    "modules": ", ".join(item["extraModules"]),
                }
            )
    return templates


def _rewrite_spec_identity(spec_file: Path, name: str, display_name: str) -> list[str]:
    """改写 spec.py 里 ``DomainSpec(name=..., display_name=...)`` 两项。

    只替换 ``DOMAIN = DomainSpec(`` 之后第一次出现的 name / display_name，
    避免误伤文件里其他同名关键字参数。返回需要人工确认的提示。"""
    notes: list[str] = []
    try:
        text = spec_file.read_text(encoding="utf-8")
    except OSError as exc:
        return [f"无法读取 {spec_file}：{exc}"]

    match = _SPEC_CALL_RE.search(text)
    if match is None:
        return [f"{spec_file.name} 里没找到 `DOMAIN = DomainSpec(`，请手动把 name 改成 {name!r}。"]

    head, tail = text[: match.end()], text[match.end():]
    quoted_name = f'"{name}"'
    quoted_display = '"' + display_name.replace("\\", "\\\\").replace('"', '\\"') + '"'

    tail, name_hits = re.subn(
        r'(name\s*=\s*)["\'][^"\']*["\']', lambda m: m.group(1) + quoted_name, tail, count=1
    )
    tail, display_hits = re.subn(
        r'(display_name\s*=\s*)["\'][^"\']*["\']', lambda m: m.group(1) + quoted_display, tail, count=1
    )
    if name_hits == 0:
        notes.append(f"{spec_file.name} 的 DomainSpec 里没找到 name 参数，请手动确认为 {name!r}。")
    if display_hits == 0:
        notes.append(f"{spec_file.name} 的 DomainSpec 里没找到 display_name 参数，显示名可能不生效。")

    spec_file.write_text(head + tail, encoding="utf-8")
    return notes


def _prepare_domain_copy(
    template: str, target_name: str, display_name: str, copy_seeds: bool
) -> tuple[Path, list[str]]:
    """把模板领域复制成 ``target_name``，返回 (目标目录, 提示列表)。"""
    source = domain_dir(template)
    if not (source / "spec.py").is_file():
        raise ValueError(f"模板领域 {template!r} 缺少 spec.py，无法作为模板。")

    target = domain_dir(target_name)
    if target.exists():
        raise ValueError(f"领域 {target_name!r} 已存在，换一个名字。")

    shutil.copytree(source, target, ignore=_DOMAIN_IGNORE)
    notes = _rewrite_spec_identity(target / "spec.py", target_name, display_name)

    seeds = target / "seeds.jsonl"
    if copy_seeds:
        if not seeds.is_file():
            seeds.write_text("", encoding="utf-8")
    else:
        # 清空 seed：新项目应该从自己的数据开始，而不是继承模板样本。
        seeds.write_text("", encoding="utf-8")
    return target, notes


def create_domain(
    name: str, display_name: str | None = None, template: str | None = None, copy_seeds: bool = False
) -> dict[str, Any]:
    """新建领域：以某个现有领域为模板复制，并改写身份信息。"""
    target_name = validate_domain_name(name)
    templates = domain_templates()
    if not templates:
        raise ValueError("没有可用作模板的领域（需要一个带 spec.py 的领域）。")

    template_name = (template or templates[0]["name"]).strip()
    if template_name not in {item["name"] for item in templates}:
        raise ValueError(f"模板领域 {template_name!r} 不存在或缺少 spec.py。")

    label = (display_name or target_name).strip() or target_name
    _target, notes = _prepare_domain_copy(template_name, target_name, label, copy_seeds)
    return {"domain": describe_domain(target_name), "notes": notes, "template": template_name}


def duplicate_domain(
    source: str,
    new_name: str,
    display_name: str | None = None,
    copy_seeds: bool = True,
    copy_outputs: bool = False,
) -> dict[str, Any]:
    """复制一个已有领域（含 seeds，可选含产物）。"""
    source_name = validate_domain_name(source)
    if not domain_dir(source_name).is_dir():
        raise ValueError(f"领域 {source_name!r} 不存在。")
    target_name = validate_domain_name(new_name)
    if target_name == source_name:
        raise ValueError("新名字不能和原名字相同。")

    label = (display_name or f"{describe_domain(source_name)['displayName']} 副本").strip()
    _target, notes = _prepare_domain_copy(source_name, target_name, label, copy_seeds)

    if copy_outputs and domain_output_dir(source_name).is_dir():
        shutil.copytree(
            domain_output_dir(source_name), domain_output_dir(target_name), ignore=_DOMAIN_IGNORE
        )
        notes.append("已连同产物一起复制。")
        # 复制过来的产物名里还是源项目的名字，不改的话副本读不到自己的数据。
        renamed = _rename_domain_prefix_files(domain_output_dir(target_name), source_name, target_name)
        if renamed:
            notes.append(f"副本产物名里的 {source_name}_ 前缀改为 {target_name}_，共 {renamed} 个文件。")
    return {"domain": describe_domain(target_name), "notes": notes, "template": source_name}


def _rename_domain_prefix_files(directory: Path, old_name: str, new_name: str) -> int:
    """把产物文件名里的项目名换成新的，返回改了几个。

    前缀是 ``<领域名>_<数据集>``，所以领域一改名，「旧名_train_*」就再也匹配不上
    新项目的默认前缀了——不跟着改，用户会以为产物凭空消失。只动 ``<旧名>_`` 开头
    的文件；手填过别的前缀（例如 ``smoke_``）不属于任何项目，原样保留。
    """
    if not directory.is_dir():
        return 0
    moved = 0
    head = f"{old_name}_"
    for path in sorted(directory.iterdir()):
        if not path.is_file() or not path.name.startswith(head):
            continue
        target = path.with_name(f"{new_name}_{path.name[len(head):]}")
        if target.exists():
            continue
        path.rename(target)
        moved += 1
    return moved


def rename_domain(name: str, new_name: str, display_name: str | None = None) -> dict[str, Any]:
    """重命名领域：目录、spec.py 里的 name、以及产物目录一起改。"""
    old_name = validate_domain_name(name)
    target_name = validate_domain_name(new_name)
    source = domain_dir(old_name)
    if not source.is_dir():
        raise ValueError(f"领域 {old_name!r} 不存在。")
    if target_name == old_name:
        raise ValueError("新名字和原名字相同，无需重命名。")
    if domain_dir(target_name).exists():
        raise ValueError(f"领域 {target_name!r} 已存在，换一个名字。")

    target = domain_dir(target_name)
    shutil.move(str(source), str(target))
    notes = _rewrite_spec_identity(target / "spec.py", target_name, display_name or target_name)

    old_outputs = OUTPUTS_DIR / old_name
    if old_outputs.is_dir():
        new_outputs = OUTPUTS_DIR / target_name
        if new_outputs.exists():
            notes.append(f"产物目录 {new_outputs} 已存在，未搬移旧产物，请手动合并。")
        else:
            shutil.move(str(old_outputs), str(new_outputs))
            notes.append(f"产物目录已改为 {new_outputs.name}。")
            # 产物名里嵌着项目名，不跟着改就再也读不出来了。
            renamed = _rename_domain_prefix_files(new_outputs, old_name, target_name)
            if renamed:
                notes.append(f"产物名里的 {old_name}_ 前缀同步改为 {target_name}_，共 {renamed} 个文件。")

    # 旧的字节码缓存带着老模块名，改名后不再被使用，直接清掉避免混淆。
    shutil.rmtree(target / "__pycache__", ignore_errors=True)
    return {"domain": describe_domain(target_name), "notes": notes}


def delete_domain(name: str) -> dict[str, Any]:
    """删除领域：移入 ``agent/.trash/<name>_<时间戳>/``，不真删，可手工恢复。"""
    target_name = validate_domain_name(name)
    source = domain_dir(target_name)
    if not source.is_dir():
        raise ValueError(f"领域 {target_name!r} 不存在。")

    stamp = time.strftime("%Y%m%d_%H%M%S")
    trash = TRASH_DIR / f"{target_name}_{stamp}"
    trash.mkdir(parents=True, exist_ok=True)
    shutil.move(str(source), str(trash / "domain"))

    moved_outputs = False
    out_dir = domain_output_dir(target_name)
    if out_dir.is_dir():
        shutil.move(str(out_dir), str(trash / "outputs"))
        moved_outputs = True

    return {
        "name": target_name,
        "trashDir": str(trash),
        "movedOutputs": moved_outputs,
        "restoreHint": f"如需恢复：mv '{trash / 'domain'}' '{source}'，产物同理。",
    }


# ---------------------------------------------------------------------------
# 领域文件在线编辑
#
# 提示词、意图分类、工具定义都写在领域包里（spec.py / prompts.py / tools.py …），
# 所以「在线编辑」= 改这些文件。三条硬规则：
#
#   1. 只能改 agent/domains/<name>/ 之内的文本文件（拒绝越界、隐藏文件、二进制）；
#   2. 保存前做语法校验（.py 用 ast，.json/.jsonl 用 json），写坏领域包直接拒绝；
#   3. 每次保存前把旧内容备份到 <领域>/.history/，随时可以回滚。
#
# 这里只改内容、不建/删文件，目录级操作交给上面的 create/rename/delete。
# ---------------------------------------------------------------------------

_DOMAIN_HISTORY_DIR = ".history"
_DOMAIN_HISTORY_KEEP = 20

# 允许在线编辑的文本后缀；其余文件在列表里只做展示。
_DOMAIN_EDIT_SUFFIXES = {
    ".py",
    ".json",
    ".jsonl",
    ".md",
    ".txt",
    ".yaml",
    ".yml",
    ".toml",
    ".cfg",
    ".ini",
    ".sh",
}
# 遍历领域目录时跳过的目录：字节码、历史备份、编辑器元数据。
_DOMAIN_EDIT_SKIP_DIRS = {
    _DOMAIN_HISTORY_DIR,
    "__pycache__",
    ".git",
    ".idea",
    ".vscode",
    ".ipynb_checkpoints",
}
_DOMAIN_EDIT_MAX_BYTES = 2 * 1024 * 1024


def _human_size(size: int) -> str:
    if size < 1024:
        return f"{size} 字节"
    if size < 1024 * 1024:
        return f"{size / 1024:.1f} KB"
    return f"{size / 1024 / 1024:.1f} MB"


def _count_total_lines(path: Path) -> int:
    """总行数（含空行），口径与编辑器行号栏一致。

    与 ``count_lines`` 不同：那个数的是非空行（JSONL 场景更合适），
    在编辑器里会和行号栏对不上。
    """
    total = 0
    last = b""
    try:
        with path.open("rb") as handle:
            while chunk := handle.read(1 << 20):
                total += chunk.count(b"\n")
                last = chunk[-1:]
    except OSError:
        return 0
    if not last:
        return 0
    return total if last == b"\n" else total + 1


def _domain_relpath(relpath: str) -> Path:
    """规范化并校验用户给的相对路径，不合法就抛 ValueError。"""
    cleaned = (relpath or "").strip().replace("\\", "/").lstrip("/")
    parts = [part for part in cleaned.split("/") if part not in ("", ".")]
    if not parts:
        raise ValueError("缺少文件路径。")
    if any(part == ".." for part in parts):
        raise ValueError("路径里不能出现 ..")
    if any(part.startswith(".") for part in parts):
        raise ValueError("不能编辑隐藏文件或隐藏目录里的文件。")
    if any(part in _DOMAIN_EDIT_SKIP_DIRS for part in parts):
        raise ValueError(f"{parts[0]!r} 是内部目录，不参与编辑。")

    rel = Path(*parts)
    if rel.suffix.lower() not in _DOMAIN_EDIT_SUFFIXES:
        allowed = "、".join(sorted(_DOMAIN_EDIT_SUFFIXES))
        raise ValueError(f"只支持编辑这些类型的文件：{allowed}")
    return rel


def _locate_domain_file(name: str, relpath: str) -> tuple[Path, str]:
    """定位领域内的文件，返回 ``(绝对路径, 规范化相对路径)``。"""
    target_name = validate_domain_name(name)
    directory = domain_dir(target_name)
    if not directory.is_dir():
        raise ValueError(f"领域 {target_name!r} 不存在。")

    rel = _domain_relpath(relpath)
    base = directory.resolve()
    target = (directory / rel).resolve()
    if target.parent != base and base not in target.parents:
        raise ValueError("路径越界：只能编辑领域目录内的文件。")
    return target, rel.as_posix()


def _file_rank(relpath: str) -> tuple[int, str]:
    """文件列表排序：spec.py 最前，seeds.jsonl 最后（它更适合用 seed 视图改）。"""
    if relpath == "spec.py":
        return 0, relpath
    if relpath.endswith(".py"):
        return 1, relpath
    if relpath.endswith(".jsonl"):
        return 3, relpath
    return 2, relpath


def list_domain_files(name: str) -> dict[str, Any]:
    """列出领域包里可在线查看/编辑的文件。"""
    target_name = validate_domain_name(name)
    directory = domain_dir(target_name)
    if not directory.is_dir():
        raise ValueError(f"领域 {target_name!r} 不存在。")

    files: list[dict[str, Any]] = []
    for item in directory.rglob("*"):
        rel = item.relative_to(directory)
        if any(part.startswith(".") or part in _DOMAIN_EDIT_SKIP_DIRS for part in rel.parts):
            continue
        try:
            if not item.is_file():
                continue
            stat = item.stat()
        except OSError:
            continue

        suffix = item.suffix.lower()
        editable = suffix in _DOMAIN_EDIT_SUFFIXES and stat.st_size <= _DOMAIN_EDIT_MAX_BYTES
        if suffix not in _DOMAIN_EDIT_SUFFIXES:
            reason = "不是文本文件，无法在线编辑"
        elif stat.st_size > _DOMAIN_EDIT_MAX_BYTES:
            reason = f"文件过大（{_human_size(stat.st_size)}），请用本地编辑器"
        elif rel.name == "seeds.jsonl":
            reason = "seed 数据建议用 seed 视图编辑"
        else:
            reason = ""

        files.append(
            {
                "path": rel.as_posix(),
                "size": stat.st_size,
                "mtime": stat.st_mtime,
                "lines": _count_total_lines(item) if stat.st_size <= _DOMAIN_EDIT_MAX_BYTES else -1,
                "editable": editable,
                "reason": reason,
                "isSpec": rel.as_posix() == "spec.py",
                "isSeeds": rel.name == "seeds.jsonl",
            }
        )

    files.sort(key=lambda item: _file_rank(item["path"]))
    return {
        "domain": target_name,
        "displayName": read_spec_display_name(directory / "spec.py") or target_name,
        "dir": str(directory),
        "files": files,
    }


def _history_dir(root: Path, key: str) -> Path:
    """``<root>/.history/<扁平化 key>/``。"""
    flat = key.replace("/", "__")
    return root / _DOMAIN_HISTORY_DIR / flat


def _file_backups(root: Path, key: str, limit: int = 0) -> list[dict[str, Any]]:
    """历史版本列表，最新的在前。"""
    directory = _history_dir(root, key)
    if not directory.is_dir():
        return []
    items: list[dict[str, Any]] = []
    for item in sorted(directory.glob("*.bak"), reverse=True):
        try:
            stat = item.stat()
        except OSError:
            continue
        items.append({"backup": item.name, "size": stat.st_size, "mtime": stat.st_mtime})
    return items[:limit] if limit else items


def _backup_file(root: Path, key: str, text: str) -> str:
    """写前备份旧内容，返回备份文件名；超出上限时丢弃最旧的。"""
    directory = _history_dir(root, key)
    directory.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    target = directory / f"{stamp}.bak"
    index = 1
    while target.exists():
        target = directory / f"{stamp}_{index}.bak"
        index += 1
    target.write_text(text, encoding="utf-8")

    backups = sorted(directory.glob("*.bak"))
    for stale in backups[:-_DOMAIN_HISTORY_KEEP]:
        try:
            stale.unlink()
        except OSError:
            pass
    return target.name


def _validate_domain_text(relpath: str, text: str) -> None:
    """保存前做语法校验：写坏领域包比不让保存更糟。"""
    suffix = Path(relpath).suffix.lower()
    if suffix == ".py":
        try:
            ast.parse(text, filename=relpath)
        except SyntaxError as exc:
            where = f"第 {exc.lineno} 行" if exc.lineno else ""
            raise ValueError(f"{relpath} 有语法错误：{where} {exc.msg}") from exc
    elif suffix == ".json":
        try:
            json.loads(text)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{relpath} 不是合法 JSON：第 {exc.lineno} 行 {exc.msg}") from exc
    elif suffix == ".jsonl":
        for number, line in enumerate(text.splitlines(), 1):
            if not line.strip():
                continue
            try:
                json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"{relpath} 第 {number} 行不是合法 JSON：{exc.msg}") from exc


def read_domain_file(name: str, relpath: str) -> dict[str, Any]:
    """读取领域内一个文本文件的内容。"""
    target_name = validate_domain_name(name)
    target, rel = _locate_domain_file(target_name, relpath)
    if not target.is_file():
        raise ValueError(f"文件 {rel} 不存在。")

    stat = target.stat()
    if stat.st_size > _DOMAIN_EDIT_MAX_BYTES:
        raise ValueError(f"{rel} 有 {_human_size(stat.st_size)}，超出在线编辑上限，请用本地编辑器。")

    text = target.read_text(encoding="utf-8", errors="replace")
    return {
        "domain": target_name,
        "path": rel,
        "content": text,
        "size": stat.st_size,
        "mtime": stat.st_mtime,
        "lines": text.count("\n") + (0 if text.endswith("\n") else 1),
        "backups": _file_backups(domain_dir(target_name), rel, limit=5),
    }


def write_domain_file(name: str, relpath: str, content: str) -> dict[str, Any]:
    """保存领域内一个文本文件：校验语法 -> 备份旧版 -> 原子写入。"""
    target_name = validate_domain_name(name)
    target, rel = _locate_domain_file(target_name, relpath)
    if not target.is_file():
        raise ValueError(f"文件 {rel} 不存在；这里只改内容，不新建文件。")

    payload = len(content.encode("utf-8"))
    if payload > _DOMAIN_EDIT_MAX_BYTES:
        raise ValueError(f"内容有 {_human_size(payload)}，超出在线编辑上限。")
    _validate_domain_text(rel, content)

    previous = target.read_text(encoding="utf-8", errors="replace")
    if previous == content:
        return {
            "domain": target_name,
            "path": rel,
            "changed": False,
            "backup": "",
            "size": payload,
            "lines": content.count("\n") + (0 if content.endswith("\n") else 1),
        }

    backup = _backup_file(domain_dir(target_name), rel, previous)
    tmp = target.with_name(target.name + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    tmp.replace(target)

    # 领域模块可能已被 import 过；清掉字节码缓存，保证下次运行读的是新代码。
    shutil.rmtree(domain_dir(target_name) / "__pycache__", ignore_errors=True)

    stat = target.stat()
    return {
        "domain": target_name,
        "path": rel,
        "changed": True,
        "backup": backup,
        "size": stat.st_size,
        "mtime": stat.st_mtime,
        "lines": content.count("\n") + (0 if content.endswith("\n") else 1),
    }


def domain_file_history(name: str, relpath: str) -> dict[str, Any]:
    """列出某个文件的历史备份。"""
    target_name = validate_domain_name(name)
    _target, rel = _locate_domain_file(target_name, relpath)
    return {
        "domain": target_name,
        "path": rel,
        "backups": _file_backups(domain_dir(target_name), rel),
    }


def restore_domain_file(name: str, relpath: str, backup: str) -> dict[str, Any]:
    """把某个历史版本恢复成当前内容（当前内容也会先备份，恢复同样可逆）。"""
    target_name = validate_domain_name(name)
    target, rel = _locate_domain_file(target_name, relpath)
    root = domain_dir(target_name)

    source = _history_dir(root, rel) / Path(backup or "").name
    if not source.is_file():
        raise ValueError(f"历史版本 {backup!r} 不存在。")

    text = source.read_text(encoding="utf-8", errors="replace")
    _validate_domain_text(rel, text)

    if target.is_file():
        _backup_file(root, rel, target.read_text(encoding="utf-8", errors="replace"))
    tmp = target.with_name(target.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(target)
    shutil.rmtree(root / "__pycache__", ignore_errors=True)

    return {
        "domain": target_name,
        "path": rel,
        "restored": source.name,
        "content": text,
        "size": target.stat().st_size,
        "mtime": target.stat().st_mtime,
    }


# ---------------------------------------------------------------------------
# 运行时配置（agent/.env）
#
# 教师模型接入（模型名 / API 端点 / Key）和超时这些东西只活在 .env 与环境变量
# 里，界面上既看不到也改不了。这里声明一份字段表，server 暴露成 /api/config，
# 写入时就地更新 .env —— 注释、空行、其它键全部原样保留。
# ---------------------------------------------------------------------------

ENV_PATH = AGENT_ROOT / ".env"

# 值里出现这些字符会把 .env 的一行拆成两行，直接拒绝。
_ENV_FORBIDDEN_RE = re.compile(r"[\r\n\x00]")

# 模板里的占位写法：整串只有一个重复字符（sk-xxxx…、****）。真实密钥不会长成
# 这样，所以只要值非空就当成真凭据、把占位符当「未配置」处理，判定很稳。
# 只用在 secret 类型字段上——端点、模型名那类值本来就可能是真实配置。
_PLACEHOLDER_SECRET_RE = re.compile(r"^(?:sk-)?[xX*]{6,}$")


@dataclass(frozen=True)
class ConfigField:
    """一个可在界面上编辑的 .env 配置项。

    ``required`` 标记「没配就跑不起来」的项。它们没有默认值：请求打到哪个网关、
    用哪个模型属于运行环境信息，藏在脚本里只会让人无从判断实际生效的是什么。
    缺值时在发起作业前就拦住并说清楚去哪儿填。

    ``fallback`` 是另一回事：留空时跟随另一个配置项的值（界面上显示「跟随 XXX」）。
    """

    key: str
    label: str
    type: str = "str"  # str | int | secret | path
    default: str = ""
    fallback: str = ""
    required: bool = False
    help: str = ""
    group: str = "模型接入"

    def to_json(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "label": self.label,
            "type": self.type,
            "default": self.default,
            "fallback": self.fallback,
            "required": self.required,
            "help": self.help,
            "group": self.group,
        }


CONFIG_FIELDS: tuple[ConfigField, ...] = (
    ConfigField(
        key="DEEPSEEK_API_KEY",
        label="API Key",
        type="secret",
        required=True,
        help="教师模型的调用凭据。留空表示不修改现有值。",
    ),
    ConfigField(
        key="DEEPSEEK_BASE_URL",
        label="API 端点",
        required=True,
        help="OpenAI 兼容接口的 base URL，例如中转站或自建网关的地址。",
    ),
    ConfigField(
        key="AGENT_DISTILL_MODEL",
        label="教师模型 · planner 轨迹",
        required=True,
        help="阶段 1 用它按 seed 合成规划轨迹。",
    ),
    ConfigField(
        key="AGENT_EXECUTOR_MODEL",
        label="教师模型 · executor 回答",
        fallback="AGENT_DISTILL_MODEL",
        help="阶段 5 用它合成执行阶段回答；留空则跟随 planner 教师模型。",
    ),
    ConfigField(
        key="AGENT_MULTI_TURN_USER_MODEL",
        label="多轮用户模型",
        fallback="AGENT_DISTILL_MODEL",
        help="多轮会话里扮演用户；留空则跟随 planner 教师模型。",
    ),
    ConfigField(
        key="AGENT_MULTI_TURN_COMPRESSION_MODEL",
        label="多轮压缩模型",
        fallback="AGENT_MULTI_TURN_USER_MODEL",
        help="压缩多轮历史用；留空则跟随多轮用户模型。",
    ),
    ConfigField(
        key="EXA_API_KEY",
        label="Exa API Key",
        type="secret",
        group="工具凭据",
        help="search_web 工具用。未配置时该工具返回「未配置」提示，其余流程不受影响；留空表示不修改现有值。",
    ),
    ConfigField(
        key="AGENT_QWEN_BASE_MODEL_PATH",
        label="本地 tokenizer 路径",
        type="path",
        default=str(QWEN_BASE_MODEL_PATH),
        group="本地模型",
        help="阶段 4 / 6 渲染训练 prompt 用；相对路径从项目根目录解析。",
    ),
    ConfigField(
        key="AGENT_DISTILL_HTTP_TIMEOUT",
        label="planner 请求超时（秒）",
        type="int",
        default="120",
        group="超时与重试",
        help="阶段 1 单次教师模型请求的最长等待时间。",
    ),
    ConfigField(
        key="AGENT_DISTILL_TOOL_TIMEOUT",
        label="planner 工具超时（秒）",
        type="int",
        default="45",
        group="超时与重试",
        help="阶段 1 单个工具调用的超时。",
    ),
    ConfigField(
        key="AGENT_DISTILL_RETRY_BACKOFF_CAP",
        label="退避上限（秒）",
        type="int",
        default="30",
        group="超时与重试",
        help="请求失败重试的指数退避封顶值。",
    ),
    ConfigField(
        key="AGENT_EXECUTOR_HTTP_TIMEOUT",
        label="executor 请求超时（秒）",
        type="int",
        default="120",
        group="超时与重试",
        help="阶段 5 单次教师模型请求的最长等待时间。",
    ),
    ConfigField(
        key="AGENT_EXECUTOR_TOOL_TIMEOUT",
        label="executor 工具超时（秒）",
        type="int",
        default="30",
        group="超时与重试",
        help="阶段 5 单个工具调用的超时。",
    ),
)

CONFIG_FIELD_BY_KEY: dict[str, ConfigField] = {item.key: item for item in CONFIG_FIELDS}


def read_env_file(path: Path | None = None) -> dict[str, str]:
    """解析 .env，返回键值对；不读也不改 ``os.environ``。

    只做最小解析（一行一条 ``KEY=VALUE``，``#`` 开头是注释），够用且不会
    误改用户手写的文件。
    """
    target = path or ENV_PATH
    if not target.exists():
        return {}
    values: dict[str, str] = {}
    for line in target.read_text(encoding="utf-8", errors="replace").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        key = key.strip()
        if key:
            values[key] = value.strip().strip('"').strip("'")
    return values


def write_env_file(updates: dict[str, str | None], path: Path | None = None) -> None:
    """就地更新 .env：已有的键替换那一行，值为 ``None`` 则删掉那一行，新键追加到末尾。

    注释、空行、其它键都原样保留；不重排、不格式化、不重写未改动的行。
    """
    target = path or ENV_PATH
    lines = target.read_text(encoding="utf-8").splitlines() if target.exists() else []
    pending = dict(updates)
    output: list[str] = []
    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            key = stripped.partition("=")[0].strip()
            if key in pending:
                value = pending.pop(key)
                if value is not None:
                    output.append(f"{key}={value}")
                continue
        output.append(line)
    for key, value in pending.items():
        if value is not None:
            output.append(f"{key}={value}")
    text = "\n".join(output).strip("\n")
    target.write_text(f"{text}\n" if text else "", encoding="utf-8")


def _effective_config(
    key: str,
    file_values: dict[str, str],
    seen: set[str] | None = None,
) -> tuple[str, str, str]:
    """解析一个配置键的生效值，返回 ``(值, 来源, 提供值的键名)``。

    来源为 file / env / default / fallback / unset。走 fallback 时第三个元素
    是最终给出值的那个键，界面据此显示「跟随 XXX」。

    环境变量优先于 .env（``load_dotenv`` 默认不覆盖已存在的变量）。界面进程
    里两者可能都看得到，用「值是否相同」区分：不同说明环境变量压过了文件。
    """
    seen = seen if seen is not None else set()
    if key in seen:  # 防御：配置表写出环时直接收手
        return "", "unset", key
    seen.add(key)

    file_value = file_values.get(key, "")
    env_value = os.environ.get(key, "")
    field = CONFIG_FIELD_BY_KEY.get(key)
    # 占位符值非空，但显然不是真凭据：当「未配置」处理。否则照模板装完、一个
    # key 都不填，界面也显示绿色的「模型接入 ✓」，跑起来全是鉴权错误。
    if field is not None and field.type == "secret":
        if _PLACEHOLDER_SECRET_RE.match(file_value):
            file_value = ""
        if _PLACEHOLDER_SECRET_RE.match(env_value):
            env_value = ""

    if env_value and env_value != file_value:
        return env_value, "env", key
    if file_value:
        return file_value, "file", key
    if env_value:
        return env_value, "env", key

    if field is None:
        return "", "unset", key
    if field.default:
        return field.default, "default", key
    if field.fallback:
        value, source, origin = _effective_config(field.fallback, file_values, seen)
        if source == "unset":
            return "", "unset", key
        return value, "fallback", origin
    return "", "unset", key


def _mask_secret(value: str) -> str:
    """密钥只回显头尾各 4 位，界面据此确认「配的是哪一把」，不泄露全量。"""
    if not value:
        return ""
    if len(value) <= 12:
        return f"{value[:2]}…"
    return f"{value[:4]}…{value[-4:]}"


def missing_required_config() -> list[tuple[str, str]]:
    """返回 ``[(键, 界面上的名字)]``，列出还没配好的必需项。

    运行前拦截与配置快照共用同一份判断，避免「界面说配好了、跑起来却说没配」。
    模板里的密钥占位符（``sk-xxxx…``）也算没配好，理由见 ``_effective_config``。
    """
    file_values = read_env_file()
    missing: list[tuple[str, str]] = []
    for field in CONFIG_FIELDS:
        if not field.required:
            continue
        value, source, _ = _effective_config(field.key, file_values)
        if source == "unset" or not value.strip():
            missing.append((field.key, field.label))
    return missing


def describe_config() -> dict[str, Any]:
    """给界面的配置快照：当前写入值、实际生效值、值从哪来。"""
    file_values = read_env_file()
    groups: list[dict[str, Any]] = []
    by_name: dict[str, dict[str, Any]] = {}
    for field in CONFIG_FIELDS:
        entry = field.to_json()
        effective, source, origin = _effective_config(field.key, file_values)
        entry["inFile"] = field.key in file_values
        entry["source"] = source
        entry["sourceKey"] = origin
        entry["sourceLabel"] = CONFIG_FIELD_BY_KEY[origin].label if origin in CONFIG_FIELD_BY_KEY else origin
        entry["missing"] = bool(field.required and (source == "unset" or not effective.strip()))
        if field.type == "secret":
            # 密钥不回显：界面只拿到掩码，留空提交即「不修改」。
            entry["value"] = ""
            entry["effective"] = _mask_secret(effective) or "未配置"
        else:
            entry["value"] = file_values.get(field.key, "")
            entry["effective"] = effective
        group = by_name.get(field.group)
        if group is None:
            group = {"name": field.group, "fields": []}
            by_name[field.group] = group
            groups.append(group)
        group["fields"].append(entry)
    return {
        "envPath": str(ENV_PATH),
        "envExists": ENV_PATH.exists(),
        "groups": groups,
        "missing": [{"key": key, "label": label} for key, label in missing_required_config()],
    }


def update_config(values: dict[str, Any]) -> dict[str, Any]:
    """把界面提交的值写进 .env，并同步当前进程的环境变量。

    同步 ``os.environ`` 是必须的：阶段子进程由本进程派生、继承这份环境，而
    子进程里的 ``load_dotenv`` 默认不覆盖已存在的变量 —— 不写进 os.environ
    的话，刚改的值对下一次运行不生效。
    """
    updates: dict[str, str | None] = {}
    for key, raw in values.items():
        field = CONFIG_FIELD_BY_KEY.get(key)
        if field is None:
            continue  # 不认识的键直接忽略，避免界面把 .env 改坏
        value = str(raw if raw is not None else "").strip()
        if _ENV_FORBIDDEN_RE.search(value):
            raise ValueError(f"「{field.label}」不能包含换行或空字符。")
        if field.type == "int" and value and not value.isdigit():
            raise ValueError(f"「{field.label}」必须是整数。")
        if field.type == "secret" and not value:
            continue  # 留空 = 不改，避免把密钥误清空
        updates[key] = value or None  # 空 = 删掉这一行，回到默认

    if not updates:
        return describe_config()

    write_env_file(updates)
    for key, value in updates.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value
    return describe_config()


def resolve_tokenizer_path() -> Path:
    """当前生效的本地 tokenizer 路径。

    不能直接用 config 里的 ``QWEN_BASE_MODEL_PATH``：那是导入时的快照，
    界面上改完 .env 之后不会变。
    """
    raw = _effective_config("AGENT_QWEN_BASE_MODEL_PATH", read_env_file())[0]
    if not raw:
        return QWEN_BASE_MODEL_PATH
    path = Path(raw).expanduser()
    return path if path.is_absolute() else PROJECT_ROOT / path

# ---------------------------------------------------------------------------
# 全局工具库：agent/tools/<name>.py
#
# 与领域文件编辑的区别：
#   - 工具是**全局**的，不属于任何领域；一个工具可以被多个项目启用。
#   - 项目只记名字（domains/<name>/tools.json），schema 不复制，改一次全生效。
#   - 保存前除了语法，还要校验 SPEC 是字面量 dict 且 function.name == 文件名，
#     否则模型输出的工具名和 runner 查找的文件名会对不上。
# ---------------------------------------------------------------------------

GLOBAL_TOOLS_DIR = AGENT_ROOT / "tools"
TOOL_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{1,63}$")
_PARAM_TYPES = ("string", "number", "integer", "boolean", "array", "object")


def _tool_library():
    """延迟导入全局工具库（它只依赖标准库，不会拉起重依赖）。"""
    try:
        from .. import tools as library
    except ImportError:  # pragma: no cover - 兼容 ``python agent/ui/server.py``
        import sys

        sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
        import tools as library  # type: ignore

    return library


def validate_tool_name(name: str) -> str:
    cleaned = (name or "").strip().lower()
    if not TOOL_NAME_RE.match(cleaned):
        raise ValueError("工具名只能用小写字母、数字和下划线，字母开头，2-64 个字符。")
    return cleaned


def _locate_tool(name: str) -> Path:
    return GLOBAL_TOOLS_DIR / f"{validate_tool_name(name)}.py"


def _tool_enabled_by(tool_name: str) -> list[str]:
    library = _tool_library()
    return [item["name"] for item in find_domains() if tool_name in library.read_enabled(item["name"])]


def _validate_tool_text(name: str, text: str) -> None:
    """保存前校验：语法 -> SPEC 是字面量 -> 名字与文件名一致。"""
    try:
        tree = ast.parse(text, filename=f"{name}.py")
    except SyntaxError as exc:
        where = f"第 {exc.lineno} 行" if exc.lineno else ""
        raise ValueError(f"{name}.py 有语法错误：{where} {exc.msg}") from exc

    spec: Any = None
    found = False
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        if not any(isinstance(target, ast.Name) and target.id == "SPEC" for target in node.targets):
            continue
        found = True
        try:
            spec = ast.literal_eval(node.value)
        except (ValueError, SyntaxError) as exc:
            raise ValueError("SPEC 必须是纯字面量 dict（不能用函数调用或变量拼出来）。") from exc
        break

    if not found:
        raise ValueError("工具必须定义 SPEC（OpenAI function-calling 格式的 dict）。")
    if not isinstance(spec, dict):
        raise ValueError("SPEC 必须是 dict。")

    function = spec.get("function") if isinstance(spec.get("function"), dict) else spec
    spec_name = str(function.get("name") or "").strip()
    if spec_name != name:
        raise ValueError(
            f"SPEC 里的 function.name 必须等于文件名 {name!r}（当前是 {spec_name!r}），"
            "否则模型调用的名字和实际加载的文件对不上。"
        )
    if not isinstance(function.get("parameters"), dict):
        raise ValueError("SPEC 缺少 function.parameters。")


def _quote(value: Any) -> str:
    """源码里的字符串字面量：优先双引号，含双引号/换行时退回 repr。"""
    text = str(value)
    if '"' not in text and "\n" not in text and "\r" not in text:
        return f'"{text}"'
    return repr(text)


def _render_tool_skeleton(name: str, description: str, params: list[Any], mock: str) -> str:
    """生成一个可直接运行的工具体：SPEC + 可选 MOCK_RESPONSE。

    界面上的「新建」只传 name，描述 / 参数 / run() 都留给使用者自己补，所以骨架里
    每个空位都配一句「这里该写什么」。完整规范见 agent/tools/_template.py。
    """
    properties: list[str] = []
    required: list[str] = []
    for item in params or []:
        if not isinstance(item, dict):
            continue
        key = str(item.get("name") or "").strip()
        if not key:
            continue
        kind = str(item.get("type") or "string").strip().lower()
        if kind not in _PARAM_TYPES:
            kind = "string"
        note = str(item.get("description") or "").strip()
        properties.append(f'                {_quote(key)}: {{"type": {_quote(kind)}, "description": {_quote(note)}}},')
        if item.get("required"):
            required.append(key)

    doc = description.strip().replace('"""', "'''")
    if doc:
        head = [f'"""{doc}"""']
    else:
        # 描述为空时别拿工具名当 docstring —— 那等于什么都没写。
        head = [f'"""{name}', "", "一句话说明这个工具做什么、什么时候该用。", '"""']

    if properties:
        property_lines: list[str] = list(properties)
        required_line = f'            "required": [{", ".join(_quote(item) for item in required)}],'
    else:
        property_lines = [
            "                # 在这里逐个列出参数，每行一个：",
            '                # "参数名": {"type": "string", "description": "用途"},',
            "                # type 可用 string / number / integer / boolean / array / object",
        ]
        required_line = '            "required": [],  # 必填的参数名，例如 ["city"]'

    spec_lines = [
        "SPEC = {",
        '    "type": "function",',
        '    "function": {',
        f'        "name": {_quote(name)},',
    ]
    if doc:
        spec_lines.append(f'        "description": {_quote(description.strip())},')
    else:
        spec_lines += [
            "        # ↓ 必填：模型就是靠这句话决定要不要调用它，",
            "        #   写「什么时候该用」比写「它做什么」更重要。",
            '        "description": "",',
        ]
    spec_lines += [
        '        "parameters": {',
        '            "type": "object",',
        '            "properties": {',
        *property_lines,
        "            },",
        required_line,
        "        },",
        "    },",
        "}",
    ]

    mock_line = (
        f"MOCK_RESPONSE = {_quote(mock.strip())}"
        if mock.strip()
        else 'MOCK_RESPONSE = ""  # 没有 run() 时返回这段文本，{参数名} 会被调用参数替换'
    )

    lines = [
        *head,
        "",
        "from __future__ import annotations",
        "",
        # 预置 import os：需要凭据的工具直接用它读环境变量，不需要就删掉。
        "import os",
        "from typing import Any",
        "",
        "# 凭据统一放 agent/.env，代码里只写变量名 —— 写死的 key 会跟着仓库提交出去，",
        "# 而且换 key 得改代码、重启服务。命名约定 <服务名>_API_KEY。",
        "# 需要凭据时去掉下面这行的注释，并在 agent/.env 里填值：",
        '# ENV_API_KEY = "MY_SERVICE_API_KEY"',
        "",
        *spec_lines,
        "",
        mock_line,
        "",
        "# 实现真实逻辑时定义 run()（可选，定义了就不再走 MOCK_RESPONSE）。",
        "# 签名二选一：run(arguments, sample=None) 可读当前样本；run(arguments) 只依赖入参。",
        "# 需要 FatalToolError（欠费/断网/key 失效时让流水线停下）时，在函数内延迟导入：",
        "#     from agent.tools import FatalToolError",
        "# 完整说明见 agent/tools/_template.py。",
        "",
        "# 示例（把 query 换成你自己的参数名）：",
        "# def run(arguments: dict[str, Any], sample: dict[str, Any] | None = None) -> str:",
        "#     payload = arguments or {}",
        '#     query = str(payload.get("query") or "").strip()',
        "#     if not query:",
        '#         return "执行失败：缺少 query 参数。"',
        '#     return f"收到 query={query}。"',
        "",
    ]
    return "\n".join(lines)


def list_global_tools(domain: str | None = None) -> dict[str, Any]:
    """工具库清单 + 某个项目已启用的子集。"""
    library = _tool_library()
    items = library.list_tools()

    usage: dict[str, list[str]] = {}
    for entry in find_domains():
        for tool_name in library.read_enabled(entry["name"]):
            usage.setdefault(tool_name, []).append(entry["name"])
    for item in items:
        item["usedBy"] = usage.get(item["name"], [])

    target = (domain or "").strip().replace("-", "_")
    return {
        "tools": items,
        "enabled": library.read_enabled(target) if target else [],
        "domain": target,
        "dir": str(GLOBAL_TOOLS_DIR),
        "own": _domain_own_tools(target),
    }


# ---------------------------------------------------------------------------
# 项目独有工具（只读）
#
# 各项目除了从全局库勾选工具，还可能在自己的模块里直接定义工具
# （health 写在 prompts.py，health_talent / customer_service 写在 spec.py）。
# 这些工具**没有开关**：进了 DomainSpec.tools 就无条件生效，而且和全局工具
# 同名时会盖掉全局那个（merge_global_tools 里 taken 集合的作用）。
#
# 所以工具页要单独列一块出来，否则用户看到「共 7 个工具」会以为项目只用这 7 个。
# 这里只负责展示和定位源码，改代码仍然走领域文件编辑器。
# ---------------------------------------------------------------------------


def _spec_dict_name(node: ast.Dict) -> str:
    """从 dict 字面量里取工具名，兼容 ``{"name":..}`` 与 ``{"function":{"name":..}}``。"""
    for key, value in zip(node.keys, node.values):
        if not isinstance(key, ast.Constant):
            continue
        if key.value == "function" and isinstance(value, ast.Dict):
            nested = _spec_dict_name(value)
            if nested:
                return nested
        elif key.value == "name" and isinstance(value, ast.Constant) and isinstance(value.value, str):
            return value.value
    return ""


def _own_tool_sources(domain: str) -> dict[str, dict[str, Any]]:
    """静态扫项目目录，建 工具名 -> {path, line} 映射。

    只认顶层列表赋值里的元素，元素要么是 ``_tool("name", ...)`` 这类构造调用，
    要么是字面量 dict —— 这样既覆盖两种写法，又不会误抓普通函数调用。
    """
    root = domain_dir(domain)
    if not root.is_dir():
        return {}

    found: dict[str, dict[str, Any]] = {}
    for path in sorted(root.glob("*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
        except (SyntaxError, OSError):
            continue
        for node in tree.body:
            value = node.value if isinstance(node, (ast.Assign, ast.AnnAssign)) else None
            if not isinstance(value, ast.List):
                continue
            for element in value.elts:
                name = ""
                if isinstance(element, ast.Call) and element.args:
                    head = element.args[0]
                    if isinstance(head, ast.Constant) and isinstance(head.value, str):
                        name = head.value
                elif isinstance(element, ast.Dict):
                    name = _spec_dict_name(element)
                if name and TOOL_NAME_RE.match(name):
                    found.setdefault(name, {"path": path.name, "line": element.lineno})
    return found


def _fresh_domain_spec(domain: str) -> Any:
    """重新 import 项目定义，保证改完 spec.py 后工具页立刻能看到新内容。

    项目模块在 UI 进程里只用于展示，缓存住会让用户改完文件看不到变化，所以
    每次展示前把 ``agent.domains.<name>.*`` 整条链清掉重来。
    """
    import sys
    from importlib import import_module, invalidate_caches

    prefix = f"agent.domains.{domain}"
    for key in [key for key in sys.modules if key == prefix or key.startswith(prefix + ".")]:
        del sys.modules[key]
    invalidate_caches()
    return import_module(f"{prefix}.spec")


def _domain_own_tools(domain: str) -> dict[str, Any]:
    """项目自己定义的工具清单（不含从全局库勾选的）。"""
    if not domain:
        return {"tools": [], "error": ""}
    try:
        target = validate_domain_name(domain)
    except ValueError as exc:
        return {"tools": [], "error": str(exc)}

    try:
        spec = _fresh_domain_spec(target)
    except Exception as exc:  # noqa: BLE001 - 项目定义坏了不能让工具页整个挂掉
        return {"tools": [], "error": f"读取项目定义失败：{exc}"}

    owner = getattr(spec, "DOMAIN", None)
    if owner is None:
        return {"tools": [], "error": "项目定义里没有 DOMAIN。"}

    global_names = {item["name"] for item in _tool_library().list_tools()}
    sources = _own_tool_sources(target)

    items: list[dict[str, Any]] = []
    for entry in getattr(owner, "tools", ()) or ():
        function = entry.get("function") if isinstance(entry, dict) else None
        if not isinstance(function, dict):
            function = entry if isinstance(entry, dict) else None
        if not isinstance(function, dict):
            continue
        name = str(function.get("name") or "").strip()
        if not name:
            continue
        parameters = function.get("parameters")
        properties = parameters.get("properties") if isinstance(parameters, dict) else None
        items.append(
            {
                "name": name,
                "description": str(function.get("description") or "").strip(),
                "params": len(properties) if isinstance(properties, dict) else 0,
                # 和全局库重名 = 这个项目独有工具会盖掉全局那个。
                "shadows": name in global_names,
                "source": sources.get(name) or None,
            }
        )
    return {"tools": items, "error": ""}


def read_global_tool(name: str) -> dict[str, Any]:
    """读取一个工具的源码。"""
    target = validate_tool_name(name)
    path = _locate_tool(target)
    if not path.is_file():
        raise ValueError(f"工具 {target!r} 不存在。")

    stat = path.stat()
    if stat.st_size > _DOMAIN_EDIT_MAX_BYTES:
        raise ValueError(f"{target}.py 有 {_human_size(stat.st_size)}，超出在线编辑上限，请用本地编辑器。")

    text = path.read_text(encoding="utf-8", errors="replace")
    library = _tool_library()
    try:
        meta = library.describe_tool(target)
        meta.pop("spec", None)
    except ValueError as exc:
        meta = {"name": target, "error": str(exc)}

    return {
        "name": target,
        "path": str(path),
        "content": text,
        "size": stat.st_size,
        "mtime": stat.st_mtime,
        "lines": text.count("\n") + (0 if text.endswith("\n") else 1),
        "backups": _file_backups(GLOBAL_TOOLS_DIR, f"{target}.py", limit=5),
        "meta": meta,
        "usedBy": _tool_enabled_by(target),
    }


def write_global_tool(name: str, content: str) -> dict[str, Any]:
    """保存工具源码：校验 -> 备份 -> 原子写入。"""
    target = validate_tool_name(name)
    path = _locate_tool(target)
    if not path.is_file():
        raise ValueError(f"工具 {target!r} 不存在；新建工具请用「新建」按钮。")

    payload = len(content.encode("utf-8"))
    if payload > _DOMAIN_EDIT_MAX_BYTES:
        raise ValueError(f"内容有 {_human_size(payload)}，超出在线编辑上限。")
    _validate_tool_text(target, content)

    previous = path.read_text(encoding="utf-8", errors="replace")
    lines = content.count("\n") + (0 if content.endswith("\n") else 1)
    if previous == content:
        return {"name": target, "changed": False, "backup": "", "size": payload, "lines": lines}

    backup = _backup_file(GLOBAL_TOOLS_DIR, f"{target}.py", previous)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    tmp.replace(path)
    shutil.rmtree(GLOBAL_TOOLS_DIR / "__pycache__", ignore_errors=True)

    stat = path.stat()
    return {
        "name": target,
        "changed": True,
        "backup": backup,
        "size": stat.st_size,
        "mtime": stat.st_mtime,
        "lines": lines,
        "usedBy": _tool_enabled_by(target),
    }


def create_global_tool(
    name: str,
    description: str = "",
    params: list[Any] | None = None,
    mock: str = "",
) -> dict[str, Any]:
    """新建一个工具：先落一个能跑的骨架（SPEC + MOCK_RESPONSE），再按需加 run()。"""
    target = validate_tool_name(name)
    path = _locate_tool(target)
    if path.exists():
        raise ValueError(f"工具 {target!r} 已存在。")

    text = _render_tool_skeleton(target, description or "", list(params or []), mock or "")
    _validate_tool_text(target, text)

    GLOBAL_TOOLS_DIR.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)
    shutil.rmtree(GLOBAL_TOOLS_DIR / "__pycache__", ignore_errors=True)

    return {"name": target, "path": str(path), "content": text, "created": True}


def delete_global_tool(name: str) -> dict[str, Any]:
    """删除工具，并从各项目的启用清单里摘掉这个名字。"""
    target = validate_tool_name(name)
    path = _locate_tool(target)
    if not path.is_file():
        raise ValueError(f"工具 {target!r} 不存在。")

    library = _tool_library()
    released = _tool_enabled_by(target)
    for domain_name in released:
        remaining = [item for item in library.read_enabled(domain_name) if item != target]
        library.write_enabled(domain_name, remaining)

    path.unlink()
    shutil.rmtree(GLOBAL_TOOLS_DIR / "__pycache__", ignore_errors=True)
    return {"name": target, "deleted": True, "releasedFrom": released}


def global_tool_history(name: str) -> dict[str, Any]:
    target = validate_tool_name(name)
    if not _locate_tool(target).is_file():
        raise ValueError(f"工具 {target!r} 不存在。")
    return {"name": target, "backups": _file_backups(GLOBAL_TOOLS_DIR, f"{target}.py")}


def restore_global_tool(name: str, backup: str) -> dict[str, Any]:
    """恢复历史版本（当前内容同样先备份，恢复可逆）。"""
    target = validate_tool_name(name)
    path = _locate_tool(target)
    if not path.is_file():
        raise ValueError(f"工具 {target!r} 不存在。")

    source = _history_dir(GLOBAL_TOOLS_DIR, f"{target}.py") / Path(backup or "").name
    if not source.is_file():
        raise ValueError(f"历史版本 {backup!r} 不存在。")

    text = source.read_text(encoding="utf-8", errors="replace")
    _validate_tool_text(target, text)

    _backup_file(GLOBAL_TOOLS_DIR, f"{target}.py", path.read_text(encoding="utf-8", errors="replace"))
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)
    shutil.rmtree(GLOBAL_TOOLS_DIR / "__pycache__", ignore_errors=True)

    return {
        "name": target,
        "restored": source.name,
        "content": text,
        "size": path.stat().st_size,
        "mtime": path.stat().st_mtime,
    }


def set_domain_tools(domain: str, names: list[Any]) -> dict[str, Any]:
    """设置某个项目开放了哪些全局工具。"""
    target = validate_domain_name(domain)
    if not domain_dir(target).is_dir():
        raise ValueError(f"领域 {target!r} 不存在。")

    library = _tool_library()
    result = library.write_enabled(target, list(names or []))
    result["tools"] = library.read_enabled(target)
    return result


__all__ = [
    "ARTIFACTS",
    "ARTIFACT_BY_KEY",
    "CONFIG_FIELDS",
    "CONFIG_FIELD_BY_KEY",
    "DEFAULT_PIPELINE",
    "DOMAINS_DIR",
    "ENV_PATH",
    "OUTPUTS_DIR",
    "Param",
    "RunContext",
    "STAGES",
    "STAGE_BY_ID",
    "Artifact",
    "ConfigField",
    "Stage",
    "artifact_path",
    "build_command",
    "coerce_params",
    "count_lines",
    "create_domain",
    "create_global_tool",
    "dataset_splits",
    "default_domain",
    "default_prefix",
    "delete_domain",
    "delete_global_tool",
    "describe_config",
    "describe_domain",
    "domain_dir",
    "domain_file_history",
    "domain_output_dir",
    "domain_templates",
    "duplicate_domain",
    "find_domains",
    "global_tool_history",
    "list_domain_files",
    "list_domains",
    "list_global_tools",
    "read_domain_file",
    "read_env_file",
    "read_global_tool",
    "rename_domain",
    "resolve_tokenizer_path",
    "restore_domain_file",
    "restore_global_tool",
    "seed_path",
    "set_domain_tools",
    "stage_file",
    "update_config",
    "validate_domain_name",
    "validate_tool_name",
    "write_domain_file",
    "write_env_file",
    "write_global_tool",
]
