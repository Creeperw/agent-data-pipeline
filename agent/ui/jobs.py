"""作业执行层：把阶段命令跑成子进程，并把日志实时推给前端。

设计要点：

- 同一时刻只允许一个作业在跑（数据集构建会写同一批输出文件，串行最安全）。
- 子进程用 ``start_new_session=True`` 起独立进程组，取消时整组一起 kill，
  避免留下孤儿 worker。
- 日志按 ``\\r`` 和 ``\\n`` 双分隔解析，这样 tqdm 的进度条刷新能实时看到，
  同时被标记为 ``transient``（前端只显示最新一条，不刷屏）。
"""

from __future__ import annotations

import asyncio
import os
import signal
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, AsyncIterator

from .registry import STAGE_BY_ID, RunContext, build_command

MAX_LINES = 4000
MAX_JOBS = 40


def _as_unbuffered(command: list[str]) -> list[str]:
    """在 ``python -m xxx`` 里插入 ``-u``，否则管道下 stdout 会块缓冲。"""
    if len(command) >= 2 and command[0].endswith(("python", "python3")) and command[1] == "-m":
        return [command[0], "-u", *command[1:]]
    return command


@dataclass
class LogLine:
    seq: int
    text: str
    stream: str = "stdout"  # stdout | stderr | system
    transient: bool = False
    ts: float = field(default_factory=time.time)

    def to_json(self) -> dict[str, Any]:
        return {
            "seq": self.seq,
            "text": self.text,
            "stream": self.stream,
            "transient": self.transient,
            "ts": self.ts,
        }


@dataclass
class StageRun:
    """一个阶段在作业内的执行状态。"""

    stage_id: str
    command: list[str]
    status: str = "pending"  # pending | running | succeeded | failed | skipped
    exit_code: int | None = None
    started_at: float | None = None
    finished_at: float | None = None
    duration: float | None = None
    error: str | None = None

    def to_json(self) -> dict[str, Any]:
        stage = STAGE_BY_ID.get(self.stage_id)
        return {
            "stageId": self.stage_id,
            "title": stage.title if stage else self.stage_id,
            "command": self.command,
            "status": self.status,
            "exitCode": self.exit_code,
            "startedAt": self.started_at,
            "finishedAt": self.finished_at,
            "duration": self.duration,
            "error": self.error,
        }


class Job:
    """一次作业：按顺序执行若干个阶段。"""

    def __init__(
        self,
        job_id: str,
        domain: str,
        prefix: str,
        stages: list[StageRun],
        ctx: RunContext,
        meta: dict[str, Any] | None = None,
    ) -> None:
        self.id = job_id
        self.domain = domain
        self.prefix = prefix
        self.stages = stages
        self.ctx = ctx
        self.meta = meta or {}
        self.status = "queued"  # queued | running | succeeded | failed | canceled
        self.created_at = time.time()
        self.started_at: float | None = None
        self.finished_at: float | None = None
        self.current_index: int = -1
        self.lines: deque[LogLine] = deque(maxlen=MAX_LINES)
        self._seq = 0
        self._subscribers: set[asyncio.Queue[dict[str, Any]]] = set()
        self._process: asyncio.subprocess.Process | None = None
        self._cancel_requested = False
        self._dropped_lines = 0

    # -- 日志 ---------------------------------------------------------------

    def append(self, text: str, stream: str = "stdout", transient: bool = False) -> None:
        # transient（tqdm 进度条）覆盖上一条，避免刷屏。
        if transient and self.lines and self.lines[-1].transient:
            self.lines[-1].text = text
            self.lines[-1].ts = time.time()
            self._publish({"type": "log", "line": self.lines[-1].to_json(), "replace": True})
            return
        # tqdm 收尾时会再把同一行以换行形式打印一次；若与上一条 transient
        # 内容相同，就直接把那条转正，不再新增一行。
        if (
            not transient
            and self.lines
            and self.lines[-1].transient
            and self.lines[-1].text == text
        ):
            self.lines[-1].transient = False
            self.lines[-1].ts = time.time()
            self._publish({"type": "log", "line": self.lines[-1].to_json(), "replace": True})
            return
        if len(self.lines) == MAX_LINES:
            self._dropped_lines += 1
        self._seq += 1
        line = LogLine(seq=self._seq, text=text, stream=stream, transient=transient)
        self.lines.append(line)
        self._publish({"type": "log", "line": line.to_json()})

    def system(self, text: str) -> None:
        self.append(text, stream="system")

    def _publish(self, event: dict[str, Any]) -> None:
        for queue in list(self._subscribers):
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:  # pragma: no cover - 慢消费者直接丢事件
                pass

    def publish_status(self) -> None:
        self._publish({"type": "status", "job": self.to_json(include_lines=False)})

    # -- 订阅 ---------------------------------------------------------------

    def subscribe(self) -> asyncio.Queue[dict[str, Any]]:
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=2000)
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[dict[str, Any]]) -> None:
        self._subscribers.discard(queue)

    # -- 序列化 -------------------------------------------------------------

    def to_json(self, include_lines: bool = True) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "id": self.id,
            "domain": self.domain,
            "prefix": self.prefix,
            "status": self.status,
            "createdAt": self.created_at,
            "startedAt": self.started_at,
            "finishedAt": self.finished_at,
            "currentIndex": self.current_index,
            "stages": [item.to_json() for item in self.stages],
            "meta": self.meta,
            "lastSeq": self._seq,
            "droppedLines": self._dropped_lines,
        }
        if include_lines:
            payload["lines"] = [line.to_json() for line in self.lines]
        return payload


