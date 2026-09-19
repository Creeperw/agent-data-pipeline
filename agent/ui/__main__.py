"""``python -m agent.ui`` 启动入口。

示例：

    python -m agent.ui                     # http://127.0.0.1:8770
    python -m agent.ui --port 9000
    python -m agent.ui --host 0.0.0.0      # 局域网可访问，注意鉴权风险
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

if __package__ in (None, ""):  # pragma: no cover - 允许直接执行本文件
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="启动 agent 数据流水线本地控制台。")
    parser.add_argument("--host", default="127.0.0.1", help="监听地址，默认 127.0.0.1。")
    parser.add_argument("--port", type=int, default=8770, help="监听端口，默认 8770。")
    parser.add_argument("--reload", action="store_true", help="开发模式：改动即重启。")
    parser.add_argument("--log-level", default="info", help="uvicorn 日志级别。")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    import uvicorn

    print(f"→ Agent 数据流水线控制台：http://{args.host}:{args.port}")
    if args.host not in {"127.0.0.1", "localhost"}:
        print("  注意：已监听非本机地址，同网段的其他机器可以访问该服务。")

    if args.reload:
        # uvicorn 的 reload / workers 只接受 import string，不能传 app 实例。
        uvicorn.run(
            "agent.ui.server:create_app",
            factory=True,
            host=args.host,
            port=args.port,
            log_level=args.log_level,
            reload=True,
            reload_dirs=[str(Path(__file__).resolve().parent)],
        )
        return

    try:
        from .server import create_app
    except ImportError:  # pragma: no cover
        from agent.ui.server import create_app  # type: ignore

    uvicorn.run(create_app(), host=args.host, port=args.port, log_level=args.log_level)


if __name__ == "__main__":
    main()
