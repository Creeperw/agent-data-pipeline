"""核对 UI 生成的命令行参数是否都被对应脚本接受。

做法：用 ast 解析每个 ``agent/*.py``，收集 argparse 里注册过的所有选项字符串，
再让 registry 为每个阶段生成一条「所有参数都填满」的命令，逐项比对。

有两种容易误判的情况需要额外处理：

* 脚本通过 ``from .other import parse_args`` 复用别的模块的 parser
  （例如 ``downsample_executor_data`` 复用 ``downsample_intent_data``）；
* 脚本根本没有 argparse，输入输出路径来自 ``config.py`` 常量
  （例如 ``rag_summary_data``）——这时 UI 也必须一个参数都不传。

用法：

    python agent/ui/_check_params.py
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from agent.ui.registry import STAGES, RunContext, build_command  # noqa: E402

AGENT_ROOT = Path(__file__).resolve().parents[1]


def _module_path(module: str) -> Path:
    return AGENT_ROOT / f"{module.split('.')[-1]}.py"


def _add_argument_options(path: Path) -> set[str]:
    """收集文件里 argparse ``add_argument`` 注册过的选项字符串。"""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    options: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
        if name != "add_argument":
            continue
        for arg in node.args:
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str) and arg.value.startswith("-"):
                options.add(arg.value)
    return options


def _reused_parse_args(path: Path) -> set[str]:
    """找到 ``from .x import parse_args`` 的来源模块名。"""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            for alias in node.names:
                if alias.name == "parse_args":
                    names.add(node.module.split(".")[-1])
    return names


def script_options(module: str, _seen: set[str] | None = None) -> tuple[set[str], bool]:
    """返回 ``(脚本接受的选项集合, 脚本是否有 argparse)``。"""
    seen = _seen if _seen is not None else set()
    name = module.split(".")[-1]
    if name in seen:
        return set(), False
    seen.add(name)

    path = _module_path(module)
    if not path.exists():
        return set(), False

    options = _add_argument_options(path)
    if options:
        return options, True

    # 本文件没有 argparse：看它是否复用了别的模块的 parse_args
    for source in _reused_parse_args(path):
        inherited, has_parser = script_options(f"agent.{source}", seen)
        if has_parser:
            options |= inherited
    return options, bool(options)


def sample_value(param) -> object:
    """给参数编一个能被 argparse 接受的样例值。"""
    if param.type == "bool":
        return True
    if param.type == "int":
        return 1
    if param.type == "float":
        return 0.5
    if param.type == "choice":
        return param.choices[0] if param.choices else "x"
    if param.type == "path":
        return "/tmp/ui-param-check"
    if param.type == "text":
        return "{}"
    return "x"


def main() -> int:
    problems: list[str] = []
    for stage in STAGES:
        values = {param.key: sample_value(param) for param in stage.params}
        ctx = RunContext(
            domain="health_talent",
            prefix="health_talent_valid",
            python=sys.executable,
            seed_input=AGENT_ROOT / "domains/health_talent/seeds.jsonl",
            output_dir=AGENT_ROOT / "outputs/health_talent",
            values={stage.id: values},
        )
        command = build_command(stage.id, ctx)
        accepted, has_parser = script_options(stage.module)
        used = [token for token in command if token.startswith("--")]

        if not has_parser:
            # 脚本自己固定输入输出，UI 也必须一个参数都不传
            if used:
                problems.append(f"{stage.id}: {stage.module} 没有 argparse，但 UI 传了 {used}")
            label = "OK " if not used else "BAD"
            print(f"{label} {stage.id:22s} {stage.module:38s} 无 argparse，传参 {len(used)}")
            continue

        unknown = [flag for flag in used if flag not in accepted]
        label = "OK " if not unknown else "BAD"
        print(f"{label} {stage.id:22s} {stage.module:38s} flags={len(used)}")
        for flag in unknown:
            print(f"      ✗ 脚本不接受：{flag}")
            problems.append(f"{stage.id}: {flag} 不在 {stage.module} 的 argparse 里")

    print()
    if problems:
        print(f"发现 {len(problems)} 处不匹配：")
        for item in problems:
            print("  -", item)
        return 1
    print("全部阶段的可选参数都能被对应脚本接受。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
