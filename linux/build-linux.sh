#!/usr/bin/env bash
# Linux进程管理工具（潍鲸 - weijing.co） - 一键打包脚本
# 在 Linux（x86_64 / aarch64 均可）上执行：
#   bash build-linux.sh
# 产物：dist/LinuxProcessManager（单文件，可直接拷贝到同架构机器运行）

set -e
cd "$(dirname "$0")"

PY=${PYTHON:-python3}

echo "==> 安装构建依赖（psutil + pyinstaller）"
$PY -m pip install --user -r requirements.txt "pyinstaller>=6.0" || {
  echo "pip 安装失败，请检查 python3-pip 是否安装（如: sudo apt install python3-pip）"
  exit 1
}

echo "==> 预检：编译检查所有源码"
$PY -m compileall -q app.py core/

echo "==> PyInstaller 打包"
$PY -m PyInstaller build-linux.spec --noconfirm

echo "==> 产物"
ls -lh dist/LinuxProcessManager
echo "运行方式：./dist/LinuxProcessManager [--port 8765] [--host 127.0.0.1]"
echo "建议以 root 运行以启用服务管理与内存清理：sudo ./dist/LinuxProcessManager"
