@echo off
cd /d "%~dp0"
echo Installation de XTTS-v2... (10-20 min, ne ferme pas cette fenetre)
call :main > install_log.txt 2>&1
type install_log.txt
echo.
echo ===== Fin. Detail dans install_log.txt =====
pause
exit /b

:main
echo --- Python 3.12 ---
py -3.12 -c "import sys; print(sys.version)"
if errorlevel 1 py install 3.12
py -3.12 -c "import sys; print(sys.version)"
if errorlevel 1 winget install -e --id Python.Python.3.12 --accept-source-agreements --accept-package-agreements
py -3.12 -c "import sys; sys.exit(0 if sys.version_info[:2]==(3,12) else 1)"
if errorlevel 1 goto pas_python
echo --- environnement separe (venv) ---
if not exist venv\Scripts\python.exe py -3.12 -m venv venv
if not exist venv\Scripts\python.exe goto pas_python
set PY=venv\Scripts\python.exe
%PY% --version
%PY% -m pip install --upgrade pip
echo --- PyTorch CUDA 12.8 ---
%PY% -m pip install torch torchaudio --index-url https://download.pytorch.org/whl/cu128
%PY% -c "import torch; print(torch.__version__, torch.cuda.is_available(), torch.cuda.get_device_name(0) if torch.cuda.is_available() else \"PAS DE GPU\")"
echo --- coqui-tts ---
%PY% -m pip install coqui-tts
%PY% -c "import torch,sys; v=tuple(int(x) for x in torch.__version__.split(\"+\")[0].split(\".\")[:2]); sys.exit(0 if v>=(2,9) else 1)" && %PY% -m pip install torchcodec
%PY% -c "from TTS.api import TTS; print(\"XTTS OK\")"
if not exist texte.txt echo Discipline is not a punishment. It is the only armor that will outlive you.> texte.txt
echo --- termine ---
exit /b
:pas_python
echo [X] Python 3.12 n est pas installe. Lance "py install 3.12" dans une invite de commandes, puis relance ce fichier.
exit /b
