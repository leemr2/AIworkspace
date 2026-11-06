#!/usr/bin/env python3
"""AI-Workspace agent runner (prototype).

Watches the role folders in a shared workspace. For every task file in a role's
inbox/ it: (1) runs the PHI tripwire, (2) builds the agent's instructions from
_agents/<role>.json (+ any approved SOP text in _sops/<role>/), (3) asks a model
backend for a DRAFT, (4) checks the draft, (5) writes it to drafts/ for a human.

Agents never publish or send anything. A human approves in the dashboard.

Settings (provider, API key, default model, paused seats, per-seat model, prices) are read
from ~/.office-ai/config.json at the start of EVERY sweep, so changes made on the Admin page
apply without restarting. That file is never inside the shared workspace (see config.py).

The workspace path has no default: pass --root, and keep this program folder outside that
workspace. If the two folders overlap, the runner refuses to start.

A problem with settings locks the runner (it processes nothing and leaves tasks queued)
except on a true first run, when no settings folder exists yet and only the stub backend
is allowed. A damaged config file keeps the last good settings from this process.

Standard library only. Usage:
    python3 runner.py --root <workspace> --once
    python3 runner.py --root <workspace>
    python3 runner.py --root <workspace> --backend anthropic
"""
import argparse, json, os, re, stat, sys, time, datetime, socket, urllib.request, urllib.error
from pathlib import Path

import config
import integrity

ROLE_RE = re.compile(r"^R\d{3}$")
TASK_RE = re.compile(r"^[A-Za-z0-9_-]{1,80}$")
FOLDER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,80}$")
MAX_TASK_BYTES = 64 * 1024
MAX_INSTRUCTIONS = 8000
STAFF_HOLD_MESSAGE = "AI runner is paused. Please tell the office manager."
_READ_CAP = 1024 * 1024

# ---- PHI tripwire: a safety net, NOT a guarantee. Humans stay responsible. ----
BLOCK_PATTERNS = [
    ("SSN-like number", re.compile(r"\b\d{3}-\d{2}-\d{4}\b")),
    ("phone number", re.compile(r"(?<!\d)(?:\+?1[\s.-]?)?\(?\d{3}\)?[\s.-]\d{3}[\s.-]\d{4}(?!\d)")),
    ("date of birth", re.compile(r"\b(?:dob|d\.o\.b|date of birth|birthdate|born on)\b", re.I)),
    ("medical record / member id", re.compile(r"\b(?:mrn|medical record(?: number| no)?|member id|subscriber id|policy (?:number|no)|insurance id)\b", re.I)),
    ("full date (possible DOB/visit)", re.compile(r"\b(?:0?[1-9]|1[0-2])[/-](?:0?[1-9]|[12]\d|3[01])[/-](?:19|20)\d{2}\b")),
    ("patient label", re.compile(r"\bpatient(?: name)?\s*[:=]\s*\S", re.I)),
]

def phi_scan(text):
    return [name for name, rx in BLOCK_PATTERNS if rx.search(text or "")]

# ---- helpers ----
def now():
    return datetime.datetime.now().astimezone().isoformat(timespec="seconds")

def log(root, event, **kw):
    """Log metadata only -- never task or draft text."""
    os.makedirs(os.path.join(root, "_logs"), exist_ok=True)
    path = os.path.join(root, "_logs", datetime.date.today().isoformat() + ".jsonl")
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps({"ts": now(), "event": event, **kw}) + "\n")

def is_regular_file(path):
    """True only for a real file. Symlinks, directories, and devices are not followed."""
    try:
        st = os.lstat(path)
    except OSError:
        return False
    return stat.S_ISREG(st.st_mode) and not stat.S_ISLNK(st.st_mode)


def read_text_regular(path, limit, truncate=False):
    """Read a regular file. Raises OSError if it is not one.

    With truncate=False (the default) a file longer than `limit` bytes raises.
    With truncate=True the extra bytes are dropped. Symlinks are not followed.
    """
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    if hasattr(os, "O_BINARY"):
        flags |= os.O_BINARY
    fd = os.open(path, flags)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError("not a regular file")
        data = os.read(fd, limit if truncate else limit + 1)
    finally:
        os.close(fd)
    if not truncate and len(data) > limit:
        raise OSError("file is too large")
    return data.decode("utf-8", errors="replace")


