@echo off
setlocal
set "SCRIPT_DIR=%~dp0"

if exist "%SCRIPT_DIR%dist\CCSwitchBatchSender.exe" (
    start "" "%SCRIPT_DIR%dist\CCSwitchBatchSender.exe"
    exit /b 0
)

where pyw.exe >nul 2>nul
if not errorlevel 1 (
    pyw -3 "%SCRIPT_DIR%ccswitch_batch_sender.py" --gui
    exit /b %errorlevel%
)

where pythonw.exe >nul 2>nul
if not errorlevel 1 (
    pythonw "%SCRIPT_DIR%ccswitch_batch_sender.py" --gui
    exit /b %errorlevel%
)

where py.exe >nul 2>nul
if not errorlevel 1 (
    py -3 "%SCRIPT_DIR%ccswitch_batch_sender.py" --gui
    exit /b %errorlevel%
)

where python.exe >nul 2>nul
if not errorlevel 1 (
    python "%SCRIPT_DIR%ccswitch_batch_sender.py" --gui
    exit /b %errorlevel%
)

echo Python 3 was not found. Build or place dist\CCSwitchBatchSender.exe first.
exit /b 2
