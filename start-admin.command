#!/bin/bash
# ============================================================================
#  START THE ADMIN PAGE  (run this on the SAME computer as the AI runner)
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
echo "The first time, you will be asked to choose a PIN (at least 6 digits)."
python3 "$(dirname "$0")/admin.py" --root "$WORKSPACE" --open
read -r -p "Press Enter to close..." _
