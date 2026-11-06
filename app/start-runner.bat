@echo off
rem ============================================================================
rem  START THE AI RUNNER  (on the ONE office computer that does the work)
rem
rem  Keep this file in the program folder, outside the shared folder.
rem  Staff must not be able to edit this folder. If they can, they could change
rem  the program that reads the API key.
rem  Edit the next line so it points at the shared data folder
rem  (the folder with the role inboxes, not this folder).
rem ============================================================================
set "WORKSPACE=C:\OfficeShare\AI-Workspace"
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
if "%WORKSPACE%"=="" (
  echo Edit the WORKSPACE line at the top of this file so it points at the shared folder.
  pause
  exit /b 1
)
echo Starting the AI runner for: %WORKSPACE%
echo Leave this window open. Press Ctrl+C to stop.
%PY% "%~dp0runner.py" --root "%WORKSPACE%"
pause
