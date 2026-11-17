#!/bin/zsh
set -eu
PROJECT_DIR=${0:A:h}
cd "$PROJECT_DIR"
if [[ ! -S "$PROJECT_DIR/runtime/camera.sock" ]]; then
  print '请先运行 start.command 打开实时预览。'
  read -k 1
  exit 1
fi
print 'macOS 需要管理员权限读取 RealSense USB 数据流。密码仅在本机系统验证。'
print '此进程仅采集相机，按 Ctrl-C 停止。AI 与预览在普通用户进程运行。'
exec sudo "$PROJECT_DIR/.venv/bin/python" -u "$PROJECT_DIR/camera_runner.py" --socket "$PROJECT_DIR/runtime/camera.sock"
