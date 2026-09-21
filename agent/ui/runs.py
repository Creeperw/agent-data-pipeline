"""运行记录：把每次作业的摘要落盘，让「按运行批次看产物」在重启之后依然成立。

为什么要落盘
------------
``Job`` 只活在内存里（``JobManager`` 保留最近 40 次），控制台一重启，「这批文件
是哪次跑出来的」就没人答得上来 —— 而产物文件在磁盘上是持久的。更麻烦的是产物
路径是固定的：同名文件会被下一次运行覆盖，于是「现在的文件」和「某次运行产出的
文件」是两码事，只看目录根本分不清。

这里在作业结束时把摘要追加到 ``<输出目录>/runs.jsonl``，其中给每个产物拍一张
``size`` / ``mtime`` 快照。于是产物页可以：

- 按运行时间分批展示，一批 = 一次运行，不用在几十个文件里靠时间戳自己猜；
- 拿快照和当前磁盘状态比对，看出某个产物在后续运行里是否被覆盖过。

写盘失败不影响作业本身：运行记录是辅助信息，不能因为写不进一行日志就把一次
成功的运行显示成失败（调用方已包 try/except）。
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

try:
    from ..config import PROJECT_ROOT
except ImportError:  # pragma: no cover - 兼容直接加载
    from agent.config import PROJECT_ROOT  # type: ignore

from .registry import ARTIFACTS, STAGE_BY_ID, artifact_path, domain_output_dir

RUNS_FILE = "runs.jsonl"
# 记录条数上限。一次运行一行，正常项目几十行就够了；超了说明跑了很久，
# 此时保留最近的即可 —— 归档属于运维的事，控制台只需要「最近的能看」。
MAX_RECORDS = 200
# 触发裁剪的文件体积。写一行才几百字节，到 512 KB 说明积累了几百次运行。
PRUNE_BYTES = 512 * 1024


def runs_path(domain: str) -> Path:
    """该领域的运行记录文件。跟随 ``domain`` 隔离，不跨领域串味。"""
    return domain_output_dir(domain) / RUNS_FILE


def _snapshot(domain: str, prefix: str) -> list[dict[str, Any]]:
    """给每个声明的产物拍一张 stat 快照。

    只取 ``size`` / ``mtime``（一次 stat，代价 O(1)）。行数不记：那要逐行读文件，
    几百 MB 的 jsonl 会让作业收尾白等好几秒，而那个数字随时能从 ``/api/artifacts``
    拿到当前值。
    """
    items: list[dict[str, Any]] = []
    for artifact in ARTIFACTS:
        item: dict[str, Any] = {
            "key": artifact.key,
            "label": artifact.label,
            "exists": False,
        }
        path = artifact_path(artifact.key, domain, prefix)
        # 记录相对项目根目录的路径：任务目录改造后，历史批次不能再拿当前
        # 顶栏任务的 ``/api/artifacts`` 结果来猜自己的文件在哪里。
        try:
            item["path"] = str(path.relative_to(PROJECT_ROOT))
        except ValueError:
            item["path"] = str(path)
        try:
            stat = path.stat()
        except OSError:
            items.append(item)
            continue
        item.update(
            {
                "exists": True,
                "size": stat.st_size,
                "mtime": stat.st_mtime,
                "isDir": path.is_dir(),
            }
        )
        items.append(item)
    return items


def build_record(job: Any) -> dict[str, Any]:
    """把一个已结束的作业整理成一条运行记录。

    只留产物页和运行页用得到的字段：命令行（``command``）里有本机绝对路径，
    日志有 ``lines``，两者都不进记录 —— 记录是给人看「什么时候跑了什么、产出
    了哪些文件」的，不是日志的副本。
    """
    started = job.started_at
    finished = job.finished_at
    stages = []
    for run in job.stages:
        stage = STAGE_BY_ID.get(run.stage_id)
        stages.append(
            {
                "stageId": run.stage_id,
                "title": stage.title if stage else run.stage_id,
                "status": run.status,
                "exitCode": run.exit_code,
                "duration": run.duration,
                "error": run.error,
            }
        )
    return {
        "id": job.id,
        "domain": job.domain,
        "prefix": job.prefix,
        "status": job.status,
        "createdAt": job.created_at,
        # 没跑起来就被取消的作业没有 started_at；此时用创建时间兜底，
        # 否则它会在按时间排序时沉到最底下，看着像丢了。
        "startedAt": started if started is not None else job.created_at,
        "finishedAt": finished,
        "duration": (finished - started) if (started and finished) else None,
        "stages": stages,
        "artifacts": _snapshot(job.domain, job.prefix),
        "recordedAt": time.time(),
    }


def record_run(job: Any) -> dict[str, Any]:
    """追加一条运行记录，必要时裁剪旧记录。返回写下的记录。"""
    payload = build_record(job)
    path = runs_path(job.domain)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False) + "\n")
    try:
        if path.stat().st_size > PRUNE_BYTES:
            _prune(path)
    except OSError:
        pass
    return payload


def _prune(path: Path) -> None:
    """只保留最近 ``MAX_RECORDS`` 条，用临时文件替换，避免写坏原文件。"""
    records = _read(path)
    if len(records) <= MAX_RECORDS:
        return
    records.sort(key=lambda item: item.get("startedAt") or item.get("createdAt") or 0)
    keep = records[-MAX_RECORDS:]
    temp = path.with_suffix(".jsonl.tmp")
    with temp.open("w", encoding="utf-8") as handle:
        for item in keep:
            handle.write(json.dumps(item, ensure_ascii=False) + "\n")
    temp.replace(path)


def _read(path: Path) -> list[dict[str, Any]]:
    """逐行读取；坏行跳过而不是整份作废（写到一半断电会留下残行）。"""
    records: list[dict[str, Any]] = []
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                text = line.strip()
                if not text:
                    continue
                try:
                    item = json.loads(text)
                except json.JSONDecodeError:
                    continue
                if isinstance(item, dict):
                    records.append(item)
    except OSError:
        return []
    return records


def load_runs(domain: str, limit: int = 20) -> list[dict[str, Any]]:
    """按开始时间倒序返回运行记录，最近的在前。"""
    records = _read(runs_path(domain))
    # 同一 id 只认最后一条：记录是追加写的，重复写入时后来的才代表最终状态。
    latest: dict[str, dict[str, Any]] = {}
    for item in records:
        key = str(item.get("id") or f"__anon_{id(item)}")
        latest[key] = item
    ordered = sorted(
        latest.values(),
        key=lambda item: item.get("startedAt") or item.get("createdAt") or 0,
        reverse=True,
    )
    return ordered[:limit]
