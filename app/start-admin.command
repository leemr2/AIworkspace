#!/bin/bash
# ============================================================================
#  START THE ADMIN PAGE  (on the SAME computer as the AI runner)
#
#  Keep this file in the program folder, outside the shared folder.
#  Staff must not be able to edit this folder.
#  Edit the next line so it points at the shared data folder
#  (the folder with the role inboxes, not this folder).
# ============================================================================
WORKSPACE="/OfficeShare/AI-Workspace"
# ============================================================================

cd "$(dirname "$0")" || exit 1
if ! command -v python3 >/dev/null 2>&1; then
  echo "Python 3 was not found. Install it from python.org, then try again."
  read -r -p "Press Enter to close..." _
  exit 1
fi
if [ -z "$WORKSPACE" ]; then
  echo "Edit the WORKSPACE line at the top of this file so it points at the shared folder."
  read -r -p "Press Enter to close..." _
  exit 1
fi
echo "The first time, you will be asked to choose a PIN (at least 6 digits)."
python3 "$(dirname "$0")/admin.py" --root "$WORKSPACE" --open
read -r -p "Press Enter to close..." _
