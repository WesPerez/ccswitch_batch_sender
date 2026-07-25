@echo off
setlocal
set "SCRIPT_DIR=%~dp0"
set "PYTHONW=C:\Program Files\Python310\pythonw.exe"
set "PYTHON=C:\Program Files\Python310\python.exe"

if exist "%PYTHONW%" goto run_gui_windowless
where pyw.exe >nul 2>nul
if not errorlevel 1 goto run_pyw
where pythonw.exe >nul 2>nul
if not errorlevel 1 goto run_pythonw
if exist "%PYTHON%" goto run_gui
where py.exe >nul 2>nul
if not errorlevel 1 goto run_py
where python.exe >nul 2>nul
if not errorlevel 1 goto run_python

echo Python 3 was not found. Install Python 3.10+ or add python.exe to PATH.
exit /b 2

:run_gui
"%PYTHON%" "%SCRIPT_DIR%ccswitch_batch_sender.py" --gui
exit /b %errorlevel%

:run_gui_windowless
"%PYTHONW%" "%SCRIPT_DIR%ccswitch_batch_sender.py" --gui
exit /b %errorlevel%

:run_pyw
pyw -3 "%SCRIPT_DIR%ccswitch_batch_sender.py" --gui
exit /b %errorlevel%

:run_pythonw
pythonw "%SCRIPT_DIR%ccswitch_batch_sender.py" --gui
exit /b %errorlevel%

:run_py
py -3 "%SCRIPT_DIR%ccswitch_batch_sender.py" --gui
exit /b %errorlevel%

:run_python
python "%SCRIPT_DIR%ccswitch_batch_sender.py" --gui
exit /b %errorlevel%
