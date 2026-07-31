@echo off
cd /d "%~dp0"
python -c "import tkinterdnd2" >nul 2>&1 || python -m pip install -r requirements.txt
start "GitDrop" pythonw main.py
