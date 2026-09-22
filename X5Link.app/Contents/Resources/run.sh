#!/bin/bash
# X5Link.app 的干活脚本：把参数转给 python 工具，输出落到 x5link-app.log。
# 由原生入口（Contents/MacOS/X5Link）作为子进程拉起。
#
# 调试用后门：第一个参数是 --exec 时，后面整串当成命令原样执行。
# 这样 ffmpeg 之类的工具也能跑在 bundle 里、拿到摄像头权限。

set -u

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"   # .../X5Link.app/Contents/Resources
X5DIR="$(cd "$HERE/../../.." && pwd)"                   # .../x5-link（三层：Resources -> Contents -> X5Link.app -> x5-link）
PY="$X5DIR/../3dgs-lab/.venv/bin/python"
LOG="$X5DIR/x5link-app.log"

: > "$LOG"
export PYTHONPATH="$X5DIR${PYTHONPATH:+:$PYTHONPATH}"
export PYTHONUNBUFFERED=1

{
  echo "=== $(date '+%F %T')  X5Link 启动 ==="
  echo "args: $*"
  echo "python: $PY"
  echo
} >> "$LOG"

if [ "${1:-}" = "--exec" ]; then
  shift
  echo "cmd: $*" >> "$LOG"
  "$@" >> "$LOG" 2>&1
  rc=$?
else
  "$PY" -m x5link "$@" >> "$LOG" 2>&1
  rc=$?
fi

echo >> "$LOG"
echo "=== 退出码 $rc  $(date '+%F %T') ===" >> "$LOG"
exit "$rc"
