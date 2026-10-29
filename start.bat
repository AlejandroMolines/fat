@echo off
cd /d "%~dp0"
where py >nul 2>nul
if not errorlevel 1 (
  py -3 fomo-app\server.py
  pause
  exit /b
)
where python >nul 2>nul
if errorlevel 1 (
  echo No se encontro Python. Instala Python 3.10+ con el lanzador py o PATH.
  pause
  exit /b 1
)
python fomo-app\server.py
pause
