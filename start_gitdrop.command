#!/bin/sh
cd "$(dirname "$0")" || exit 1
PYTHON_BIN="$(command -v python3 || command -v python)"
if [ -z "$PYTHON_BIN" ]; then
  osascript -e 'display alert "GitDrop" message "未找到 Python 3，请先安装 Python 3。" as critical'
  exit 1
fi
"$PYTHON_BIN" -c "import tkinterdnd2" >/dev/null 2>&1 || "$PYTHON_BIN" -m pip install -r requirements.txt
exec "$PYTHON_BIN" main.py
