#!/bin/zsh
set -eu
PROJECT_DIR=${0:A:h}
cd "$PROJECT_DIR"
exec "$PROJECT_DIR/.venv/bin/python" -u "$PROJECT_DIR/app.py" --open
