#!/usr/bin/env python3
"""Create a fresh shared workspace from workspace-template/.

The new folder gets the seven role seats (_agents), SOP placeholders (_sops),
and empty inbox/drafts/approved/archive folders. It does not copy the program,
the dashboard, or any settings.

    python init-workspace.py --dest D:\\OfficeShare\\AI-Workspace

Run this from the program folder, or with the path to this file. The destination
must be outside the program folder. Standard library only.
"""
import argparse
import shutil
import sys
from pathlib import Path

import config


def template_dir():
    return Path(__file__).resolve().parent.parent / "workspace-template"


def create_workspace(dest):
    src = template_dir()
    if not src.is_dir():
        raise SystemExit("Cannot find the template folder: %s" % src)
    dest = Path(dest).resolve()
    try:
        config.assert_separated(dest)
    except config.LayoutError as e:
        raise SystemExit("ERROR: " + str(e))
    if dest.exists() and any(dest.iterdir()):
        raise SystemExit("Destination is not empty: %s" % dest)
    shutil.copytree(src, dest, dirs_exist_ok=True)
    return dest


def main(argv=None):
    ap = argparse.ArgumentParser(description="Create a shared Office AI workspace from the template.")
    ap.add_argument("--dest", required=True, help="new shared folder (must be outside this program folder)")
    a = ap.parse_args(argv)
    dest = create_workspace(a.dest)
    print("Workspace created at %s" % dest)
    print("Next: point the WORKSPACE line in the launchers at this folder, and open dashboard/dashboard.html on staff computers.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
