#!/usr/bin/env bash
# ============================================================================
#  BigPixels 放大台 · macOS / Linux 启动脚本
#
#      ./start_web.sh               起服务并自动开浏览器
#      ./start_web.sh --check       只自检（环境 + 模型），不起服务
#      PORT=8766 ./start_web.sh     换端口（默认 8765）
#      ./start_web.sh --source hf   模型走 huggingface.co（默认走国内镜像）
#
#  和 Windows 那边一样，这里也只负责「找到一个能用的 Python 3.9+」，
#  剩下三件事（环境 → 模型 → 服务）全交给 app/server/bootstrap.py。中文提示都在
#  Python 里，脚本本身不输出任何东西 —— 两边行为保持一致，省得各改一遍。
#
#  第一次跑先给执行权限：  chmod +x start_web.sh
# ============================================================================
set -euo pipefail

cd "$(dirname "$0")"

PORT="${PORT:-8765}"

# 临时文件、pip 缓存都收在本目录的 .cache 下，不往家目录写
export TEMP="$PWD/.cache/tmp"
export TMP="$PWD/.cache/tmp"
export PIP_CACHE_DIR="$PWD/.cache/pip"
mkdir -p "$TEMP" "$PIP_CACHE_DIR"

# 子进程一律按 UTF-8 说话（跟 Windows 那边的 chcp 65001 对齐）
export PYTHONIOENCODING=utf-8
export PYTHONUTF8=1

VENV="$PWD/.venv"
PY="$VENV/bin/python"

# 找一个 3.9 以上的解释器。onnxruntime 的轮子从那儿开始发，代码也用了新语法。
find_python() {
  local c
  for c in python3 python; do
    if command -v "$c" >/dev/null 2>&1; then
      if "$c" -c 'import sys;sys.exit(0 if sys.version_info>=(3,9) else 1)' >/dev/null 2>&1; then
        printf '%s' "$c"
        return 0
      fi
    fi
  done
  return 1
}

if [ -x "$PY" ]; then
  RUNNER="$PY"
elif RUNNER="$(find_python)"; then
  :
else
  echo
  echo "  ！没找到 Python 3.9 以上。"
  echo
  echo "     macOS   brew install python"
  echo "     Debian  sudo apt install python3 python3-venv"
  echo
  echo "     装完再跑一次本脚本即可。"
  echo
  exit 1
fi

exec "$RUNNER" app/server/bootstrap.py --port "$PORT" --venv "$VENV" "$@"