def _read_json_regular(path, limit=_READ_CAP):
    if not is_regular_file(path):
        return None
    try:
        return json.loads(read_text_regular(path, limit))
    except (OSError, ValueError):
        return None


def load_agents(root):
    agents = {}
    d = os.path.join(root, "_agents")
    try:
        names = os.listdir(d)
    except OSError:
        return agents
    for fn in names:
        if fn.endswith(".json") and ROLE_RE.match(fn[:-5]):
            data = _read_json_regular(os.path.join(d, fn))
            if isinstance(data, dict):
                agents[fn[:-5]] = data
    return agents


def role_dir(root, agent):
    """Role folder from the agent config only. Never a path taken from a task file."""
    folder = agent.get("folder") if isinstance(agent, dict) else None
    if not isinstance(folder, str) or not FOLDER_RE.match(folder):
        return None
    return os.path.join(root, folder)


def load_sop_text(root, rid, limit=12000):
    d = os.path.join(root, "_sops", rid)
    out, used = [], 0
    if not is_regular_file(d) and _is_real_dir(d):
        try:
            names = sorted(os.listdir(d))
        except OSError:
            names = []
        for fn in names:
            if fn.startswith("PUT-") or not fn.lower().endswith((".txt", ".md")):
                continue
            path = os.path.join(d, fn)
            if not is_regular_file(path):
                continue
            budget = max(0, limit - used)
            if budget == 0:
                break
            try:
                t = read_text_regular(path, budget, truncate=True)
            except OSError:
                continue
            used += len(t)
            out.append(f"--- {fn} ---\n{t}")
    return "\n".join(out)


def _is_real_dir(path):
    try:
        st = os.lstat(path)
    except OSError:
        return False
    return stat.S_ISDIR(st.st_mode) and not stat.S_ISLNK(st.st_mode)

def build_system_prompt(root, agent):
    w, a = agent["from_workbook"], agent["authored_for_prototype"]
    duties = "\n".join(f"- {x['duty']} ({x['frequency']})" for x in w["duties"][:25])
    sops = "\n".join(f"- {s['title']} [{s['status']}]" for s in w["sops"][:25]) or "- none located"
    rules = "\n".join(f"- {r}" for r in a["must_not"])
    dr_path = os.path.join(root, "_agents", "_decision_rights.json")
    dr_data = _read_json_regular(dr_path)
    dr = ""
    if isinstance(dr_data, list):
        dr = "\n".join(f"- {x['decision']}: {x['owner']} ({x['limit_or_note']})" for x in dr_data if isinstance(x, dict))
    ref = load_sop_text(root, agent["role_id"]) or "(No approved SOP text has been placed in _sops yet. Say so if the task depends on one.)"
    return (
        f"You are the AI support seat for the role '{agent['role_title']}' ({agent['role_id']}) at a small optometry practice. "
        f"You assist the human staff member; you do not replace their judgment.\n\n"
        f"ROLE PURPOSE: {w['purpose']}\nAUTHORITY: {w['decision_authority']}\nKPIs: {w['kpis']}\n\n"
        f"RECURRING DUTIES:\n{duties}\n\nSOPs ON RECORD (status as listed):\n{sops}\n\n"
        f"DECISION RIGHTS (escalate; never override):\n{dr}\n\n"
        f"HARD RULES:\n{rules}\n\n"
        f"REFERENCE MATERIAL:\n{ref}\n\n"
        "Produce a DRAFT only, in clear plain text. Begin with a one-line note listing any assumptions or [CONFIRM: ...] items."
    )

def build_user_prompt(agent, task):
    tt = next((t for t in agent["authored_for_prototype"]["task_types"] if t["id"] == task.get("task_type")), None)
    parts = [f"TASK TYPE: {tt['label'] if tt else task.get('task_type')}"]
    if tt:
        parts.append(f"GUIDANCE: {tt['prompt']}")
    parts += [f"TONE: {task.get('tone') or 'warm, professional, plain language'}",
              f"DUE: {task.get('due') or 'not specified'}",
              f"REQUEST FROM {task.get('requester') or 'staff'}:\n{task.get('instructions','')}"]
    if task.get("feedback"):
        parts.append("REVIEWER FEEDBACK ON THE PREVIOUS DRAFT (revise accordingly):\n" + task["feedback"])
    return "\n\n".join(parts)

