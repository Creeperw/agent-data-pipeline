"""FastAPI 服务端：为 agent 数据流水线提供本地 Web 控制台。

所有路径都由 ``registry`` 从「领域 + 前缀」派生，接口不接受任意磁盘路径，
因此不存在路径穿越问题。
"""

from __future__ import annotations

import asyncio
import json
import shutil
import sys
import time
from pathlib import Path
from typing import Any

from fastapi import BackgroundTasks, Body, FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

try:  # 正常以 ``agent.ui.server`` 导入
    from ..config import AGENT_ROOT, OUTPUT_DOMAIN_NAME, PROJECT_ROOT
    from . import __version__, restart
    from .jobs import JobManager, stream_job
    from .registry import (
        ARTIFACTS,
        ARTIFACT_BY_KEY,
        DEFAULT_PIPELINE,
        STAGES,
        STAGE_BY_ID,
        RunContext,
        Stage,
        artifact_path,
        coerce_params,
        count_lines,
        create_domain,
        create_global_tool,
        create_role,
        dataset_splits,
        default_domain,
        default_prefix,
        delete_domain,
        delete_global_tool,
        describe_config,
        describe_domain,
        describe_env_file,
        domain_dir,
        domain_file_history,
        domain_output_dir,
        domain_templates,
        duplicate_domain,
        find_domains,
        global_tool_history,
        list_domain_files,
        list_domains,
        list_global_tools,
        list_roles,
        list_tasks,
        migrate_legacy_files,
        missing_required_config,
        read_domain_file,
        read_global_tool,
        read_role,
        rename_domain,
        resolve_tokenizer_path,
        restore_domain_file,
        restore_global_tool,
        seed_path,
        set_domain_tools,
        update_config,
        update_env_file,
        write_domain_file,
        write_global_tool,
        write_role,
        delete_role,
        role_templates,
    )
    from .registry import DOMAINS_DIR, OUTPUTS_DIR
    from .runs import load_runs, record_run
except ImportError:  # pragma: no cover - 兼容 ``python agent/ui/server.py``
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from agent.config import AGENT_ROOT, OUTPUT_DOMAIN_NAME, PROJECT_ROOT  # type: ignore
    from agent.ui import __version__, restart  # type: ignore
    from agent.ui.jobs import JobManager, stream_job  # type: ignore
    from agent.ui.registry import (  # type: ignore
        ARTIFACTS,
        ARTIFACT_BY_KEY,
        DEFAULT_PIPELINE,
        STAGES,
        STAGE_BY_ID,
        RunContext,
        Stage,
        artifact_path,
        coerce_params,
        count_lines,
        create_domain,
        create_global_tool,
        create_role,
        dataset_splits,
        default_domain,
        default_prefix,
        delete_domain,
        delete_global_tool,
        describe_config,
        describe_domain,
        describe_env_file,
        domain_dir,
        domain_file_history,
        domain_output_dir,
        domain_templates,
        duplicate_domain,
        find_domains,
        global_tool_history,
        list_domain_files,
        list_domains,
        list_global_tools,
        list_roles,
        list_tasks,
        migrate_legacy_files,
        missing_required_config,
        read_domain_file,
        read_global_tool,
        read_role,
        rename_domain,
        resolve_tokenizer_path,
        restore_domain_file,
        restore_global_tool,
        seed_path,
        set_domain_tools,
        update_config,
        update_env_file,
        write_domain_file,
        write_global_tool,
        write_role,
        delete_role,
        role_templates,
    )
    from agent.ui.registry import DOMAINS_DIR, OUTPUTS_DIR  # type: ignore
    from agent.ui.runs import load_runs, record_run  # type: ignore


STATIC_DIR = Path(__file__).resolve().parent / "static"
if getattr(sys, "frozen", False):
    STATIC_DIR = Path(getattr(sys, "_MEIPASS", STATIC_DIR)) / "agent" / "ui" / "static"
PREVIEW_MAX_CHARS = 40_000
PREVIEW_MAX_ROWS = 200
SEED_EDIT_MAX_ROWS = 3000
SEED_EDIT_MAX_BYTES = 16 * 1024 * 1024


class _NoCacheStaticFiles(StaticFiles):
    """静态资源每次回源校验，改了前端普通刷新就能生效。

    ``StaticFiles`` 默认只发 ``last-modified`` + ``etag``，浏览器会启发式缓存，
    于是改完 JS 后按 F5 仍可能跑旧代码，看着像「改了没用」。

    ``no-cache`` 不是「不缓存」，而是「每次带 etag 回源校验」：内容没变仍返回
    304，不会真的重传，只是多一次极轻的请求。
    """

    def file_response(self, *args: Any, **kwargs: Any):  # type: ignore[override]
        response = super().file_response(*args, **kwargs)
        response.headers["Cache-Control"] = "no-cache"
        return response

_line_cache: dict[tuple[str, int, int], int] = {}


# ---------------------------------------------------------------------------
# 文件状态辅助
# ---------------------------------------------------------------------------


def _count_lines(path: Path) -> int:
    """带缓存的快速行数统计（按 mtime + size 失效）。"""
    try:
        stat = path.stat()
    except OSError:
        return 0
    cache_key = (str(path), stat.st_size, stat.st_mtime_ns)
    cached = _line_cache.get(cache_key)
    if cached is not None:
        return cached
    total = count_lines(path)
    if len(_line_cache) > 512:  # 简单清理，避免无限增长
        _line_cache.clear()
    _line_cache[cache_key] = total
    return total


