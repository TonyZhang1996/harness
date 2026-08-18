@echo off
setlocal

rem Launch the source-based AI Harness GUI without machine-specific paths.
set "ROOT=%~dp0.."
for %%I in ("%ROOT%") do set "ROOT=%%~fI"
set "PYTHONPATH=%ROOT%\src;%PYTHONPATH%"

if exist "%ROOT%\.venv\Scripts\python.exe" (
    "%ROOT%\.venv\Scripts\python.exe" -m ai_harness --gui %*
    exit /b %errorlevel%
)

where py >nul 2>nul
if errorlevel 1 (
    echo 未找到 Python。请先创建 .venv 或安装 Python Launcher。
    exit /b 1
)
py -3 -m ai_harness --gui %*

endlocal