# ---- model backends ----
# A backend is a function (system, user, task, agent, model=None, api_key=None) that returns
# (text, usage) with usage = {model, input_tokens, output_tokens, latency_ms}.
class ModelError(RuntimeError):
    """A model call failed. The message is plain language and never contains the API key."""
    def __init__(self, kind, message):
        super().__init__(message)
        self.kind = kind

def _http_error(code):
    if code in (401, 403):
        return "bad_key", "The AI provider rejected the API key (it may be wrong, expired, or not allowed to use this model)."
    if code == 404:
        return "bad_model", "The AI provider does not recognize that model name. Check the spelling."
    if code == 400:
        return "bad_request", "The AI provider rejected the request (HTTP 400). Check the model name."
    if code == 429:
        return "rate_limited", "The AI provider says too many requests were sent or a usage limit was reached. Wait a minute and try again."
    if code >= 500:
        return "provider_down", "The AI provider is having problems right now. Try again later."
    return "other", "The AI provider returned an unexpected error (HTTP %d)." % code

def call_anthropic(key, model, system, user, max_tokens=2000, timeout=120):
    """Call the Anthropic Messages API. Returns (text, usage). Raises ModelError (key-free messages)."""
    if not key:
        raise ModelError("no_key", "No API key is set. Add one on the Admin page (or set ANTHROPIC_API_KEY on the runner computer).")
    t0 = time.monotonic()
    body = json.dumps({"model": model, "max_tokens": max_tokens,
                       "system": system, "messages": [{"role": "user", "content": user}]}).encode()
    try:
        req = urllib.request.Request("https://api.anthropic.com/v1/messages", data=body, method="POST",
            headers={"x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.loads(r.read())
    except urllib.error.HTTPError as e:
        raise ModelError(*_http_error(e.code)) from None
    except (socket.timeout, TimeoutError):
        raise ModelError("timeout", "The AI provider took too long to answer. Try again.") from None
    except urllib.error.URLError:
        raise ModelError("no_network", "Could not reach the AI provider. Check this computer's internet connection.") from None
    except (OSError, ValueError):
        raise ModelError("other", "The AI provider's answer could not be read. Try again.") from None
    u = data.get("usage") if isinstance(data, dict) else None
    u = u if isinstance(u, dict) else {}
    text = "".join(b.get("text", "") for b in data.get("content", []) if isinstance(b, dict))
    return text, {"model": model, "input_tokens": int(u.get("input_tokens") or 0),
                  "output_tokens": int(u.get("output_tokens") or 0),
                  "latency_ms": int((time.monotonic() - t0) * 1000)}

def backend_stub(system, user, task, agent, model=None, api_key=None):
    t0 = time.monotonic()
    text = ("[STUB OUTPUT - no model was called. This only proves the folder workflow works.]\n"
            f"Assumptions: none (stub).\n\nRole seat: {agent['role_title']}\n"
            f"Task type: {task.get('task_type')}\n\nRequest received:\n{task.get('instructions','')}\n")
    return text, {"model": "stub", "input_tokens": 0, "output_tokens": 0,
                  "latency_ms": int((time.monotonic() - t0) * 1000)}

def backend_anthropic(system, user, task, agent, model=None, api_key=None):
    key = api_key or os.environ.get("ANTHROPIC_API_KEY")
    model = model or os.environ.get("AI_MODEL") or config.DEFAULT_MODEL
    mt = task.get("max_tokens")  # lets the Admin page ask for tiny test calls
    mt = mt if isinstance(mt, int) and not isinstance(mt, bool) and 1 <= mt <= 4000 else 2000
    return call_anthropic(key, model, system, user, max_tokens=mt)

BACKENDS = {"stub": backend_stub, "anthropic": backend_anthropic}

def normalize_result(result, model, t0):
    """Accept (text, usage) -- or a bare string from an older custom backend."""
    text, usage = result if isinstance(result, tuple) else (result, {})
    usage = usage if isinstance(usage, dict) else {}
    return str(text), {"model": usage.get("model") or model,
                       "input_tokens": int(usage.get("input_tokens") or 0),
                       "output_tokens": int(usage.get("output_tokens") or 0),
                       "latency_ms": int(usage["latency_ms"]) if usage.get("latency_ms") is not None
                                     else int((time.monotonic() - t0) * 1000)}

def usage_fields(cfg, usage):
    """The metadata-only fields added to log events. estimated_cost only if a price is set."""
    out = {"model": usage["model"], "input_tokens": usage["input_tokens"],
           "output_tokens": usage["output_tokens"], "latency_ms": usage["latency_ms"]}
    cost = config.estimate_cost(cfg.get("prices"), usage["model"], usage["input_tokens"], usage["output_tokens"])
    if cost is not None:
        out["estimated_cost"] = cost
    return out

# ---- core ----
_retry = {}  # task path -> (failures, earliest next attempt); stops a bad key from retrying every 5 seconds
_last_draft = {}  # role id -> time of the last draft this runner wrote (shown to staff as last_draft_at)

def _entry_kind(st):
    if stat.S_ISLNK(st.st_mode):
        return "symlink"
    if stat.S_ISDIR(st.st_mode):
        return "directory"
    return "other"


def _quarantine(root, agent, folder, path, fn, st):
    """Move a non-file out of the inbox. A symlink is unlinked and its target is not touched."""
    kind = _entry_kind(st)
    log(root, "input_quarantined", role=agent["role_id"], file=fn, kind=kind)
    print("  ! %s %s: quarantined (%s)" % (agent["role_id"], fn, kind), file=sys.stderr)
    if kind == "symlink":
        try:
            os.unlink(path)
        except OSError as e:
            print("  ! could not remove symlink %s: %s" % (fn, e), file=sys.stderr)
        return
    archive = os.path.join(folder, "archive")
    safe = re.fullmatch(r"[A-Za-z0-9._-]{1,120}", fn)
    dest_name = (fn + ".quarantined") if safe else "quarantined-entry"
    try:
        os.makedirs(archive, exist_ok=True)
        dest = os.path.join(archive, dest_name)
        n = 0
        while os.path.lexists(dest):
            n += 1
            dest = os.path.join(archive, dest_name + "-" + str(n))
        os.replace(path, dest)
    except OSError as e:
        print("  ! could not quarantine %s: %s" % (fn, e), file=sys.stderr)


def _reject_input(root, agent, folder, path, fn, reason, note, flag, extra=None):
    """Write a short rejection draft and remove the inbox file. The draft does not contain the file body."""
    tid = fn[:-5] if fn.endswith(".json") else ""
    fields = {"role": agent["role_id"], "reason": reason, **(extra or {})}
    if TASK_RE.match(tid):
        fields["task"] = tid
        draft = {"task_id": tid, "role_id": agent["role_id"], "status": "rejected_input",
                 "rejection_note": note, "draft": "", "flags": [flag], "instructions": "[REMOVED]",
                 "created_draft": now()}
        try:
            os.makedirs(os.path.join(folder, "drafts"), exist_ok=True)
            _write(os.path.join(folder, "drafts", tid + ".json"), draft)
            os.remove(path)
        except OSError as e:
            print("  ! could not reject %s: %s" % (fn, e), file=sys.stderr)
    else:
        fields["file"] = fn
        try:
            os.remove(path)
        except OSError:
            pass
    log(root, "input_rejected", **fields)


def _inbox_entries(root, agent, folder):
    """Yield (fn, path, st) for regular *.json files. Non-files are quarantined and not followed."""
    inbox = os.path.join(folder, "inbox")
    if not _is_real_dir(inbox):
        return
    try:
        names = sorted(os.listdir(inbox))
    except OSError:
        return
    for fn in names:
        if fn.startswith("."):
            continue
        path = os.path.join(inbox, fn)
        try:
            st = os.lstat(path)
        except OSError:
            continue
        if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode):
            _quarantine(root, agent, folder, path, fn, st)
            continue
        if fn.endswith(".json"):
            yield fn, path, st


def process_task(root, agent, folder, fn, backend, cfg=None, st=None):
    cfg = cfg if cfg is not None else config.default_config()
    path = os.path.join(folder, "inbox", fn)
    tid = fn[:-5]
    try:
        st = os.lstat(path)
    except OSError:
        return
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode):
        _quarantine(root, agent, folder, path, fn, st)
        return
    if st.st_size > MAX_TASK_BYTES:
        _reject_input(root, agent, folder, path, fn, "too_large",
                      "Not processed: the file is larger than 64 KB. Shorten it and resubmit.",
                      "INPUT_TOO_LARGE", {"bytes": st.st_size})
        return
    try:
        task = json.loads(read_text_regular(path, MAX_TASK_BYTES))
    except (OSError, ValueError):
        log(root, "task_unreadable", role=agent["role_id"], file=fn, error="could not read")
        return
    if not isinstance(task, dict):
        log(root, "task_unreadable", role=agent["role_id"], file=fn, error="could not read")
        return
    # Ids come from the file name and the agent, never from an unvalidated field. Paths are not
    # built from any other value inside the task file.
    if not TASK_RE.match(tid) or task.get("role_id") != agent["role_id"] or task.get("task_id") != tid:
        log(root, "task_rejected_bad_header", role=agent["role_id"], file=fn)
        try:
            os.makedirs(os.path.join(folder, "archive"), exist_ok=True)
            os.replace(path, os.path.join(folder, "archive", tid + ".rejected.json"))
        except OSError as e:
            print("  ! could not archive %s: %s" % (fn, e), file=sys.stderr)
        return
    instructions = task.get("instructions")
    if isinstance(instructions, str) and len(instructions) > MAX_INSTRUCTIONS:
        _reject_input(root, agent, folder, path, fn, "instructions_too_long",
                      "Not processed: the request is longer than 8,000 characters. Shorten it and resubmit.",
                      "INSTRUCTIONS_TOO_LONG", {"chars": len(instructions)})
        return
    scan_text = " ".join(str(task.get(k, "")) for k in ("instructions", "feedback", "requester", "tone"))
    hits = phi_scan(scan_text)
    if hits:
        # Redact: do not copy the flagged text into any new file, and remove the original.
        for k in ("instructions", "feedback"):
            if task.get(k):
                task[k] = "[REMOVED by privacy check]"
        task["status"] = "rejected_phi_tripwire"
        task["rejection_note"] = ("Not processed: the request appears to contain patient or personal identifiers ("
                                  + ", ".join(hits) + "). Remove them and resubmit.")
        _write(os.path.join(folder, "drafts", tid + ".json"), {**task, "draft": "", "flags": ["PHI_TRIPWIRE"], "status": "rejected_phi_tripwire", "created_draft": now()})
        os.remove(path)
        log(root, "phi_tripwire", role=agent["role_id"], task=tid, kinds=hits)
        return
    system, user = build_system_prompt(root, agent), build_user_prompt(agent, task)
    model = config.resolve_model(cfg, agent["role_id"])
    key, _src = config.get_api_key(cfg)
    t0 = time.monotonic()
    try:
        text, usage = normalize_result(
            BACKENDS[backend](system, user, task, agent, model=model, api_key=key), model, t0)
    except (RuntimeError, urllib.error.URLError, OSError, ValueError) as e:
        msg = config.redact(e, key)[:150]
        usage = {"model": model, "input_tokens": 0, "output_tokens": 0,
                 "latency_ms": int((time.monotonic() - t0) * 1000)}
        log(root, "model_error", role=agent["role_id"], task=tid, error=msg, **usage_fields(cfg, usage))
        fails = _retry.get(path, (0, 0))[0] + 1
        _retry[path] = (fails, time.monotonic() + min(600, 30 * 2 ** (fails - 1)))
        print(f"  ! {tid}: model error: {msg} (task left in inbox)", file=sys.stderr)
        return
    _retry.pop(path, None)
    flags = []
    out_hits = phi_scan(text)
    if out_hits:
        flags.append("OUTPUT_CONTAINS_IDENTIFIER_PATTERN: " + ", ".join(out_hits))
    low = text.lower()
    for ph in agent["authored_for_prototype"].get("output_flag_phrases", []):
        if ph in low:
            flags.append(f"NEEDS_ATTENTION: draft contains '{ph}' (may imply care/patient relationship)")
    if "[confirm" in low:
        flags.append("HAS_CONFIRM_PLACEHOLDERS")
    unvalidated = [s["title"] for s in agent["from_workbook"]["sops"] if "pending" in s["status"].lower() or "review before" in s["status"].lower()]
    draft = {**task, "draft": text, "flags": flags, "backend": backend, "status": "pending_review",
             "sops_unvalidated_on_record": len(unvalidated), "created_draft": now()}
    _write(os.path.join(folder, "drafts", tid + ".json"), draft)
    os.replace(path, os.path.join(folder, "archive", tid + ".json"))
    _last_draft[agent["role_id"]] = now()
    log(root, "draft_created", role=agent["role_id"], task=tid, backend=backend, flags=len(flags),
        **usage_fields(cfg, usage))
    print(f"  + {agent['role_id']} {tid}: draft ready ({len(flags)} flag(s))")