def _file_status(path: Path) -> dict[str, Any]:
    status: dict[str, Any] = {"path": str(path), "exists": False}
    if not path.exists():
        return status
    try:
        stat = path.stat()
    except OSError:
        return status
    status.update(
        {
            "exists": True,
            "size": stat.st_size,
            "mtime": stat.st_mtime,
            "isDir": path.is_dir(),
        }
    )
    if path.is_file() and path.suffix in {".jsonl", ".json", ".md"}:
        status["lines"] = _count_lines(path)
    return status


def _artifact_status(key: str, domain: str, prefix: str) -> dict[str, Any]:
    artifact = ARTIFACT_BY_KEY[key]
    status = _file_status(artifact_path(key, domain, prefix))
    status.update({"key": key, "label": artifact.label, "kind": artifact.kind, "description": artifact.description})
    return status


def _safe_domain(domain: str) -> str:
    from .registry import safe_path_part  # 局部导入，避免循环

    cleaned = safe_path_part(domain, "health")
    if cleaned != domain:
        raise HTTPException(status_code=400, detail=f"非法领域名：{domain!r}")
    return cleaned


def _safe_prefix(prefix: str, domain: str = "") -> str:
    """校验产物前缀；留空表示「用这个项目的默认前缀」。

    默认值按领域现算（``<领域>_train``），而不是某个全局常量：领域一换，前缀就
    该跟着换，否则会去读另一个项目的产物。
    """
    from .registry import default_prefix, safe_path_part

    fallback = default_prefix(domain or OUTPUT_DOMAIN_NAME)
    text = (prefix or "").strip()
    if not text:
        return fallback
    cleaned = safe_path_part(text, fallback)
    if cleaned != text:
        raise HTTPException(status_code=400, detail=f"非法前缀：{prefix!r}")
    return cleaned


def _rel(path: Any) -> str:
    """项目内相对路径，用于回给界面的报错文本。

    项目根对使用者是个常量，写全了只是噪音；界面上的路径也统一是这个口径。
    """
    text = str(path)
    root = str(PROJECT_ROOT)
    if text == root:
        return "."
    if text.startswith(root):
        return text[len(root):].lstrip("/\\")
    return text


def _build_context(domain: str, prefix: str, params: dict[str, Any] | None) -> RunContext:
    values: dict[str, dict[str, Any]] = {}
    for stage_id, raw in (params or {}).items():
        stage = STAGE_BY_ID.get(stage_id)
        if stage is None:
            continue
        values[stage_id] = coerce_params(stage, raw or {})
    return RunContext(
        domain=domain,
        prefix=prefix,
        python=sys.executable,
        seed_input=seed_path(domain),
        output_dir=domain_output_dir(domain),
        values=values,
    )


async def _run_and_record(manager: JobManager, job: Any) -> None:
    """跑完作业后记一笔运行记录。

    记录是给产物页分批用的辅助信息，写盘失败不能反过来影响作业本身 —— 一次成功
    的运行不能因为写不进一行 JSON 就在界面上显示成失败，所以这里兜住所有异常。
    记录写在 ``finally`` 里：失败和被取消的作业同样要留痕，它们才是最需要回头
    看日志的那几次。
    """
    try:
        await manager.run(job)
    finally:
        try:
            record_run(job)
        except Exception as exc:  # pragma: no cover - 防御性
            print(f"[ui] 运行记录写入失败：{exc!r}", file=sys.stderr)


def _stage_blocker(stage: Stage, ctx: RunContext) -> str:
    """这个阶段眼下跨不过去的门槛；空串表示没有。

    目前只有「本地 tokenizer 路径不存在」这一道。单独抽出来是因为它有两个消费
    方：阶段自己被勾选时要报；产出别人所需输入的阶段更要报 —— 后者才是「明明
    勾了去重、却还是缺文件」的答案。
    """
    if "model" not in stage.requires:
        return ""
    model_path = ctx.stage_values(stage.id).get("model_path") or resolve_tokenizer_path()
    if Path(str(model_path)).exists():
        return ""
    return f"需要本地 tokenizer，但路径不存在：{_rel(model_path)}"


def _producers_of(key: str) -> list[Stage]:
    """产出这份产物的阶段（同一个产物可能被先后写，所以是复数）。"""
    return [item for item in STAGES if key in item.outputs]


def _strict_inputs(stage: Stage) -> list[str]:
    """除掉「自己产出自己吃」之后，真正需要前序阶段准备的输入。"""
    return [key for key in stage.inputs if key not in stage.outputs]


def _unresolved_inputs(
    stage: Stage, ctx: RunContext, picked: set[str], doomed: set[str]
) -> list[str]:
    """跑完这次之后仍然不会有的输入。

    「盘上没有」不等于「缺」：刚勾上「合并」时它的产物还没生成，而这次运行本来就
    会生成它。判据与界面（app.js 的 refreshSelectionDiag）保持同一条：盘上有，或
    者有某个已选、且自己跑得出来的阶段会产出它，就不算缺。
    """
    missing: list[str] = []
    for key in stage.inputs:
        if key in stage.outputs:
            continue
        if artifact_path(key, ctx.domain, ctx.prefix).exists():
            continue
        if any(item.id in picked and item.id not in doomed for item in _producers_of(key)):
            continue
        missing.append(key)
    return missing


