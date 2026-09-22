"""Regression tests for the v1.3 desktop distribution contract."""

from __future__ import annotations

from pathlib import Path
import sys

from fastapi.testclient import TestClient

import launcher
from agent.ui.server import create_app


def test_health_endpoint_is_lightweight() -> None:
    response = TestClient(create_app()).get("/api/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"
    assert response.json()["version"] == "1.3.0"


def test_free_port_is_bindable() -> None:
    port = launcher._free_port()
    assert 0 < port < 65536


def test_uvicorn_config_supports_gui_without_standard_streams(monkeypatch) -> None:
    with monkeypatch.context() as patch:
        patch.setattr(sys, "stdout", None)
        patch.setattr(sys, "stderr", None)
        config = launcher._create_uvicorn_config(create_app(), "127.0.0.1", 8770)

    assert config.log_config is None
    assert config.access_log is False


def test_stage_argv_dispatch() -> None:
    original = launcher.sys.argv
    try:
        launcher.sys.argv = ["AgentDataPipeline.exe", "-m", "agent.data_stats", "--help"]
        assert launcher._stage_from_argv() == ("agent.data_stats", ["--help"])
        launcher.sys.argv = [
            "AgentDataPipeline.exe", "--stage-module", "agent.data_stats", "--format", "json"
        ]
        assert launcher._stage_from_argv() == ("agent.data_stats", ["--format", "json"])
    finally:
        launcher.sys.argv = original


def test_initialize_user_data_preserves_existing_files(tmp_path: Path, monkeypatch) -> None:
    import agent.config as config

    resources = tmp_path / "resources" / "agent"
    user = tmp_path / "user"
    (resources / "domains" / "demo").mkdir(parents=True)
    (resources / "tools").mkdir(parents=True)
    (resources / "domains" / "demo" / "spec.py").write_text("VALUE = 'default'\n", encoding="utf-8")
    (resources / "tools" / "sample.py").write_text("SPEC = {}\n", encoding="utf-8")
    (resources / ".env.example").write_text("KEY=\n", encoding="utf-8")
    (user / "domains" / "demo").mkdir(parents=True)
    existing = user / "domains" / "demo" / "spec.py"
    existing.write_text("VALUE = 'custom'\n", encoding="utf-8")

    monkeypatch.setattr(config, "IS_FROZEN", True)
    monkeypatch.setattr(config, "RESOURCE_AGENT_ROOT", resources)
    monkeypatch.setattr(config, "AGENT_ROOT", user)
    monkeypatch.setattr(config, "AGENT_DATA_DIR", user / "data")
    monkeypatch.setattr(config, "AGENT_OUTPUTS_DIR", user / "outputs")
    config.initialize_user_data()

    assert existing.read_text(encoding="utf-8") == "VALUE = 'custom'\n"
    assert (user / "tools" / "sample.py").is_file()
    assert (user / ".env").read_text(encoding="utf-8") == "KEY=\n"