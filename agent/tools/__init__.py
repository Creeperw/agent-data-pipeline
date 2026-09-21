"""Global tool library shared across domains.

A tool is one ``.py`` file in this directory exporting:

``SPEC`` (required)
    OpenAI function-calling schema as a **plain literal** ``dict``. The UI and
    the loader read it through :mod:`ast` without executing the file, so it
    cannot be produced by a function call.

``MOCK_RESPONSE`` (optional)
    Text returned when the tool has no ``run`` implementation. ``{arg}``
    placeholders are filled from the call arguments; unknown placeholders are
    left untouched.

``run(arguments, sample=None)`` (optional)
    Real implementation, returns a string. When present the tool is executed
    for real; otherwise the mock text is returned.

Tool modules must not import ``agent.core`` or any heavy dependency, because
``load_domain`` reads them while a domain is being loaded.

Credentials never belong in a tool file. Read them from the environment with
``os.getenv("<SERVICE>_API_KEY")``; the values live in ``agent/.env`` and can be
edited from the console's Tools page. A key committed to the source
tree cannot be taken back, and rotating it would mean editing code.

Start a new tool by copying ``_template.py`` — it is skipped by the loader (the
leading underscore) and documents the contract, both failure modes, and the
credential pattern inline.
"""

from __future__ import annotations

import ast
import importlib.util
import inspect
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

try:
    from agent.config import AGENT_ROOT
except Exception:  # pragma: no cover - direct static loading fallback
    AGENT_ROOT = Path(__file__).resolve().parent.parent

TOOLS_DIR = AGENT_ROOT / "tools"
DOMAINS_DIR = AGENT_ROOT / "domains"

# 工具凭据统一放在 agent/.env。这里显式加载一次，让「工具试运行」「单独 import 工具」
# 这类不走 agent.config 的调用路径也读得到 key，否则 os.getenv 会静默拿到空值，表现
# 成「明明配了却说未配置」。
#
# 路径写死不走 find_dotenv()：后者靠调用栈推断起点，在 python -c / 交互式环境下会找
# 不到文件。override 保持默认的 False，流水线里已导出的环境变量仍然优先。
load_dotenv(AGENT_ROOT / ".env")

# Each domain stores the subset of the library it opens up in this file.
ENABLED_FILENAME = "tools.json"

# Tool file names double as the function name, so keep them API-safe.
TOOL_NAME_MAX = 64


@dataclass(frozen=True)
class ToolResult:
    """Outcome of a tool call, mirroring the per-domain ``ToolResult``.

    The pipeline reads ``name`` and ``content`` off whatever the domain's
    ``tool_runner`` returns (see ``generator`` and ``executor_generator``), so a
    global tool has to hand back the same shape as a domain-owned one instead of
    a bare string.
    """

    name: str
    content: str


class FatalToolError(RuntimeError):
    """工具用它把整条流水线停下来，而不是只记一条失败观测。

    ``run_tool`` 默认把任何异常都转成工具观测文本（工具失败本身是一种观测），
    但欠费 / 断网 / key 失效这类问题继续跑只会白烧教师模型额度。工具抛本异常
    即可让 ``DomainSpec.is_fatal_tool_error`` 判定为致命，``generator`` 会取消
    其余任务并以 ``SystemExit(2)`` 结束，已写入的样本仍可断点续跑。

    工具模块在 ``run()`` 里延迟导入即可，不要在模块顶层导入（加载期是循环依赖）:

        def run(arguments, sample=None):
            from agent.tools import FatalToolError
            ...
            raise FatalToolError("Exa 调用失败：...", query=query)
    """

    def __init__(
        self,
        message: str,
        *,
        query: str | None = None,
        original: BaseException | None = None,
    ) -> None:
        super().__init__(message)
        self.query = query
        self.original = original


def _tool_files() -> list[Path]:
    if not TOOLS_DIR.is_dir():
        return []
    return sorted(
        item
        for item in TOOLS_DIR.glob("*.py")
        if item.is_file() and not item.name.startswith("_")
    )


def _literal_assignment(tree: ast.Module, name: str) -> tuple[bool, Any]:
    """Return ``(found, value)`` for a module-level ``NAME = <literal>``."""
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            if isinstance(target, ast.Name) and target.id == name:
                try:
                    return True, ast.literal_eval(node.value)
                except (ValueError, SyntaxError):
                    return True, None
    return False, None


