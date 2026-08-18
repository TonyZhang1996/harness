@echo off
setlocal

rem Launch the source-based AI Harness GUI from its project environment.
start "" /D "D:\harness" "D:\miniconda3\python.exe" -m ai_harness --gui %*

endlocal
