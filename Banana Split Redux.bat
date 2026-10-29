@echo off
rem Double-click in Explorer to start Banana Split Redux from source.
rem First run creates a private Python environment in .venv-win (needs internet once).
setlocal
cd /d "%~dp0"
set "PY="
for %%v in (3.13 3.12 3.11 3.10) do (
  if not defined PY ( py -%%v -c "import sys" >/dev/null 2>&1 && set "PY=py -%%v" )
)
if not defined PY ( python -c "import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)" >/dev/null 2>&1 && set "PY=python" )
if not defined PY (
  echo Banana Split Redux needs Python 3.10 or newer from https://www.python.org/downloads/windows/
  pause
  exit /b 1
)
if not exist ".venv-win\Scripts\python.exe" (
  echo First run: creating .venv-win ...
  %PY% -m venv .venv-win || ( pause & exit /b 1 )
  ".venv-win\Scripts\python.exe" -m pip install --upgrade pip >nul
)
".venv-win\Scripts\python.exe" -c "import av, numpy, PIL, PySide6" >/dev/null 2>&1 || (
  echo Installing dependencies ^(PyAV with bundled FFmpeg, NumPy, Pillow, PySide6^) ...
  ".venv-win\Scripts\python.exe" -m pip install -r requirements.txt || ( pause & exit /b 1 )
)
start "" ".venv-win\Scripts\pythonw.exe" -m bsr_ui %*
