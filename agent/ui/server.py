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

from fastapi import Body, FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

try:  # 正常以 ``agent.ui.server`` 导入
    from ..config import AGENT_ROOT, OUTPUT_DOMAIN_NAME, PROJECT_ROOT
    from . import __version__
    from .jobs import JobManager, stream_job
    from .registry import (
        ARTIFACTS,
        ARTIFACT_BY_KEY,
        DEFAULT_PIPELINE,
        STAGES,
        STAGE_BY_ID,
        RunContext,
        artifact_path,
        coerce_params,
        count_lines,
        create_domain,
        create_global_tool,
        dataset_splits,
        default_domain,
        default_prefix,
        delete_domain,
        delete_global_tool,
        describe_config,
        describe_domain,
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
        missing_required_config,
        read_domain_file,
        read_global_tool,
        rename_domain,
        resolve_tokenizer_path,
        restore_domain_file,
        restore_global_tool,
        seed_path,
        set_domain_tools,
        update_config,
        write_domain_file,
        write_global_tool,
    )
    from .registry import DOMAINS_DIR, OUTPUTS_DIR
except ImportError:  # pragma: no cover - 兼容 ``python agent/ui/server.py``
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from agent.config import AGENT_ROOT, OUTPUT_DOMAIN_NAME, PROJECT_ROOT  # type: ignore
    from agent.ui import __version__  # type: ignore
    from agent.ui.jobs import JobManager, stream_job  # type: ignore
    from agent.ui.registry import (  # type: ignore
        ARTIFACTS,
        ARTIFACT_BY_KEY,
        DEFAULT_PIPELINE,
        STAGES,
        STAGE_BY_ID,
        RunContext,
        artifact_path,
        coerce_params,
        count_lines,
        create_domain,
        create_global_tool,
        dataset_splits,
        default_domain,
        default_prefix,
        delete_domain,
        delete_global_tool,
        describe_config,
        describe_domain,
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
        missing_required_config,
        read_domain_file,
        read_global_tool,
        rename_domain,
        resolve_tokenizer_path,
        restore_domain_file,
        restore_global_tool,
        seed_path,
        set_domain_tools,
        update_config,
        write_domain_file,
        write_global_tool,
    )
    from agent.ui.registry import DOMAINS_DIR, OUTPUTS_DIR  # type: ignore


STATIC_DIR = Path(__file__).resolve().parent / "static"
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


def _check_prerequisites(stage_ids: list[str], ctx: RunContext) -> list[str]:
    """返回缺失的前置文件说明；空列表表示可以直接跑。"""
    problems: list[str] = []
    for stage_id in stage_ids:
        stage = STAGE_BY_ID[stage_id]
        if stage.strict_inputs:
            for key in stage.inputs:
                path = artifact_path(key, ctx.domain, ctx.prefix)
                if not path.exists():
                    label = ARTIFACT_BY_KEY[key].label
                    problems.append(f"阶段「{stage.title}」缺少输入：{label}（{path}）")
        elif not any(
            artifact_path(key, ctx.domain, ctx.prefix).exists() for key in stage.inputs
        ):
            problems.append(
                f"阶段「{stage.title}」的候选输入全部不存在，跑起来会直接报错。"
            )
        if "model" in stage.requires:
            model_path = ctx.stage_values(stage_id).get("model_path") or resolve_tokenizer_path()
            if not Path(str(model_path)).exists():
                problems.append(f"阶段「{stage.title}」需要本地 tokenizer，但路径不存在：{model_path}")
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


def create_app() -> FastAPI:
    app = FastAPI(title="Agent 数据流水线控制台", version=__version__)
    manager = JobManager(python=sys.executable, project_root=PROJECT_ROOT)

    app.state.manager = manager

    # -- 元信息 -------------------------------------------------------------

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
            # 当前项目的输出前缀默认值与可选数据集：界面切换到别的项目时按这个重算，
            # 免得停在旧项目的前缀上读另一个项目的产物。
            "defaultPrefix": default_prefix(default_domain(domains) or OUTPUT_DOMAIN_NAME),
            "datasetSplits": dataset_splits(default_domain(domains) or OUTPUT_DOMAIN_NAME),
            "artifacts": [
                {"key": item.key, "label": item.label, "kind": item.kind, "description": item.description}
                for item in ARTIFACTS
            ],
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

        asyncio.create_task(manager.run(job))
        return job.to_json(include_lines=False)

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
