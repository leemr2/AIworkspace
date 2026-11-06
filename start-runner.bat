@echo off
rem ============================================================================
rem  START THE AI RUNNER  (run this on the ONE office computer that does the work)
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
echo Starting the AI runner for: %WORKSPACE%
echo Leave this window open. Press Ctrl+C to stop.
%PY% "%~dp0runner.py" --root "%WORKSPACE%"
pause
