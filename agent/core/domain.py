"""Domain specification primitives for generalized agent data synthesis.

The core package must stay domain-agnostic: no health-management intents,
templates, tools, or safety rules should be hard-coded here. A domain package
under ``agent.domains.<name>`` provides a ``DOMAIN`` object instead.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from importlib import import_module
from typing import Any, Callable


ExecutorUserPromptBuilder = Callable[[str, str | None, str, str | None, str | None], str]
ToolRunner = Callable[[str, dict[str, Any], dict[str, Any] | None], Any]
SampleContextSetter = Callable[[dict[str, Any] | None], None]


@dataclass(frozen=True)
class DomainSpec:
    """A pluggable domain pack for planner/executor synthesis.

    ``intent`` and ``answer_template`` are optional capabilities. Domains that
    do not need them should set ``has_intent=False`` or
    ``has_answer_template=False`` and leave the corresponding mappings empty.
    The planner action protocol is intentionally shared across all domains.
    """

    name: str
    display_name: str
    planner_system_prompt: str
    executor_system_prompt: str
    executor_assembly_system_prompt: str | None = None
    tools: list[dict[str, Any]] = field(default_factory=list)
    has_intent: bool = False
    has_answer_template: bool = False
    has_clarification: bool = True
    intent_taxonomy: dict[str, str] = field(default_factory=dict)
    intent_aliases: dict[str, str] = field(default_factory=dict)
    answer_templates: dict[str, str] = field(default_factory=dict)
    planner_action_space: tuple[str, ...] = ("tool_call", "planning_finish", "ask_clarification")
    eval_config: dict[str, Any] = field(default_factory=dict)
    executor_user_prompt_builder: ExecutorUserPromptBuilder | None = None
    tool_runner: ToolRunner | None = None
    sample_context_setter: SampleContextSetter | None = None
    fatal_tool_error_types: tuple[type[BaseException], ...] = field(default_factory=tuple)
    tool_selection: dict[str, Any] = field(default_factory=dict)

    def intent_names(self) -> list[str]:
        return list(self.intent_taxonomy)

    def default_intent(self) -> str | None:
        if not self.has_intent:
            return None
        if "其他" in self.intent_taxonomy:
            return "其他"
        return next(iter(self.intent_taxonomy), None)

    def resolve_intent(self, intent: Any, *, fallback_to_default: bool = True) -> str | None:
        """Resolve a raw intent label through this domain's taxonomy.

        Aliases are domain-owned. The generic generator should not know about
        health-specific labels such as ``食疗咨询`` or ``体质辨识``.
        """

        if not self.has_intent:
            return None
        raw = str(intent or "").strip()
        if not raw:
            return None
        canonical = self.intent_aliases.get(raw, raw)
        if canonical in self.intent_taxonomy:
            return canonical
        return self.default_intent() if fallback_to_default else None

    def get_answer_template(self, intent: str | None = None) -> str:
        if not self.has_answer_template:
            return ""
        key = (intent or self.default_intent() or "").strip()
        return self.answer_templates.get(key) or self.answer_templates.get("其他", "")

    def build_executor_user_prompt(
        self,
        *,
        user_query: str,
        intent: str | None = None,
        external_info: str = "",
        answer_template: str | None = None,
        clarification_ask: str | None = None,
    ) -> str:
        """Render the executor user prompt for this domain.

        Domain packs may provide a custom builder to preserve an existing
        training distribution exactly. Otherwise a generic optional-field
        renderer is used.
        """

        if self.executor_user_prompt_builder is not None:
            resolved_intent = intent or self.default_intent() or ""
            return self.executor_user_prompt_builder(
                user_query,
                resolved_intent,
                external_info,
                answer_template,
                clarification_ask,
            )

        sections = [f"【用户问题】\n{user_query.strip()}"]
        if self.has_intent:
            sections.append(f"【识别意图】\n{(intent or self.default_intent() or '').strip()}")
        if self.has_answer_template:
            template = (answer_template if answer_template is not None else self.get_answer_template(intent)).strip()
            sections.append(f"【回答建议】\n{template or '无特定回答模板，请按通用要求作答。'}")
        if self.has_clarification and clarification_ask:
            sections.append(f"【规划阶段追问建议】\n{clarification_ask.strip()}")
        sections.append(f"【外部参考信息】\n{external_info.strip() or '无外部补充信息。'}")
        return "\n\n".join(sections) + "\n"

    def run_tool(self, name: str, arguments: dict[str, Any], sample: dict[str, Any] | None = None) -> Any:
        """Run a domain-specific tool if a runner is provided."""

        if self.tool_runner is None:
            raise KeyError(f"domain {self.name!r} does not define tool_runner for {name!r}")
        return self.tool_runner(name, arguments, sample)

    def prepare_sample_context(self, sample: dict[str, Any] | None = None) -> None:
        """Let a domain initialize per-sample tool context before synthesis."""

        if self.sample_context_setter is not None:
            self.sample_context_setter(sample)

    def is_fatal_tool_error(self, exc: BaseException) -> bool:
        return bool(self.fatal_tool_error_types and isinstance(exc, self.fatal_tool_error_types))

    def tool_intent_hint(self, tool_name: str) -> str | None:
        hints = self.tool_selection.get("tool_intent_hints") or {}
        return self.resolve_intent(hints.get(tool_name), fallback_to_default=False)

    def required_tool_names(self, sample: dict[str, Any], catalog: dict[str, dict[str, Any]]) -> list[str]:
        """Return domain-preferred tool names for a sample.

        The default implementation is data-driven and works for non-health
        domains: samples may provide ``required_tools`` / ``reference_tool_names``
        / ``expected_tool_names``. Domain packs can additionally configure
        ``tool_selection.intent_required_tools`` and
        ``tool_selection.scenario_required_tools``.
        """

        required_names: list[str] = []

        def add(name: Any) -> None:
            tool_name = str(name or "").strip()
            if tool_name and tool_name in catalog and tool_name not in required_names:
                required_names.append(tool_name)

        for field in ("required_tools", "reference_tool_names", "expected_tool_names"):
            value = sample.get(field)
            if isinstance(value, str):
                add(value)
            elif isinstance(value, list):
                for item in value:
                    add(item)

        intent_value = self.resolve_intent(sample.get("expected_intent") or sample.get("intent"), fallback_to_default=False) or ""
        intent_rules = self.tool_selection.get("intent_required_tools") or {}
        for name in intent_rules.get(intent_value, []):
            add(name)

        scenario = str(sample.get("scenario") or "")
        scenario_rules = self.tool_selection.get("scenario_required_tools") or {}
        for scenario_key, names in scenario_rules.items():
            if str(scenario_key) in scenario:
                for name in names:
                    add(name)

        default_tools = self.tool_selection.get("default_required_tools") or []
        if not required_names:
            for name in default_tools:
                add(name)

        return required_names

    def related_tool_names(self, sample: dict[str, Any], catalog: dict[str, dict[str, Any]]) -> set[str]:
        related = set(self.required_tool_names(sample, catalog))
        intent_value = self.resolve_intent(sample.get("expected_intent") or sample.get("intent"), fallback_to_default=False) or ""
        intent_rules = self.tool_selection.get("intent_required_tools") or {}
        for name in intent_rules.get(intent_value, []):
            if name in catalog:
                related.add(name)
        return related

    def is_plan_only_tool_allowed(self, name: str, required_names: set[str]) -> bool:
        plan_only = set(self.tool_selection.get("plan_only_tools") or [])
        return name not in plan_only or name in required_names

    def file_write_plan_tool_names(self) -> set[str]:
        names = self.tool_selection.get("file_write_plan_tools") or []
        return {str(name).strip() for name in names if str(name).strip()}

    def file_write_tool_name(self) -> str | None:
        name = str(self.tool_selection.get("file_write_tool") or "").strip()
        return name or None

    def file_write_default(self, key: str, fallback: str = "") -> str:
        defaults = self.tool_selection.get("file_write_defaults") or {}
        return str(defaults.get(key) or fallback).strip()


def _spec_tool_name(spec: Any) -> str:
    """工具名：兼容 ``{"type":"function","function":{...}}`` 和裸 ``{"name":...}``。"""
    if not isinstance(spec, dict):
        return ""
    function = spec.get("function")
    if isinstance(function, dict):
        return str(function.get("name") or "").strip()
    return str(spec.get("name") or "").strip()


def merge_global_tools(domain: DomainSpec, normalized_name: str) -> DomainSpec:
    """Append the global-library tools this project has opened up.

    The domain's own tools always win: a global tool with the same name is
    skipped so an existing pipeline can never be changed by a library edit.
    Returns the original spec untouched when nothing is enabled.
    """

    try:
        from agent.tools import FatalToolError, read_enabled, run_tool, tool_specs
    except Exception:  # noqa: BLE001 - a missing/broken library must not block a run
        return domain

    try:
        enabled = read_enabled(normalized_name)
    except Exception:  # noqa: BLE001
        return domain
    if not enabled:
        return domain

    taken = {name for name in (_spec_tool_name(item) for item in domain.tools) if name}
    extra: list[dict[str, Any]] = []
    added: set[str] = set()
    for spec in tool_specs(enabled):
        name = _spec_tool_name(spec)
        if not name or name in taken or name in added:
            continue
        added.add(name)
        extra.append(spec)
    if not extra:
        return domain

    original_runner = domain.tool_runner

    def merged_runner(name: str, arguments: dict[str, Any], sample: dict[str, Any] | None = None) -> Any:
        if name in added:
            return run_tool(name, arguments, sample)
        if original_runner is None:
            raise KeyError(f"domain {domain.name!r} does not define tool_runner for {name!r}")
        return original_runner(name, arguments, sample)

    # 全局工具抛 FatalToolError 时同样要中断数据集生成，所以把它并进领域的致命
    # 错误集合，generator / executor_generator 的 is_fatal_tool_error 才认。
    fatal = domain.fatal_tool_error_types
    if FatalToolError not in fatal:
        fatal = (*fatal, FatalToolError)

    return replace(
        domain,
        tools=[*domain.tools, *extra],
        tool_runner=merged_runner,
        fatal_tool_error_types=fatal,
    )


def load_domain(name: str = "health") -> DomainSpec:
    """Load ``agent.domains.<name>.spec.DOMAIN`` and open up enabled global tools."""

    normalized = (name or "health").strip().replace("-", "_")
    module = import_module(f"agent.domains.{normalized}.spec")
    domain = getattr(module, "DOMAIN", None)
    if not isinstance(domain, DomainSpec):
        raise TypeError(f"agent.domains.{normalized}.spec.DOMAIN must be a DomainSpec")
    return merge_global_tools(domain, normalized)