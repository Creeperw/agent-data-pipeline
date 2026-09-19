"""``python -m agent.ui`` 启动入口。

示例：

    python -m agent.ui                     # http://127.0.0.1:8770
    python -m agent.ui --port 9000
    python -m agent.ui --host 0.0.0.0      # 局域网可访问，注意鉴权风险
"""

from __future__ import annotations

import argparse
import os
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


def check_layout() -> None:
    """确认这个包是从仓库里运行的，而不是被装进 site-packages。

    本项目的所有路径都由 ``__file__`` 推导：``agent/.env``（运行时配置）、
    ``agent/tools/``（控制台会往里写新工具）、``agent/domains/<领域>/seeds.jsonl``
    （在线编辑）、``agent/data``、``agent/outputs``，以及仓库同级的 ``MODELS/``。
    也就是说包目录本身就是工作目录，必须可写、可见。

    装进 site-packages 后这些路径会指向库目录：轻则配置和产物散落在库里、
    升级时被覆盖，重则在只读的 site-packages 上直接报权限错。与其让人对着一堆
    路径报错排查，不如在这里直接说清该怎么做。
    """
    package_dir = Path(__file__).resolve().parents[1]
    if not ({p.lower() for p in package_dir.parts} & {"site-packages", "dist-packages"}):
        return
    if os.environ.get("AGENT_ALLOW_SITE_PACKAGES") == "1":
        print("  警告：正在从 site-packages 运行（AGENT_ALLOW_SITE_PACKAGES=1）。")
        print("        配置与产物会写进库目录，升级或重装时可能被覆盖。")
        return

    print("错误：这个包被装进了 site-packages，无法从那里运行。", file=sys.stderr)
    print("", file=sys.stderr)
    print(f"      包目录：{package_dir}", file=sys.stderr)
    print("", file=sys.stderr)
    print("控制台、seed 编辑器和新工具都要往包目录里写文件，", file=sys.stderr)
    print("所以它必须运行在一个可读写的仓库副本里。请改用以下任一方式：", file=sys.stderr)
    print("", file=sys.stderr)
    print("  1) 克隆仓库后原地运行（推荐）：", file=sys.stderr)
    print("       git clone https://github.com/Creeperw/agent-data-pipeline.git", file=sys.stderr)
    print("       cd agent-data-pipeline && ./install.sh", file=sys.stderr)
    print("", file=sys.stderr)
    print("  2) 已经克隆过了，就改成可编辑安装，让包指回仓库：", file=sys.stderr)
    print("       pip uninstall agent-data-pipeline", file=sys.stderr)
    print("       pip install -e .", file=sys.stderr)
    print("", file=sys.stderr)
    print("  3) 确认自己知道后果，也可以强行跳过本检查：", file=sys.stderr)
    print("       AGENT_ALLOW_SITE_PACKAGES=1", file=sys.stderr)
    raise SystemExit(2)


def main() -> None:
    check_layout()
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
