@echo off
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Crie o ambiente e instale as dependencias conforme README.md.
  pause
  exit /b 1
)
".venv\Scripts\python.exe" app.py
pause
