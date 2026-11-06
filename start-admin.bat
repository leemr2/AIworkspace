@echo off
rem ============================================================================
rem  START THE ADMIN PAGE  (run this on the SAME computer as the AI runner)
rem
rem  EDIT THE NEXT LINE if the shared AI-Workspace folder is somewhere else,
rem  for example:  set "WORKSPACE=Z:\AI-Workspace"
rem  Leave it as it is if this file sits inside the AI-Workspace folder.
rem ============================================================================
set "WORKSPACE=%~dp0."
rem ============================================================================

cd /d "%~dp0"
set "PY="
where py >nul 2>nul && set "PY=py -3"
if not defined PY where python >nul 2>nul && set "PY=python"
if not defined PY (
  echo Python 3 was not found. Install it from python.org, then try again.
  pause
  exit /b 1
)
echo The first time, you will be asked to choose a PIN (at least 6 digits).
%PY% "%~dp0admin.py" --root "%WORKSPACE%" --open
pause
