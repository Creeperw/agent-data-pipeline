"""控制台原地重启的测试。

分两层：`agent.ui.restart` 自己的行为（记录命令行、重放、不可用时的报错），
以及 `/api/restart` 接口的三条分支（不可重启、有作业在跑、正常受理）。

正常受理那条会把 `restart()` 换成探针 —— 否则测试进程自己就被 execv 换掉了。
"""

from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient

from agent.config import PROJECT_ROOT
from agent.ui import restart as restart_module
from agent.ui.server import create_app


@pytest.fixture(autouse=True)
def _isolate_launch(monkeypatch: pytest.MonkeyPatch) -> None:
    """每个用例都从「没记过启动命令行」开始，互不串味。"""
    monkeypatch.setattr(restart_module, "_launch", None)
    monkeypatch.setattr(restart_module, "_launch_cwd", None)


# ---------------------------------------------------------------------------
# restart 模块
# ---------------------------------------------------------------------------


def test_unavailable_without_recorded_launch() -> None:
    assert restart_module.available() is False
    assert restart_module.launch_command() == []
    assert "没有记录启动命令行" in restart_module.unavailable_reason()
    with pytest.raises(RuntimeError):
        restart_module.restart()


def test_describe_reports_unavailable_reason() -> None:
    info = restart_module.describe()
    assert info["restartable"] is False
    assert info["restartBlockedReason"]
    assert info["launchCommand"] == ""
    assert info["pid"] == os.getpid()
    assert info["startedAt"] == restart_module.STARTED_AT


def test_record_launch_keeps_command() -> None:
    restart_module.record_launch(["python", "-m", "agent.ui", "--port", "8770"])
    assert restart_module.available() is True
    assert restart_module.launch_command() == ["python", "-m", "agent.ui", "--port", "8770"]
    assert restart_module.unavailable_reason() == ""
    assert restart_module.describe()["launchCommand"] == "python -m agent.ui --port 8770"


def test_restart_replays_command_in_launch_cwd(monkeypatch: pytest.MonkeyPatch) -> None:
    """重启执行的就是当初那条命令行，并且回到启动目录 —— 相对路径的 argv[0] 靠它才找得到。"""
    command = ["/usr/bin/python", "agent/ui/__main__.py", "--port", "8770"]
    restart_module.record_launch(command, cwd=str(PROJECT_ROOT))

    calls: dict[str, object] = {}
    monkeypatch.setattr(restart_module.os, "chdir", lambda path: calls.update(cwd=path))
    monkeypatch.setattr(
        restart_module.os, "execv", lambda exe, argv: calls.update(exe=exe, argv=list(argv))
    )

    restart_module.restart()

    assert calls["exe"] == "/usr/bin/python"
    assert calls["argv"] == command
    assert calls["cwd"] == str(PROJECT_ROOT)


# ---------------------------------------------------------------------------
# /api/restart
# ---------------------------------------------------------------------------


def test_meta_exposes_restart_capability() -> None:
    payload = TestClient(create_app()).get("/api/meta").json()
    for key in ("startedAt", "pid", "restartable", "restartBlockedReason", "launchCommand"):
        assert key in payload, key
    # 测试进程没走过 __main__，所以这里应该是「不可重启」并且给出原因。
    assert payload["restartable"] is False
    assert payload["restartBlockedReason"]


def test_restart_endpoint_rejects_when_not_restartable() -> None:
    response = TestClient(create_app()).post("/api/restart")
    assert response.status_code == 409
    assert "没有记录启动命令行" in response.json()["detail"]


def test_restart_endpoint_rejects_while_job_running(monkeypatch: pytest.MonkeyPatch) -> None:
    """有作业在跑时拒绝：作业是本进程的子进程，重启会切断它的日志。"""
    restart_module.record_launch(["python", "-m", "agent.ui"])

    class FakeJob:
        id = "deadbeef1234"

    app = create_app()
    monkeypatch.setattr(app.state.manager, "_active", FakeJob())

    response = TestClient(app).post("/api/restart")

    assert response.status_code == 409
    assert "deadbeef1234" in response.json()["detail"]


def test_restart_endpoint_schedules_in_place_restart(monkeypatch: pytest.MonkeyPatch) -> None:
    restart_module.record_launch(["python", "-m", "agent.ui"])

    calls: list[str] = []
    monkeypatch.setattr(restart_module, "restart", lambda: calls.append("restart"))

    response = TestClient(create_app()).post("/api/restart")

    assert response.status_code == 200
    assert response.json()["ok"] is True
    assert response.json()["restartable"] is True
    # 响应先发出去、再换进程：TestClient 会把后台任务跑完，探针应当被调用过一次。
    assert calls == ["restart"]