def _write(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=1)
    os.replace(tmp, path)  # atomic: the dashboard never sees a half-written file

_state = {"good_cfg": None, "warned": None, "fingerprint": None, "provider": None}
_paused_logged = {}          # role id -> when we last logged seat_paused_skip
PAUSED_LOG_EVERY = 600       # seconds; still "at most one per sweep", just not every 5 seconds
STATUS_HEARTBEAT = 60        # seconds between status-file refreshes when nothing changed

def _warn_once(key, message, root=None, event=None):
    """Print a state message once per distinct key. The workspace log, if any, gets the event name only."""
    if _state.get("warned") == key:
        return
    _state["warned"] = key
    print(message, file=sys.stderr)
    if root and event:
        log(root, event)


def resolve_runtime(root):
    """Decide whether this sweep may process tasks. Fail closed: a settings problem does not turn seats on."""
    try:
        config.assert_separated(root)
    except config.LayoutError as e:
        _warn_once("layout", "ERROR: " + str(e))
        return {"state": "locked", "cfg": None, "process": False, "review": set(), "note": None, "code": "layout"}
    ev = config.evaluate_settings(root)
    if ev["state"] == "locked" and ev["recoverable"] and _state.get("good_cfg") is not None:
        _warn_once("lastgood", "SETTINGS: using last good settings. The settings file could not be read, so this run is still using the copy that loaded earlier.")
        return {"state": "ready", "cfg": _state["good_cfg"], "process": True, "review": set(),
                "note": config.LAST_GOOD_NOTE, "code": "lastgood"}
    if ev["state"] == "locked":
        if not ev["recoverable"]:
            _state["good_cfg"] = None
        _warn_once("locked:" + ev["code"],
                   "RUNNER LOCKED: " + (ev["detail"] or "Settings are not usable.")
                   + " No tasks will be processed until this is fixed. Waiting requests stay in the inbox.",
                   root, "runner_locked")
        return {"state": "locked", "cfg": None, "process": False, "review": set(), "note": None, "code": ev["code"]}
    if ev["state"] == "first_run":
        _warn_once("first", "FIRST RUN: no settings folder exists yet. Using the stub backend with every seat on. "
                   "Open the Admin page when you want a real model.")
        return {"state": "first_run", "cfg": ev["cfg"], "process": True, "review": set(), "note": None, "code": "first_run"}
    _state["good_cfg"] = ev["cfg"]
    if _state.get("warned"):
        print("Settings look OK again. Processing will resume.", file=sys.stderr)
        _state["warned"] = None
    return {"state": "ready", "cfg": ev["cfg"], "process": True, "review": set(), "note": None, "code": "ok"}


