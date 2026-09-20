@echo off
rem ===========================================================================
rem  BigPixels - double-click launcher for Windows
rem
rem  KEEP THIS FILE PURE ASCII, SAVED WITH CRLF LINE ENDINGS.
rem
rem  cmd.exe walks a .bat by byte offset, one line at a time. Multi-byte
rem  characters (any Chinese at all) and LF-only line endings both
rem  desynchronise it: it then runs fragments like  'd'  or  '\pip'  and
rem  silently skips commands. So there is not one Chinese character below -
rem  every message the user sees comes from Python (web\bootstrap.py), which
rem  handles UTF-8 properly. The console is switched to UTF-8 for that reason.
rem
rem  All this file does: find a Python 3.9+ interpreter, then hand over.
rem ===========================================================================

setlocal
chcp 65001 >nul
cd /d "%~dp0"

rem  change the port here if 8765 is taken
set "PORT=8765"

rem  keep temp files and the pip cache inside this folder instead of on C:
set "TEMP=%~dp0.cache\tmp"
set "TMP=%~dp0.cache\tmp"
set "PIP_CACHE_DIR=%~dp0.cache\pip"
if not exist "%TEMP%" md "%TEMP%" >nul 2>&1
if not exist "%PIP_CACHE_DIR%" md "%PIP_CACHE_DIR%" >nul 2>&1

rem  make every python child speak UTF-8, matching the chcp above
set "PYTHONIOENCODING=utf-8"
set "PYTHONUTF8=1"

set "VENV=%~dp0.venv"
set "PY=%~dp0.venv\Scripts\python.exe"

if exist "%PY%" goto :have_venv

call :find_python
if not defined PYBIN goto :no_python
set "RUNNER=%PYBIN%"
set "RUNARG=%PYARG%"
goto :launch

:have_venv
set "RUNNER=%PY%"
set "RUNARG="

:launch
"%RUNNER%" %RUNARG% "web\bootstrap.py" --port %PORT% --venv "%VENV%"
exit /b %errorlevel%


rem ===========================================================================
rem  helpers
rem ===========================================================================

rem  Look for a usable Python. 3.9 is the floor - onnxruntime wheels start
rem  there and the code uses newer syntax. The bare "python.exe" the Microsoft
rem  Store parks in WindowsApps is an empty stub that only opens the Store, so
rem  it gets rejected by the version probe instead of failing much later.
:find_python
set "PYBIN="
set "PYARG="
for /f "delims=" %%p in ('where py 2^>nul') do (
  if not defined PYBIN (
    "%%p" -3 -c "import sys;sys.exit(0 if sys.version_info>=(3,9) else 1)" >nul 2>&1
    if not errorlevel 1 (
      set "PYBIN=%%p"
      set "PYARG=-3"
    )
  )
)
for /f "delims=" %%p in ('where python 2^>nul') do (
  if not defined PYBIN (
    "%%p" -c "import sys;sys.exit(0 if sys.version_info>=(3,9) else 1)" >nul 2>&1
    if not errorlevel 1 set "PYBIN=%%p"
  )
)
rem  Installed but not on PATH: sweep the usual locations.
if not defined PYBIN for /d %%d in ("%LOCALAPPDATA%\Programs\Python\Python3*") do (
  if not defined PYBIN if exist "%%d\python.exe" set "PYBIN=%%d\python.exe"
)
if not defined PYBIN for /d %%d in ("C:\Program Files\Python3*") do (
  if not defined PYBIN if exist "%%d\python.exe" set "PYBIN=%%d\python.exe"
)
if not defined PYBIN for /d %%d in ("C:\Python3*") do (
  if not defined PYBIN if exist "%%d\python.exe" set "PYBIN=%%d\python.exe"
)
exit /b

:no_python
echo.
echo   Python 3.9 or newer was not found on this machine.
echo.
echo     Install one from:
echo         https://www.python.org/downloads/windows/
echo.
echo     During setup, TICK "Add python.exe to PATH" - otherwise this
echo     launcher still will not find it. Then double-click this file
echo     again; no reboot needed.
echo.
pause
exit /b 1
