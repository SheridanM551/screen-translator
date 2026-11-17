@echo off
rem Starts the translator (control panel + tray icon, no console window).
rem   start.bat                 use the engine saved in config.json (default: LLM)
rem   start.bat --engine nmt    this run with the non-LLM engine
rem   start.bat --engine llm    this run with the LLM engine
rem   start.bat --console ...   keep a console window with live logs
cd /d "%~dp0"
if /i "%~1"=="--console" (
    ".venv\Scripts\python.exe" -m app.main %*
) else (
    start "" ".venv\Scripts\pythonw.exe" -m app.main %*
)