def apply_manifest(root, cfg, agents, rt):
    """Pause seats whose instructions changed since the saved baseline. Create the baseline on first use."""
    if rt["state"] == "first_run":
        return rt
    try:
        saved = integrity.load_manifest()
    except integrity.ManifestError as e:
        _warn_once("manifest", "RUNNER LOCKED: " + str(e), root, "runner_locked")
        rt.update(state="locked", process=False, review=set(), note=None, code="manifest")
        return rt
    current = integrity.snapshot(root, (cfg or {}).get("dashboard_path"))
    if saved is None:
        try:
            integrity.save_manifest(current, root)
        except config.ConfigError as e:
            _warn_once("manifest-save", "RUNNER LOCKED: " + str(e), root, "runner_locked")
            rt.update(state="locked", process=False, review=set(), note=None, code="manifest")
            return rt
        log(root, "manifest_baseline", files=len(current))
        print("Integrity baseline saved (%d file(s)). Future changes need approval on the Admin page." % len(current))
        rt["review"] = set()
        return rt
    changes = integrity.diff((saved.get("files") or {}), current)
    if not changes:
        rt["review"] = set()
        if isinstance(_state.get("warned"), str) and _state["warned"].startswith("review:"):
            print("Configuration matches the approved baseline. Seats paused for review will resume.", file=sys.stderr)
            _state["warned"] = None
        return rt
    paused = integrity.paused_seats(changes, agents.keys())
    rt["review"] = paused
    rt["state"] = "paused_for_review"
    _warn_once("review:" + ",".join(c["name"] for c in changes),
               "PAUSED FOR REVIEW: configuration changed, owner review needed (%s). "
               "Tasks stay queued. Approve the current files on the Admin page." % (", ".join(sorted(paused)) or "no seats"))
    return rt


