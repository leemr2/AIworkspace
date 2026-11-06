"""Shared settings and secrets for runner.py and admin.py (standard library only).

Everything secret (API key, PIN hash) lives in ~/.office-ai/config.json on the
runner computer -- NEVER inside the shared workspace folder.

Rules this module enforces:
  * the settings folder is created 0700 and the file 0600 (POSIX); on Windows the
    file is restricted to the current user with icacls (failure is reported, not hidden);
  * a settings folder that sits inside the workspace root is refused;
  * writes are atomic (temp file + os.replace);
  * the API key is only ever shown masked (last 4 characters) and can be scrubbed
    out of any text with redact().
"""
import hashlib
import hmac
import json
import math
import os
import re
import secrets
import subprocess
import sys
from pathlib import Path

DEFAULT_MODEL = "claude-sonnet-5-5"
DEFAULT_PORT = 8787
DEFAULT_SESSION_MINUTES = 15
PROVIDERS = ("anthropic", "stub")
PIN_MIN_DIGITS = 6
PIN_MAX_DIGITS = 64
PIN_ITERATIONS = 310_000
PIN_MIN_ITERATIONS = 200_000
MODEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@-]{0,99}$")
ROLE_RE = re.compile(r"^R\d{3}$")
PIN_RE = re.compile(r"^[0-9]{%d,%d}$" % (PIN_MIN_DIGITS, PIN_MAX_DIGITS))

_dir_override = None            # tests only: see set_config_dir()
_perm_state = {"windows_ok": None, "windows_note": None}


class ConfigError(Exception):
    """Settings could not be read or written. Messages never contain secrets."""


class ConfigLocationError(ConfigError):
    """The settings folder is inside the shared workspace."""


class ConfigMissingError(ConfigError):
    """The settings folder exists, but config.json does not."""


class LayoutError(ConfigError):
    """The program folder and the shared workspace overlap."""


LAST_GOOD_NOTE = "using last good settings"

# Tests only: pretend the program lives somewhere else. None means "this file's folder".
_app_dir_override = None


# --------------------------------------------------------------------------- location
def set_config_dir(path):
    """Tests only: use another settings folder. Pass None to go back to ~/.office-ai."""
    global _dir_override
    _dir_override = Path(path) if path else None


def set_app_dir(path):
    """Tests only: treat another folder as the program folder. Pass None to reset."""
    global _app_dir_override
    _app_dir_override = str(Path(path).resolve()) if path else None


def app_dir():
    """Folder that holds runner.py, admin.py, and config.py (symlinks resolved)."""
    if _app_dir_override:
        return _app_dir_override
    return str(Path(__file__).resolve().parent)


def same_or_nested(a, b):
    """True if the two resolved paths are the same or one is inside the other."""
    a = os.path.normcase(os.path.abspath(str(a)))
    b = os.path.normcase(os.path.abspath(str(b)))
    try:
        common = os.path.normcase(os.path.commonpath([a, b]))
    except ValueError:
        return False
    return common == a or common == b


def assert_separated(root):
    """Refuse to run when the program folder and the workspace overlap.

    Both paths are resolved (symlinks followed) before the comparison.
    """
    app = str(Path(app_dir()).resolve())
    ws = str(Path(root).resolve())
    if same_or_nested(app, ws):
        raise LayoutError(
            "The program folder and the shared workspace are the same folder, or one is inside the other, "
            "so this program will not start. Keep runner.py, admin.py, config.py, and the launchers in a "
            "folder that staff cannot change, and keep that folder outside the shared workspace. The shared "
            "workspace must not be inside the program folder either. "
            "Program folder: %s. Shared workspace: %s." % (app, ws))


def config_dir():
    return _dir_override if _dir_override else Path.home() / ".office-ai"


def config_path():
    return config_dir() / "config.json"


def check_location(root):
    """Raise ConfigLocationError if the settings folder is the workspace or inside it."""
    if root is None:
        return
    d = config_dir().resolve()
    r = Path(root).resolve()
    if d == r or r in d.parents:
        raise ConfigLocationError(
            "The settings folder (%s) is inside the shared workspace (%s). Anyone who can open the "
            "workspace could read the API key, so it will not be used. Choose a workspace folder that "
            "does not contain your user folder (for example a separate folder on a network drive)." % (d, r))


