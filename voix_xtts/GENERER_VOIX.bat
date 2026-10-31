@echo off
cd /d "%~dp0"
if not exist venv\Scripts\python.exe (echo Lance d abord INSTALLER_XTTS.bat & pause & exit /b)
venv\Scripts\python.exe cloner.py %*
pause