def write_public_status(root, agents, cfg, runner_state, review_paused, settings_note=None):
    """Non-secret status for the staff dashboard. The runner never reads this file back to decide anything.

    `message` is a fixed sentence for staff, or empty. It never includes paths, keys, or the reason.
    """
    message = STAFF_HOLD_MESSAGE if runner_state in ("locked", "paused_for_review") else ""
    review_paused = set(review_paused or ())
    path = os.path.join(root, "_agents", "_public_status.json")
    prev = _read_json_regular(path)
    prev_seats = prev.get("seats") if isinstance(prev, dict) and isinstance(prev.get("seats"), dict) else {}
    seats = {}
    for rid in sorted(agents):
        last = _last_draft.get(rid) or (prev_seats.get(rid) or {}).get("last_draft_at")
        if runner_state == "locked" or rid in review_paused or cfg is None:
            enabled = False
        else:
            enabled = config.seat_settings(cfg, rid)["enabled"]
        seats[rid] = {"enabled": enabled, "last_draft_at": last if isinstance(last, str) else None}
    payload = {"updated": now(), "runner_state": runner_state, "message": message, "seats": seats}
    if settings_note:
        payload["settings_note"] = settings_note
    if isinstance(prev, dict) and prev.get("seats") == seats and prev.get("runner_state") == runner_state \
            and prev.get("message") == message and prev.get("settings_note") == settings_note:
        try:
            age = (datetime.datetime.now().astimezone() - datetime.datetime.fromisoformat(prev["updated"])).total_seconds()
        except (KeyError, TypeError, ValueError):
            age = STATUS_HEARTBEAT
        if 0 <= age < STATUS_HEARTBEAT:
            return
    try:
        _write(path, payload)
    except OSError as e:
        print(f"  ! could not write _public_status.json: {e}", file=sys.stderr)


