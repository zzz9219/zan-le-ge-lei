@echo off
cd /d "%~dp0"
.venv\Scripts\python.exe entry_probe.py login
if errorlevel 1 pause