def _has_function(tree: ast.Module, name: str) -> bool:
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return True
    return False


def describe_tool(name: str) -> dict[str, Any]:
    """Static metadata for one tool, parsed without executing it.

    Raises ``ValueError`` when the file is missing or its ``SPEC`` cannot be
    read as a literal.
    """

    path = TOOLS_DIR / f"{name}.py"
    if not path.is_file():
        raise ValueError(f"工具 {name!r} 不存在。")

    try:
        source = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ValueError(f"无法读取工具 {name!r}：{exc}") from exc

    try:
        tree = ast.parse(source, filename=path.name)
    except SyntaxError as exc:
        where = f"第 {exc.lineno} 行" if exc.lineno else ""
        raise ValueError(f"{name}.py 有语法错误：{where} {exc.msg}") from exc

    found, spec = _literal_assignment(tree, "SPEC")
    if not found:
        raise ValueError(f"{name}.py 里没有 SPEC，无法识别为工具。")
    if not isinstance(spec, dict):
        raise ValueError(f"{name}.py 的 SPEC 必须是字面量 dict（不能用函数调用生成）。")

    function = spec.get("function") if isinstance(spec.get("function"), dict) else spec
    spec_name = str(function.get("name") or "").strip()
    description = str(function.get("description") or "").strip()
    parameters = function.get("parameters")
    properties = parameters.get("properties") if isinstance(parameters, dict) else None
    required = parameters.get("required") if isinstance(parameters, dict) else None

    has_mock = _literal_assignment(tree, "MOCK_RESPONSE")[0]
    stat = path.stat()
    return {
        "name": name,
        "spec": spec,
        "specName": spec_name,
        "description": description,
        "params": len(properties) if isinstance(properties, dict) else 0,
        "required": len(required) if isinstance(required, list) else 0,
        "paramNames": list(properties) if isinstance(properties, dict) else [],
        "hasRun": _has_function(tree, "run"),
        "hasMock": has_mock,
        "size": stat.st_size,
        "mtime": stat.st_mtime,
        "path": str(path),
    }


def list_tools() -> list[dict[str, Any]]:
    """Describe every tool in the library; broken files are reported, not raised."""
    items: list[dict[str, Any]] = []
    for path in _tool_files():
        name = path.stem
        try:
            item = describe_tool(name)
            item.pop("spec", None)  # 列表不需要完整 schema，避免 payload 过大
            items.append(item)
        except ValueError as exc:
            items.append(
                {
                    "name": name,
                    "specName": "",
                    "description": "",
                    "params": 0,
                    "required": 0,
                    "paramNames": [],
                    "hasRun": False,
                    "hasMock": False,
                    "size": path.stat().st_size if path.exists() else 0,
                    "mtime": path.stat().st_mtime if path.exists() else 0.0,
                    "path": str(path),
                    "error": str(exc),
                }
            )
    items.sort(key=lambda item: (bool(item.get("error")), item["name"]))
    return items


def tool_names() -> list[str]:
    return [path.stem for path in _tool_files()]


def load_module(name: str) -> Any:
    """Import a tool module, executing its top-level code."""

    path = TOOLS_DIR / f"{name}.py"
    if not path.is_file():
        raise ValueError(f"工具 {name!r} 不存在。")

    spec = importlib.util.spec_from_file_location(f"agent_tool_{name}", path)
    if spec is None or spec.loader is None:
        raise ValueError(f"无法加载工具 {name!r}。")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def tool_spec(name: str) -> dict[str, Any]:
    """The OpenAI tool schema, ready to hand to the model."""
    return describe_tool(name)["spec"]


def tool_specs(names: list[str] | None = None) -> list[dict[str, Any]]:
    """Schemas for the requested tools (all of them when ``names`` is None)."""
    selected = list(names) if names is not None else tool_names()
    specs: list[dict[str, Any]] = []
    for name in selected:
        try:
            specs.append(tool_spec(name))
        except (ValueError, SyntaxError, OSError):
            continue
    return specs


# ---------------------------------------------------------------------------
# 项目启用清单：<领域>/tools.json  ->  {"enabled": ["calculate_bmi", ...]}
#
# 只记工具名，不复制 schema —— 工具改一次，所有启用它的项目同步生效。
# 文件不存在等价于「一个都没开」，所以老项目不受影响。
# ---------------------------------------------------------------------------