class JobManager:
    """全局作业管理器：串行执行、保留最近若干次作业。"""

    def __init__(self, python: str, project_root: Path) -> None:
        self.python = python
        self.project_root = project_root
        self._jobs: dict[str, Job] = {}
        self._order: deque[str] = deque(maxlen=MAX_JOBS)
        self._lock = asyncio.Lock()
        self._active: Job | None = None

    # -- 查询 ---------------------------------------------------------------

    def get(self, job_id: str) -> Job | None:
        return self._jobs.get(job_id)

    def list_jobs(self) -> list[dict[str, Any]]:
        return [self._jobs[job_id].to_json(include_lines=False) for job_id in reversed(self._order)]

    @property
    def active(self) -> Job | None:
        return self._active

    # -- 创建 ---------------------------------------------------------------

    def create(
        self,
        stage_ids: list[str],
        ctx: RunContext,
        meta: dict[str, Any] | None = None,
    ) -> Job:
        unknown = [item for item in stage_ids if item not in STAGE_BY_ID]
        if unknown:
            raise ValueError(f"未知阶段：{', '.join(unknown)}")
        if not stage_ids:
            raise ValueError("至少选择一个阶段。")

        runs: list[StageRun] = []
        for stage_id in stage_ids:
            # 先算一次用于展示；真正执行时会在 _run_stage 里重建，
            # 这样前面阶段刚产出的文件能被后续阶段看到。
            command = _as_unbuffered(build_command(stage_id, ctx))
            runs.append(StageRun(stage_id=stage_id, command=command))

        job = Job(
            job_id=uuid.uuid4().hex[:12],
            domain=ctx.domain,
            prefix=ctx.prefix,
            stages=runs,
            ctx=ctx,
            meta=meta,
        )
        self._jobs[job.id] = job
        self._order.append(job.id)
        return job

    # -- 执行 ---------------------------------------------------------------

    async def run(self, job: Job) -> None:
        """在后台把作业跑完；同一时刻只允许一个作业。"""
        async with self._lock:
            self._active = job
            job.status = "running"
            job.started_at = time.time()
            job.publish_status()
            job.system(f"作业开始：{len(job.stages)} 个阶段 · 领域 {job.domain} · 前缀 {job.prefix}")

            try:
                for index, run in enumerate(job.stages):
                    job.current_index = index
                    job.publish_status()
                    ok = await self._run_stage(job, run)
                    if not ok:
                        job.status = "canceled" if job._cancel_requested else "failed"
                        for skipped in job.stages[index + 1 :]:
                            skipped.status = "skipped"
                        break
                else:
                    job.status = "succeeded"
            except Exception as exc:  # pragma: no cover - 防御性
                job.status = "failed"
                job.system(f"作业异常终止：{exc!r}")
            finally:
                job.finished_at = time.time()
                job.current_index = -1
                self._active = None
                duration = (job.finished_at - (job.started_at or job.finished_at))
                job.system(f"作业结束：{job.status}，耗时 {duration:.1f}s")
                job.publish_status()
                job._publish({"type": "done", "job": job.to_json(include_lines=False)})

    async def _run_stage(self, job: Job, run: StageRun) -> bool:
        stage = STAGE_BY_ID[run.stage_id]
        # 惰性重建：此时前面阶段的产物已经落盘，data_stats 这类
        # “有多少算多少”的阶段才能看到它们。
        try:
            run.command = _as_unbuffered(build_command(run.stage_id, job.ctx))
        except (ValueError, KeyError) as exc:
            run.status = "failed"
            run.error = str(exc)
            run.finished_at = time.time()
            job.system(f"阶段「{stage.title}」无法构造命令：{exc}")
            return False
        run.status = "running"
        run.started_at = time.time()
        job.publish_status()
        job.system(f"$ {' '.join(run.command)}")

        env = os.environ.copy()
        env["PYTHONUNBUFFERED"] = "1"
        env.setdefault("PYTHONIOENCODING", "utf-8")

        try:
            process = await asyncio.create_subprocess_exec(
                *run.command,
                cwd=str(self.project_root),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                env=env,
                start_new_session=True,
            )
        except FileNotFoundError as exc:
            run.status = "failed"
            run.error = f"无法启动进程：{exc}"
            run.finished_at = time.time()
            job.system(run.error)
            return False

        job._process = process
        await self._pump(job, process)

        returncode = await process.wait()
        job._process = None
        run.exit_code = returncode
        run.finished_at = time.time()
        run.duration = run.finished_at - (run.started_at or run.finished_at)

        if job._cancel_requested:
            run.status = "failed"
            run.error = "已取消"
            job.system(f"阶段「{stage.title}」已取消")
            return False
        if returncode != 0:
            run.status = "failed"
            run.error = f"退出码 {returncode}"
            job.system(f"阶段「{stage.title}」失败，退出码 {returncode}")
            return False

        run.status = "succeeded"
        job.system(f"阶段「{stage.title}」完成，耗时 {run.duration:.1f}s")
        job.publish_status()
        return True

    async def _pump(self, job: Job, process: asyncio.subprocess.Process) -> None:
        """把子进程输出按行（含 tqdm 的 \\r 刷新）喂给日志缓冲。"""
        assert process.stdout is not None
        buffer = ""
        while True:
            try:
                chunk = await process.stdout.read(8192)
            except asyncio.CancelledError:  # pragma: no cover
                raise
            if not chunk:
                break
            text = chunk.decode("utf-8", errors="replace")
            buffer += text
            lines, buffer = _split_lines(buffer)
            for content, transient in lines:
                if content.strip():
                    job.append(content.rstrip(), transient=transient)
        if buffer.strip():
            job.append(buffer.rstrip())

    # -- 取消 ---------------------------------------------------------------

    def cancel(self, job_id: str) -> bool:
        job = self._jobs.get(job_id)
        if job is None or job.status not in {"queued", "running"}:
            return False
        job._cancel_requested = True
        job.system("收到取消请求，正在终止子进程…")
        process = job._process
        if process is not None and process.returncode is None:
            _terminate_group(process)
        else:
            # 还没起进程（排队中），直接标记。
            job.status = "canceled"
            job.publish_status()
        return True


