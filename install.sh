#!/usr/bin/env bash
#
# 一键安装：建虚拟环境 → 装依赖 → 生成 agent/.env → 打印后续步骤。
#
#   ./install.sh                  # 装到仓库里的 .venv
#   PYTHON=python3.12 ./install.sh
#   ./install.sh --no-venv        # 直接用当前解释器（不推荐，会污染系统环境）
#   ./install.sh --skip-deps      # 只做检查和 .env 准备
#
# 已经存在的 agent/.env 不会被覆盖：里面可能填着你的密钥。

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$REPO_ROOT"

USE_VENV=1
SKIP_DEPS=0
for arg in "$@"; do
  case "$arg" in
    --no-venv)   USE_VENV=0 ;;
    --skip-deps) SKIP_DEPS=1 ;;
    -h|--help)
      # 打文件开头的注释块：从第 2 行到第一个空行为止，加了参数也不用改范围。
      sed -n '2,/^$/p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
      exit 0
      ;;
    *) echo "未知参数：$arg（用 --help 看用法）" >&2; exit 2 ;;
  esac
done

say()  { printf '%s\n' "$*"; }
fail() { printf '错误：%s\n' "$*" >&2; exit 1; }

# ---------------------------------------------------------------- 解释器
# 版本下限与 pyproject.toml 的 requires-python 保持一致。
VERSION_OK='raise SystemExit(0 if __import__("sys").version_info >= (3, 10) else 1)'

# PATH 里的 python3.12 不一定能用：Debian/Ubuntu 把 ensurepip 单独打包进
# python3.x-venv，缺了它 -m venv 会直接失败。所以逐个试，只认能建环境的那个。
usable() {
  command -v "$1" >/dev/null 2>&1 || return 1
  "$1" -c "$VERSION_OK" 2>/dev/null || return 1
  if [ "$USE_VENV" = "1" ]; then
    "$1" -c 'import venv, ensurepip' 2>/dev/null || return 1
  fi
  return 0
}

PYTHON="${PYTHON:-}"
if [ -n "$PYTHON" ]; then
  usable "$PYTHON" || fail "指定的解释器 $PYTHON 不可用：需要 3.10+，建虚拟环境还要求自带 ensurepip 模块。"
else
  SKIPPED=""
  for candidate in python3.12 python3.11 python3.10 python3 python; do
    if usable "$candidate"; then PYTHON="$candidate"; break; fi
    if command -v "$candidate" >/dev/null 2>&1; then SKIPPED="$SKIPPED $candidate"; fi
  done
  if [ -z "$PYTHON" ]; then
    if [ -n "$SKIPPED" ]; then
      say "错误：PATH 里的$SKIPPED 都不能用来建虚拟环境（需要 3.10+，且自带 ensurepip）。" >&2
      say "" >&2
      say "  Debian/Ubuntu 上装一下 python3-venv 即可，例如：apt install python3.12-venv" >&2
      say "  或者指定一个可用的解释器：PYTHON=/path/to/python3 ./install.sh" >&2
      exit 1
    fi
    fail "找不到 python3，请先安装 Python 3.10 或更高版本。"
  fi
  if [ -n "$SKIPPED" ]; then
    say "已跳过$SKIPPED（不适合建虚拟环境）"
  fi
fi

say "解释器    $("$PYTHON" -c 'import sys; print(sys.executable)')"
say "版本      $("$PYTHON" -c 'import sys; print(".".join(map(str, sys.version_info[:3])))')"
say "仓库      $REPO_ROOT"

# ---------------------------------------------------------------- 虚拟环境
if [ "$USE_VENV" = "1" ]; then
  VENV_DIR="$REPO_ROOT/.venv"
  if [ ! -x "$VENV_DIR/bin/python" ]; then
    say "创建虚拟环境 .venv …"
    "$PYTHON" -m venv "$VENV_DIR" || fail "创建虚拟环境失败。Debian/Ubuntu 上可能需要先装 python3-venv。"
  else
    say "复用已有的 .venv"
  fi
  PY="$VENV_DIR/bin/python"
else
  PY="$PYTHON"
  say "跳过虚拟环境，直接使用当前解释器"
fi

# ---------------------------------------------------------------- 依赖
if [ "$SKIP_DEPS" = "0" ]; then
  say "安装依赖（首次会下载 transformers，需要几分钟）…"
  # 可编辑安装：包仍然指向仓库本身，控制台才能往 agent/tools、agent/.env 里写文件。
  "$PY" -m pip install --quiet --upgrade pip
  "$PY" -m pip install --quiet -e . || fail "依赖安装失败，请检查网络或 pip 源。"
  say "依赖安装完成"
else
  say "跳过依赖安装"
fi

# ---------------------------------------------------------------- 配置
ENV_FILE="$REPO_ROOT/agent/.env"
if [ -f "$ENV_FILE" ]; then
  say "agent/.env 已存在，保持原样（不覆盖你的密钥）"
else
  cp "$REPO_ROOT/agent/.env.example" "$ENV_FILE"
  say "已生成 agent/.env（下一步要填密钥）"
fi

# ---------------------------------------------------------------- 收尾
say ""
say "────────────────────────────────────────────────────────"
say "接下来："
say ""
if [ -f "$ENV_FILE" ] && ! grep -qE '^DEEPSEEK_API_KEY=sk-[^x]' "$ENV_FILE" 2>/dev/null; then
  say "  1. 编辑 agent/.env，至少填好这三项："
  say "       DEEPSEEK_API_KEY / DEEPSEEK_BASE_URL / AGENT_DISTILL_MODEL"
  say "     也可以启动控制台后点顶栏齿轮填。"
  N=2
else
  N=1
fi
if [ "$USE_VENV" = "1" ]; then
  say "  $N. 启动控制台："
  say "       .venv/bin/python -m agent.ui"
  say "     （或先 source .venv/bin/activate，之后直接敲 agent-pipeline）"
else
  say "  $N. 启动控制台："
  say "       $PY -m agent.ui"
fi
say ""
say "  浏览器打开   http://127.0.0.1:8770"
say "────────────────────────────────────────────────────────"