# --------------------------------------------------------------------------- defaults / normalising
def default_config():
    return {
        "provider": "stub",
        "api_key": "",
        "default_model": None,
        "per_seat": {},
        "prices": {},
        "pin": None,
        "admin_port": DEFAULT_PORT,
        "session_timeout_minutes": DEFAULT_SESSION_MINUTES,
        "recent_models": [],
        "dashboard_path": None,
    }


def valid_model(name):
    return isinstance(name, str) and bool(MODEL_RE.match(name))


def _price(x):
    if isinstance(x, bool):
        return None
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) and 0 <= v <= 1_000_000 else None


def normalize(raw):
    """Merge a loaded dict with defaults and coerce every field to a safe type."""
    cfg = default_config()
    if not isinstance(raw, dict):
        return cfg
    if raw.get("provider") in PROVIDERS:
        cfg["provider"] = raw["provider"]
    if isinstance(raw.get("api_key"), str):
        cfg["api_key"] = raw["api_key"].strip()
    if valid_model(raw.get("default_model")):
        cfg["default_model"] = raw["default_model"]
    ps = raw.get("per_seat")
    if isinstance(ps, dict):
        for rid, v in ps.items():
            if ROLE_RE.match(str(rid)) and isinstance(v, dict):
                cfg["per_seat"][rid] = {
                    "enabled": v.get("enabled") is not False,
                    "model": v["model"] if valid_model(v.get("model")) else None,
                }
    pr = raw.get("prices")
    if isinstance(pr, dict):
        for model, v in pr.items():
            if valid_model(model) and isinstance(v, dict):
                i, o = _price(v.get("input_per_mtok")), _price(v.get("output_per_mtok"))
                if i is not None and o is not None:
                    cfg["prices"][model] = {"input_per_mtok": i, "output_per_mtok": o}
    pin = raw.get("pin")
    if isinstance(pin, dict) and all(isinstance(pin.get(k), (str, int)) for k in ("salt", "hash", "iterations")):
        cfg["pin"] = {"salt": str(pin["salt"]), "hash": str(pin["hash"]), "iterations": int(pin["iterations"])}
    port = raw.get("admin_port")
    if isinstance(port, int) and not isinstance(port, bool) and 1024 <= port <= 65535:
        cfg["admin_port"] = port
    mins = raw.get("session_timeout_minutes")
    if isinstance(mins, (int, float)) and not isinstance(mins, bool) and 1 <= mins <= 480:
        cfg["session_timeout_minutes"] = mins
    rm = raw.get("recent_models")
    if isinstance(rm, list):
        cfg["recent_models"] = [m for m in rm if valid_model(m)][:10]
    dp = raw.get("dashboard_path")
    if isinstance(dp, str):
        dp = dp.strip()
        if dp and len(dp) <= 500 and "\n" not in dp and "\r" not in dp:
            cfg["dashboard_path"] = dp
    return cfg


