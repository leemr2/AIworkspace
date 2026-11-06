"""Integrity manifest for agent instructions, SOP text, and the dashboard file.

The manifest lives in the settings folder (~/.office-ai/manifest.json), never in the
shared workspace. It stores SHA-256 hashes so a changed instruction file is noticed
before the runner will use it. Standard library only.
"""
import datetime
import hashlib
import json
import os
import stat

import config

# The runner rewrites this file every check. Hashing it would pause the office forever.
SKIP_AGENT_FILES = {"_public_status.json"}
HASH_PREFIX = 12


class ManifestError(Exception):
    """The manifest exists but cannot be trusted. Messages contain no file contents."""


def manifest_path():
    return config.config_dir() / "manifest.json"


def _now():
    return datetime.datetime.now().astimezone().isoformat(timespec="seconds")


def _sha256(path):
    """Hash a regular file. Does not follow symlinks (O_NOFOLLOW where the OS has it)."""
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    if hasattr(os, "O_BINARY"):
        flags |= os.O_BINARY
    fd = os.open(path, flags)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError("not a regular file")
        h = hashlib.sha256()
        while True:
            chunk = os.read(fd, 65536)
            if not chunk:
                break
            h.update(chunk)
        return h.hexdigest()
    finally:
        os.close(fd)


def _lstat(path):
    try:
        return os.lstat(path)
    except OSError:
        return None


def _real_dir(path):
    st = _lstat(path)
    return bool(st and stat.S_ISDIR(st.st_mode) and not stat.S_ISLNK(st.st_mode))


def _put(files, path, label):
    st = _lstat(path)
    if st is None:
        return
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode):
        files[label] = {"kind": "not_regular"}
        return
    try:
        digest = _sha256(path)
    except OSError:
        files[label] = {"kind": "unreadable"}
        return
    files[label] = {"kind": "file", "sha256": digest, "size": st.st_size, "mtime": st.st_mtime}


def snapshot(root, dashboard_path=None):
    """Hash the instruction files that exist right now. Keys are workspace-relative labels.

    `_agents/_public_status.json` is skipped: the runner writes it, so it is not an instruction.
    A symlink is recorded as not-regular and is not followed.
    """
    files = {}
    agents = os.path.join(root, "_agents")
    if _real_dir(agents):
        try:
            names = sorted(os.listdir(agents))
        except OSError:
            names = []
        for fn in names:
            if fn in SKIP_AGENT_FILES or fn.startswith(".") or not fn.endswith(".json"):
                continue
            _put(files, os.path.join(agents, fn), "_agents/" + fn)
    elif _lstat(agents) is not None:
        files["_agents"] = {"kind": "not_regular"}
    sops = os.path.join(root, "_sops")
    if _real_dir(sops):
        try:
            roles = sorted(os.listdir(sops))
        except OSError:
            roles = []
        for rid in roles:
            if rid.startswith("."):
                continue
            d = os.path.join(sops, rid)
            st = _lstat(d)
            if st is None:
                continue
            if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
                _put(files, d, "_sops/" + rid)
                continue
            try:
                inner = sorted(os.listdir(d))
            except OSError:
                continue
            for fn in inner:
                if fn.startswith("."):
                    continue
                _put(files, os.path.join(d, fn), "_sops/%s/%s" % (rid, fn))
    elif _lstat(sops) is not None:
        files["_sops"] = {"kind": "not_regular"}
    if isinstance(dashboard_path, str) and dashboard_path.strip():
        base = os.path.basename(dashboard_path.strip()) or "dashboard"
        label = "dashboard:" + base
        st = _lstat(dashboard_path)
        if st is None:
            files[label] = {"kind": "missing", "missing": True}
        else:
            _put(files, dashboard_path, label)
    return files


def _stored(entry):
    """Drop live-only fields (mtime) before the manifest is written."""
    if not isinstance(entry, dict):
        return {"kind": "unreadable"}
    out = {"kind": entry.get("kind") or "unreadable"}
    if entry.get("missing"):
        out["missing"] = True
        out["kind"] = "missing"
    if entry.get("kind") == "file" and entry.get("sha256"):
        out["sha256"] = entry["sha256"]
        out["size"] = int(entry.get("size") or 0)
    return out


