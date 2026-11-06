#!/usr/bin/env python3
"""Office AI Workspace -- Admin page (runs on the runner computer ONLY).

    python3 admin.py --root <workspace folder>      # start the page (prints the address)
    python3 admin.py --root <workspace folder> --set-pin   # set or reset the PIN in this terminal

Security model (see README "Admin page"):
  * binds to 127.0.0.1 only -- never 0.0.0.0 -- default port 8787;
  * PIN login (salted PBKDF2), HttpOnly+SameSite=Strict session cookie, idle timeout, lock-out;
  * every request must carry Host 127.0.0.1:<port> or localhost:<port> (DNS-rebinding guard);
    every POST needs a CSRF header and a same-origin Origin (if the browser sends one);
  * the API key lives in ~/.office-ai/config.json (outside the shared folder) and is only ever
    sent to the browser masked; it is never logged and never put in an error message.

Standard library only.
"""
import argparse
import datetime
import getpass
import hmac
import http.cookies
import json
import os
import re
import secrets
import subprocess
import sys
import threading
import time
import urllib.error
import webbrowser
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit, parse_qs

import config
import integrity
import runner

BIND_HOST = "127.0.0.1"          # never 0.0.0.0
COOKIE = "oai_session"
MAX_BODY = 64 * 1024
MAX_PROMPT = 4000
LOCK_AFTER = 5                   # wrong PINs
LOCK_SECONDS = 300
TEST_CALLS_PER_MINUTE = 6
CSP = ("default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'self'; "
       "base-uri 'none'; form-action 'none'; frame-ancestors 'none'")
TEST_SYSTEM = "You are a helpful assistant. This is a short test of a model; answer briefly."
TEST_AGENT = {"role_id": "R000", "role_title": "Admin test"}
EVENT_KINDS = ("draft_created", "phi_tripwire", "model_error")
REVISION_NAME_RE = re.compile(r"-r\d+\.json$")


class ApiError(Exception):
    def __init__(self, status, message, **extra):
        super().__init__(message)
        self.status, self.message, self.extra = status, message, extra


# =========================================================================== usage aggregation
def _to_date(value):
    try:
        return datetime.date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _mtime_date(path):
    try:
        return datetime.date.fromtimestamp(os.path.getmtime(path))
    except OSError:
        return None


def _bucket():
    return {"req": set(), "drafts": 0, "blocks": 0, "errors": 0, "tin": 0, "tout": 0, "cost": 0.0,
            "priced": 0, "unpriced": 0, "lat": 0, "lat_n": 0, "approved": 0, "revised": 0, "discarded": 0}


def _merge(into, b):
    into["req"] |= b["req"]
    for k in b:
        if k != "req":
            into[k] += b[k]


def _finish(b):
    denom = b["approved"] + b["revised"] + b["discarded"]
    status = "complete" if not b["unpriced"] else ("unset" if not b["priced"] else "partial")
    return {
        "requests": len(b["req"]), "drafts": b["drafts"], "tripwire_blocks": b["blocks"],
        "model_errors": b["errors"], "tokens_in": b["tin"], "tokens_out": b["tout"],
        "estimated_cost": None if status == "unset" else round(b["cost"], 6),
        "cost_status": status, "unpriced_events": b["unpriced"],
        "avg_time_to_draft_ms": round(b["lat"] / b["lat_n"]) if b["lat_n"] else None,
        "approved": b["approved"], "revised": b["revised"], "discarded": b["discarded"],
        "approval_rate": round(b["approved"] / denom, 4) if denom else None,
    }


def _event_cost(ev, prices):
    """(dollars, is_priced, needs_price). Uses the logged cost, else today's price table."""
    tin, tout = int(ev.get("input_tokens") or 0), int(ev.get("output_tokens") or 0)
    if tin + tout == 0:
        return 0.0, False, False
    logged = ev.get("estimated_cost")
    if isinstance(logged, (int, float)) and not isinstance(logged, bool):
        return float(logged), True, False
    c = config.estimate_cost(prices, ev.get("model"), tin, tout)
    return (c, True, False) if c is not None else (0.0, False, True)


def _read_jsonl(path):
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                try:
                    ev = json.loads(line)
                except ValueError:
                    continue
                if isinstance(ev, dict):
                    yield ev
    except OSError:
        return


def _json_files(folder):
    try:
        names = sorted(os.listdir(folder))
    except OSError:
        return
    for fn in names:
        if fn.endswith(".json") and not fn.startswith("."):
            path = os.path.join(folder, fn)
            if not runner.is_regular_file(path):
                continue
            try:
                with open(path, encoding="utf-8") as f:
                    data = json.load(f)
            except (OSError, ValueError):
                data = None
            yield fn, path, data if isinstance(data, dict) else {}


def compute_usage(root, days, cfg, today=None):
    """Aggregate _logs/*.jsonl plus the role folders. Metadata only; no request/draft text is read
    back out (the folder scans read file *names* and a few date/parent fields)."""
    agents = runner.load_agents(root)
    today = today or datetime.date.today()
    days = max(1, min(int(days), 365))
    start = today - datetime.timedelta(days=days - 1)
    week_now = today - datetime.timedelta(days=today.weekday())
    prices = cfg.get("prices") or {}
    seat_b = {rid: _bucket() for rid in agents}
    week_b = {}
    this_week = {rid: set() for rid in agents}
    last_used = {}

    def wb(d, rid):
        wk = (d - datetime.timedelta(days=d.weekday())).isoformat()
        return week_b.setdefault((wk, rid), _bucket())

    logdir = os.path.join(root, "_logs")
    try:
        logfiles = sorted(fn for fn in os.listdir(logdir) if re.fullmatch(r"\d{4}-\d\d-\d\d\.jsonl", fn))
    except OSError:
        logfiles = []
    for fn in logfiles:
        fdate = _to_date(fn)
        for n, ev in enumerate(_read_jsonl(os.path.join(logdir, fn))):
            kind, rid = ev.get("event"), ev.get("role")
            if kind not in EVENT_KINDS or rid not in agents:
                continue
            ts = str(ev.get("ts") or "")
            d = _to_date(ts) or fdate
            if ts and ts > last_used.get(rid, ""):
                last_used[rid] = ts
            if d is None:
                continue
            req_id = str(ev.get("task") or "%s#%d" % (fn, n))
            if d >= week_now:
                this_week[rid].add(req_id)
            if d < start:
                continue
            for b in (seat_b[rid], wb(d, rid)):
                b["req"].add(req_id)
                if kind == "draft_created":
                    b["drafts"] += 1
                    if isinstance(ev.get("latency_ms"), (int, float)):
                        b["lat"] += ev["latency_ms"]
                        b["lat_n"] += 1
                elif kind == "phi_tripwire":
                    b["blocks"] += 1
                else:
                    b["errors"] += 1
                b["tin"] += int(ev.get("input_tokens") or 0)
                b["tout"] += int(ev.get("output_tokens") or 0)
                cost, priced, needs = _event_cost(ev, prices)
                b["cost"] += cost
                b["priced"] += 1 if priced else 0
                b["unpriced"] += 1 if needs else 0

    # ---- role folders: approved / revised / discarded ----
    def count(rid, kind, d):
        if d is not None and d >= start:
            seat_b[rid][kind] += 1
            wb(d, rid)[kind] += 1

    for rid, agent in agents.items():
        folder = os.path.join(root, agent["folder"])
        for fn, path, data in _json_files(os.path.join(folder, "approved")):
            count(rid, "approved", _to_date(data.get("approved_at")) or _mtime_date(path))
        for fn, path, data in _json_files(os.path.join(folder, "archive")):
            if fn.endswith(".rejected.json"):
                continue
            if fn.endswith(".discarded.json"):
                count(rid, "discarded", _to_date(data.get("discarded_at")) or _mtime_date(path))
            elif REVISION_NAME_RE.search(fn) or data.get("parent_id"):
                count(rid, "revised", _to_date(data.get("created")) or _mtime_date(path))
        for fn, path, data in _json_files(os.path.join(folder, "inbox")):   # revisions still waiting
            if data.get("parent_id"):
                count(rid, "revised", _to_date(data.get("created")) or _mtime_date(path))

    seats, total = [], _bucket()
    for rid in sorted(agents):
        _merge(total, seat_b[rid])
        row = _finish(seat_b[rid])
        row.update(rid=rid, title=agents[rid].get("role_title", rid),
                   enabled=config.seat_settings(cfg, rid)["enabled"],
                   last_used=last_used.get(rid), requests_this_week=len(this_week[rid]),
                   unused=row["requests"] == 0)
        seats.append(row)
    weeks = []
    for (wk, rid), b in sorted(week_b.items()):
        row = _finish(b)
        row.update(week=wk, rid=rid)
        weeks.append(row)
    return {"days": days, "start": start.isoformat(), "end": today.isoformat(),
            "totals": _finish(total), "seats": seats, "weeks": weeks,
            "not_used_recently": [s["rid"] for s in seats if s["unused"]],
            "prices_set": bool(prices)}


