@echo off
chcp 65001 >nul
cd /d "%~dp0"
set CIBLE=D:\studio_dark_data
echo ================================================
echo  LIBERER DE LA PLACE SUR C:
echo  Deplace la banque de clips, les videos finies, les videos a decouper
echo  et les fichiers de travail vers %CIBLE%
echo ================================================
echo.
echo IMPORTANT : le decoupeur et le studio doivent etre FERMES avant de continuer.
pause
if not exist D:\ (echo [X] Disque D: introuvable. & pause & exit /b)
mkdir "%CIBLE%" 2>nul
for %%F in (mes_clips sorties a_decouper cache) do (
  if exist "%%F" (
    echo --- Deplacement de %%F vers %CIBLE%\%%F ...
    robocopy "%%F" "%CIBLE%\%%F" /E /MOVE /NFL /NDL /NJH /NP
  )
)
findstr /b "DOSSIER_DONNEES=" .env >nul 2>nul || (echo.>> .env & echo DOSSIER_DONNEES=%CIBLE%>> .env)
echo.
echo --- Le studio utilisera desormais %CIBLE% (reglage ajoute dans .env).
echo.
echo Les fichiers d installation telecharges par pip (dont PyTorch, environ 3 Go) sont gardes en cache
echo sur C: alors qu ils ne servent plus. Les supprimer libere beaucoup de place, sans rien casser.
set /p REP=Vider ce cache pip ? (O/N) 
if /i "%REP%"=="O" (
  python -m pip cache purge
  if exist "%USERPROFILE%\Documents\xtts\venv\Scripts\python.exe" "%USERPROFILE%\Documents\xtts\venv\Scripts\python.exe" -m pip cache purge
)
echo.
echo ===== Termine. Place libre sur C: =====
for /f "tokens=3" %%a in ('dir C:\ ^| find "octets libres"') do echo %%a octets libres
pause