def load_manifest():
    """Return the saved manifest, None if there is none yet, or raise ManifestError."""
    p = manifest_path()
    try:
        st = os.lstat(p)
    except OSError:
        return None
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode):
        raise ManifestError(
            "The integrity manifest is not a regular file, so it will not be used. "
            "The runner will not process tasks until you approve the current files on the Admin page.")
    try:
        with open(p, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        raise ManifestError(
            "The integrity manifest could not be read. It may be damaged. "
            "The runner will not process tasks until you approve the current files on the Admin page.") from None
    if not isinstance(data, dict) or not isinstance(data.get("files"), dict):
        raise ManifestError(
            "The integrity manifest is not valid. "
            "The runner will not process tasks until you approve the current files on the Admin page.")
    return data


def save_manifest(files, root=None):
    """Write a new baseline into the settings folder. Refuses a settings folder inside the workspace."""
    if root is not None:
        config.check_location(root)
    config._ensure_dir(config.config_dir())
    body = {"version": 1, "updated": _now(),
            "files": {k: _stored(v) for k, v in sorted(files.items())}}
    p = manifest_path()
    tmp = p.with_name(p.name + ".tmp-%d" % os.getpid())
    data = (json.dumps(body, indent=1) + "\n").encode("utf-8")
    try:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(str(tmp), str(p))
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise config.ConfigError("The integrity manifest could not be saved.") from None
    config.lock_down_file(p)
    config.ensure_private()
    return body


def _identity(entry):
    if not isinstance(entry, dict):
        return None
    if entry.get("missing") or entry.get("kind") == "missing":
        return ("missing",)
    if entry.get("kind") != "file":
        return (entry.get("kind") or "other",)
    return ("file", entry.get("sha256"))


def _prefix(entry):
    if isinstance(entry, dict) and isinstance(entry.get("sha256"), str) and entry["sha256"]:
        return entry["sha256"][:HASH_PREFIX]
    return None


def _display_name(label):
    if label.startswith("dashboard:"):
        return label.split(":", 1)[1] or "dashboard"
    return label


def _modified(entry):
    if not isinstance(entry, dict) or not entry.get("mtime"):
        return None
    try:
        return datetime.datetime.fromtimestamp(entry["mtime"]).astimezone().isoformat(timespec="seconds")
    except (OverflowError, OSError, ValueError):
        return None


def diff(saved, current):
    """List added, removed, and content-changed files. Mtime alone is not a change."""
    saved = saved if isinstance(saved, dict) else {}
    current = current if isinstance(current, dict) else {}
    changes = []
    for label in sorted(set(saved) | set(current)):
        old, new = saved.get(label), current.get(label)
        if _identity(old) == _identity(new):
            continue
        if old is None:
            kind = "added"
        elif new is None:
            kind = "removed"
        else:
            kind = "modified"
        changes.append({
            "label": label,
            "name": _display_name(label),
            "change": kind,
            "size": new.get("size") if isinstance(new, dict) and new.get("kind") == "file" else None,
            "modified": _modified(new),
            "old_hash": _prefix(old),
            "new_hash": _prefix(new),
        })
    return changes


def affected_seat(label):
    """Return a role id, or 'all' when the file is shared (decision rights, dashboard, anything else)."""
    if not isinstance(label, str) or label.startswith("dashboard:"):
        return "all"
    if label == "_agents/_decision_rights.json":
        return "all"
    parts = label.split("/")
    if len(parts) >= 2 and parts[0] == "_agents" and parts[1].endswith(".json"):
        rid = parts[1][:-5]
        return rid if config.ROLE_RE.match(rid) else "all"
    if len(parts) >= 2 and parts[0] == "_sops" and config.ROLE_RE.match(parts[1]):
        return parts[1]
    return "all"


def paused_seats(changes, known):
    """Role ids that must not run until the owner approves. Shared-file changes pause every seat."""
    known = set(known)
    paused, all_of_them = set(), False
    for change in changes:
        rid = affected_seat(change.get("label"))
        if rid == "all" or rid not in known:
            all_of_them = True
        else:
            paused.add(rid)
    return set(known) if all_of_them else paused
