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

Standard library only. Usage:
    python3 runner.py --root . --once            # process the inbox once and exit
    python3 runner.py --root .                   # keep watching (Ctrl+C to stop)
    python3 runner.py --root . --backend anthropic   # force a provider (otherwise the Admin setting is used)
"""
import argparse, json, os, re, sys, time, datetime, socket, urllib.request, urllib.error

import config

ROLE_RE = re.compile(r"^R\d{3}$")
TASK_RE = re.compile(r"^[A-Za-z0-9_-]{1,80}$")

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

def load_agents(root):
    agents = {}
    d = os.path.join(root, "_agents")
    for fn in os.listdir(d):
        if fn.endswith(".json") and ROLE_RE.match(fn[:-5]):
            with open(os.path.join(d, fn), encoding="utf-8") as f:
                agents[fn[:-5]] = json.load(f)
    return agents

def load_sop_text(root, rid, limit=12000):
    d = os.path.join(root, "_sops", rid)
    out, used = [], 0
    if os.path.isdir(d):
        for fn in sorted(os.listdir(d)):
            if fn.startswith("PUT-") or not fn.lower().endswith((".txt", ".md")):
                continue
            with open(os.path.join(d, fn), encoding="utf-8", errors="replace") as f:
                t = f.read()[: max(0, limit - used)]
            used += len(t)
            out.append(f"--- {fn} ---\n{t}")
    return "\n".join(out)

def build_system_prompt(root, agent):
    w, a = agent["from_workbook"], agent["authored_for_prototype"]
    duties = "\n".join(f"- {x['duty']} ({x['frequency']})" for x in w["duties"][:25])
    sops = "\n".join(f"- {s['title']} [{s['status']}]" for s in w["sops"][:25]) or "- none located"
    rules = "\n".join(f"- {r}" for r in a["must_not"])
    dr_path = os.path.join(root, "_agents", "_decision_rights.json")
    dr = ""
    if os.path.exists(dr_path):
        with open(dr_path, encoding="utf-8") as f:
            dr = "\n".join(f"- {x['decision']}: {x['owner']} ({x['limit_or_note']})" for x in json.load(f))
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

def process_task(root, agent, folder, fn, backend, cfg=None):
    cfg = cfg if cfg is not None else config.default_config()
    inbox = os.path.join(folder, "inbox")
    path = os.path.join(inbox, fn)
    tid = fn[:-5]
    try:
        with open(path, encoding="utf-8") as f:
            task = json.load(f)
    except Exception as e:
        log(root, "task_unreadable", role=agent["role_id"], file=fn, error=str(e)[:100])
        return
    # never trust ids/roles from inside the file
    if not TASK_RE.match(tid) or task.get("role_id") != agent["role_id"]:
        log(root, "task_rejected_bad_header", role=agent["role_id"], file=fn)
        os.replace(path, os.path.join(folder, "archive", tid + ".rejected.json"))
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

def load_runtime_config(root):
    """Read config.json fresh. A damaged file keeps the last good settings; a settings folder
    inside the workspace is refused (defaults are used and the problem is reported once)."""
    try:
        cfg = config.load(root)
        _state["good_cfg"], _state["warned"] = cfg, None
        return cfg
    except config.ConfigError as e:
        msg = str(e)
        if _state["warned"] != msg:
            _state["warned"] = msg
            print("SETTINGS PROBLEM: " + msg, file=sys.stderr)
            log(root, "config_error", error=msg[:300])
        if isinstance(e, config.ConfigLocationError):
            return config.default_config()
        return _state["good_cfg"] or config.default_config()

def _read_json(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None

def write_public_status(root, agents, cfg):
    """Non-secret status for the staff dashboard. The runner is the source of truth:
    it never reads this file back to decide anything."""
    path = os.path.join(root, "_agents", "_public_status.json")
    prev = _read_json(path)
    prev_seats = prev.get("seats") if isinstance(prev, dict) and isinstance(prev.get("seats"), dict) else {}
    seats = {}
    for rid in sorted(agents):
        last = _last_draft.get(rid) or (prev_seats.get(rid) or {}).get("last_draft_at")
        seats[rid] = {"enabled": config.seat_settings(cfg, rid)["enabled"],
                      "last_draft_at": last if isinstance(last, str) else None}
    if isinstance(prev, dict) and prev.get("seats") == seats:
        try:
            age = (datetime.datetime.now().astimezone() - datetime.datetime.fromisoformat(prev["updated"])).total_seconds()
        except (KeyError, TypeError, ValueError):
            age = STATUS_HEARTBEAT
        if 0 <= age < STATUS_HEARTBEAT:
            return
    try:
        _write(path, {"updated": now(), "seats": seats})
    except OSError as e:
        print(f"  ! could not write _public_status.json: {e}", file=sys.stderr)

def sweep(root, agents, backend=None):
    """One pass over every seat. `backend` forces a provider; otherwise the Admin setting is used."""
    cfg = load_runtime_config(root)
    fp = config.fingerprint(cfg)
    if fp != _state["fingerprint"]:
        _state["fingerprint"] = fp
        _retry.clear()  # settings changed: give failed tasks a fresh try straight away
    chosen = backend or cfg["provider"]
    if chosen not in BACKENDS:
        chosen = "stub"
    if chosen != _state["provider"]:
        _state["provider"] = chosen
        if chosen != "stub":
            print("NOTE: provider '%s' sends request text to an outside model provider. Use only for non-PHI work, "
                  "or only under a signed BAA with the provider's HIPAA-eligible configuration." % chosen)
    n = 0
    for rid, agent in agents.items():
        folder = os.path.join(root, agent["folder"])
        inbox = os.path.join(folder, "inbox")
        if not os.path.isdir(inbox):
            continue
        ready = [fn for fn in sorted(os.listdir(inbox))
                 if fn.endswith(".json") and not fn.startswith(".")
                 and time.time() - os.path.getmtime(os.path.join(inbox, fn)) > 1.5]
        if not config.seat_settings(cfg, rid)["enabled"]:
            # Paused: leave tasks queued (do not archive or touch them).
            if ready and time.monotonic() - _paused_logged.get(rid, -PAUSED_LOG_EVERY) >= PAUSED_LOG_EVERY:
                _paused_logged[rid] = time.monotonic()
                log(root, "seat_paused_skip", role=rid, queued=len(ready))
            continue
        _paused_logged.pop(rid, None)
        for fn in ready:
            wait = _retry.get(os.path.join(inbox, fn))
            if wait and time.monotonic() < wait[1]:
                continue
            process_task(root, agent, folder, fn, chosen, cfg)
            n += 1
    write_public_status(root, agents, cfg)
    return n

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=".")
    ap.add_argument("--backend", choices=list(BACKENDS), default=None,
                    help="force a provider (default: the provider chosen on the Admin page, else stub)")
    ap.add_argument("--interval", type=float, default=5.0)
    ap.add_argument("--once", action="store_true")
    a = ap.parse_args()
    root = os.path.abspath(a.root)
    agents = load_agents(root)
    cfg = load_runtime_config(root)
    key, src = config.get_api_key(cfg)
    shown = a.backend or cfg["provider"]
    print(f"Runner: {len(agents)} agent seat(s), provider={shown}, root={root}")
    if shown == "anthropic":
        where = "environment variable" if src == "env" else "Admin settings"
        print("API key: " + (f"{config.mask_key(key)} (from {where})" if key else "NOT SET - open the Admin page to add one"))
    if a.once:
        sweep(root, agents, a.backend)
        return
    try:
        while True:
            sweep(root, agents, a.backend)
            time.sleep(a.interval)
    except KeyboardInterrupt:
        print("stopped")

if __name__ == "__main__":
    main()
