@echo off
cd /d "%~dp0"
echo Installation du studio... (ne ferme pas cette fenetre)
call :main > install_log.txt 2>&1
type install_log.txt
echo.
echo ===== Fin. Le detail est dans install_log.txt =====
if exist .env notepad .env
pause
exit /b

:main
echo --- where python ---
where python
where py
echo --- python --version ---
python --version
py --version
echo --- ffmpeg ---
where ffmpeg
ffmpeg -version
echo --- pip install ---
python -m pip install -r requirements.txt
if errorlevel 1 py -m pip install -r requirements.txt
if not exist .env copy .env.example .env
echo --- termine ---
exit /b