def _selected_doomed(stage_ids: list[str], ctx: RunContext) -> set[str]:
    """这批选择下注定跑不出来的阶段。

    缺 tokenizer 直接算；缺输入则看有没有「已选、且自己跑得出来」的产出者。两者
    都会顺着链往下传 —— 上游跑不出来，靠它吃饭的下游也跑不出来，所以用不动点迭
    代到稳定，比逐条判断准。

    这一层曾经缺失：后端只问「文件此刻在不在」，于是将要产出它的阶段也被当成没
    产出。后果是勾满主链路、tokenizer 也配好之后，仍然会被自己的「还没生成」拦
    一次，而界面上当时显示「跑不通 0」。
    """
    picked = set(stage_ids)
    doomed: set[str] = set()
    changed = True
    while changed:
        changed = False
        for stage_id in stage_ids:
            stage = STAGE_BY_ID[stage_id]
            if stage_id in doomed:
                continue
            if _stage_blocker(stage, ctx):
                doomed.add(stage_id)
                changed = True
                continue
            missing = _unresolved_inputs(stage, ctx, picked, doomed)
            if not missing:
                continue
            total = len(_strict_inputs(stage))
            # 非严格阶段缺的输入会自动跳过，只有一项都不剩时才真跑不了。
            if stage.strict_inputs is False and total and len(missing) < total:
                continue
            doomed.add(stage_id)
            changed = True
    return doomed


def _input_origin(key: str, ctx: RunContext, picked: set[str], doomed: set[str]) -> str:
    """一句话说清某份输入由谁产出、为什么现在没有。

    「缺少输入：合并后的 final prompt」只报了症状，人还得自己回阶段列表里找是谁
    产它的、为什么没跑。缺输入的原因无非两种：产出它的那个阶段这次没勾选，或者
    那个阶段自己也跑不出来。先说结论，省一轮来回。
    """
    parts: list[str] = []
    for producer in _producers_of(key):
        blocker = _stage_blocker(producer, ctx)
        if producer.id in picked:
            if producer.id not in doomed:
                continue  # 这次会产出它，调用方也不会为它报缺
            # 勾了却仍拿不到，只有一种可能：它自己也跑不出来。必须点名 —— 否则人
            # 对着「缺少输入」和另一条「需要 tokenizer」两条并列的提示，还得自己
            # 补上「4 产出 6 的输入」这层关系才看得懂。
            reason = blocker or "自己也缺输入，这次跑不出来"
            parts.append(
                f"它由「{producer.title}」产出，那个阶段本次勾了，但它{reason}"
                f" —— 得先把它跑通，这里的输入才会出现"
            )
            continue
        tail = (
            f"，而它{blocker} —— 勾上去也跑不通，得先把 tokenizer 配好"
            if blocker
            else "，勾上那个阶段即可产出"
        )
        parts.append(f"它由「{producer.title}」产出，那个阶段本次没勾选{tail}")
    return "；".join(parts)


def _check_prerequisites(stage_ids: list[str], ctx: RunContext) -> list[str]:
    """返回真正会拦住这次运行的说明；空列表表示可以直接跑。

    报的不只是「缺什么」，还有「为什么会缺」：缺输入多半是因为产出它的阶段没被
    勾选，而没勾选多半是因为那个阶段缺 tokenizer 跑不了。把这条链讲完，使用者
    才知道该去勾阶段，还是该先去配 tokenizer。

    判据与界面一致（见 _selected_doomed）：**这次运行会产出的输入不算缺**。
    """
    picked = set(stage_ids)
    doomed = _selected_doomed(stage_ids, ctx)
    problems: list[str] = []
    for stage_id in stage_ids:
        stage = STAGE_BY_ID[stage_id]
        missing = _unresolved_inputs(stage, ctx, picked, doomed)
        if stage.strict_inputs:
            for key in missing:
                path = artifact_path(key, ctx.domain, ctx.prefix)
                note = _input_origin(key, ctx, picked, doomed)
                line = f"阶段「{stage.title}」缺少输入：{ARTIFACT_BY_KEY[key].label}（{path}）"
                problems.append(f"{line}\n    · {note}" if note else line)
        elif missing and len(missing) == len(_strict_inputs(stage)):
            problems.append(
                f"阶段「{stage.title}」的候选输入全部不存在，跑起来会直接报错。"
            )
        blocker = _stage_blocker(stage, ctx)
        if blocker:
            problems.append(f"阶段「{stage.title}」{blocker}")
    return problems


def _check_api_config(stage_ids: list[str]) -> list[str]:
    """返回缺少的模型接入配置。

    这些项不给「忽略并继续」的选项：没配端点或模型，请求根本发不出去，硬跑只会
    在日志里留下一串 HTTP 错误。在点运行的那一刻就说清楚去哪儿填。
    """
    if not any("api" in STAGE_BY_ID[stage_id].requires for stage_id in stage_ids):
        return []
    missing = missing_required_config()
    if not missing:
        return []
    return [f"{label}（{key}）" for key, label in missing]


# ---------------------------------------------------------------------------
# 应用
# ---------------------------------------------------------------------------


async def _restart_after(delay: float) -> None:
    """等响应落进套接字后原地重启；成功时本函数不返回。

    ``os.execv`` 失败（比如解释器被删了）会抛 OSError —— 那时响应已经发出去了，
    只能写日志：进程还在，用户可以手工重启。
    """
    await asyncio.sleep(delay)
    try:
        restart.restart()
    except OSError as exc:  # pragma: no cover - 只在 execv 真的失败时走到
        print(f"[restart] 重启失败：{exc}", file=sys.stderr, flush=True)


