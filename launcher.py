"""Desktop launcher for the Agent Data Pipeline distribution.

The same executable is used as the UI server and as a stage subprocess in a
PyInstaller build. ``--stage-module`` is intentionally internal and keeps the
existing ``python -m agent.<module>`` command construction unchanged.
"""

from __future__ import annotations

import argparse
import logging
import os
import socket
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path


def _free_port(host: str = "127.0.0.1") -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((host, 0))
        return int(sock.getsockname()[1])


def _default_user_data_root() -> Path:
    configured = (os.getenv("AGENT_USER_DATA_DIR") or "").strip()
    if configured:
        return Path(configured).expanduser()
    if os.name == "nt":
        return Path(os.getenv("APPDATA") or Path.home()) / "AgentDataPipeline"
    return Path(os.getenv("XDG_DATA_HOME") or (Path.home() / ".local" / "share")) / "AgentDataPipeline"


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Agent Data Pipeline")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--no-browser", action="store_true")
    return parser.parse_args()


def _prepare_frozen_environment() -> None:
    if getattr(sys, "frozen", False):
        os.environ.setdefault("AGENT_RESOURCE_ROOT", str(getattr(sys, "_MEIPASS", Path(sys.executable).parent)))
        os.environ.setdefault("AGENT_USER_DATA_DIR", str(_default_user_data_root()))


def _stage_from_argv() -> tuple[str, list[str]] | None:
    """Recognize packaged child-stage commands before argparse consumes them."""

    for flag in ("--stage-module", "-m"):
        if flag not in sys.argv[1:]:
            continue
        index = sys.argv.index(flag)
        if index + 1 < len(sys.argv):
            return sys.argv[index + 1], sys.argv[index + 2 :]
    return None


def _run_stage(module: str, args: list[str]) -> int:
    import runpy

    old_argv = sys.argv[:]
    sys.argv = [module, *args]
    try:
        runpy.run_module(module, run_name="__main__")
    except SystemExit as exc:
        return int(exc.code or 0)
    finally:
        sys.argv = old_argv
    return 0


def run_stage_subprocess(module: str, args: list[str]) -> int:
    return _run_stage(module, args)


def _wait_for_health(url: str, timeout: float = 30.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=1.0) as response:
                return response.status == 200
        except (OSError, urllib.error.URLError):
            time.sleep(0.15)
    return False


def _open_browser(url: str) -> None:
    # Opening from a short-lived helper avoids blocking the server startup on
    # platforms where the browser command waits for an existing process.
    threading.Thread(target=lambda: webbrowser.open(url), daemon=True).start()


def _configure_file_logging() -> Path:
    log_dir = _default_user_data_root() / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / "launcher.log"
    logging.basicConfig(
        filename=log_file,
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        encoding="utf-8",
    )
    return log_file


def _show_fatal_error(message: str) -> None:
    logging.exception(message)
    if os.name == "nt":
        try:
            import ctypes

            ctypes.windll.user32.MessageBoxW(0, message, "Agent Data Pipeline", 0x10)
        except Exception:
            pass


def _create_uvicorn_config(app: object, host: str, port: int):
    """Create a server config that also works in a ``console=False`` build.

    PyInstaller's Windows GUI bootloader leaves ``sys.stdout`` and
    ``sys.stderr`` unset.  Uvicorn's default colour-aware formatters inspect
    ``isatty()`` on those streams during ``Config`` construction, so using the
    default log config makes the desktop executable fail before it binds its
    port.  The launcher already configures a persistent file logger; disabling
    Uvicorn's console log config makes its records follow that logger instead.
    """

    import uvicorn

    return uvicorn.Config(
        app,
        host=host,
        port=port,
        log_level="info",
        access_log=False,
        log_config=None,
    )


def main() -> int:
    _prepare_frozen_environment()
    log_file = _configure_file_logging() if getattr(sys, "frozen", False) else None
    child = _stage_from_argv()
    if child is not None:
        return _run_stage(*child)
    args = _parse_args()

    port = args.port or _free_port(args.host)
    url = f"http://{args.host}:{port}"
    os.environ["AGENT_UI_PORT"] = str(port)
    print(f"Agent Data Pipeline starting at {url}", flush=True)
    logging.info("Agent Data Pipeline starting at %s", url)

    if getattr(sys, "frozen", False):
        from agent.ui.server import create_app
        from agent.ui.restart import record_launch
        import uvicorn

        record_launch([sys.executable, *sys.argv], cwd=os.getcwd())
        server = uvicorn.Server(
            _create_uvicorn_config(create_app(), args.host, port)
        )
        thread = threading.Thread(target=server.run, daemon=True)
        thread.start()
        if not _wait_for_health(f"{url}/api/health"):
            message = f"The local console did not become ready within 30 seconds. Log: {log_file}"
            print(message, file=sys.stderr)
            logging.error(message)
            return 1
        if not args.no_browser:
            _open_browser(url)
        try:
            while thread.is_alive():
                thread.join(timeout=0.5)
        except KeyboardInterrupt:
            server.should_exit = True
        return 0

    from agent.ui.__main__ import main as source_main

    sys.argv = [sys.argv[0], "--host", args.host, "--port", str(port)]
    source_main()
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except Exception as exc:
        message = f"Agent Data Pipeline 启动失败：{exc}\n\n日志目录：{_default_user_data_root() / 'logs'}"
        _show_fatal_error(message)
        raise