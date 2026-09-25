#!/usr/bin/env bash
# 直接以源码方式启动（无需打包）
#   bash start.sh              # 本机访问 http://127.0.0.1:8765
#   bash start.sh --host 0.0.0.0 --port 8765   # 允许局域网访问
# 建议 sudo 运行以启用服务管理 / 内存清理：
#   sudo bash start.sh

set -e
cd "$(dirname "$0")"

PY=${PYTHON:-python3}
$PY -m pip install --user -r requirements.txt >/dev/null 2>&1 || true
exec $PY app.py "$@"