def sweep(root, agents, backend=None):
    """One pass over every seat. `backend` forces a provider; otherwise the Admin setting is used.

    Locked sweeps write status and return without touching the inbox. A seat paused for review
    keeps its tasks queued.
    """
    rt = resolve_runtime(root)
    if not rt["process"]:
        if rt.get("code") != "layout":
            write_public_status(root, agents, None, "locked", set(), None)
        return 0
    fresh = load_agents(root)
    if fresh:
        agents = fresh
    if rt["state"] != "first_run":
        rt = apply_manifest(root, rt["cfg"], agents, rt)
        if not rt["process"]:
            write_public_status(root, agents, None, "locked", set(), None)
            return 0
    cfg = rt["cfg"]
    chosen = backend or cfg.get("provider") or "stub"
    if chosen not in BACKENDS:
        chosen = "stub"
    if rt["state"] == "first_run" and chosen != "stub":
        _warn_once("nostub", "REFUSED: there is no settings folder yet (first run). Only the stub backend is allowed "
                   "until you finish setup on the Admin page. No tasks were processed.")
        write_public_status(root, agents, cfg, "first_run", set(), None)
        return 0
    fp = config.fingerprint(cfg)
    if fp != _state["fingerprint"]:
        _state["fingerprint"] = fp
        _retry.clear()  # settings changed: give failed tasks a fresh try straight away
    if chosen != _state["provider"]:
        _state["provider"] = chosen
        if chosen != "stub":
            print("NOTE: provider '%s' sends request text to an outside model provider. Use only for non-PHI work, "
                  "or only under a signed BAA with the provider's HIPAA-eligible configuration." % chosen)
    review = set(rt["review"])
    n = 0
    for rid, agent in agents.items():
        folder = role_dir(root, agent)
        if folder is None:
            log(root, "seat_skipped", role=rid, reason="bad_folder")
            continue
        ready = []
        for fn, path, st in _inbox_entries(root, agent, folder):
            if time.time() - st.st_mtime > 1.5:
                ready.append((fn, path, st))
        admin_paused = not config.seat_settings(cfg, rid)["enabled"]
        if rid in review or admin_paused:
            # Paused: leave task files queued. Non-files were already quarantined above.
            if ready and time.monotonic() - _paused_logged.get(rid, -PAUSED_LOG_EVERY) >= PAUSED_LOG_EVERY:
                _paused_logged[rid] = time.monotonic()
                if rid in review:
                    log(root, "config_review_skip", role=rid, queued=len(ready),
                        reason="configuration changed, owner review needed")
                else:
                    log(root, "seat_paused_skip", role=rid, queued=len(ready))
            continue
        _paused_logged.pop(rid, None)
        for fn, path, st in ready:
            wait = _retry.get(path)
            if wait and time.monotonic() < wait[1]:
                continue
            process_task(root, agent, folder, fn, chosen, cfg, st)
            n += 1
    pub = rt["state"] if rt["state"] in ("first_run", "paused_for_review", "locked") else "ready"
    write_public_status(root, agents, cfg, pub, review, rt.get("note"))
    return n


