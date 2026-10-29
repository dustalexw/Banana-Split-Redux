@echo off
rem Command-line engine (probe / analyze / sheet / export). Uses the same .venv-win as the app.
cd /d "%~dp0"
if not exist ".venv-win\Scripts\python.exe" ( echo Run "Banana Split Redux.bat" once first to set up .venv-win & exit /b 1 )
".venv-win\Scripts\python.exe" -m bsr.cli %*