# --------------------------------------------------------------------------- load / save
def load(root=None):
    """Read config.json (defaults if it does not exist yet).

    If `root` is given and the settings folder is inside it, raises ConfigLocationError.
    """
    check_location(root)
    p = config_path()
    if not p.exists():
        if config_dir().exists():
            raise ConfigMissingError(
                "The settings folder exists but config.json is missing. The runner will not process any "
                "tasks until that file is restored. Waiting requests stay in the inbox.")
        return default_config()
    try:
        with open(p, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except (OSError, ValueError):
        raise ConfigError("The settings file (%s) could not be read. It may be damaged; "
                          "restore a copy or delete it and set things up again." % p) from None
    if not isinstance(raw, dict):
        raise ConfigError("The settings file (%s) is not valid. It should be a JSON object with the saved settings." % p)
    return normalize(raw)


def save(cfg, root=None):
    """Write config.json atomically with private permissions."""
    check_location(root)
    cfg = normalize(cfg)
    d, p = config_dir(), config_path()
    _ensure_dir(d)
    data = (json.dumps(cfg, indent=1) + "\n").encode("utf-8")
    tmp = p.with_name(p.name + ".tmp-%d" % os.getpid())
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
        raise ConfigError("The settings file could not be saved (%s). Check that the folder is writable." % p) from None
    ensure_private()


def _ensure_dir(d):
    created = not d.exists()
    try:
        os.makedirs(str(d), mode=0o700, exist_ok=True)
        if os.name != "nt":
            os.chmod(str(d), 0o700)
    except OSError:
        raise ConfigError("The settings folder (%s) could not be created." % d) from None
    if created and os.name == "nt":
        _restrict_windows(d, is_dir=True)


# --------------------------------------------------------------------------- permissions
def _windows_user():
    name = os.environ.get("USERNAME")
    if not name:
        return None
    dom = os.environ.get("USERDOMAIN")
    return "%s\\%s" % (dom, name) if dom else name


def _restrict_windows(path, is_dir=False):
    """Remove inherited permissions and grant only the current user. Returns True on success."""
    user = _windows_user()
    if not user:
        _perm_state.update(windows_ok=False, windows_note="could not work out the current Windows user name")
        return False
    grant = "%s:(OI)(CI)F" % user if is_dir else "%s:F" % user
    # Python 3.13's mkdir(mode=0o700) already adds SYSTEM / Administrators / OWNER RIGHTS entries;
    # drop them too (by well-known SID, so this works on any Windows language).
    try:
        r = subprocess.run(["icacls", str(path), "/inheritance:r", "/grant:r", grant,
                            "/remove:g", "*S-1-5-18", "*S-1-5-32-544", "*S-1-3-4"],
                           capture_output=True, timeout=20,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        ok = r.returncode == 0
        note = None if ok else "icacls returned an error"
    except (OSError, subprocess.SubprocessError):
        ok, note = False, "icacls could not be run"
    _perm_state.update(windows_ok=ok, windows_note=note)
    return ok


def lock_down_file(path):
    """Best-effort: make one settings file readable only by the current user."""
    if not os.path.exists(path):
        return
    if os.name == "nt":
        _restrict_windows(path)
    else:
        try:
            os.chmod(str(path), 0o600)
        except OSError:
            pass


def ensure_private():
    """(Re)apply private permissions to the settings folder and file. Safe to call often."""
    d, p = config_dir(), config_path()
    if not d.exists():
        return
    manifest = d / "manifest.json"
    if os.name == "nt":
        ok = True
        if p.exists():
            ok = _restrict_windows(p)
        if ok and manifest.exists():
            ok = _restrict_windows(manifest)
        if ok:
            _restrict_windows(d, is_dir=True)
    else:
        try:
            os.chmod(str(d), 0o700)
            if p.exists():
                os.chmod(str(p), 0o600)
            if manifest.exists():
                os.chmod(str(manifest), 0o600)
        except OSError:
            pass


def evaluate_settings(root):
    """Classify settings for one check. Does not use an in-memory last-good copy.

    Returns state (first_run, ready, or locked), cfg, a detail string for the owner,
    recoverable (True only when a damaged config.json may fall back to last-good), and code.
    A settings folder inside the workspace is locked and the file is not read.
    """
    if not config_dir().exists():
        return {"state": "first_run", "cfg": default_config(), "detail": None,
                "recoverable": False, "code": "first_run"}
    try:
        check_location(root)
    except ConfigLocationError as e:
        return {"state": "locked", "cfg": None, "detail": str(e), "recoverable": False, "code": "location"}
    if not config_path().exists():
        return {"state": "locked", "cfg": None, "recoverable": False, "code": "missing",
                "detail": "The settings folder exists but config.json is missing. The runner will not process "
                          "any tasks until that file is restored. Waiting requests stay in the inbox."}
    try:
        cfg = load(root)
    except ConfigError as e:
        return {"state": "locked", "cfg": None, "detail": str(e), "recoverable": True, "code": "invalid"}
    return {"state": "ready", "cfg": cfg, "detail": None, "recoverable": False, "code": "ok"}


def permission_report():
    """Return a list of plain-language warnings (empty list = locked down)."""
    d, p = config_dir(), config_path()
    warnings = []
    if os.name == "nt":
        if _perm_state["windows_ok"] is False:
            warnings.append(
                "Could not restrict the settings file to your Windows account (%s). Other people who use this "
                "computer might be able to read the saved key. Use the ANTHROPIC_API_KEY environment variable "
                "instead, or ask your IT person to restrict %s." % (_perm_state["windows_note"], d))
        return warnings
    for path, limit in ((d, 0o700), (p, 0o600)):
        try:
            mode = os.stat(str(path)).st_mode & 0o777
        except OSError:
            continue
        if mode & 0o077:
            warnings.append("%s can be read by other users on this computer (permissions %s, should be %s). "
                            "Run: chmod %s \"%s\"" % (path, oct(mode)[2:], oct(limit)[2:], oct(limit)[2:], path))
    return warnings


# --------------------------------------------------------------------------- keys
def get_api_key(cfg):
    """Return (key, source). Environment variable beats the saved key. source: env|file|None."""
    env = (os.environ.get("ANTHROPIC_API_KEY") or "").strip()
    if env:
        return env, "env"
    if cfg.get("api_key"):
        return cfg["api_key"], "file"
    return "", None


def mask_key(key):
    """'sk-...abcd' -- only the last 4 characters are ever revealed."""
    if not key:
        return ""
    if len(key) < 12:
        return "..."
    return ("sk-" if key.startswith("sk-") else "") + "..." + key[-4:]


def valid_key_format(key):
    return isinstance(key, str) and 8 <= len(key) <= 300 and re.fullmatch(r"[\x21-\x7e]+", key) is not None


def redact(text, *secrets_):
    """Remove any secret value (and the env key) from text before it is shown or logged."""
    text = str(text)
    env = os.environ.get("ANTHROPIC_API_KEY") or ""
    for s in list(secrets_) + [env]:
        if s and len(s) >= 4:
            text = text.replace(s, "[hidden]")
    return text


# --------------------------------------------------------------------------- PIN
def valid_pin_format(pin):
    return isinstance(pin, str) and PIN_RE.match(pin) is not None


def make_pin_record(pin):
    salt = secrets.token_bytes(16)
    h = hashlib.pbkdf2_hmac("sha256", pin.encode("utf-8"), salt, PIN_ITERATIONS)
    return {"salt": salt.hex(), "hash": h.hex(), "iterations": PIN_ITERATIONS}


def pin_is_set(cfg):
    p = cfg.get("pin")
    return bool(p) and p.get("iterations", 0) >= PIN_MIN_ITERATIONS


def verify_pin(cfg, pin):
    if not pin_is_set(cfg) or not isinstance(pin, str) or len(pin) > 200:
        return False
    p = cfg["pin"]
    try:
        h = hashlib.pbkdf2_hmac("sha256", pin.encode("utf-8"), bytes.fromhex(p["salt"]), int(p["iterations"]))
        return hmac.compare_digest(h.hex(), p["hash"])
    except ValueError:
        return False


# --------------------------------------------------------------------------- per-seat / model / price helpers
def seat_settings(cfg, rid):
    s = (cfg.get("per_seat") or {}).get(rid) or {}
    return {"enabled": s.get("enabled") is not False, "model": s.get("model") or None}


def resolve_model(cfg, rid=None, environ=None):
    """Seat override, else default_model, else AI_MODEL env var, else built-in default."""
    env = environ if environ is not None else os.environ
    seat = seat_settings(cfg, rid)["model"] if rid else None
    return seat or cfg.get("default_model") or (env.get("AI_MODEL") or "").strip() or DEFAULT_MODEL


def estimate_cost(prices, model, input_tokens, output_tokens):
    """Dollars, or None when no price is configured for this model (we never guess)."""
    p = (prices or {}).get(model)
    if not p:
        return None
    i, o = _price(p.get("input_per_mtok")), _price(p.get("output_per_mtok"))
    if i is None or o is None:
        return None
    return round((int(input_tokens or 0) * i + int(output_tokens or 0) * o) / 1_000_000, 6)


def fingerprint(cfg):
    """Changes whenever a setting that could fix a failing model call changes."""
    blob = json.dumps([cfg.get("provider"), cfg.get("default_model"), cfg.get("per_seat"),
                       get_api_key(cfg)[0]], sort_keys=True)
    return hashlib.sha256(blob.encode()).hexdigest()


def remember_model(cfg, model):
    if valid_model(model):
        cfg["recent_models"] = ([model] + [m for m in cfg.get("recent_models", []) if m != model])[:10]


if __name__ == "__main__":
    print("config.py is a library used by runner.py and admin.py.", file=sys.stderr)