def _split_lines(buffer: str) -> tuple[list[tuple[str, bool]], str]:
    """按 ``\\n`` / ``\\r`` 切分；``\\r`` 结尾的片段标记为 transient（进度条）。"""
    result: list[tuple[str, bool]] = []
    current = ""
    index = 0
    length = len(buffer)
    while index < length:
        char = buffer[index]
        if char == "\n":
            result.append((current, False))
            current = ""
        elif char == "\r":
            # \r\n 视为普通换行。
            if index + 1 < length and buffer[index + 1] == "\n":
                result.append((current, False))
                current = ""
                index += 1
            else:
                if current:
                    result.append((current, True))
                current = ""
        else:
            current += char
        index += 1
    return result, current


def _terminate_group(process: asyncio.subprocess.Process) -> None:
    """先 SIGTERM 整个进程组，宽限后 SIGKILL。

    用线程定时器而不是 ``loop.call_later``，这样从任意线程（包括同步路由的
    AnyIO worker 线程）调用都不会依赖事件循环。
    """
    try:
        pgid = os.getpgid(process.pid)
    except (ProcessLookupError, PermissionError):
        return

    def _term() -> None:
        try:
            os.killpg(pgid, signal.SIGTERM)
        except ProcessLookupError:
            return
        except PermissionError:  # pragma: no cover
            process.terminate()

    _term()
    timer = threading.Timer(5.0, _force_kill, args=(process, pgid))
    timer.daemon = True
    timer.start()


def _force_kill(process: asyncio.subprocess.Process, pgid: int) -> None:
    if process.returncode is not None:
        return
    try:
        os.killpg(pgid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):  # pragma: no cover
        pass


async def stream_job(job: Job, since: int = 0) -> AsyncIterator[dict[str, Any]]:
    """SSE 事件生成器：先补历史日志，再持续推增量。"""
    queue = job.subscribe()
    try:
        yield {"type": "snapshot", "job": job.to_json(include_lines=False)}
        for line in list(job.lines):
            if line.seq > since:
                yield {"type": "log", "line": line.to_json()}
        if job.status in {"succeeded", "failed", "canceled"}:
            yield {"type": "done", "job": job.to_json(include_lines=False)}
            return
        while True:
            try:
                event = await asyncio.wait_for(queue.get(), timeout=15.0)
            except asyncio.TimeoutError:
                yield {"type": "ping"}
                if job.status in {"succeeded", "failed", "canceled"}:
                    yield {"type": "done", "job": job.to_json(include_lines=False)}
                    return
                continue
            yield event
            if event.get("type") == "done":
                return
    finally:
        job.unsubscribe(queue)


__all__ = ["Job", "JobManager", "LogLine", "StageRun", "stream_job"]
