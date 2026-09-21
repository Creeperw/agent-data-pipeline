"""控制台原地重启。

为什么需要它：控制台是常驻进程，``agent/core/**``（DomainSpec、renderer、
schemas）、``agent/config.py``、``agent/ui/*.py`` 都只在进程启动时导入一次，
之后改这些文件不会生效 —— 领域包的热加载只覆盖 ``agent/domains/<领域>.*``。
以前只能回终端 Ctrl-C 再起一遍，界面上没有入口。

做法是 ``os.execv`` 原地替换进程镜像：PID、终端、日志重定向、会话都不变，
所以 ``nohup python -m agent.ui > /tmp/agent-ui.log &`` 这类启动方式重启后依然
写同一个日志。监听套接字带 CLOEXEC，内核会在 exec 时关掉它，因此新进程能立刻
重新 bind 同一个端口，不会撞「Address already in use」。

只有「自己就是服务进程」的启动方式（``python -m agent.ui``）会在 ``main()`` 里
记下命令行。uvicorn 的 ``--reload`` 是父进程另起子进程跑
``agent.ui.server:create_app``，子进程不经过 ``__main__``，记不到东西 ——
那种情况 ``available()`` 返回 False，界面会说明「开发模式自动重载，不用手动重启」。
"""

from __future__ import annotations

import os
import time
from typing import Any

# 本模块被导入的时刻，近似「这份代码开始生效的时刻」。
#
# 界面靠它判断「响应的是新进程还是旧进程」：execv 不换 PID，所以比 PID 没用；
# 而 execv 之后模块会被重新导入，这个值一定变。
STARTED_AT = time.time()

_launch: list[str] | None = None
_launch_cwd: str | None = None


def record_launch(command: list[str], cwd: str | None = None) -> None:
    """记下启动命令行，重启时原样重放。由 ``agent/ui/__main__.py`` 调用。

    ``command`` 传的是真实用过的命令行（含 ``sys.argv[0]``），不是拼出来的
    ``-m agent.ui``：从别的目录用绝对路径启动时，拼 ``-m`` 会因为当前目录不在
    ``sys.path`` 而失败，照搬原命令行则永远能起来。
    """
    global _launch, _launch_cwd
    _launch = list(command)
    _launch_cwd = cwd or None


def available() -> bool:
    """当前进程能不能原地重启。"""
    return bool(_launch)


def launch_command() -> list[str]:
    """重启时会执行的命令行；不可重启时返回空列表。"""
    return list(_launch or [])


def unavailable_reason() -> str:
    """不能重启时给界面看的原因；能重启时返回空串。"""
    if _launch:
        return ""
    return (
        "当前进程没有记录启动命令行，无法原地重启。"
        "（用 uvicorn --reload 或自定义脚本启动时是这样；--reload 下改动本来就会自动生效。）"
    )


def describe() -> dict[str, Any]:
    """给 ``/api/meta`` 的重启能力快照。"""
    return {
        "startedAt": STARTED_AT,
        "pid": os.getpid(),
        "restartable": available(),
        "restartBlockedReason": unavailable_reason(),
        "launchCommand": " ".join(_launch or []),
    }


def restart() -> None:
    """原地重启。成功时**不返回** —— 当前进程镜像已经被替换掉。"""
    if not _launch:
        raise RuntimeError(unavailable_reason())
    # execv 保留 cwd，但如果启动时用的是相对路径（``python agent/ui/__main__.py``），
    # 只有在原来的目录下才找得到它，所以显式回到启动目录再 exec。
    if _launch_cwd and os.path.isdir(_launch_cwd):
        try:
            os.chdir(_launch_cwd)
        except OSError:  # pragma: no cover - 目录突然不可访问，按原 cwd 继续
            pass
    os.execv(_launch[0], _launch)