def create_app() -> FastAPI:
    app = FastAPI(title="Agent 数据流水线控制台", version=__version__)
    manager = JobManager(python=sys.executable, project_root=PROJECT_ROOT)

    app.state.manager = manager

    # -- 元信息 -------------------------------------------------------------

    @app.get("/api/health")
    def api_health() -> dict[str, Any]:
        """轻量启动探针，不触发模型、网络或领域重依赖加载。"""

        return {"status": "ok", "version": __version__}

    @app.get("/api/meta")
    def api_meta() -> dict[str, Any]:
        domains = find_domains()
        # tokenizer 路径每次现算：界面上改完 .env 后徽章要能立刻跟着变。
        tokenizer_path = resolve_tokenizer_path()
        # 同理，模型接入是否配好也每次现算，而不是只看有没有 API Key。
        missing_config = missing_required_config()
        return {
            "version": __version__,
            "python": sys.executable,
            "projectRoot": str(PROJECT_ROOT),
            "agentRoot": str(AGENT_ROOT),
            "resourceRoot": str(getattr(sys, "_MEIPASS", PROJECT_ROOT)),
            "modelPath": str(tokenizer_path),
            "modelPathExists": tokenizer_path.exists(),
            # 与徽章、运行前拦截同一份判断：只看环境变量非空的话，模板里的
            # sk-xxxx 占位符会让它误报成已配置。
            "apiKeyConfigured": not any(key == "DEEPSEEK_API_KEY" for key, _ in missing_config),
            "missingConfig": [{"key": key, "label": label} for key, label in missing_config],
            "configReady": not missing_config,
            "domains": domains,
            "defaultDomain": default_domain(domains),
            "defaultPipeline": list(DEFAULT_PIPELINE),
            # 当前项目的默认构建任务名与可选数据集档位：界面切换到别的项目时按这个
            # 重算，免得停在旧项目的任务名上读另一个项目的产物。
            "defaultPrefix": default_prefix(default_domain(domains) or OUTPUT_DOMAIN_NAME),
            "datasetSplits": dataset_splits(default_domain(domains) or OUTPUT_DOMAIN_NAME),
            "artifacts": [
                {"key": item.key, "label": item.label, "kind": item.kind, "description": item.description}
                for item in ARTIFACTS
            ],
            # 重启能力：界面上的「重启」按钮据此决定能不能点、点了会执行什么命令。
            # execv 不换 PID，所以界面得靠 startedAt 认出「响应的是新进程」。
            **restart.describe(),
            "activeJobId": manager.active.id if manager.active else None,
        }

    @app.get("/api/config")
    def api_config() -> dict[str, Any]:
        """教师模型接入 / 超时等运行时配置的当前值（密钥只回显掩码）。"""
        return describe_config()

    @app.put("/api/config")
    def api_update_config(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        values = payload.get("values")
        if not isinstance(values, dict):
            raise HTTPException(status_code=400, detail="values 必须是一个对象。")
        try:
            return update_config(values)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except OSError as exc:
            raise HTTPException(status_code=500, detail=f"写入 .env 失败：{exc}") from exc

    # -- 全局环境文件（工具凭据）------------------------------------------
    #
    # 「运行时配置」是白名单（模型接入那几个键）；这里是整份 agent/.env。
    # 工具凭据的键名由工具自己定，代码里登记不过来，所以放开给界面自由增删。

    @app.get("/api/env")
    def api_env() -> dict[str, Any]:
        """agent/.env 的全部键值（密钥只回显掩码）。"""
        return describe_env_file()

    @app.put("/api/env")
    def api_update_env(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        values = payload.get("values")
        if values is not None and not isinstance(values, dict):
            raise HTTPException(status_code=400, detail="values 必须是一个对象。")
        removals = payload.get("removals")
        if removals is not None and not isinstance(removals, list):
            raise HTTPException(status_code=400, detail="removals 必须是一个数组。")
        try:
            return update_env_file(values or {}, removals or [])
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except OSError as exc:
            raise HTTPException(status_code=500, detail=f"写入 .env 失败：{exc}") from exc

    # -- 重启 ---------------------------------------------------------------

    @app.post("/api/restart")
    async def api_restart(background: BackgroundTasks) -> dict[str, Any]:
        """原地重启控制台进程。

        改完 ``agent/core/**``、``agent/config.py`` 或 ``agent/ui/*.py`` 后必须
        重启才生效：这些模块只在进程启动时导入一次，领域包的热加载只覆盖
        ``agent/domains/<领域>.*``。

        有作业在跑时直接拒绝，而不是「先重启再说」：作业是这里的子进程，重启后
        它还会继续写产物，但日志再也没人读、也取消不了，事后只能靠猜。
        """
        if not restart.available():
            raise HTTPException(status_code=409, detail=restart.unavailable_reason())
        active = manager.active
        if active is not None:
            raise HTTPException(
                status_code=409,
                detail=f"作业「{active.id}」正在运行，重启会切断它的日志。请先等它结束或取消。",
            )
        # 先把响应发回去，再 execv：延迟放在后台任务里，事件循环这一轮就把响应刷进
        # 套接字了。直接 execv 会连还在缓冲里的响应一起丢掉，前端只能看到连接被重置。
        background.add_task(_restart_after, 0.6)
        return {"ok": True, **restart.describe()}

    @app.get("/api/stages")
    def api_stages(
        domain: str = Query("health"),
        prefix: str = Query(""),
    ) -> dict[str, Any]:
        domain = _safe_domain(domain)
        prefix = _safe_prefix(prefix, domain)
        payload = []
        for stage in STAGES:
            item = stage.to_json()
            item["inputsStatus"] = [_artifact_status(key, domain, prefix) for key in stage.inputs]
            item["outputsStatus"] = [_artifact_status(key, domain, prefix) for key in stage.outputs]
            payload.append(item)
        return {
            "domain": domain,
            "prefix": prefix,
            "outputDir": str(domain_output_dir(domain)),
            "seedPath": str(seed_path(domain)),
            "seedExists": seed_path(domain).exists(),
            "stages": payload,
        }

    @app.get("/api/artifacts")
    def api_artifacts(
        domain: str = Query("health"),
        prefix: str = Query(""),
    ) -> dict[str, Any]:
        domain = _safe_domain(domain)
        prefix = _safe_prefix(prefix, domain)
        return {
            "outputDir": str(domain_output_dir(domain)),
            "artifacts": [_artifact_status(item.key, domain, prefix) for item in ARTIFACTS],
        }

    @app.get("/api/tasks")
    def api_tasks(domain: str = Query("health")) -> dict[str, Any]:
        """这个项目下的构建任务。产物按任务分目录存放，这里是目录清单。"""
        domain = _safe_domain(domain)
        tasks = list_tasks(domain)
        for item in tasks:
            item["path"] = _rel(item["path"])
        pending = [item for item in tasks if item["legacy"]]
        return {
            "domain": domain,
            "outputDir": _rel(domain_output_dir(domain)),
            "tasks": tasks,
            "legacyTasks": [item["name"] for item in pending],
            "legacyFiles": sum(item["legacyFiles"] for item in tasks),
        }

    @app.post("/api/tasks/migrate")
    def api_migrate_tasks(payload: dict[str, Any] = Body(default={})) -> dict[str, Any]:
        """把老布局的产物文件搬进各自的任务目录（``<任务>_<产物>`` → ``<任务>/<产物>``）。

        搬之前先确认没有作业在跑：产物正在被写入时移动文件会两头写坏。
        """
        if manager.active is not None:
            raise HTTPException(
                status_code=409,
                detail=f"作业 {manager.active.id} 正在运行，产物还在被写入。请等它结束或先取消。",
            )
        domain = _safe_domain(str(payload.get("domain") or OUTPUT_DOMAIN_NAME))
        raw = payload.get("tasks")
        wanted = [str(item) for item in raw] if isinstance(raw, list) and raw else None
        result = migrate_legacy_files(domain, wanted)
        return {"domain": domain, **result}

    @app.get("/api/artifacts/preview")
    def api_preview(
        key: str = Query(...),
        domain: str = Query("health"),
        prefix: str = Query(""),
        offset: int = Query(0, ge=0, le=200_000),
        limit: int = Query(20, ge=1, le=PREVIEW_MAX_ROWS),
    ) -> dict[str, Any]:
        domain = _safe_domain(domain)
        prefix = _safe_prefix(prefix, domain)
        if key not in ARTIFACT_BY_KEY:
            raise HTTPException(status_code=404, detail=f"未知产物：{key}")
        artifact = ARTIFACT_BY_KEY[key]
        path = artifact_path(key, domain, prefix)
        if not path.exists():
            raise HTTPException(status_code=404, detail=f"文件不存在：{_rel(path)}")
        if path.is_dir():
            entries = []
            for child in sorted(path.iterdir())[:200]:
                entries.append({"name": child.name, "size": child.stat().st_size if child.is_file() else None})
            return {"kind": "directory", "path": str(path), "entries": entries}
        if artifact.kind == "markdown":
            text = path.read_text(encoding="utf-8", errors="replace")
            truncated = len(text) > 200_000
            return {
                "kind": "text",
                "path": str(path),
                "text": text[:200_000],
                "truncated": truncated,
            }
        if artifact.kind == "json":
            text = path.read_text(encoding="utf-8", errors="replace")
            return {
                "kind": "text",
                "path": str(path),
                "text": text[:400_000],
                "truncated": len(text) > 400_000,
            }
        rows = _read_jsonl_slice(path, offset, limit)
        return {
            "kind": "jsonl",
            "path": str(path),
            "offset": offset,
            "limit": limit,
            "rows": rows,
            "total": _count_lines(path),
        }

    @app.get("/api/artifacts/download")
    def api_download(
        key: str = Query(...),
        domain: str = Query("health"),
        prefix: str = Query(""),
    ) -> FileResponse:
        """按原文件下载产物。

        浏览器对 blob: 下载支持不一（VS Code 集成浏览器就不认），走一次真正的
        HTTP 下载更稳，文件名也由服务端定。
        """
        domain = _safe_domain(domain)
        prefix = _safe_prefix(prefix, domain)
        if key not in ARTIFACT_BY_KEY:
            raise HTTPException(status_code=404, detail=f"未知产物：{key}")
        path = artifact_path(key, domain, prefix)
        if not path.exists() or path.is_dir():
            raise HTTPException(status_code=404, detail=f"文件不存在：{_rel(path)}")
        return FileResponse(
            path,
            media_type="text/markdown; charset=utf-8",
            # 文件名本身已带前缀（valid_data_stats.md），只补领域名即可。
            filename=f"{domain}_{path.name}",
        )

    # -- 作业 ---------------------------------------------------------------

    @app.post("/api/jobs")
    async def api_create_job(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        stage_ids = payload.get("stages") or []
        if not isinstance(stage_ids, list) or not stage_ids:
            raise HTTPException(status_code=400, detail="请至少选择一个阶段。")
        domain = _safe_domain(str(payload.get("domain") or "health"))
        prefix = _safe_prefix(str(payload.get("prefix") or ""), domain)
        force = bool(payload.get("force"))
        params = payload.get("params") or {}

        if manager.active is not None:
            raise HTTPException(
                status_code=409,
                detail=f"已有作业在运行（{manager.active.id}），请先等它结束或取消。",
            )

        try:
            ctx = _build_context(domain, prefix, params)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        missing_config = _check_api_config(stage_ids)
        if missing_config:
            raise HTTPException(
                status_code=400,
                detail="还没配好模型接入："
                + "、".join(missing_config)
                + "。请点顶栏的齿轮 → 模型接入 填写后重试。",
            )

        problems = _check_prerequisites(stage_ids, ctx)
        if problems and not force:
            raise HTTPException(
                status_code=409,
                detail="前置文件不满足：\n" + "\n".join(problems) + "\n\n确认要忽略并继续吗？",
            )
        try:
            job = manager.create(stage_ids, ctx, meta={"force": force, "problems": problems})
        except (ValueError, KeyError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        asyncio.create_task(_run_and_record(manager, job))
        return job.to_json(include_lines=False)

    @app.get("/api/runs")
    def api_runs(
        domain: str = Query("health"),
        limit: int = Query(20, ge=1, le=100),
    ) -> dict[str, Any]:
        """本领域的运行批次，最近的在前。

        每个批次里的产物快照会当场跟磁盘比一次：``changed`` 表示这个文件在
        这次运行之后又被改过（同名产物被后来的运行覆盖了）。产物路径是固定的，
        没有这个对比就分不出「文件是那次跑的」和「文件现在长这样」。
        """
        domain = _safe_domain(domain)
        runs = load_runs(domain, limit)
        for run in runs:
            try:
                prefix = _safe_prefix(str(run.get("prefix") or ""), domain)
            except HTTPException:
                # 历史记录里的前缀可能因为默认值变更而不再合法，这种批次仍然
                # 要能看时间线，只是不参与「跟当前盘面对比」。
                continue
            for item in run.get("artifacts", []):
                key = item.get("key")
                if key not in ARTIFACT_BY_KEY:
                    continue
                try:
                    current = _artifact_status(key, domain, prefix)
                except OSError:
                    item["current"] = False
                    continue
                item["current"] = bool(current.get("exists"))
                # 这些字段属于「该历史批次对应任务」的当前盘面，不是顶栏当前
                # 任务的状态。前端直接消费它，避免切换任务后历史明细串档。
                # 旧版 runs.jsonl 没有保存 path；首次读取时用这条记录自己的
                # domain/prefix 补回，前端就不会把路径留空，也不会回退到当前任务。
                if not item.get("path"):
                    item["path"] = current.get("path")
                item["currentPath"] = current.get("path")
                item["currentSize"] = current.get("size")
                item["currentMtime"] = current.get("mtime")
                item["currentLines"] = current.get("lines")
                item["currentIsDir"] = bool(current.get("isDir"))
                item["changed"] = bool(
                    item["current"]
                    and abs(float(item["currentMtime"] or 0) - float(item.get("mtime") or 0)) > 1
                )
        return {"domain": domain, "runs": runs, "outputDir": str(domain_output_dir(domain))}

    @app.get("/api/jobs")
    def api_jobs() -> dict[str, Any]:
        return {"jobs": manager.list_jobs(), "activeJobId": manager.active.id if manager.active else None}

    @app.get("/api/jobs/{job_id}")
    def api_job(job_id: str) -> dict[str, Any]:
        job = manager.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="作业不存在。")
        return job.to_json()

    @app.post("/api/jobs/{job_id}/cancel")
    async def api_cancel(job_id: str) -> dict[str, Any]:
        if not manager.cancel(job_id):
            raise HTTPException(status_code=400, detail="该作业不在运行中。")
        return {"ok": True}

    @app.get("/api/jobs/{job_id}/stream")
    async def api_stream(job_id: str, since: int = Query(0, ge=0)) -> StreamingResponse:
        job = manager.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="作业不存在。")

        async def event_source():
            async for event in stream_job(job, since=since):
                yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"

        return StreamingResponse(
            event_source(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    # -- seed 管理 -----------------------------------------------------------

    @app.get("/api/seeds")
    def api_seeds(
        domain: str = Query("health"),
        offset: int = Query(0, ge=0),
        limit: int = Query(50, ge=1, le=500),
    ) -> dict[str, Any]:
        domain = _safe_domain(domain)
        path = seed_path(domain)
        if not path.exists():
            return {"path": str(path), "exists": False, "rows": [], "total": 0, "editable": True}
        size = path.stat().st_size
        total = _count_lines(path)
        editable = total <= SEED_EDIT_MAX_ROWS and size <= SEED_EDIT_MAX_BYTES
        rows = _read_jsonl_slice(path, offset, limit)
        return {
            "path": str(path),
            "exists": True,
            "size": size,
            "total": total,
            "offset": offset,
            "limit": limit,
            "rows": rows,
            "editable": editable,
            "editableHint": "" if editable else f"文件较大（{total} 行 / {size} 字节），已切换为只读预览。",
        }

    @app.post("/api/seeds")
    def api_save_seeds(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        domain = _safe_domain(str(payload.get("domain") or "health"))
        rows = payload.get("rows")
        if not isinstance(rows, list):
            raise HTTPException(status_code=400, detail="rows 必须是数组。")
        for index, row in enumerate(rows):
            if not isinstance(row, dict):
                raise HTTPException(status_code=400, detail=f"第 {index + 1} 条不是 JSON 对象。")

        path = seed_path(domain)
        if path.exists() and _count_lines(path) > SEED_EDIT_MAX_ROWS:
            raise HTTPException(status_code=413, detail="文件过大，禁止通过界面整体覆盖。")

        backup: str | None = None
        if path.exists():
            backup_path = path.with_name(f"{path.name}.bak_{time.strftime('%Y%m%d_%H%M%S')}")
            shutil.copy2(path, backup_path)
            backup = str(backup_path)

        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = path.with_suffix(".jsonl.tmp")
        with tmp_path.open("w", encoding="utf-8") as handle:
            for row in rows:
                handle.write(json.dumps(row, ensure_ascii=False) + "\n")
        tmp_path.replace(path)
        _line_cache.clear()
        return {"ok": True, "path": str(path), "count": len(rows), "backup": backup}

    # -- 领域（项目）管理 ---------------------------------------------------
    #
    # 每个领域是一份自包含的项目：代码包在 agent/domains/<name>/，产物在
    # agent/outputs/<name>/。这里的接口只做目录级的增删改，不改动业务脚本。

    def _busy_guard(action: str, domain: str) -> None:
        """作业运行时不允许改目录，否则子进程可能写到一半被搬走。"""
        if manager.active is None:
            return
        raise HTTPException(
            status_code=409,
            detail=f"作业 {manager.active.id} 正在运行，无法{action}领域 {domain!r}。请等它结束或先取消。",
        )

    def _domain_action(callable_, *args: Any, **kwargs: Any) -> dict[str, Any]:
        """把领域操作的 ValueError 统一转成 400。"""
        try:
            result = callable_(*args, **kwargs)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        _line_cache.clear()
        return result

    @app.get("/api/domains")
    def api_domains() -> dict[str, Any]:
        domains = list_domains()
        return {
            "domains": domains,
            "templates": domain_templates(),
            "domainsDir": str(DOMAINS_DIR),
            "outputsDir": str(OUTPUTS_DIR),
            "activeJobId": manager.active.id if manager.active else None,
        }

    @app.post("/api/domains")
    def api_create_domain(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        return _domain_action(
            create_domain,
            str(payload.get("name") or ""),
            display_name=payload.get("displayName"),
            template=payload.get("template"),
            copy_seeds=bool(payload.get("copySeeds")),
        )

    @app.post("/api/domains/{name}/duplicate")
    def api_duplicate_domain(name: str, payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        return _domain_action(
            duplicate_domain,
            name,
            str(payload.get("newName") or ""),
            display_name=payload.get("displayName"),
            copy_seeds=bool(payload.get("copySeeds", True)),
            copy_outputs=bool(payload.get("copyOutputs")),
        )

    @app.post("/api/domains/{name}/rename")
    def api_rename_domain(name: str, payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        _busy_guard("重命名", name)
        return _domain_action(
            rename_domain, name, str(payload.get("newName") or ""), display_name=payload.get("displayName")
        )

    @app.delete("/api/domains/{name}")
    def api_delete_domain(name: str) -> dict[str, Any]:
        _busy_guard("删除", name)
        remaining = [item["name"] for item in list_domains() if item["name"] != name]
        if not remaining:
            raise HTTPException(status_code=400, detail="至少要保留一个领域，否则界面无法工作。")
        return _domain_action(delete_domain, name)

    @app.get("/api/domains/{name}")
    def api_domain_detail(name: str) -> dict[str, Any]:
        if not domain_dir(name).is_dir():
            raise HTTPException(status_code=404, detail=f"领域 {name!r} 不存在。")
        return describe_domain(name)

    # -- 领域文件在线编辑 ---------------------------------------------------
    #
    # 提示词 / 意图分类 / 工具定义都在领域包里，这里提供读写接口。路径由
    # registry 校验（必须在 agent/domains/<name>/ 内），保存前做语法校验并
    # 自动备份到 <领域>/.history/。作业运行中禁止保存，避免同一批数据前后
    # 读到不同版本的提示词。

    @app.get("/api/domains/{name}/files")
    def api_domain_files(name: str) -> dict[str, Any]:
        return _domain_action(list_domain_files, name)

    @app.get("/api/domains/{name}/file")
    def api_read_domain_file(name: str, path: str = Query(...)) -> dict[str, Any]:
        return _domain_action(read_domain_file, name, path)

    @app.put("/api/domains/{name}/file")
    def api_write_domain_file(name: str, payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        _busy_guard("修改文件", name)
        return _domain_action(
            write_domain_file,
            name,
            str(payload.get("path") or ""),
            str(payload.get("content") or ""),
        )

    @app.get("/api/domains/{name}/file/history")
    def api_domain_file_history(name: str, path: str = Query(...)) -> dict[str, Any]:
        return _domain_action(domain_file_history, name, path)

    @app.post("/api/domains/{name}/file/restore")
    def api_restore_domain_file(name: str, payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        _busy_guard("恢复文件", name)
        return _domain_action(
            restore_domain_file,
            name,
            str(payload.get("path") or ""),
            str(payload.get("backup") or ""),
        )

    # -- 全局工具库 ---------------------------------------------------------
    #
    # 工具是跨项目共用的：一个工具在 agent/tools/<name>.py 里定义一次，
    # 各项目通过 agent/domains/<name>/tools.json 只记「开了哪些」。
    # 改工具源码立刻对所有启用它的项目生效（下一个子进程读到的就是新版）。

    def _tool_action(callable_, *args: Any, **kwargs: Any) -> dict[str, Any]:
        try:
            result = callable_(*args, **kwargs)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return result

    @app.get("/api/tools")
    def api_tools(domain: str = Query("")) -> dict[str, Any]:
        return _tool_action(list_global_tools, domain or None)

    @app.post("/api/tools")
    def api_create_tool(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        return _tool_action(
            create_global_tool,
            str(payload.get("name") or ""),
            str(payload.get("description") or ""),
            payload.get("params") or [],
            str(payload.get("mock") or ""),
        )

    # -- 角色模板 -----------------------------------------------------------
    # 角色是结构化配置资产，暂不直接生成或执行任意 Python 源码。
    @app.get("/api/roles")
    def api_roles(domain: str = Query(...)) -> dict[str, Any]:
        return _tool_action(list_roles, domain)

    @app.post("/api/roles")
    def api_create_role(domain: str = Query(...), payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        template = payload.get("template")
        data = dict(payload)
        data.pop("template", None)
        return _tool_action(create_role, domain, data, template=template)

    @app.get("/api/roles/{name}")
    def api_read_role(name: str, domain: str = Query(...)) -> dict[str, Any]:
        return _tool_action(read_role, domain, name)

    @app.put("/api/roles/{name}")
    def api_write_role(name: str, domain: str = Query(...), payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        return _tool_action(write_role, domain, name, payload)

    @app.delete("/api/roles/{name}")
    def api_delete_role(name: str, domain: str = Query(...)) -> dict[str, Any]:
        return _tool_action(delete_role, domain, name)

    @app.get("/api/tools/{name}")
    def api_read_tool(name: str) -> dict[str, Any]:
        return _tool_action(read_global_tool, name)

    @app.put("/api/tools/{name}")
    def api_write_tool(name: str, payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        return _tool_action(write_global_tool, name, str(payload.get("content") or ""))

    @app.delete("/api/tools/{name}")
    def api_delete_tool(name: str) -> dict[str, Any]:
        return _tool_action(delete_global_tool, name)

    @app.get("/api/tools/{name}/history")
    def api_tool_history(name: str) -> dict[str, Any]:
        return _tool_action(global_tool_history, name)

    @app.post("/api/tools/{name}/restore")
    def api_restore_tool(name: str, payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        return _tool_action(restore_global_tool, name, str(payload.get("backup") or ""))

    @app.put("/api/domains/{name}/tools")
    def api_set_domain_tools(name: str, payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        """设置某个项目开放了哪些全局工具（立即生效，无需重启）。"""
        _busy_guard("修改工具配置", name)
        return _domain_action(set_domain_tools, name, payload.get("enabled") or [])

    # -- 统计 ---------------------------------------------------------------

    @app.get("/api/stats")
    def api_stats(
        domain: str = Query("health"),
        prefix: str = Query(""),
    ) -> dict[str, Any]:
        domain = _safe_domain(domain)
        prefix = _safe_prefix(prefix, domain)
        json_path = artifact_path("data_stats_json", domain, prefix)
        md_path = artifact_path("data_stats_md", domain, prefix)
        payload: dict[str, Any] = {
            "jsonPath": str(json_path),
            "mdPath": str(md_path),
            "jsonExists": json_path.exists(),
            "mdExists": md_path.exists(),
        }
        if json_path.exists():
            try:
                payload["report"] = json.loads(json_path.read_text(encoding="utf-8", errors="replace"))
            except json.JSONDecodeError as exc:
                payload["error"] = f"统计 JSON 解析失败：{exc}"
        return payload

    # -- 静态资源 -----------------------------------------------------------

    @app.get("/")
    def index() -> FileResponse:
        # 同样禁掉强制缓存：index.html 里引着 app.js / style.css，它被缓存住的话
        # 后面两个改了什么都不会被重新请求。
        return FileResponse(STATIC_DIR / "index.html", headers={"Cache-Control": "no-cache"})

    app.mount("/static", _NoCacheStaticFiles(directory=STATIC_DIR), name="static")

    return app


def _read_jsonl_slice(path: Path, offset: int, limit: int) -> list[dict[str, Any]]:
    """读取 JSONL 的 [offset, offset+limit) 行；单行过长时截断并标记。"""
    rows: list[dict[str, Any]] = []
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            for index, line in enumerate(handle):
                if index < offset:
                    continue
                if len(rows) >= limit:
                    break
                text = line.rstrip("\n")
                if not text.strip():
                    continue
                truncated = len(text) > PREVIEW_MAX_CHARS
                body = text[:PREVIEW_MAX_CHARS] if truncated else text
                try:
                    rows.append({"index": index, "data": json.loads(body), "truncated": truncated})
                except json.JSONDecodeError as exc:
                    rows.append(
                        {
                            "index": index,
                            "raw": body,
                            "error": f"JSON 解析失败：{exc}",
                            "truncated": truncated,
                        }
                    )
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"读取失败：{exc}") from exc
    return rows


app = create_app()

__all__ = ["app", "create_app"]