def enabled_file(domain: str) -> Path:
    return DOMAINS_DIR / str(domain or "").strip().replace("-", "_") / ENABLED_FILENAME


def read_enabled(domain: str) -> list[str]:
    """工具名列表，按文件里的顺序；文件损坏时返回空表而不是抛错。"""
    path = enabled_file(domain)
    if not path.is_file():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []

    raw = payload.get("enabled") if isinstance(payload, dict) else payload
    if not isinstance(raw, list):
        return []

    known = set(tool_names())
    seen: list[str] = []
    for item in raw:
        name = str(item or "").strip()
        if name and name in known and name not in seen:
            seen.append(name)
    return seen


def write_enabled(domain: str, names: list[str]) -> dict[str, Any]:
    """Persist the enabled subset; an empty list removes the file."""
    path = enabled_file(domain)
    known = set(tool_names())
    cleaned: list[str] = []
    for item in names or []:
        name = str(item or "").strip()
        if name in known and name not in cleaned:
            cleaned.append(name)

    if not cleaned:
        if path.is_file():
            try:
                path.unlink()
            except OSError as exc:
                raise ValueError(f"无法删除 {path.name}：{exc}") from exc
        return {"domain": domain, "enabled": [], "removed": True}

    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps({"enabled": cleaned}, ensure_ascii=False, indent=2) + "\n"
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)
    return {"domain": domain, "enabled": cleaned, "removed": False}


def enabled_specs(domain: str) -> list[dict[str, Any]]:
    """Schemas of the tools a domain has opened up."""
    return tool_specs(read_enabled(domain))


__all__ = [
    "DOMAINS_DIR",
    "ENABLED_FILENAME",
    "TOOLS_DIR",
    "ToolResult",
    "describe_tool",
    "enabled_file",
    "enabled_specs",
    "list_tools",
    "load_module",
    "read_enabled",
    "render_mock",
    "run_tool",
    "tool_names",
    "tool_spec",
    "tool_specs",
    "write_enabled",
]


class _Placeholders(dict):
    def __missing__(self, key: str) -> str:
        return "{" + key + "}"


def render_mock(name: str, arguments: dict[str, Any]) -> str:
    """Mock text for a tool without ``run``; falls back to a generic line."""
    template = ""
    try:
        module = load_module(name)
        template = str(getattr(module, "MOCK_RESPONSE", "") or "")
    except Exception:  # noqa: BLE001 - a broken mock must not break the pipeline
        template = ""

    values = _Placeholders({key: value for key, value in (arguments or {}).items()})
    if template:
        try:
            return template.format_map(values)
        except (ValueError, IndexError):
            return template

    if arguments:
        rendered = "；".join(f"{key}={value}" for key, value in arguments.items())
        return f"（模拟结果）{name} 已执行：{rendered}"
    return f"（模拟结果）{name} 已执行。"


def run_tool(
    name: str,
    arguments: dict[str, Any] | None = None,
    sample: dict[str, Any] | None = None,
) -> ToolResult:
    """Execute a global tool: real ``run`` when present, mock text otherwise.

    Never raises, with one exception: :class:`FatalToolError` propagates so the
    pipeline can halt on unrecoverable backend problems (quota, network, bad
    key) instead of writing degraded output into the dataset.
    """
    payload = arguments if isinstance(arguments, dict) else {}
    try:
        module = load_module(name)
    except ValueError:
        return ToolResult(name, f"工具 {name} 不存在，无法返回结果。")
    except Exception as exc:  # noqa: BLE001 - surface import errors as tool output
        return ToolResult(name, f"工具 {name} 加载失败：{exc}")

    runner = getattr(module, "run", None)
    if not callable(runner):
        return ToolResult(name, render_mock(name, payload))

    try:
        parameter_count = len(inspect.signature(runner).parameters)
    except (TypeError, ValueError):
        parameter_count = 1

    try:
        result = runner(payload, sample) if parameter_count >= 2 else runner(payload)
    except FatalToolError:
        raise
    except Exception as exc:  # noqa: BLE001 - tool failures are observations
        return ToolResult(name, f"工具 {name} 执行失败：{exc}")

    content = getattr(result, "content", None)
    return ToolResult(name, str(content if content is not None else result))


__all__ = [
    "FatalToolError",
    "TOOLS_DIR",
    "describe_tool",
    "list_tools",
    "load_module",
    "render_mock",
    "run_tool",
    "tool_names",
    "tool_spec",
    "tool_specs",
]
