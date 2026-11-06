#!/bin/bash
# ============================================================================
#  START THE AI RUNNER  (run this on the ONE office computer that does the work)
#
#  EDIT THE NEXT LINE if the shared AI-Workspace folder is somewhere else,
#  for example:  WORKSPACE="/Volumes/Office/AI-Workspace"
#  Leave it as it is if this file sits inside the AI-Workspace folder.
# ============================================================================
WORKSPACE="$(cd "$(dirname "$0")" && pwd)"
# ============================================================================

cd "$(dirname "$0")" || exit 1
if ! command -v python3 >/dev/null 2>&1; then
  echo "Python 3 was not found. Install it from python.org, then try again."
  read -r -p "Press Enter to close..." _
  exit 1
fi
echo "Starting the AI runner for: $WORKSPACE"
echo "Leave this window open. Press Ctrl+C to stop."
python3 "$(dirname "$0")/runner.py" --root "$WORKSPACE"
read -r -p "Press Enter to close..." _