def main(argv=None):
    ap = argparse.ArgumentParser(description="Office AI Workspace runner.")
    ap.add_argument("--root", required=True, help="shared workspace folder (must be outside this program folder)")
    ap.add_argument("--backend", choices=list(BACKENDS), default=None,
                    help="force a provider (default: the provider chosen on the Admin page, else stub)")
    ap.add_argument("--interval", type=float, default=5.0)
    ap.add_argument("--once", action="store_true")
    a = ap.parse_args(argv)
    try:
        root = str(Path(a.root).resolve())
    except OSError:
        print("ERROR: the workspace path could not be resolved.", file=sys.stderr)
        return 2
    try:
        config.assert_separated(root)
    except config.LayoutError as e:
        print("ERROR: " + str(e), file=sys.stderr)
        return 2
    if not os.path.isdir(os.path.join(root, "_agents")):
        print("ERROR: %s does not look like a workspace (no _agents folder). "
              "Point --root at the shared data folder, not the program folder." % root, file=sys.stderr)
        return 2
    agents = load_agents(root)
    ev = config.evaluate_settings(root)
    cfg = ev["cfg"] if ev["state"] in ("ready", "first_run") else None
    shown = a.backend or (cfg["provider"] if cfg else "stub")
    print(f"Runner: {len(agents)} agent seat(s), provider={shown}, root={root}")
    if shown == "anthropic" and cfg is not None and ev["state"] != "first_run":
        key, src = config.get_api_key(cfg)
        where = "environment variable" if src == "env" else "Admin settings"
        print("API key: " + (f"{config.mask_key(key)} (from {where})" if key else "NOT SET - open the Admin page to add one"))
    if a.once:
        sweep(root, agents, a.backend)
        return 0
    try:
        while True:
            sweep(root, agents, a.backend)
            time.sleep(a.interval)
    except KeyboardInterrupt:
        print("stopped")
    return 0

if __name__ == "__main__":
    sys.exit(main())