# =========================================================================== server state
class AdminApp:
    def __init__(self, root):
        self.root = root
        self.cfg_lock = threading.RLock()      # read-modify-write of config.json
        self.pin_lock = threading.Lock()       # one PIN check at a time (also slows guessing)
        self.state_lock = threading.Lock()
        self.sessions = {}                     # token -> {"csrf", "seen"}
        self.fails = 0
        self.locked_until = 0.0
        self.calls = deque()                   # timestamps of recent paid test calls
        self.port = None
        self.allowed_hosts = set()
        self.good_cfg = None                    # last config.json that loaded in this process
        self.using_last_good = False
        self.settings_detail = None

    # ---- sessions ----
    def new_session(self, timeout):
        with self.state_lock:
            now = time.monotonic()
            for t in [t for t, s in self.sessions.items() if now - s["seen"] > timeout * 60]:
                del self.sessions[t]
            while len(self.sessions) >= 20:
                del self.sessions[min(self.sessions, key=lambda t: self.sessions[t]["seen"])]
            token = secrets.token_urlsafe(32)
            self.sessions[token] = {"csrf": secrets.token_urlsafe(32), "seen": now}
            return token, self.sessions[token]["csrf"]

    def get_session(self, token, timeout):
        with self.state_lock:
            s = self.sessions.get(token or "")
            now = time.monotonic()
            if not s:
                return None
            if now - s["seen"] > timeout * 60:
                del self.sessions[token]
                return None
            s["seen"] = now
            return s

    # ---- PIN lock-out ----
    def check_pin(self, cfg, pin):
        """Verify a PIN with lock-out. Raises ApiError(429/401)."""
        with self.pin_lock:
            wait = self.locked_until - time.monotonic()
            if wait > 0:
                raise ApiError(429, "Too many wrong PINs. Logins are locked for %d more minute(s)." % max(1, int(wait // 60) + 1),
                               retry_after=int(wait))
            if config.verify_pin(cfg, pin):
                self.fails = 0
                return
            self.fails += 1
            if self.fails >= LOCK_AFTER:
                self.fails = 0
                self.locked_until = time.monotonic() + LOCK_SECONDS
                raise ApiError(429, "Too many wrong PINs. Logins are locked for 5 minutes.", retry_after=LOCK_SECONDS)
            raise ApiError(401, "That PIN is not correct. %d tr%s left before a 5-minute lock."
                           % (LOCK_AFTER - self.fails, "y" if LOCK_AFTER - self.fails == 1 else "ies"))

    # ---- paid-call rate limit ----
    def take_calls(self, n):
        with self.state_lock:
            now = time.monotonic()
            while self.calls and now - self.calls[0] > 60:
                self.calls.popleft()
            if len(self.calls) + n > TEST_CALLS_PER_MINUTE:
                raise ApiError(429, "Please wait a minute before running more tests (limit: %d model calls per minute)."
                               % TEST_CALLS_PER_MINUTE)
            self.calls.extend([now] * n)


def find_env_files(root, max_depth=3, limit=5):
    found, base = [], root.rstrip("\\/").count(os.sep)
    for dirpath, dirnames, filenames in os.walk(root):
        if dirpath.count(os.sep) - base >= max_depth:
            dirnames[:] = []
        for fn in filenames:
            if fn == ".env" or fn.startswith(".env.") or fn.endswith(".env"):
                found.append(os.path.relpath(os.path.join(dirpath, fn), root))
        if len(found) >= limit:
            break
    return found[:limit]


def _num(v):
    if isinstance(v, bool) or v is None or (isinstance(v, str) and not v.strip()):
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        raise ApiError(400, "Prices must be plain numbers in dollars per million tokens, for example 3 or 15.5.")
    if f != f or f in (float("inf"), float("-inf")) or f < 0 or f > 1_000_000:
        raise ApiError(400, "Prices must be plain numbers in dollars per million tokens, for example 3 or 15.5.")
    return f


def _model_or_none(v):
    if v is None or (isinstance(v, str) and not v.strip()):
        return None
    if isinstance(v, str) and config.valid_model(v.strip()):
        return v.strip()
    raise ApiError(400, "Model names can only use letters, numbers, dots, dashes, underscores, colons and slashes.")


# =========================================================================== safety check (read-only; never changes permissions)
_WRITE_LETTERS = {"F", "M", "W", "WD", "AD", "DC", "WEA", "WDAC", "DE"}


def _safety_item(item_id, level, status, text):
    return {"id": item_id, "level": level, "status": status, "text": text}


def _has_write(perm):
    for group in re.findall(r"\(([^)]*)\)", perm or ""):
        letters = {part.strip().upper() for part in group.split(",") if part.strip()}
        if letters & _WRITE_LETTERS:
            return True
    return False


def _broad_identity(name):
    n = (name or "").strip().lower()
    if n in ("everyone", "users", "authenticated users"):
        return True
    return n.endswith("\\everyone") or n.endswith("\\users") or n.endswith("\\authenticated users")


def _icacls(path):
    """Return icacls stdout, or raise OSError. Does not change anything."""
    try:
        r = subprocess.run(["icacls", str(path)], capture_output=True, text=True, timeout=20,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.SubprocessError) as e:
        raise OSError("icacls could not be run") from e
    if r.returncode != 0 or not (r.stdout or "").strip():
        raise OSError("icacls failed")
    return r.stdout


def _advisory_windows(item_id, path, label, text):
    writers, saw = [], False
    norm_path = os.path.normcase(os.path.abspath(path))
    for line in (text or "").splitlines():
        if ":(" not in line:
            continue
        saw = True
        left, perm = line.split(":(", 1)
        perm = ":(" + perm
        if line[:1].isspace():
            ident = left.strip()
        else:
            raw_left = left.rstrip()
            if os.path.normcase(raw_left).startswith(norm_path):
                ident = raw_left[len(os.path.abspath(path)):].strip()
            else:
                ident = raw_left.strip().split()[-1] if raw_left.strip() else ""
        if _broad_identity(ident) and _has_write(perm):
            writers.append(ident)
    if not saw:
        return _safety_item(item_id, "advisory", "unknown",
                            label + ": could not verify (the permission listing was not available).")
    if writers:
        return _safety_item(item_id, "advisory", "warn",
                            label + " may be changeable by Everyone, Users, or Authenticated Users ("
                            + ", ".join(writers) + "). Permissions were not changed.")
    return _safety_item(item_id, "advisory", "pass",
                        label + " does not grant write or modify to Everyone, Users, or Authenticated Users.")


def _advisory_posix(item_id, label, mode):
    if mode & 0o022:
        return _safety_item(item_id, "advisory", "warn",
                            "%s is writable by the group or by other users (mode %s). Permissions were not changed."
                            % (label, oct(mode & 0o777)))
    return _safety_item(item_id, "advisory", "pass", label + " is not writable by the group or by other users.")


def _advisory(item_id, path, label, platform, icacls, stat_mode):
    if not path or not os.path.exists(path):
        return _safety_item(item_id, "advisory", "unknown",
                            label + " was not found, so this check could not verify it.")
    if platform == "nt":
        try:
            text = (icacls or _icacls)(path)
        except (OSError, subprocess.SubprocessError):
            return _safety_item(item_id, "advisory", "unknown",
                                label + ": could not verify (the permission listing was not available).")
        if not isinstance(text, str):
            return _safety_item(item_id, "advisory", "unknown",
                                label + ": could not verify (the permission listing was not available).")
        return _advisory_windows(item_id, path, label, text)
    try:
        mode = stat_mode(path) if stat_mode else os.stat(path).st_mode
    except OSError:
        return _safety_item(item_id, "advisory", "unknown",
                            label + ": could not verify (the permission listing was not available).")
    return _advisory_posix(item_id, label, mode)


def build_safety(root, app_directory=None, dashboard_path=None, platform=None, icacls=None, stat_mode=None):
    """Pass/fail hard checks, plus best-effort permission warnings. Never changes permissions."""
    platform = platform or os.name
    app_directory = app_directory or config.app_dir()
    items = []
    app = str(Path(app_directory).resolve())
    ws = str(Path(root).resolve())
    if config.same_or_nested(app, ws):
        items.append(_safety_item("app_outside_workspace", "hard", "fail",
                                  "The program folder and the shared workspace overlap. Code must live outside the shared folder. The runner will not start until this is fixed."))
    else:
        items.append(_safety_item("app_outside_workspace", "hard", "pass",
                                  "The program folder is outside the shared workspace."))
    try:
        config.check_location(root)
        items.append(_safety_item("settings_outside_workspace", "hard", "pass",
                                  "The settings folder is outside the shared workspace."))
    except config.ConfigLocationError:
        items.append(_safety_item("settings_outside_workspace", "hard", "fail",
                                  "The settings folder is inside the shared workspace. The runner will not use it."))
    if not config.config_path().exists():
        items.append(_safety_item("config_permissions", "hard", "unknown",
                                  "The settings file does not exist yet, so this check could not verify its permissions."))
    else:
        warns = config.permission_report()
        if warns:
            items.append(_safety_item("config_permissions", "hard", "fail", " ".join(warns)))
        else:
            items.append(_safety_item("config_permissions", "hard", "pass",
                                      "The settings file is locked down so only this account should be able to read it."))
    for item_id, path, label in (
        ("perm_agents", os.path.join(root, "_agents"), "The _agents folder"),
        ("perm_sops", os.path.join(root, "_sops"), "The _sops folder"),
        ("perm_app", app_directory, "The program folder"),
    ):
        items.append(_advisory(item_id, path, label, platform, icacls, stat_mode))
    if dashboard_path:
        items.append(_advisory("perm_dashboard", dashboard_path, "The dashboard file", platform, icacls, stat_mode))
    else:
        items.append(_safety_item("perm_dashboard", "advisory", "unknown",
                                  "The dashboard file path is not set, so this check could not verify it."))
    items.append(_safety_item(
        "manual_staff_account", "manual", "manual",
        "Log in as a staff account and confirm you cannot edit _agents or the app folder."))
    return items


# =========================================================================== request handler
class AdminHandler(BaseHTTPRequestHandler):
    app = None  # set by make_server
    server_version = "OfficeAIAdmin"
    sys_version = ""

    def log_message(self, fmt, *args):   # keep the console quiet and free of request details
        pass

    # ---- plumbing ----
    def _send(self, status, payload=None, body=None, ctype="application/json; charset=utf-8", headers=()):
        data = body if body is not None else json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Pragma", "no-cache")
        self.send_header("Content-Security-Policy", CSP)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        for k, v in headers:
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def _fail(self, status, message, **extra):
        self._send(status, {"error": message, **extra})

    def _guard_host(self):
        host = (self.headers.get("Host") or "").strip().lower()
        if host not in self.app.allowed_hosts:
            raise ApiError(403, "This page only answers requests addressed to 127.0.0.1 or localhost.")
        return host

    def _guard_post(self, host):
        origin = self.headers.get("Origin")
        if origin is not None and origin.strip().lower() != "http://" + host:
            raise ApiError(403, "Request rejected: it did not come from this page.")
        if self.headers.get_content_type() != "application/json":
            raise ApiError(415, "Requests must be JSON.")

    def _guard_fetch_site(self):
        site = self.headers.get("Sec-Fetch-Site")
        if site is not None and site not in ("same-origin", "none"):
            raise ApiError(403, "Request rejected: it did not come from this page.")

    def _cfg(self):
        try:
            cfg = config.load(self.app.root)
        except config.ConfigLocationError as e:
            raise ApiError(500, str(e))
        except config.ConfigError as e:
            if self.app.good_cfg is not None:
                self.app.using_last_good = True
                self.app.settings_detail = str(e)
                return self.app.good_cfg
            raise ApiError(500, str(e))
        self.app.good_cfg = cfg
        self.app.using_last_good = False
        self.app.settings_detail = None
        return cfg

    def _session(self, cfg):
        token = None
        try:
            jar = http.cookies.SimpleCookie()
            jar.load(self.headers.get("Cookie") or "")
            token = jar[COOKIE].value if COOKIE in jar else None
        except http.cookies.CookieError:
            pass
        s = self.app.get_session(token, cfg["session_timeout_minutes"])
        return token, s

    def _require(self, cfg, post):
        token, s = self._session(cfg)
        if not s:
            raise ApiError(401, "Please enter your PIN.")
        if post:
            sent = self.headers.get("X-CSRF-Token") or ""
            if not hmac.compare_digest(sent.encode(), s["csrf"].encode()):
                raise ApiError(403, "Request rejected (missing security token). Reload the page and sign in again.")
        return token, s

    def _body(self, raw):
        try:
            data = json.loads(raw.decode("utf-8")) if raw else {}
        except (ValueError, UnicodeDecodeError):
            raise ApiError(400, "The request was not valid JSON.")
        if not isinstance(data, dict):
            raise ApiError(400, "The request was not valid.")
        return data

    # ---- verbs ----
    def do_GET(self):
        try:
            self._guard_host()
            url = urlsplit(self.path)
            if url.path in ("/", "/index.html"):
                return self._send(200, body=PAGE.encode("utf-8"), ctype="text/html; charset=utf-8")
            if not url.path.startswith("/api/"):
                raise ApiError(404, "Not found.")
            self._guard_fetch_site()
            if url.path == "/api/session":
                cfg = self._cfg()
                _, s = self._session(cfg)
                return self._send(200, {"authenticated": bool(s), "csrf": s["csrf"] if s else None,
                                        "pin_set": config.pin_is_set(cfg)})
            if url.path == "/api/state":
                cfg = self._cfg()
                self._require(cfg, False)
                return self._send(200, self.state_payload(cfg))
            if url.path == "/api/usage":
                cfg = self._cfg()
                self._require(cfg, False)
                try:
                    days = int((parse_qs(url.query).get("days") or ["30"])[0])
                except ValueError:
                    days = 30
                return self._send(200, compute_usage(self.app.root, days if 1 <= days <= 365 else 30, cfg))
            if url.path == "/api/safety":
                cfg = self._cfg()
                self._require(cfg, False)
                return self._send(200, {"items": build_safety(self.app.root, dashboard_path=cfg.get("dashboard_path"))})
            raise ApiError(404, "Not found.")
        except ApiError as e:
            self._fail(e.status, e.message, **e.extra)
        except Exception as e:   # never print messages: they could contain secrets
            self._crash(e)

    def do_POST(self):
        try:
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                length = -1
            if length < 0 or length > MAX_BODY:
                self.close_connection = True
                raise ApiError(413, "That request is too large.")
            raw = self.rfile.read(length) if length else b""
            host = self._guard_host()
            self._guard_post(host)
            self._guard_fetch_site()
            url = urlsplit(self.path)
            route = POST_ROUTES.get(url.path)
            if not route:
                raise ApiError(404, "Not found.")
            data = self._body(raw)
            cfg = self._cfg()
            if url.path == "/api/login":
                return self.login(cfg, data)
            token, session = self._require(cfg, True)
            result = route(self, cfg, data, token)
            self._send(200, result)
        except ApiError as e:
            self._fail(e.status, e.message, **e.extra)
        except Exception as e:
            self._crash(e)

    def _method_not_allowed(self):
        try:
            n = int(self.headers.get("Content-Length") or 0)
            if 0 < n <= MAX_BODY:
                self.rfile.read(n)        # drain so the client sees our answer, not a reset
        except ValueError:
            pass
        self._fail(405, "Method not allowed.")
    do_PUT = do_DELETE = do_PATCH = do_OPTIONS = _method_not_allowed

    def _crash(self, e):
        tb = e.__traceback__
        while tb and tb.tb_next:
            tb = tb.tb_next
        print("Admin page error: %s (admin.py line %s)" % (type(e).__name__, tb.tb_lineno if tb else "?"), file=sys.stderr)
        try:
            self._fail(500, "Something went wrong inside the admin page. Nothing was changed by this request.")
        except Exception:
            pass

    # ---- GET payloads ----
    def state_payload(self, cfg):
        agents = runner.load_agents(self.app.root)
        key, source = config.get_api_key(cfg)
        seats = []
        for rid in sorted(agents):
            s = config.seat_settings(cfg, rid)
            seats.append({"rid": rid, "title": agents[rid].get("role_title", rid),
                          "enabled": s["enabled"], "model": s["model"]})
        return {
            "seats": seats,
            "provider": cfg["provider"],
            "default_model": cfg["default_model"],
            "effective_default_model": config.resolve_model(cfg),
            "key": {"present": bool(key), "source": source, "masked": config.mask_key(key),
                    "file_masked": config.mask_key(cfg["api_key"]),
                    "env_overrides_file": bool(source == "env" and cfg["api_key"])},
            "config": {"path": str(config.config_path()), "warnings": config.permission_report(),
                       "locked_down": not config.permission_report()},
            "workspace_warnings": ["A file named %s is inside the shared workspace. Never keep the API key in a .env "
                                   "file in the shared folder." % p for p in find_env_files(self.app.root)],
            "prices": cfg["prices"],
            "recent_models": cfg["recent_models"],
            "port": self.app.port,
            "session_timeout_minutes": cfg["session_timeout_minutes"],
            "dashboard_path": cfg.get("dashboard_path"),
            "hold": self._hold(cfg),
        }

    def _change_report(self, cfg):
        try:
            saved = integrity.load_manifest()
        except integrity.ManifestError as e:
            return [], str(e), False
        current = integrity.snapshot(self.app.root, cfg.get("dashboard_path") if cfg else None)
        if saved is None:
            return [], None, False
        return integrity.diff(saved.get("files") or {}, current), None, True

    def _hold(self, cfg):
        """Owner-facing runner state. Detailed text stays here; the staff file gets a generic sentence."""
        changes, manifest_error, baseline = self._change_report(cfg)
        ev = config.evaluate_settings(self.app.root)
        state, detail, note = ev["state"], ev["detail"], None
        if state == "locked" and ev["recoverable"] and self.app.using_last_good:
            state, note = "ready", config.LAST_GOOD_NOTE
            detail = self.app.settings_detail
        if manifest_error and state in ("ready", "first_run"):
            state, detail = "locked", manifest_error
        elif changes and state == "ready":
            state, detail = "paused_for_review", "configuration changed, owner review needed"
        return {"state": state, "detail": detail, "settings_note": note, "changes": changes,
                "manifest_error": manifest_error, "baseline_present": baseline}

    # ---- POST handlers ----
    def login(self, cfg, data):
        pin = data.get("pin")
        if not config.pin_is_set(cfg):
            raise ApiError(409, "No PIN has been set yet. Run  admin.py --set-pin  in the terminal first.")
        self.app.check_pin(cfg, pin if isinstance(pin, str) else "")
        token, csrf = self.app.new_session(cfg["session_timeout_minutes"])
        self._send(200, {"csrf": csrf},
                   headers=[("Set-Cookie", "%s=%s; HttpOnly; SameSite=Strict; Path=/" % (COOKIE, token))])

    def logout(self, cfg, data, token):
        with self.app.state_lock:
            self.app.sessions.pop(token, None)
        return {"ok": True}

    def connection(self, cfg, data, token):
        with self.app.cfg_lock:
            cfg = self._cfg()
            if "provider" in data:
                if data["provider"] not in config.PROVIDERS:
                    raise ApiError(400, "Choose Anthropic or Stub.")
                cfg["provider"] = data["provider"]
            if "default_model" in data:
                cfg["default_model"] = _model_or_none(data["default_model"])
                if cfg["default_model"]:
                    config.remember_model(cfg, cfg["default_model"])
            key = data.get("api_key")
            if isinstance(key, str) and key.strip():
                if not config.valid_key_format(key.strip()):
                    raise ApiError(400, "That does not look like an API key (it should be one unbroken string of "
                                        "letters, numbers and dashes). Nothing was saved.")
                cfg["api_key"] = key.strip()
            self._save(cfg)
        return self.state_payload(self._cfg())

    def _save(self, cfg):
        try:
            config.save(cfg, self.app.root)
        except config.ConfigError as e:
            raise ApiError(500, str(e))

    def _call(self, provider, model, key, prompt, max_tokens):
        """One model call for the admin page. Returns a result dict; never raises on model trouble."""
        task = {"task_type": "admin_test", "instructions": prompt, "max_tokens": max_tokens}
        t0 = time.monotonic()
        try:
            text, usage = runner.normalize_result(
                runner.BACKENDS[provider](TEST_SYSTEM, prompt, task, TEST_AGENT, model=model, api_key=key), model, t0)
            return {"model": model, "ok": True, "text": text, "error": None, **{k: usage[k] for k in
                    ("latency_ms", "input_tokens", "output_tokens")}}
        except runner.ModelError as e:
            err = config.redact(e, key)
        except (RuntimeError, OSError, ValueError, urllib.error.URLError) as e:
            err = "The test could not be completed: " + config.redact(e, key)[:150]
        return {"model": model, "ok": False, "text": "", "error": err, "input_tokens": 0, "output_tokens": 0,
                "latency_ms": int((time.monotonic() - t0) * 1000)}

    def test_connection(self, cfg, data, token):
        if cfg["provider"] == "stub":
            return {"ok": True, "message": "Stub mode is selected. No key is needed and nothing is sent to any provider."}
        key, source = config.get_api_key(cfg)
        if not key:
            return {"ok": False, "message": "No API key is set yet. Paste your key above and press Save."}
        model = config.resolve_model(cfg)
        self.app.take_calls(1)
        r = self._call(cfg["provider"], model, key, "Reply with the single word: OK", 16)
        runner.log(self.app.root, "admin_test", kind="connection", models=[model], ok=[r["ok"]],
                   input_tokens=r["input_tokens"], output_tokens=r["output_tokens"], latency_ms=r["latency_ms"])
        if r["ok"]:
            return {"ok": True, "model": model, "latency_ms": r["latency_ms"],
                    "message": "Connected. The key works with model %s (%.1f s)." % (model, r["latency_ms"] / 1000)}
        return {"ok": False, "message": r["error"]}

    def test_models(self, cfg, data, token):
        prompt = data.get("prompt")
        if not isinstance(prompt, str) or not prompt.strip():
            raise ApiError(400, "Type some fake, non-patient test text first.")
        if len(prompt) > MAX_PROMPT:
            raise ApiError(400, "Test text can be at most %d characters." % MAX_PROMPT)
        models = data.get("models")
        if not isinstance(models, list) or not 1 <= len(models) <= 2:
            raise ApiError(400, "Choose one or two models to test.")
        names = []
        for m in models:
            m = _model_or_none(m)
            if not m:
                raise ApiError(400, "Enter a model name for each box you use.")
            if m not in names:
                names.append(m)
        hits = runner.phi_scan(prompt)          # privacy check BEFORE anything is sent anywhere
        if hits:
            runner.log(self.app.root, "admin_test", kind="models", blocked=True, kinds=hits)
            raise ApiError(400, "Not sent: the text looks like it contains personal identifiers (%s). "
                                "Use fake, non-patient text only." % ", ".join(hits), blocked=True, reasons=hits)
        provider = cfg["provider"]
        key, source = config.get_api_key(cfg)
        if provider == "anthropic" and not key:
            raise ApiError(400, "No API key is set yet. Add one on the Connection tab first.")
        self.app.take_calls(len(names))
        results = []
        for m in names:
            r = self._call(provider, m, key, prompt, 1000)
            r["estimated_cost"] = (config.estimate_cost(cfg["prices"], m, r["input_tokens"], r["output_tokens"])
                                   if r["ok"] else None)
            results.append(r)
        with self.app.cfg_lock:
            c2 = self._cfg()
            for m in reversed(names):
                config.remember_model(c2, m)
            self._save(c2)
        costs = [r["estimated_cost"] for r in results if r["estimated_cost"] is not None]
        extra = {"estimated_cost": round(sum(costs), 6)} if costs else {}
        runner.log(self.app.root, "admin_test", kind="models", models=names, ok=[r["ok"] for r in results],
                   input_tokens=sum(r["input_tokens"] for r in results),
                   output_tokens=sum(r["output_tokens"] for r in results),
                   latency_ms=[r["latency_ms"] for r in results], **extra)
        return {"results": results, "provider": provider}

    def seat(self, cfg, data, token):
        rid = data.get("rid")
        if not isinstance(rid, str) or rid not in runner.load_agents(self.app.root):
            raise ApiError(400, "Unknown seat.")
        if "enabled" not in data and "model" not in data:
            raise ApiError(400, "Nothing to change.")
        if "enabled" in data and not isinstance(data["enabled"], bool):
            raise ApiError(400, "The on/off value must be true or false.")
        with self.app.cfg_lock:
            cfg = self._cfg()
            cur = config.seat_settings(cfg, rid)
            if "enabled" in data:
                cur["enabled"] = data["enabled"]
            if "model" in data:
                cur["model"] = _model_or_none(data["model"])
                if cur["model"]:
                    config.remember_model(cfg, cur["model"])
            cfg["per_seat"][rid] = cur
            self._save(cfg)
        return self.state_payload(self._cfg())

    def prices(self, cfg, data, token):
        prices = data.get("prices")
        if not isinstance(prices, dict) or len(prices) > 50:
            raise ApiError(400, "The price list was not valid.")
        clean = {}
        for model, p in prices.items():
            m = _model_or_none(model)
            if not m:
                continue
            p = p if isinstance(p, dict) else {}
            i, o = _num(p.get("input_per_mtok")), _num(p.get("output_per_mtok"))
            if i is None and o is None:
                continue
            if i is None or o is None:
                raise ApiError(400, "Enter both the input and the output price for %s (or clear both)." % m)
            clean[m] = {"input_per_mtok": i, "output_per_mtok": o}
        with self.app.cfg_lock:
            cfg = self._cfg()
            cfg["prices"] = clean
            self._save(cfg)
        return self.state_payload(self._cfg())

    def set_pin(self, cfg, data, token):
        cur, new = data.get("current_pin"), data.get("new_pin")
        if not config.valid_pin_format(new):
            raise ApiError(400, "The new PIN must be at least %d digits (numbers only)." % config.PIN_MIN_DIGITS)
        self.app.check_pin(cfg, cur if isinstance(cur, str) else "")
        with self.app.cfg_lock:
            cfg = self._cfg()
            cfg["pin"] = config.make_pin_record(new)
            self._save(cfg)
        with self.app.state_lock:   # other sessions must sign in again with the new PIN
            self.app.sessions = {t: s for t, s in self.app.sessions.items() if t == token}
        return {"ok": True}

    def approve_manifest(self, cfg, data, token):
        """Re-baseline the manifest from the files on disk. The browser confirms before it calls this."""
        current = integrity.snapshot(self.app.root, cfg.get("dashboard_path"))
        try:
            integrity.save_manifest(current, self.app.root)
        except config.ConfigError as e:
            raise ApiError(500, str(e))
        runner.log(self.app.root, "manifest_approved", files=len(current))
        return self.state_payload(self._cfg())

    def dashboard_path(self, cfg, data, token):
        raw = data.get("dashboard_path")
        if raw is None or (isinstance(raw, str) and not raw.strip()):
            path = None
        elif isinstance(raw, str) and len(raw.strip()) <= 500 and "\n" not in raw and "\r" not in raw:
            path = raw.strip()
        else:
            raise ApiError(400, "Enter the full path to dashboard.html, or leave the box blank to clear it.")
        with self.app.cfg_lock:
            cfg = self._cfg()
            cfg["dashboard_path"] = path
            self._save(cfg)
        return self.state_payload(self._cfg())


POST_ROUTES = {
    "/api/login": AdminHandler.login,
    "/api/logout": AdminHandler.logout,
    "/api/connection": AdminHandler.connection,
    "/api/test_connection": AdminHandler.test_connection,
    "/api/test_models": AdminHandler.test_models,
    "/api/seat": AdminHandler.seat,
    "/api/prices": AdminHandler.prices,
    "/api/set_pin": AdminHandler.set_pin,
    "/api/manifest/approve": AdminHandler.approve_manifest,
    "/api/dashboard_path": AdminHandler.dashboard_path,
}


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    # On Windows, SO_REUSEADDR lets a second program bind a port that is already in use, so another
    # process could silently share (or shadow) the admin port. Only enable it on POSIX, where it
    # merely allows a quick restart.
    allow_reuse_address = os.name != "nt"


def make_server(root, port=None):
    """Create (not start) the admin server, bound to 127.0.0.1 only. port=0 picks a free port."""
    config.assert_separated(root)
    config.check_location(root)
    if port is None:
        try:
            port = config.load(root)["admin_port"]
        except config.ConfigError:
            port = config.DEFAULT_PORT
    app = AdminApp(root)
    handler = type("BoundAdminHandler", (AdminHandler,), {"app": app})
    srv = _Server((BIND_HOST, port), handler)
    app.port = srv.server_address[1]
    app.allowed_hosts = {"127.0.0.1:%d" % app.port, "localhost:%d" % app.port}
    srv.app = app
    return srv


# =========================================================================== command line
def read_secret(prompt):
    """Read a PIN without echoing it. On Windows, show one '*' per key: the standard getpass shows
    nothing at all there, which makes it look as if typing is not working."""
    if os.name == "nt":
        try:
            import msvcrt
        except ImportError:
            return getpass.getpass(prompt)
        sys.stdout.write(prompt)
        sys.stdout.flush()
        chars = []
        while True:
            ch = msvcrt.getwch()
            if ch in ("\r", "\n"):
                sys.stdout.write("\n")
                return "".join(chars)
            if ch == "\x03":
                raise KeyboardInterrupt
            if ch in ("\x00", "\xe0"):      # arrow / function key: swallow the second code
                msvcrt.getwch()
            elif ch == "\b":
                if chars:
                    chars.pop()
                    sys.stdout.write("\b \b")
            elif ch.isprintable():
                chars.append(ch)
                sys.stdout.write("*")
            sys.stdout.flush()
    return getpass.getpass(prompt)


def set_pin_interactive(root):
    print("Set the Admin PIN: at least %d digits, numbers only. (It is stored only as a salted hash.)" % config.PIN_MIN_DIGITS)
    print("Click in this window and type; you will see a * for each digit. Press Enter when done.")
    for _ in range(3):
        a = read_secret("New PIN: ")
        if not config.valid_pin_format(a):
            print("That PIN is not valid: use at least %d digits, numbers only." % config.PIN_MIN_DIGITS)
            continue
        if read_secret("Type it again: ") != a:
            print("The two entries did not match.")
            continue
        try:
            cfg = config.load(root)
        except config.ConfigMissingError:
            cfg = config.default_config()
        cfg["pin"] = config.make_pin_record(a)
        config.save(cfg, root)
        print("PIN saved.")
        return 0
    print("PIN was not set.", file=sys.stderr)
    return 1


def main(argv=None):
    ap = argparse.ArgumentParser(description="Office AI Workspace admin page (this computer only).")
    ap.add_argument("--root", required=True, help="the shared workspace folder (must be outside this program folder)")
    ap.add_argument("--port", type=int, default=None, help="port (default: saved setting, else %d)" % config.DEFAULT_PORT)
    ap.add_argument("--set-pin", action="store_true", help="set or reset the PIN, then exit")
    ap.add_argument("--open", action="store_true", help="open the page in your browser (localhost only)")
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
    try:
        config.check_location(root)
    except config.ConfigLocationError as e:
        print("ERROR: " + str(e), file=sys.stderr)
        return 2
    cfg = None
    try:
        cfg = config.load(root)
    except config.ConfigMissingError as e:
        print("WARNING: " + str(e), file=sys.stderr)
        print("The runner is paused until a settings file exists. Setting a PIN now creates a new one.", file=sys.stderr)
        cfg = config.default_config()
    except config.ConfigError as e:
        print("WARNING: " + str(e), file=sys.stderr)
        print("The admin page will still start so you can see the problem. "
              "The runner processes nothing until the settings file is fixed.", file=sys.stderr)
    config.ensure_private()
    for w in config.permission_report():
        print("WARNING: " + w, file=sys.stderr)
    if a.set_pin:
        if cfg is None:
            print("ERROR: the settings file is damaged, so the PIN was not changed. "
                  "Fix or delete that file, then run this again.", file=sys.stderr)
            return 2
        return set_pin_interactive(root)
    if cfg is not None and not config.pin_is_set(cfg):
        if not sys.stdin.isatty():
            print("No PIN is set yet. Run this once in a terminal:  python3 admin.py --root \"%s\" --set-pin" % root,
                  file=sys.stderr)
            return 2
        print("First run: you need a PIN before the admin page can start.")
        rc = set_pin_interactive(root)
        if rc:
            return rc
        cfg = config.load(root)
    port = a.port if a.port is not None else (cfg["admin_port"] if cfg else config.DEFAULT_PORT)
    if not 1 <= port <= 65535:
        print("ERROR: the port must be between 1 and 65535.", file=sys.stderr)
        return 2
    try:
        srv = make_server(root, port)
    except OSError:
        print("ERROR: port %d is already in use. Close the other admin window, or pick another with --port." % port,
              file=sys.stderr)
        return 1
    url = "http://127.0.0.1:%d/" % srv.app.port
    print("Admin page is running at  %s  (this computer only). Press Ctrl+C to stop." % url)
    if a.open:
        webbrowser.open(url)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("stopped")
    finally:
        srv.server_close()
    return 0


# =========================================================================== the page
PAGE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Office AI Admin</title>
<style>
:root{--bg:#f6f7f9;--card:#fff;--ink:#1b2430;--mute:#5d6b7c;--line:#dde2e8;--acc:#1f5fbf;--accink:#fff;--warn:#9a5b00;--warnbg:#fff3dc;--bad:#a12626;--badbg:#fde8e8;--ok:#1d6b43;--okbg:#e4f4ea}
@media (prefers-color-scheme:dark){:root{--bg:#14181e;--card:#1c222b;--ink:#e8ecf1;--mute:#9aa7b6;--line:#2f3844;--acc:#6aa3ff;--accink:#0c1624;--warn:#f0c070;--warnbg:#3a2d12;--bad:#ff9a9a;--badbg:#3b1d1d;--ok:#8fd9ae;--okbg:#17301f}}
*{box-sizing:border-box}
body{margin:0;font:15px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif;background:var(--bg);color:var(--ink)}
header{display:flex;flex-wrap:wrap;gap:12px;align-items:center;padding:14px 20px;background:var(--card);border-bottom:1px solid var(--line)}
header h1{font-size:17px;margin:0;font-weight:650;flex:1 1 auto}
.lock{font-size:12px;color:var(--ok);background:var(--okbg);padding:3px 9px;border-radius:99px}
main{max-width:1060px;margin:0 auto;padding:20px}
button{font:inherit;cursor:pointer;border-radius:8px;border:1px solid var(--line);background:var(--card);color:var(--ink);padding:7px 14px}
button.primary{background:var(--acc);color:var(--accink);border-color:var(--acc);font-weight:600}
button.danger{color:var(--bad)}
button:disabled{opacity:.5;cursor:not-allowed}
input,select,textarea{font:inherit;color:var(--ink);background:var(--card);border:1px solid var(--line);border-radius:8px;padding:8px 10px;width:100%}
textarea{min-height:140px;resize:vertical}
label{display:block;font-size:13px;color:var(--mute);margin:12px 0 4px}
nav{display:flex;gap:4px;border-bottom:1px solid var(--line);margin-bottom:16px;flex-wrap:wrap}
nav button{border:none;border-bottom:3px solid transparent;border-radius:0;background:none;padding:10px 14px;color:var(--mute)}
nav button[aria-selected=true]{color:var(--ink);border-bottom-color:var(--acc);font-weight:600}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:16px;margin-bottom:14px}
.card h2{font-size:16px;margin:0 0 4px}
.row{display:flex;gap:12px;flex-wrap:wrap}.row>*{flex:1 1 200px}
.meta{font-size:13px;color:var(--mute)}
.chip{display:inline-block;font-size:12px;padding:2px 8px;border-radius:99px;margin:0 6px 0 0}
.chip.warn{background:var(--warnbg);color:var(--warn)}.chip.bad{background:var(--badbg);color:var(--bad)}.chip.ok{background:var(--okbg);color:var(--ok)}
.empty{color:var(--mute);text-align:center;padding:24px 0}
.actions{display:flex;gap:8px;flex-wrap:wrap;margin-top:12px;align-items:center}
.msg{padding:10px 14px;border-radius:8px;margin:10px 0}.msg.bad{background:var(--badbg);color:var(--bad)}.msg.ok{background:var(--okbg);color:var(--ok)}.msg.warn{background:var(--warnbg);color:var(--warn)}
.hint{font-size:13px;color:var(--mute);margin-top:6px}
.hidden{display:none}
.tablewrap{overflow-x:auto}
table{border-collapse:collapse;width:100%;font-size:14px}
th,td{text-align:left;padding:8px 10px;border-bottom:1px solid var(--line);white-space:nowrap}
th{font-size:12px;color:var(--mute);font-weight:600}
td.num,th.num{text-align:right;font-variant-numeric:tabular-nums}
td:first-child{white-space:normal;min-width:180px}
tr.paused td{background:var(--warnbg);color:var(--mute)}
tr.paused td:first-child{border-left:4px solid var(--warn)}
.switch{position:relative;display:inline-block;width:46px;height:26px;margin:0}
.switch input{opacity:0;position:absolute;inset:0;width:100%;height:100%;margin:0;cursor:pointer;z-index:1}
.slider{position:absolute;inset:0;background:var(--line);border-radius:99px;transition:background .15s}
.slider::before{content:"";position:absolute;width:20px;height:20px;left:3px;top:3px;background:#fff;border-radius:50%;transition:transform .15s}
.switch input:checked+.slider{background:var(--ok)}
.switch input:checked+.slider::before{transform:translateX(20px)}
.switch input:focus-visible+.slider{outline:2px solid var(--acc);outline-offset:2px}
.grid2{display:grid;grid-template-columns:repeat(auto-fit,minmax(300px,1fr));gap:14px}
pre{white-space:pre-wrap;word-wrap:break-word;font:13px/1.45 ui-monospace,Consolas,monospace;background:var(--bg);border:1px solid var(--line);border-radius:8px;padding:10px;max-height:340px;overflow:auto;margin:8px 0 0}
.bar{display:inline-block;vertical-align:middle;width:90px;height:8px;background:var(--line);border-radius:99px;overflow:hidden;margin-left:8px}
.bar>span{display:block;height:100%;background:var(--acc)}
.big{font-size:22px;font-weight:650}
.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;margin-bottom:14px}
.stats .card{margin:0}
details summary{cursor:pointer;color:var(--mute)}
</style>
</head>
<body>
<header>
  <h1>Office AI Admin</h1>
  <span class="lock" title="This page only works on the computer that runs the AI runner">This computer only</span>
  <button id="lockbtn" class="hidden">Lock</button>
</header>
<main>
  <div id="flash"></div>
  <div id="login" class="card hidden" style="max-width:420px;margin:40px auto">
    <h2>Enter your PIN</h2>
    <p class="meta">The PIN was set in the terminal when the admin page was first started.</p>
    <label for="pin">PIN</label>
    <input id="pin" type="password" inputmode="numeric" autocomplete="off" spellcheck="false" maxlength="64">
    <div id="loginmsg"></div>
    <div class="actions"><button id="go" class="primary">Unlock</button></div>
  </div>
  <div id="app" class="hidden">
    <nav id="tabs" role="tablist"></nav>
    <div id="view"></div>
  </div>
</main>
<script>
(function(){
'use strict';
const $=id=>document.getElementById(id);
function el(tag,attrs,...kids){const e=document.createElement(tag);
  for(const[k,v]of Object.entries(attrs||{})){if(k==='class')e.className=v;else if(k.startsWith('on'))e.addEventListener(k.slice(2),v);else if(v!==false&&v!=null)e.setAttribute(k,v===true?'':v)}
  for(const c of kids.flat()){if(c==null)continue;e.append(c.nodeType?c:document.createTextNode(String(c)))}return e}
const msg=(kind,text)=>el('div',{class:'msg '+kind},text);
const S={csrf:null,tab:'conn',state:null,usage:null,days:30,testResults:null,testForm:{prompt:'',a:'',b:''},priceRows:null,safety:null};
const TABS=[['conn','Connection'],['test','Test models'],['seats','Seats'],['usage','Usage'],['changes','Changes'],['safety','Safety check']];

function flash(text,kind){const f=$('flash');f.replaceChildren(text?msg(kind||'ok',text):'');if(text)setTimeout(()=>{if(f.textContent===text)f.replaceChildren()},7000)}
async function api(path,body){
  const opt={method:body===undefined?'GET':'POST',headers:{},cache:'no-store'};
  if(body!==undefined){opt.headers['Content-Type']='application/json';opt.headers['X-CSRF-Token']=S.csrf||'';opt.body=JSON.stringify(body)}
  let r;try{r=await fetch(path,opt)}catch(_){throw new Error('Could not reach the admin page. Is it still running in the terminal window?')}
  let data={};try{data=await r.json()}catch(_){}
  if(r.status===401&&path!=='/api/login'&&path!=='/api/set_pin'){showLogin('Your session ended. Please enter your PIN again.')}
  if(!r.ok){const e=new Error(data.error||('Request failed ('+r.status+')'));e.data=data;e.status=r.status;throw e}
  return data}
const money=c=>c==null?'set prices to estimate cost':'$'+(c<1?c.toFixed(4):c.toFixed(2));
const costText=(c,status)=>status==='unset'||c==null?'set prices to estimate cost':(status==='partial'?money(c)+' (some usage has no price)':money(c));
const secs=ms=>ms==null?'-':(ms/1000).toFixed(1)+' s';
const pct=r=>r==null?'-':Math.round(r*100)+'%';
const when=ts=>{if(!ts)return 'never';const d=new Date(ts);return isNaN(d)?'never':d.toLocaleString()};
const num=n=>Number(n||0).toLocaleString();

// ---------- login ----------
function showLogin(note){S.csrf=null;$('app').classList.add('hidden');$('login').classList.remove('hidden');$('lockbtn').classList.add('hidden');
  $('loginmsg').replaceChildren(note?msg('bad',note):'');$('pin').value='';$('pin').focus()}
async function doLogin(){const pin=$('pin').value;if(!pin)return;
  try{const r=await api('/api/login',{pin});S.csrf=r.csrf;$('pin').value='';await enter()}
  catch(e){$('pin').value='';$('loginmsg').replaceChildren(msg('bad',e.message))}}
async function enter(){$('login').classList.add('hidden');$('app').classList.remove('hidden');$('lockbtn').classList.remove('hidden');
  S.tab='conn';await reload();}
async function reload(){try{S.state=await api('/api/state');S.priceRows=null;render()}catch(e){flash(e.message,'bad')}}
async function loadUsage(){try{S.usage=await api('/api/usage?days='+S.days);render()}catch(e){flash(e.message,'bad')}}

// ---------- shell ----------
function holdBits(){
  const h=S.state&&S.state.hold;if(!h)return [];
  const out=[];
  if(h.settings_note)out.push(msg('warn',h.settings_note));
  if(h.state==='locked')out.push(msg('bad',h.detail||'The runner is locked and is not processing requests.'));
  if(h.state==='paused_for_review')out.push(msg('warn','Configuration changed, owner review needed. Open Changes, review the list, and approve the current version when you are sure.'));
  return out}
function render(){
  const nav=$('tabs');nav.replaceChildren(...TABS.map(([id,label])=>el('button',{role:'tab','aria-selected':S.tab===id,onclick:()=>{S.tab=id;if(id==='seats'||id==='usage')loadUsage();if(id==='safety')loadSafety();render()}},label)));
  const v=$('view');if(!S.state){v.replaceChildren();return}
  const body=S.tab==='conn'?connView():S.tab==='test'?testView():S.tab==='seats'?seatsView():S.tab==='changes'?changesView():S.tab==='safety'?safetyView():usageView();
  v.replaceChildren(...holdBits(),body)}
async function loadSafety(){try{S.safety=await api('/api/safety');render()}catch(e){flash(e.message,'bad')}}

// ---------- 1. Connection ----------
function connView(){
  const st=S.state,k=st.key;
  const prov=el('select',{id:'c-prov'},el('option',{value:'anthropic'},'Anthropic (real AI)'),el('option',{value:'stub'},'Stub (no AI - free, for trying things out)'));prov.value=st.provider;
  const keyIn=el('input',{id:'c-key',type:'password',autocomplete:'off',spellcheck:'false',autocapitalize:'off',placeholder:'Paste a new key here to replace the current one'});
  const model=el('input',{id:'c-model',list:'recent',autocomplete:'off',placeholder:st.effective_default_model});model.value=st.default_model||'';
  const out=el('div',{id:'c-out'});
  let keyLine;
  if(!k.present)keyLine=msg('warn','No API key is saved yet.');
  else if(k.source==='env')keyLine=msg('ok','Using the key from the ANTHROPIC_API_KEY environment variable: '+k.masked+'. This overrides any key saved on this page.'+(k.env_overrides_file?' (The key saved here, '+k.file_masked+', is not being used.)':''));
  else keyLine=msg('ok','Using the key saved on this page: '+k.masked);
  const save=el('button',{class:'primary',onclick:async()=>{
    try{const body={provider:prov.value,default_model:model.value.trim()};if(keyIn.value.trim())body.api_key=keyIn.value;
      S.state=await api('/api/connection',body);keyIn.value='';render();flash('Saved. The runner picks up changes on its next check.')}
    catch(e){out.replaceChildren(msg('bad',e.message))}}},'Save');
  const test=el('button',{onclick:async()=>{
    if(keyIn.value.trim()||prov.value!==st.provider||model.value.trim()!==(st.default_model||'')){out.replaceChildren(msg('warn','Press Save first, then test.'));return}
    test.disabled=true;out.replaceChildren(msg('warn','Testing...'));
    try{const r=await api('/api/test_connection',{});out.replaceChildren(msg(r.ok?'ok':'bad',r.message))}
    catch(e){out.replaceChildren(msg('bad',e.message))}finally{test.disabled=false}}},'Test connection');
  const cf=st.config;
  const where=el('div',{class:'card'},el('h2',{},'Where the key is stored'),
    el('p',{class:'meta'},'On this computer only: '+cf.path+'. It is never placed in the shared workspace folder.'),
    cf.locked_down?el('span',{class:'chip ok'},'File permissions locked down to your account'):null,
    ...cf.warnings.map(w=>msg('warn',w)),...st.workspace_warnings.map(w=>msg('bad',w)));
  const pc=el('input',{type:'password',autocomplete:'off',inputmode:'numeric',id:'p-cur'}),pn=el('input',{type:'password',autocomplete:'off',inputmode:'numeric',id:'p-new'}),pn2=el('input',{type:'password',autocomplete:'off',inputmode:'numeric',id:'p-new2'});
  const pout=el('div');
  const pinCard=el('div',{class:'card'},el('details',{},el('summary',{},'Change PIN'),
    el('label',{for:'p-cur'},'Current PIN'),pc,el('label',{for:'p-new'},'New PIN (at least 6 digits)'),pn,el('label',{for:'p-new2'},'New PIN again'),pn2,pout,
    el('div',{class:'actions'},el('button',{onclick:async()=>{
      if(pn.value!==pn2.value){pout.replaceChildren(msg('bad','The two new PINs do not match.'));return}
      try{await api('/api/set_pin',{current_pin:pc.value,new_pin:pn.value});pc.value=pn.value=pn2.value='';pout.replaceChildren(msg('ok','PIN changed.'))}
      catch(e){pout.replaceChildren(msg('bad',e.message))}}},'Change PIN'))));
  return el('div',{},
    el('div',{class:'card'},el('h2',{},'AI connection'),
      el('p',{class:'meta'},'Using a real provider sends request text to an outside company. Use it only for non-patient work, or under a signed BAA.'),
      el('label',{for:'c-prov'},'Provider'),prov,
      el('label',{},'API key'),keyLine,
      el('label',{for:'c-key'},'Replace key (leave blank to keep the current one)'),keyIn,
      el('label',{for:'c-model'},'Default model (used by every seat that has no model of its own)'),model,
      el('datalist',{id:'recent'},st.recent_models.map(m=>el('option',{value:m}))),
      el('div',{class:'hint'},'If blank, the runner uses the AI_MODEL environment variable, else '+st.effective_default_model+'.'),
      el('div',{class:'actions'},save,test),out),
    where,pinCard)}

// ---------- 2. Test models ----------
function testView(){
  const st=S.state,F=S.testForm;
  const prompt=el('textarea',{id:'t-prompt',maxlength:'4000',placeholder:'Type a made-up example, for instance: Write a friendly two-sentence reminder that the office is closed on Monday.'});prompt.value=F.prompt;
  const a=el('input',{id:'t-a',list:'recent2',autocomplete:'off'}),b=el('input',{id:'t-b',list:'recent2',autocomplete:'off',placeholder:'(optional) a second model to compare'});
  a.value=F.a||st.effective_default_model;b.value=F.b;
  const out=el('div',{id:'t-out'});
  if(S.testResults)out.append(...resultCards(S.testResults));
  const run=el('button',{class:'primary',onclick:async()=>{
    F.prompt=prompt.value;F.a=a.value.trim();F.b=b.value.trim();
    const models=[F.a,F.b].filter(Boolean);
    run.disabled=true;out.replaceChildren(msg('warn','Checking the text for personal information, then asking the model...'));
    try{const r=await api('/api/test_models',{prompt:prompt.value,models});S.testResults=r.results;
      out.replaceChildren(...resultCards(r.results));const s=await api('/api/state');S.state=s}
    catch(e){S.testResults=null;out.replaceChildren(msg('bad',e.message))}finally{run.disabled=false}}},'Run');
  return el('div',{},
    el('div',{class:'card'},el('h2',{},'Try a model'),
      msg('warn','Use fake, non-patient text only. A privacy check runs first and blocks anything that looks like personal information; nothing is sent until the text passes.'),
      el('label',{for:'t-prompt'},'Test text'),prompt,
      el('div',{class:'row'},el('div',{},el('label',{for:'t-a'},'Model A'),a),el('div',{},el('label',{for:'t-b'},'Model B (optional, to compare)'),b)),
      el('datalist',{id:'recent2'},st.recent_models.map(m=>el('option',{value:m}))),
      st.provider==='stub'?el('div',{class:'hint'},'The provider is set to Stub, so no real model is called. Change it on the Connection tab.'):null,
      el('div',{class:'actions'},run)),out)}
function resultCards(results){return [el('div',{class:'grid2'},results.map(r=>el('div',{class:'card'},el('h2',{},r.model),
  r.ok?el('div',{class:'meta'},'Time '+secs(r.latency_ms)+' · '+num(r.input_tokens)+' tokens in · '+num(r.output_tokens)+' tokens out · '+(r.estimated_cost==null?'set prices to estimate cost':'about '+money(r.estimated_cost))):null,
  r.ok?el('pre',{},r.text):msg('bad',r.error))))]}

// ---------- 3. Seats ----------
function seatsView(){
  const st=S.state,u={};if(S.usage)for(const s of S.usage.seats)u[s.rid]=s;
  const rows=st.seats.map(s=>{
    const us=u[s.rid];
    const cb=el('input',{type:'checkbox',role:'switch','aria-label':s.rid+' '+s.title+' on or off',onchange:async()=>{
      try{S.state=await api('/api/seat',{rid:s.rid,enabled:cb.checked});if(S.usage)S.usage.seats.forEach(x=>{if(x.rid===s.rid)x.enabled=cb.checked});render();
        flash(cb.checked?s.rid+' is on again. Waiting requests will now be processed.':s.rid+' is paused. New requests wait in its inbox; nothing is lost.')}
      catch(e){cb.checked=!cb.checked;flash(e.message,'bad')}}});
    cb.checked=s.enabled;
    const mi=el('input',{list:'recent3','aria-label':'Model for '+s.rid,placeholder:'default: '+st.effective_default_model,autocomplete:'off',style:'min-width:200px',onchange:async()=>{
      try{S.state=await api('/api/seat',{rid:s.rid,model:mi.value.trim()});render();flash('Model for '+s.rid+' '+(mi.value.trim()?'set to '+mi.value.trim():'cleared (uses the default)')+'.')}
      catch(e){flash(e.message,'bad');render()}}});
    mi.value=s.model||'';
    return el('tr',{class:s.enabled?'':'paused'},
      el('td',{},el('strong',{},s.rid),' ',s.title,' ',s.enabled?null:el('span',{class:'chip warn'},'PAUSED')),
      el('td',{},el('label',{class:'switch'},cb,el('span',{class:'slider'}))),
      el('td',{},mi),
      el('td',{},us?when(us.last_used):'...'),
      el('td',{class:'num'},us?us.requests_this_week:'...'),
      el('td',{class:'num'},us?pct(us.approval_rate):'...'))});
  return el('div',{},el('div',{class:'card'},el('h2',{},'AI seats'),
    el('p',{class:'meta'},'Turn a seat off to pause it. Its requests stay waiting in the inbox (nothing is deleted) and staff see that the seat is paused. A model typed here applies to that seat only; leave it blank to use the default. Changes apply within a few seconds.'),
    el('div',{class:'tablewrap'},el('table',{},el('thead',{},el('tr',{},el('th',{},'Seat'),el('th',{},'On / off'),el('th',{},'Model (optional)'),el('th',{},'Last used'),el('th',{class:'num'},'Requests this week'),el('th',{class:'num'},'Approval rate'))),el('tbody',{},rows))),
    el('datalist',{id:'recent3'},st.recent_models.map(m=>el('option',{value:m})))))}

// ---------- 4. Usage ----------
function usageView(){
  const st=S.state,sel=el('select',{id:'u-days',style:'width:auto',onchange:()=>{S.days=parseInt(sel.value,10);S.usage=null;loadUsage();render()}},[7,30,90].map(d=>el('option',{value:d},'Last '+d+' days')));sel.value=String(S.days);
  const head=el('div',{class:'row',style:'align-items:end;margin-bottom:12px'},el('div',{style:'flex:0 0 200px'},el('label',{for:'u-days',style:'margin-top:0'},'Period'),sel));
  const u=S.usage;
  if(!u)return el('div',{},head,el('div',{class:'empty'},'Loading...'),pricesCard());
  const t=u.totals,maxReq=Math.max(1,...u.weeks.map(w=>w.requests));
  const stat=(label,value,sub)=>el('div',{class:'card'},el('div',{class:'meta'},label),el('div',{class:'big'},value),sub?el('div',{class:'meta'},sub):null);
  const spend=t.cost_status==='unset'?el('div',{class:'meta',style:'margin-top:6px'},'set prices to estimate cost'):el('div',{class:'big'},money(t.estimated_cost));
  const stats=el('div',{class:'stats'},
    el('div',{class:'card'},el('div',{class:'meta'},'Estimated spend'),spend,t.cost_status==='partial'?el('div',{class:'meta'},'Some usage has no price yet'):null),
    stat('Requests',num(t.requests),num(t.drafts)+' drafts'),stat('Model errors',num(t.model_errors),num(t.tripwire_blocks)+' privacy blocks'),
    stat('Approval rate',pct(t.approval_rate),t.approved+' approved · '+t.revised+' revised · '+t.discarded+' discarded'));
  const th=(l,n)=>el('th',{class:n?'num':''},l),td=(v,n)=>el('td',{class:n?'num':''},v);
  const seatTable=el('div',{class:'card'},el('h2',{},'By seat'),el('div',{class:'tablewrap'},el('table',{},
    el('thead',{},el('tr',{},th('Seat'),th('Requests',1),th('Drafts',1),th('Privacy blocks',1),th('Model errors',1),th('Tokens in',1),th('Tokens out',1),th('Est. cost',1),th('Avg time to draft',1),th('Approved',1),th('Revised',1),th('Discarded',1),th('Approval rate',1))),
    el('tbody',{},u.seats.map(s=>el('tr',{class:s.enabled?'':'paused'},td(s.rid+' '+s.title),td(s.requests,1),td(s.drafts,1),td(s.tripwire_blocks,1),td(s.model_errors,1),td(num(s.tokens_in),1),td(num(s.tokens_out),1),td(s.cost_status==='unset'?'set prices':money(s.estimated_cost),1),td(secs(s.avg_time_to_draft_ms),1),td(s.approved,1),td(s.revised,1),td(s.discarded,1),td(pct(s.approval_rate),1)))))),
    el('div',{class:'hint'},'A revision counts as a new request. "Avg time to draft" is how long the model took to answer.'));
  const weekTable=el('div',{class:'card'},el('h2',{},'By week'),u.weeks.length?el('div',{class:'tablewrap'},el('table',{},
    el('thead',{},el('tr',{},th('Week starting'),th('Seat'),th('Requests'),th('Drafts',1),th('Privacy blocks',1),th('Model errors',1),th('Tokens in/out',1),th('Est. cost',1))),
    el('tbody',{},u.weeks.map(w=>el('tr',{},td(w.week),td(w.rid),el('td',{},String(w.requests),el('span',{class:'bar'},el('span',{style:'width:'+Math.round(100*w.requests/maxReq)+'%'}))),td(w.drafts,1),td(w.tripwire_blocks,1),td(w.model_errors,1),td(num(w.tokens_in)+' / '+num(w.tokens_out),1),td(w.cost_status==='unset'?'set prices':money(w.estimated_cost),1)))))):el('div',{class:'empty'},'No activity in this period.'));
  const unused=el('div',{class:'card'},el('h2',{},'Not used recently'),u.not_used_recently.length?el('ul',{},u.seats.filter(s=>s.unused).map(s=>el('li',{},s.rid+' '+s.title+' - last used: '+when(s.last_used)))):el('div',{class:'meta'},'Every seat was used in this period.'));
  return el('div',{},head,stats,seatTable,weekTable,unused,pricesCard())}

function pricesCard(){
  const st=S.state;
  if(!S.priceRows)S.priceRows=Object.entries(st.prices).map(([m,p])=>({model:m,i:p.input_per_mtok,o:p.output_per_mtok}));
  const out=el('div');
  const list=el('div');
  const draw=()=>{list.replaceChildren(...S.priceRows.map((r,idx)=>{
    const m=el('input',{list:'recent4','aria-label':'Model name',value:r.model,autocomplete:'off',oninput:()=>{r.model=m.value}});
    const i=el('input',{type:'number',min:'0',step:'any','aria-label':'Dollars per million input tokens',value:r.i==null?'':r.i,oninput:()=>{r.i=i.value}});
    const o=el('input',{type:'number',min:'0',step:'any','aria-label':'Dollars per million output tokens',value:r.o==null?'':r.o,oninput:()=>{r.o=o.value}});
    return el('div',{class:'row',style:'align-items:end'},el('div',{},idx===0?el('label',{},'Model'):null,m),el('div',{},idx===0?el('label',{},'$ per million input tokens'):null,i),el('div',{},idx===0?el('label',{},'$ per million output tokens'):null,o),
      el('div',{style:'flex:0 0 90px'},el('button',{class:'danger',onclick:()=>{S.priceRows.splice(idx,1);draw()}},'Remove')))}))};
  draw();
  return el('div',{class:'card'},el('h2',{},'Model prices (for cost estimates)'),
    el('p',{class:'meta'},'No prices are pre-filled because they change. Confirm the current price per million tokens on your AI provider\'s pricing page, then type them here. Costs on this page are estimates, not your bill.'),
    list,el('datalist',{id:'recent4'},[...new Set([st.effective_default_model,...st.recent_models])].map(m=>el('option',{value:m}))),out,
    el('div',{class:'actions'},el('button',{onclick:()=>{S.priceRows.push({model:S.priceRows.length?'':st.effective_default_model,i:'',o:''});draw()}},'Add a model'),
      el('button',{class:'primary',onclick:async()=>{
        const prices={};for(const r of S.priceRows){if(!String(r.model).trim())continue;prices[String(r.model).trim()]={input_per_mtok:r.i===''?null:r.i,output_per_mtok:r.o===''?null:r.o}}
        try{S.state=await api('/api/prices',{prices});S.priceRows=null;if(S.tab==='usage')await loadUsage();else render();flash('Prices saved.')}
        catch(e){out.replaceChildren(msg('bad',e.message))}}},'Save prices')))}

// ---------- 5. Changes ----------
function changesView(){
  const h=(S.state&&S.state.hold)||{}, rows=h.changes||[];
  const path=el('input',{id:'dash-path',spellcheck:'false',autocomplete:'off',placeholder:'Full path to dashboard.html (optional)'});
  path.value=S.state.dashboard_path||'';
  const pout=el('div');
  const when=ts=>ts||'-';
  const hash=v=>v||'-';
  const table=rows.length?el('div',{class:'tablewrap'},el('table',{},
    el('thead',{},el('tr',{},el('th',{},'File'),el('th',{},'Change'),el('th',{class:'num'},'Size'),el('th',{},'Modified'),el('th',{},'Previous hash'),el('th',{},'Current hash'))),
    el('tbody',{},rows.map(r=>el('tr',{},el('td',{},r.name),el('td',{},r.change),el('td',{class:'num'},r.size==null?'-':String(r.size)),el('td',{},when(r.modified)),el('td',{},hash(r.old_hash)),el('td',{},hash(r.new_hash))))))):
    el('p',{class:'meta'},h.baseline_present?'No unapproved changes.':'No baseline yet. The runner saves one on its next check, or you can approve the current files now.');
  const approve=el('button',{class:'primary',onclick:async()=>{
    if(!confirm('Approve the current version of these files? Seats paused for this review will resume on the next check.'))return;
    try{S.state=await api('/api/manifest/approve',{});S.safety=null;flash('Approved. The runner resumes paused seats on its next check.');render()}
    catch(e){flash(e.message,'bad')}}},'Approve current version');
  return el('div',{},el('div',{class:'card'},el('h2',{},'Changes'),
    el('p',{class:'meta'},'The runner compares agent instructions, SOP files, and the dashboard file (if you set its path) with a saved baseline. A change pauses the affected seat until you approve it here. Shared files, including decision rights, pause every seat.'),
    h.manifest_error?msg('bad',h.manifest_error):null,
    table,
    el('div',{class:'actions'},approve)),
    el('div',{class:'card'},el('h2',{},'Dashboard file'),
      el('p',{class:'meta'},'Optional. Point this at dashboard.html so a change to that file is noticed too. The file itself stays outside the shared workspace.'),
      el('label',{for:'dash-path'},'Path'),path,pout,
      el('div',{class:'actions'},el('button',{onclick:async()=>{
        try{S.state=await api('/api/dashboard_path',{dashboard_path:path.value.trim()});pout.replaceChildren();flash('Dashboard path saved.')}
        catch(e){pout.replaceChildren(msg('bad',e.message))}}},'Save path'))))}

// ---------- 6. Safety check ----------
function safetyView(){
  if(!S.safety)return el('div',{class:'empty'},'Loading...');
  const chip=s=>s==='pass'?'ok':s==='fail'?'bad':'warn';
  return el('div',{class:'card'},el('h2',{},'Safety check'),
    el('p',{class:'meta'},'Hard checks must pass before real use. Advisory checks are a best effort. Could not verify means this computer did not return enough information. Nothing on this tab changes permissions.'),
    ...S.safety.items.map(it=>el('div',{style:'margin:10px 0'},el('span',{class:'chip '+chip(it.status)},it.status),' ',it.text,el('div',{class:'meta'},it.level))))}

// ---------- start ----------
$('go').addEventListener('click',doLogin);
$('pin').addEventListener('keydown',e=>{if(e.key==='Enter')doLogin()});
$('lockbtn').addEventListener('click',async()=>{try{await api('/api/logout',{})}catch(_){}S.state=null;S.usage=null;S.testResults=null;showLogin('')});
(async function boot(){
  try{const s=await api('/api/session');
    if(s.authenticated){S.csrf=s.csrf;await enter()}
    else{showLogin(s.pin_set?'':'No PIN has been set yet. Close this page, run the admin launcher or  admin.py --set-pin  in a terminal, then reload.')}}
  catch(e){showLogin(e.message)}})();
})();
</script>
</body>
</html>
"""

if __name__ == "__main__":
    sys.exit(main())
