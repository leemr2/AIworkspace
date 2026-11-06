#!/usr/bin/env python3
"""Acceptance tests for the Admin page work. Standard library only. No real API calls:
the stub backend is real, the anthropic backend is replaced by a mock.

    python tests/test_acceptance.py            (or:  python -m unittest discover -s tests -v)

Test 9 (dashboard) needs Node.js to run the dashboard's real script against a fake DOM; it is
skipped with a message if `node` is not installed.
"""
import datetime
import http.client
import io
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

PROJECT = Path(__file__).resolve().parent.parent          # the program folder (app/)
REPO = PROJECT.parent
TEMPLATE = REPO / "workspace-template"
DASHBOARD = REPO / "dashboard" / "dashboard.html"
sys.path.insert(0, str(PROJECT))
import admin      # noqa: E402
import config     # noqa: E402
import runner     # noqa: E402

FAKE_KEY = "sk-ant-FAKEKEY-0123456789-abcdWXYZ"
ENV_KEY = "sk-ant-ENVKEY-9876543210-zzzz1111"
PIN = "482913"
TODAY = datetime.date.today()


def _rmtree(path):
    def onerr(func, p, _exc):
        try:
            os.chmod(p, stat.S_IWRITE)
            func(p)
        except OSError:
            pass
    shutil.rmtree(path, onerror=onerr)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="oai-test-")
        self.ws = os.path.join(self.tmp, "workspace")
        self.cfgdir = os.path.join(self.tmp, "home", ".office-ai")      # NOT inside the workspace
        os.makedirs(self.ws)
        shutil.copytree(TEMPLATE / "_agents", os.path.join(self.ws, "_agents"),
                        ignore=shutil.ignore_patterns("_public_status.json"))
        self.agents = runner.load_agents(self.ws)
        for a in self.agents.values():
            for sub in ("inbox", "drafts", "approved", "archive"):
                os.makedirs(os.path.join(self.ws, a["folder"], sub))
        config.set_config_dir(self.cfgdir)
        self._env = {k: os.environ.pop(k, None) for k in ("ANTHROPIC_API_KEY", "AI_MODEL")}
        self._backends = dict(runner.BACKENDS)
        runner._retry.clear(); runner._paused_logged.clear(); runner._last_draft.clear()
        runner._state.update(good_cfg=None, warned=None, fingerprint=None, provider=None)
        self._streams = (sys.stdout, sys.stderr)
        self.out = io.StringIO()
        sys.stdout = sys.stderr = self.out          # everything the code prints is checked in tearDown
        self.servers = []

    def tearDown(self):
        sys.stdout, sys.stderr = self._streams
        for srv in self.servers:
            srv.shutdown(); srv.server_close()
        runner.BACKENDS.clear(); runner.BACKENDS.update(self._backends)
        for k, v in self._env.items():
            os.environ.pop(k, None)
            if v is not None:
                os.environ[k] = v
        config.set_config_dir(None)
        config.set_app_dir(None)
        printed = self.out.getvalue()
        _rmtree(self.tmp)
        for secret in (FAKE_KEY, ENV_KEY):
            self.assertNotIn(secret, printed, "a secret was printed to the console")

    # ---- helpers ----
    def save_cfg(self, **changes):
        cfg = config.load()
        cfg.update(changes)
        config.save(cfg, self.ws)
        return cfg

    def set_pin(self, pin=PIN):
        cfg = config.load()
        cfg["pin"] = config.make_pin_record(pin)
        config.save(cfg, self.ws)

    def add_task(self, rid, tid, text="Please draft a friendly reminder about Monday hours.", age=10, **extra):
        a = self.agents[rid]
        task = {"task_id": tid, "role_id": rid, "task_type": a["authored_for_prototype"]["task_types"][0]["id"],
                "requester": "Pat", "created": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
                "instructions": text, "status": "queued", **extra}
        p = os.path.join(self.ws, a["folder"], "inbox", tid + ".json")
        with open(p, "w", encoding="utf-8") as f:
            json.dump(task, f)
        t = os.path.getmtime(p) - age
        os.utime(p, (t, t))
        return p

    def folder(self, rid, sub):
        return os.path.join(self.ws, self.agents[rid]["folder"], sub)

    def names(self, rid, sub):
        return sorted(os.listdir(self.folder(rid, sub)))

    def sweep(self, backend=None):
        return runner.sweep(self.ws, self.agents, backend)

    def log_events(self, name=None):
        out = []
        d = os.path.join(self.ws, "_logs")
        for fn in sorted(os.listdir(d)) if os.path.isdir(d) else []:
            with open(os.path.join(d, fn), encoding="utf-8") as f:
                out += [json.loads(l) for l in f if l.strip()]
        return [e for e in out if name is None or e["event"] == name]

    def status(self):
        with open(os.path.join(self.ws, "_agents", "_public_status.json"), encoding="utf-8") as f:
            return json.load(f)

    def workspace_bytes(self):
        for dp, _dn, fns in os.walk(self.ws):
            for fn in fns:
                with open(os.path.join(dp, fn), "rb") as f:
                    yield os.path.join(dp, fn), f.read()

    def start_server(self):
        srv = admin.make_server(self.ws, 0)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.servers.append(srv)
        return srv

    def client(self, srv):
        return Client(srv.server_address[1])

    def logged_in(self, pin=PIN):
        self.set_pin(pin)
        srv = self.start_server()
        c = self.client(srv)
        status, data, _ = c.login(pin)
        self.assertEqual(status, 200, data)
        return srv, c

    def mock_anthropic(self, usage=None, text="DRAFT TEXT"):
        calls = []

        def backend(system, user, task, agent, model=None, api_key=None):
            calls.append({"model": model, "key": api_key, "task": task.get("task_type")})
            return text, {"model": model, "input_tokens": 100, "output_tokens": 50, "latency_ms": 12, **(usage or {})}
        runner.BACKENDS["anthropic"] = backend
        return calls


class Client:
    def __init__(self, port):
        self.port, self.cookie, self.csrf = port, None, None

    def req(self, method, path, body=None, host=None, csrf=True, cookie=True, origin=None, ctype="application/json", extra=None):
        h = {"Host": host or "127.0.0.1:%d" % self.port}
        data = None
        if body is not None:
            data = json.dumps(body)
            h["Content-Type"] = ctype
        if cookie and self.cookie:
            h["Cookie"] = self.cookie
        if method == "POST" and csrf and self.csrf:
            h["X-CSRF-Token"] = self.csrf
        if origin:
            h["Origin"] = origin
        h.update(extra or {})
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        conn.request(method, path, data, h)
        r = conn.getresponse()
        raw = r.read()
        conn.close()
        try:
            parsed = json.loads(raw)
        except ValueError:
            parsed = None
        return r.status, parsed, r, raw

    def login(self, pin):
        status, data, r, _ = self.req("POST", "/api/login", {"pin": pin})
        if status == 200:
            self.cookie = r.getheader("Set-Cookie").split(";")[0]
            self.csrf = data["csrf"]
        return status, data, r

    def post(self, path, body):
        status, data, _r, raw = self.req("POST", path, body)
        return status, data, raw

    def get(self, path):
        status, data, _r, raw = self.req("GET", path)
        return status, data, raw


# ============================================================================
class T1_ConfigAndSecrets(Base):
    def test_config_file_permissions_and_key_never_leaks(self):
        srv, c = self.logged_in()
        status, data, raw = c.post("/api/connection", {"provider": "anthropic", "api_key": FAKE_KEY, "default_model": "model-d"})
        self.assertEqual(status, 200, data)
        self.assertTrue(config.config_path().exists())
        if os.name != "nt":
            self.assertEqual(os.stat(config.config_path()).st_mode & 0o777, 0o600)
            self.assertEqual(os.stat(config.config_dir()).st_mode & 0o777, 0o700)
        else:
            self.assertEqual(config.permission_report(), [], "icacls lock-down should have succeeded here")
            for target in (config.config_path(), config.config_dir()):       # only the current user may have access
                out = subprocess.run(["icacls", str(target)], capture_output=True, text=True).stdout
                entries = [l for l in out.splitlines() if ":(" in l]
                self.assertEqual(len(entries), 1, out)
                self.assertIn(os.environ["USERNAME"].lower(), entries[0].lower())
        # the key is stored (outside the workspace) ...
        self.assertIn(FAKE_KEY.encode(), config.config_path().read_bytes())
        # ... and appears nowhere under the workspace, in /api/state, or in any API answer
        self.assertNotIn(FAKE_KEY.encode(), raw)
        self.assertEqual(data["key"]["masked"], "sk-...WXYZ")
        status, state, raw = c.get("/api/state")
        self.assertNotIn(FAKE_KEY.encode(), raw)
        self.assertEqual(state["key"]["source"], "file")
        # errors never echo the key: a backend that (badly) puts the key in its error text
        def leaky(system, user, task, agent, model=None, api_key=None):
            raise RuntimeError("provider said no to key " + api_key)
        runner.BACKENDS["anthropic"] = leaky
        status, data, raw = c.post("/api/test_connection", {})
        self.assertNotIn(FAKE_KEY.encode(), raw)
        self.assertFalse(data["ok"])
        status, data, raw = c.post("/api/test_models", {"prompt": "fake text", "models": ["m1"]})
        self.assertEqual(status, 200)
        self.assertNotIn(FAKE_KEY.encode(), raw)
        self.assertFalse(data["results"][0]["ok"])
        # a malformed key is refused without being echoed
        status, data, raw = c.post("/api/connection", {"api_key": "not a valid key with spaces"})
        self.assertEqual(status, 400)
        self.assertNotIn(b"not a valid key", raw)
        # the runner's own error path: log + console
        self.add_task("R033", "t-leak")
        self.sweep()
        errs = self.log_events("model_error")
        self.assertEqual(len(errs), 1)
        for path, blob in self.workspace_bytes():
            self.assertNotIn(FAKE_KEY.encode(), blob, path)
        self.assertNotIn(PIN.encode(), config.config_path().read_bytes())
        self.assertIn("[hidden]", errs[0]["error"])

    def test_env_key_overrides_file_and_is_masked(self):
        self.save_cfg(provider="anthropic", api_key=FAKE_KEY)
        os.environ["ANTHROPIC_API_KEY"] = ENV_KEY
        srv, c = self.logged_in()
        status, state, raw = c.get("/api/state")
        self.assertEqual(state["key"]["source"], "env")
        self.assertTrue(state["key"]["env_overrides_file"])
        self.assertEqual(state["key"]["masked"], "sk-...1111")
        for secret in (FAKE_KEY, ENV_KEY):
            self.assertNotIn(secret.encode(), raw)
        calls = self.mock_anthropic()
        self.add_task("R033", "t-env")
        self.sweep()
        self.assertEqual(calls[0]["key"], ENV_KEY)
        del os.environ["ANTHROPIC_API_KEY"]
        self.add_task("R033", "t-file")
        self.sweep()
        self.assertEqual(calls[1]["key"], FAKE_KEY)

    def test_pin_is_hashed_and_validated(self):
        srv, c = self.logged_in()
        raw = config.config_path().read_text()
        self.assertNotIn(PIN, raw)
        rec = json.loads(raw)["pin"]
        self.assertGreaterEqual(rec["iterations"], 200_000)
        self.assertEqual(set(rec), {"salt", "hash", "iterations"})
        for bad in ("12345", "abcdef", "12 3456", ""):
            status, data, _ = c.post("/api/set_pin", {"current_pin": PIN, "new_pin": bad})
            self.assertEqual(status, 400, bad)
        status, data, _ = c.post("/api/set_pin", {"current_pin": "000000", "new_pin": "135790"})
        self.assertEqual(status, 401)
        status, data, _ = c.post("/api/set_pin", {"current_pin": PIN, "new_pin": "135790"})
        self.assertEqual(status, 200)
        self.assertTrue(config.verify_pin(config.load(), "135790"))
        self.assertFalse(config.verify_pin(config.load(), PIN))

    def test_prices_start_empty_and_unset_price_has_no_cost(self):
        srv, c = self.logged_in()
        status, state, _ = c.get("/api/state")
        self.assertEqual(state["prices"], {})
        self.assertIsNone(config.estimate_cost({}, "any-model", 1000, 1000))
        status, data, _ = c.post("/api/prices", {"prices": {"m1": {"input_per_mtok": 3, "output_per_mtok": "15.5"}}})
        self.assertEqual(status, 200)
        self.assertEqual(data["prices"]["m1"], {"input_per_mtok": 3.0, "output_per_mtok": 15.5})
        status, data, _ = c.post("/api/prices", {"prices": {"m1": {"input_per_mtok": 3, "output_per_mtok": ""}}})
        self.assertEqual(status, 400)
        status, data, _ = c.post("/api/prices", {"prices": {"m1": {"input_per_mtok": -1, "output_per_mtok": 1}}})
        self.assertEqual(status, 400)


class T2_ConfigLocation(Base):
    def test_admin_refuses_config_dir_inside_workspace(self):
        config.set_config_dir(os.path.join(self.ws, ".office-ai"))
        with mock.patch.object(admin, "make_server", side_effect=AssertionError("server must not start")):
            rc = admin.main(["--root", self.ws, "--port", "0"])
        self.assertEqual(rc, 2)
        self.assertIn("inside the shared workspace", self.out.getvalue())
        with self.assertRaises(config.ConfigLocationError):
            config.save(config.default_config(), self.ws)
        self.assertFalse(os.path.exists(os.path.join(self.ws, ".office-ai")))

    def test_workspace_that_contains_the_home_folder_is_refused_too(self):
        with self.assertRaises(config.ConfigLocationError):
            config.check_location(self.tmp)       # tmp contains tmp/home/.office-ai

    def test_runner_locks_when_config_is_inside_the_workspace(self):
        bad = Path(self.ws) / ".office-ai"
        bad.mkdir()
        (bad / "config.json").write_text(json.dumps({"provider": "anthropic", "api_key": FAKE_KEY}))
        config.set_config_dir(bad)
        calls = self.mock_anthropic()
        self.add_task("R033", "t-1")
        self.sweep()
        self.assertEqual(calls, [], "key from an unsafe settings folder must not be used")
        self.assertEqual(self.names("R033", "inbox"), ["t-1.json"])
        self.assertEqual(self.names("R033", "drafts"), [])
        self.assertEqual(self.status()["runner_state"], "locked")
        self.assertEqual(self.status()["message"], runner.STAFF_HOLD_MESSAGE)
        self.assertNotIn(self.ws, self.status()["message"])
        self.assertTrue(self.log_events("runner_locked"))
        self.assertNotIn(FAKE_KEY, json.dumps(self.status()))


class T3_RequestGuards(Base):
    def test_bad_host_missing_csrf_and_no_cookie_are_rejected(self):
        srv, c = self.logged_in()
        port = srv.server_address[1]
        self.assertEqual(srv.server_address[0], "127.0.0.1")
        for host in ("evil.example:%d" % port, "127.0.0.1:%d" % (port + 1), "127.0.0.1", "attacker.test", "192.168.1.5:%d" % port):
            self.assertEqual(c.req("GET", "/", host=host)[0], 403, host)
            self.assertEqual(c.req("GET", "/api/state", host=host)[0], 403, host)
            self.assertEqual(c.req("POST", "/api/seat", {"rid": "R033", "enabled": False}, host=host)[0], 403, host)
        status, _d, r, raw = c.req("GET", "/", host="localhost:%d" % port)
        self.assertEqual(status, 200)
        csp = r.getheader("Content-Security-Policy")
        for part in ("default-src 'none'", "script-src 'unsafe-inline'", "style-src 'unsafe-inline'",
                     "connect-src 'self'", "base-uri 'none'", "form-action 'none'"):
            self.assertIn(part, csp)
        self.assertEqual(r.getheader("Cache-Control"), "no-store")
        self.assertFalse([h for h, _ in r.getheaders() if h.lower().startswith("access-control")])
        # no session cookie
        anon = self.client(srv)
        self.assertEqual(anon.req("GET", "/api/state")[0], 401)
        self.assertEqual(anon.req("GET", "/api/usage")[0], 401)
        self.assertEqual(anon.req("POST", "/api/seat", {"rid": "R033", "enabled": False})[0], 401)
        # session but no / wrong CSRF token, wrong Origin, wrong content type
        body = {"rid": "R033", "enabled": False}
        self.assertEqual(c.req("POST", "/api/seat", body, csrf=False)[0], 403)
        self.assertEqual(c.req("POST", "/api/seat", body, extra={"X-CSRF-Token": "wrong"}, csrf=False)[0], 403)
        self.assertEqual(c.req("POST", "/api/seat", body, origin="http://evil.example")[0], 403)
        self.assertEqual(c.req("POST", "/api/seat", body, origin="null")[0], 403)
        self.assertEqual(c.req("POST", "/api/seat", body, ctype="text/plain")[0], 415)
        self.assertEqual(c.req("POST", "/api/seat", body, extra={"Sec-Fetch-Site": "cross-site"})[0], 403)
        self.assertTrue(config.seat_settings(config.load(), "R033")["enabled"], "rejected requests must change nothing")
        self.assertEqual(c.req("POST", "/api/seat", body, origin="http://127.0.0.1:%d" % port)[0], 200)
        self.assertEqual(c.req("PUT", "/api/seat", body)[0], 405)

    def test_cookie_flags_and_idle_timeout(self):
        self.set_pin()
        srv = self.start_server()
        c = self.client(srv)
        status, data, r = c.login(PIN)
        cookie = r.getheader("Set-Cookie")
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=Strict", cookie)
        self.assertEqual(c.get("/api/state")[0], 200)
        for s in srv.app.sessions.values():
            s["seen"] -= 16 * 60                  # 16 idle minutes > the 15 minute default
        self.assertEqual(c.get("/api/state")[0], 401)

    def test_five_wrong_pins_lock_logins(self):
        self.set_pin()
        srv = self.start_server()
        c = self.client(srv)
        for i in range(4):
            self.assertEqual(c.login("111111")[0], 401)
        self.assertEqual(c.login("111111")[0], 429)       # 5th wrong PIN triggers the lock
        status, data, _ = c.login(PIN)                    # even the right PIN is refused now
        self.assertEqual(status, 429)
        self.assertIsNone(c.cookie)
        srv.app.locked_until = 0                          # pretend 5 minutes passed
        self.assertEqual(c.login(PIN)[0], 200)

    @unittest.skipUnless(os.name == "nt", "Windows console prompt")
    def test_windows_pin_prompt_shows_stars_and_handles_backspace(self):
        keys = iter(list("12x\b3456") + ["\xe0", "H"] + list("78") + ["\r"])      # includes a backspace and an arrow key
        fake = mock.Mock(getwch=lambda: next(keys))
        with mock.patch.dict(sys.modules, {"msvcrt": fake}):
            pin = admin.read_secret("PIN: ")
        self.assertEqual(pin, "12345678")
        shown = self.out.getvalue()
        self.assertIn("********", shown.replace("\b \b", ""))
        self.assertNotIn("12345678", shown)

    def test_pin_not_set_means_no_login(self):
        srv = self.start_server()
        self.assertEqual(self.client(srv).login("123456")[0], 409)

    def test_second_instance_cannot_share_the_port(self):
        self.set_pin()                       # so main() never stops to ask for one
        srv = self.start_server()
        with self.assertRaises(OSError):
            admin.make_server(self.ws, srv.server_address[1])
        self.assertEqual(admin.main(["--root", self.ws, "--port", str(srv.server_address[1])]), 1)
        self.assertIn("already in use", self.out.getvalue())

    def test_server_only_binds_loopback(self):
        srv = self.start_server()
        self.assertEqual(srv.server_address[0], "127.0.0.1")
        self.assertEqual(admin.BIND_HOST, "127.0.0.1")
        src = (PROJECT / "admin.py").read_text(encoding="utf-8")
        self.assertNotIn('"0.0.0.0"', src)             # never used as a bind address
        self.assertNotIn("''", src.split("def make_server")[1].split("def set_pin_interactive")[0])   # nor the empty-string wildcard


class T4_TestModels(Base):
    def test_phi_text_is_refused_before_any_backend_call(self):
        srv, c = self.logged_in()
        calls = []
        runner.BACKENDS["stub"] = lambda *a, **k: calls.append(1) or ("x", {})
        status, data, raw = c.post("/api/test_models", {"prompt": "Reminder for DOB 04/12/1980 please", "models": ["m1", "m2"]})
        self.assertEqual(status, 400)
        self.assertTrue(data["blocked"])
        self.assertIn("date of birth", data["reasons"])
        self.assertIn("full date (possible DOB/visit)", data["reasons"])
        self.assertEqual(calls, [])
        for _p, blob in self.workspace_bytes():
            self.assertNotIn(b"04/12/1980", blob)
        ev = self.log_events("admin_test")
        self.assertEqual(len(ev), 1)
        self.assertTrue(ev[0]["blocked"])

    def test_clean_text_runs_two_models_and_limits(self):
        srv, c = self.logged_in()
        seen = []

        def fake(system, user, task, agent, model=None, api_key=None):
            seen.append((model, user))
            return "answer from " + model, {"model": model, "input_tokens": 1000, "output_tokens": 500, "latency_ms": 250}
        runner.BACKENDS["stub"] = fake
        c.post("/api/prices", {"prices": {"m1": {"input_per_mtok": 3, "output_per_mtok": 15}}})
        status, data, _ = c.post("/api/test_models", {"prompt": "Say hello to a made-up person named Zed.", "models": ["m1", "m2"]})
        self.assertEqual(status, 200, data)
        r1, r2 = data["results"]
        self.assertEqual((r1["model"], r1["text"], r1["input_tokens"], r1["output_tokens"], r1["latency_ms"]), ("m1", "answer from m1", 1000, 500, 250))
        self.assertAlmostEqual(r1["estimated_cost"], (1000 * 3 + 500 * 15) / 1e6)
        self.assertIsNone(r2["estimated_cost"])                          # no price set for m2
        self.assertEqual([m for m, _ in seen], ["m1", "m2"])
        ev = self.log_events("admin_test")[-1]
        self.assertEqual(ev["input_tokens"], 2000)
        self.assertNotIn("Zed", json.dumps(self.log_events()))           # prompt text is never logged
        self.assertIn("m1", config.load()["recent_models"])
        # rate limit: 2 calls used; 4 more allowed, then refused
        self.assertEqual(c.post("/api/test_models", {"prompt": "a", "models": ["m1", "m2"]})[0], 200)
        self.assertEqual(c.post("/api/test_models", {"prompt": "a", "models": ["m1", "m2"]})[0], 200)
        self.assertEqual(c.post("/api/test_models", {"prompt": "a", "models": ["m1"]})[0], 429)
        # input limits
        srv.app.calls.clear()
        self.assertEqual(c.post("/api/test_models", {"prompt": "x" * 4001, "models": ["m1"]})[0], 400)
        self.assertEqual(c.post("/api/test_models", {"prompt": "x", "models": []})[0], 400)
        self.assertEqual(c.post("/api/test_models", {"prompt": "x", "models": ["a", "b", "c"]})[0], 400)
        self.assertEqual(c.post("/api/test_models", {"prompt": "x", "models": ["bad model!"]})[0], 400)

    def test_connection_test_messages(self):
        self.save_cfg(provider="anthropic")
        srv, c = self.logged_in()
        status, data, _ = c.post("/api/test_connection", {})
        self.assertFalse(data["ok"])
        self.assertIn("No API key", data["message"])
        self.save_cfg(api_key=FAKE_KEY)

        def bad_key(*a, **k):
            raise runner.ModelError("bad_key", runner._http_error(401)[1])
        runner.BACKENDS["anthropic"] = bad_key
        status, data, raw = c.post("/api/test_connection", {})
        self.assertFalse(data["ok"])
        self.assertIn("rejected the API key", data["message"])
        self.mock_anthropic()
        status, data, _ = c.post("/api/test_connection", {})
        self.assertTrue(data["ok"])


class T5_PauseResume(Base):
    def test_paused_seat_keeps_tasks_and_status_file_tells_the_truth(self):
        srv, c = self.logged_in()
        self.add_task("R033", "t-a")
        self.add_task("R034", "t-b")
        self.assertEqual(c.post("/api/seat", {"rid": "R033", "enabled": False})[0], 200)
        self.assertEqual(self.sweep(), 1)
        self.assertEqual(self.names("R033", "inbox"), ["t-a.json"])        # still queued, not archived
        self.assertEqual(self.names("R033", "drafts"), [])
        self.assertEqual(self.names("R033", "archive"), [])
        self.assertEqual(self.names("R034", "drafts"), ["t-b.json"])        # other seats keep working
        st = self.status()
        self.assertFalse(st["seats"]["R033"]["enabled"])
        self.assertTrue(st["seats"]["R034"]["enabled"])
        self.assertIsNotNone(st["seats"]["R034"]["last_draft_at"])
        self.assertIsNone(st["seats"]["R033"]["last_draft_at"])
        self.assertIn("updated", st)
        self.assertEqual(len(self.log_events("seat_paused_skip")), 1)
        # staff editing the shared file must not un-pause the seat
        p = os.path.join(self.ws, "_agents", "_public_status.json")
        st["seats"]["R033"]["enabled"] = True
        with open(p, "w") as f:
            json.dump(st, f)
        self.sweep()
        self.assertEqual(self.names("R033", "inbox"), ["t-a.json"])
        self.assertFalse(self.status()["seats"]["R033"]["enabled"], "runner must rewrite the true value")
        self.assertEqual(len(self.log_events("seat_paused_skip")), 1, "no flood of skip events")
        # resume: the queued task is processed
        self.assertEqual(c.post("/api/seat", {"rid": "R033", "enabled": True})[0], 200)
        self.sweep()
        self.assertEqual(self.names("R033", "inbox"), [])
        self.assertEqual(self.names("R033", "drafts"), ["t-a.json"])
        self.assertTrue(self.status()["seats"]["R033"]["enabled"])

    def test_changes_apply_without_restart(self):
        self.add_task("R033", "t-1")
        self.save_cfg(per_seat={"R033": {"enabled": False, "model": None}})
        self.sweep()
        self.assertEqual(self.names("R033", "drafts"), [])
        self.save_cfg(per_seat={"R033": {"enabled": True, "model": None}})
        self.sweep()
        self.assertEqual(self.names("R033", "drafts"), ["t-1.json"])


class T6_PerSeatModel(Base):
    def test_model_precedence_key_and_log_fields(self):
        self.save_cfg(provider="anthropic", api_key=FAKE_KEY, default_model="model-d",
                      per_seat={"R033": {"enabled": True, "model": "model-x"}})
        calls = self.mock_anthropic()
        self.add_task("R033", "t-1")
        self.add_task("R034", "t-2")
        self.sweep()
        self.assertEqual(sorted(c["model"] for c in calls), ["model-d", "model-x"])
        self.assertTrue(all(c["key"] == FAKE_KEY for c in calls))
        evs = {e["role"]: e for e in self.log_events("draft_created")}
        self.assertEqual(evs["R033"]["model"], "model-x")
        self.assertEqual(evs["R034"]["model"], "model-d")
        for e in evs.values():
            self.assertEqual((e["input_tokens"], e["output_tokens"], e["latency_ms"]), (100, 50, 12))
            self.assertNotIn("estimated_cost", e)                        # no price configured
        for e in self.log_events():
            self.assertNotIn("DRAFT TEXT", json.dumps(e))
            self.assertNotIn("friendly reminder", json.dumps(e))
        # fallbacks: no seat model, no default -> AI_MODEL env -> built-in default
        self.save_cfg(default_model=None, per_seat={})
        os.environ["AI_MODEL"] = "model-env"
        self.add_task("R034", "t-3")
        self.sweep()
        self.assertEqual(calls[-1]["model"], "model-env")
        del os.environ["AI_MODEL"]
        self.add_task("R034", "t-4")
        self.sweep()
        self.assertEqual(calls[-1]["model"], config.DEFAULT_MODEL)
        # prices configured -> estimated_cost appears on the log event
        self.save_cfg(default_model="model-d", prices={"model-d": {"input_per_mtok": 3, "output_per_mtok": 15}})
        self.add_task("R034", "t-5")
        self.sweep()
        last = self.log_events("draft_created")[-1]
        self.assertAlmostEqual(last["estimated_cost"], (100 * 3 + 50 * 15) / 1e6)

    def test_model_error_event_has_usage_fields_and_backs_off(self):
        self.save_cfg(provider="anthropic", api_key=FAKE_KEY, default_model="model-d")

        def boom(*a, **k):
            raise runner.ModelError("rate_limited", runner._http_error(429)[1])
        runner.BACKENDS["anthropic"] = boom
        p = self.add_task("R033", "t-1")
        self.sweep()
        self.sweep()                                                       # backed off: no second attempt
        errs = self.log_events("model_error")
        self.assertEqual(len(errs), 1)
        e = errs[0]
        self.assertEqual((e["model"], e["input_tokens"], e["output_tokens"]), ("model-d", 0, 0))
        self.assertIn("latency_ms", e)
        self.assertTrue(os.path.exists(p), "failed task stays in the inbox")
        calls = self.mock_anthropic()
        self.save_cfg(default_model="model-e")                              # changing settings clears the back-off
        self.sweep()
        self.assertEqual(calls[0]["model"], "model-e")
        self.assertEqual(self.names("R033", "drafts"), ["t-1.json"])

    def test_stub_returns_zero_tokens(self):
        text, usage = runner.backend_stub("s", "u", {"task_type": "x", "instructions": "hi"}, {"role_title": "T"})
        self.assertEqual((usage["input_tokens"], usage["output_tokens"], usage["model"]), (0, 0, "stub"))
        self.assertIn("latency_ms", usage)

    def test_anthropic_backend_reads_usage_from_response(self):
        body = json.dumps({"content": [{"type": "text", "text": "Hello"}], "usage": {"input_tokens": 12, "output_tokens": 7}}).encode()

        class Resp:
            def __enter__(s): return s
            def __exit__(s, *a): return False
            def read(s): return body
        seen = {}

        def fake_urlopen(req, timeout=0):
            seen["headers"] = dict(req.header_items())
            seen["body"] = json.loads(req.data)
            return Resp()
        with mock.patch.object(runner.urllib.request, "urlopen", fake_urlopen):
            text, usage = runner.backend_anthropic("sys", "usr", {}, {}, model="model-q", api_key=FAKE_KEY)
        self.assertEqual(text, "Hello")
        self.assertEqual((usage["input_tokens"], usage["output_tokens"], usage["model"]), (12, 7, "model-q"))
        self.assertEqual(seen["body"]["model"], "model-q")
        # error texts are plain language and never contain the key
        import urllib.error as ue
        for exc, kind in ((ue.HTTPError("u", 401, "x", {}, None), "bad_key"), (ue.HTTPError("u", 429, "x", {}, None), "rate_limited"),
                          (ue.URLError("dns failure for " + FAKE_KEY), "no_network")):
            with mock.patch.object(runner.urllib.request, "urlopen", side_effect=exc):
                with self.assertRaises(runner.ModelError) as cm:
                    runner.backend_anthropic("s", "u", {}, {}, model="m", api_key=FAKE_KEY)
            self.assertEqual(cm.exception.kind, kind)
            self.assertNotIn(FAKE_KEY, str(cm.exception))


class T7_Usage(Base):
    def seed_logs(self, rows):
        os.makedirs(os.path.join(self.ws, "_logs"), exist_ok=True)
        for day, ev in rows:
            p = os.path.join(self.ws, "_logs", day.isoformat() + ".jsonl")
            with open(p, "a", encoding="utf-8") as f:
                f.write(json.dumps({"ts": day.isoformat() + "T10:00:00-08:00", **ev}) + "\n")

    def seed_folders(self):
        iso = TODAY.isoformat() + "T09:00:00Z"
        def w(rid, sub, name, obj):
            with open(os.path.join(self.folder(rid, sub), name), "w") as f:
                json.dump(obj, f)
        w("R033", "approved", "t-a1.json", {"approved_at": iso, "final_text": "x"})
        w("R033", "approved", "t-a4.json", {"approved_at": iso, "final_text": "x"})
        w("R033", "archive", "t-a1-r1.json", {"created": iso})            # revised (by -rN name)
        w("R033", "archive", "t-9.json", {"created": iso, "parent_id": "t-8"})   # revised (by parent_id)
        w("R033", "archive", "t-a3.discarded.json", {"task_id": "t-a3", "discarded_at": iso})
        w("R033", "archive", "t-7.rejected.json", {"created": iso})       # not counted
        w("R033", "archive", "t-a2.json", {"created": iso})               # plain archived task: not counted

    def test_aggregation_counts_approval_rate_and_cost(self):
        d10, d40 = TODAY - datetime.timedelta(days=10), TODAY - datetime.timedelta(days=40)
        e = lambda **k: {"event": "draft_created", "role": "R033", "backend": "x", "flags": 0, **k}
        self.seed_logs([
            (TODAY, e(task="a1", model="m1", input_tokens=1000, output_tokens=500, latency_ms=2000)),
            (TODAY, {"event": "phi_tripwire", "role": "R033", "task": "a2", "kinds": ["x"]}),
            (TODAY, {"event": "model_error", "role": "R033", "task": "a3", "error": "e", "model": "m1", "input_tokens": 0, "output_tokens": 0, "latency_ms": 5}),
            (TODAY, {"event": "model_error", "role": "R033", "task": "a3", "error": "e", "model": "m1", "input_tokens": 0, "output_tokens": 0, "latency_ms": 5}),
            (TODAY, e(task="a4", model="m1", input_tokens=2000, output_tokens=1000, latency_ms=4000)),
            (d10, e(task="b1", model="m1", input_tokens=1000, output_tokens=1000, latency_ms=3000)),
            (d40, e(task="c1", model="m1", input_tokens=5000, output_tokens=5000, latency_ms=1000)),
            (TODAY, {"event": "draft_created", "role": "R999", "task": "zz"}),       # unknown seat: ignored
        ])
        self.seed_folders()
        cfg = config.default_config()
        u = admin.compute_usage(self.ws, 30, cfg)
        s = {x["rid"]: x for x in u["seats"]}
        r = s["R033"]
        self.assertEqual((r["requests"], r["drafts"], r["tripwire_blocks"], r["model_errors"]), (5, 3 - 0, 1, 2))  # a1,a2,a3,a4,b1 ; retries of a3 = 2 errors
        self.assertEqual((r["tokens_in"], r["tokens_out"]), (4000, 2500))
        self.assertEqual(r["avg_time_to_draft_ms"], 3000)
        self.assertEqual((r["approved"], r["revised"], r["discarded"]), (2, 2, 1))
        self.assertAlmostEqual(r["approval_rate"], 2 / 5)
        self.assertEqual(r["requests_this_week"], 4)
        self.assertIsNone(r["estimated_cost"])                              # no prices -> no number
        self.assertEqual(r["cost_status"], "unset")
        self.assertIsNotNone(r["last_used"])
        self.assertFalse(r["unused"])
        self.assertTrue(s["R034"]["unused"])                                # zero requests in period
        self.assertIsNone(s["R034"]["last_used"])
        self.assertIsNone(s["R034"]["approval_rate"])
        self.assertIn("R034", u["not_used_recently"])
        self.assertNotIn("R033", u["not_used_recently"])
        self.assertEqual(u["totals"]["requests"], 5)
        self.assertIsNone(u["totals"]["estimated_cost"])
        weeks = [w for w in u["weeks"] if w["rid"] == "R033"]
        self.assertEqual(sum(w["requests"] for w in weeks), 5)
        self.assertEqual(sum(w["drafts"] for w in weeks), 3)
        # with prices
        cfg["prices"] = {"m1": {"input_per_mtok": 3, "output_per_mtok": 15}}
        u = admin.compute_usage(self.ws, 30, cfg)
        r = {x["rid"]: x for x in u["seats"]}["R033"]
        self.assertAlmostEqual(r["estimated_cost"], (4000 * 3 + 2500 * 15) / 1e6)
        self.assertEqual(r["cost_status"], "complete")
        self.assertAlmostEqual(u["totals"]["estimated_cost"], 0.0495)
        # other periods
        u7 = {x["rid"]: x for x in admin.compute_usage(self.ws, 7, cfg)["seats"]}["R033"]
        self.assertEqual((u7["requests"], u7["tokens_in"]), (4, 3000))
        u90 = {x["rid"]: x for x in admin.compute_usage(self.ws, 90, cfg)["seats"]}["R033"]
        self.assertEqual((u90["requests"], u90["tokens_in"]), (6, 9000))

    def test_logged_cost_is_used_and_partial_pricing_is_flagged(self):
        self.seed_logs([
            (TODAY, {"event": "draft_created", "role": "R034", "task": "k1", "model": "m2", "input_tokens": 10, "output_tokens": 10, "latency_ms": 1, "estimated_cost": 0.01}),
            (TODAY, {"event": "draft_created", "role": "R033", "task": "k2", "model": "m3", "input_tokens": 10, "output_tokens": 10, "latency_ms": 1}),
            (TODAY, {"event": "draft_created", "role": "R035", "task": "k3", "model": "stub", "input_tokens": 0, "output_tokens": 0, "latency_ms": 1}),
        ])
        u = admin.compute_usage(self.ws, 30, config.default_config())
        s = {x["rid"]: x for x in u["seats"]}
        self.assertEqual((s["R034"]["estimated_cost"], s["R034"]["cost_status"]), (0.01, "complete"))
        self.assertEqual(s["R033"]["cost_status"], "unset")
        self.assertEqual((s["R035"]["estimated_cost"], s["R035"]["cost_status"]), (0.0, "complete"))   # zero tokens: nothing to price
        self.assertEqual(u["totals"]["cost_status"], "partial")
        self.assertEqual(u["totals"]["estimated_cost"], 0.01)

    def test_usage_endpoint(self):
        self.seed_logs([(TODAY, {"event": "draft_created", "role": "R033", "task": "x1", "model": "m1", "input_tokens": 1, "output_tokens": 1, "latency_ms": 9})])
        srv, c = self.logged_in()
        status, data, raw = c.get("/api/usage?days=7")
        self.assertEqual(status, 200)
        self.assertEqual(data["days"], 7)
        self.assertEqual({x["rid"]: x for x in data["seats"]}["R033"]["requests"], 1)
        self.assertEqual(c.get("/api/usage?days=junk")[1]["days"], 30)
        self.assertEqual(c.get("/api/usage?days=0")[1]["days"], 30)


class T8_ExistingFlow(Base):
    def test_stub_backend_still_makes_a_draft(self):
        self.add_task("R033", "t-20261106-100000-abc", "Draft a short note about our new Saturday hours.")
        self.assertEqual(self.sweep(), 1)
        self.assertEqual(self.names("R033", "inbox"), [])
        self.assertEqual(self.names("R033", "archive"), ["t-20261106-100000-abc.json"])
        with open(os.path.join(self.folder("R033", "drafts"), "t-20261106-100000-abc.json"), encoding="utf-8") as f:
            d = json.load(f)
        self.assertEqual((d["status"], d["backend"]), ("pending_review", "stub"))
        self.assertIn("STUB OUTPUT", d["draft"])
        self.assertIn("created_draft", d)
        self.assertNotIn("model", d)                                         # staff never see models
        ev = self.log_events("draft_created")[0]
        self.assertEqual((ev["model"], ev["input_tokens"], ev["output_tokens"]), ("stub", 0, 0))
        self.assertNotIn("estimated_cost", ev)

    def test_tripwire_still_blocks_and_redacts(self):
        self.add_task("R033", "t-phi", "Patient name: Jane, SSN 123-45-6789, call 555-123-4567", feedback="DOB 01/02/1980")
        self.sweep()
        self.assertEqual(self.names("R033", "inbox"), [])
        with open(os.path.join(self.folder("R033", "drafts"), "t-phi.json"), encoding="utf-8") as f:
            d = json.load(f)
        self.assertEqual(d["status"], "rejected_phi_tripwire")
        self.assertEqual(d["instructions"], "[REMOVED by privacy check]")
        self.assertEqual(d["draft"], "")
        self.assertTrue(self.log_events("phi_tripwire"))
        for path, blob in self.workspace_bytes():
            for needle in (b"123-45-6789", b"555-123-4567", b"01/02/1980", b"Jane"):
                self.assertNotIn(needle, blob, path)

    def test_bad_header_still_rejected(self):
        self.add_task("R033", "t-bad")
        p = os.path.join(self.folder("R033", "inbox"), "t-bad.json")
        d = json.load(open(p)); d["role_id"] = "R034"; json.dump(d, open(p, "w"))
        os.utime(p, (1, 1))
        self.sweep()
        self.assertEqual(self.names("R033", "archive"), ["t-bad.rejected.json"])

    def test_old_style_single_string_backend_still_works(self):
        runner.BACKENDS["stub"] = lambda system, user, task, agent, **kw: "plain old string"
        self.add_task("R033", "t-old")
        self.sweep()
        with open(os.path.join(self.folder("R033", "drafts"), "t-old.json"), encoding="utf-8") as f:
            self.assertEqual(json.load(f)["draft"], "plain old string")


class T9_Dashboard(Base):
    def test_static_properties(self):
        html = DASHBOARD.read_text(encoding="utf-8")
        csp = html.split('http-equiv="Content-Security-Policy" content="')[1].split('"')[0]
        self.assertNotIn("connect-src", csp)
        self.assertIn("default-src 'none'", csp)
        self.assertNotIn("innerHTML", html)
        self.assertIn("_public_status.json", html)
        self.assertIn(".discarded.json", html)
        self.assertNotIn("api_key", html.lower())

    def test_admin_page_is_self_contained(self):
        page = admin.PAGE
        self.assertNotIn("innerHTML", page)
        self.assertNotIn("<script src", page)
        self.assertNotIn("<link", page)
        self.assertNotIn("http://", page)
        self.assertNotIn("https://", page)
        self.assertNotIn("@import", page)
        self.assertNotIn("url(", page)
        self.assertIn('type="password"', page)
        self.assertIn("--bg:#f6f7f9", page)                    # same variables as the staff dashboard

    @unittest.skipUnless(shutil.which("node"), "Node.js not installed: dashboard behaviour test skipped")
    def test_paused_seat_disabled_and_discard_marker(self):
        r = subprocess.run(["node", str(PROJECT / "tests" / "dashboard_harness.js"), str(DASHBOARD),
                            str(TEMPLATE / "_agents")], capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        rep = json.loads(r.stdout)
        opt = {o["value"]: o for o in rep["paused"]["options"]}
        self.assertTrue(opt["R033"]["disabled"])
        self.assertIn("(paused)", opt["R033"]["text"])
        self.assertFalse(opt["R034"]["disabled"])
        self.assertNotEqual(rep["paused"]["selected"], "R033")           # a paused seat is never the selection
        self.assertIn("R033", rep["paused"]["notices"])
        self.assertNotIn("offline", rep["paused"]["notices"])
        self.assertTrue(rep["allPaused"]["sendDisabled"])
        self.assertIn("may be offline", rep["stale"]["notices"])          # stale file
        self.assertEqual(rep["none"]["notices"], "")                      # no status file: no notices
        self.assertFalse(any(o["disabled"] for o in rep["none"]["options"]))
        # discard
        self.assertTrue(rep["discardButtonFound"])
        self.assertEqual(rep["draftsFiles"], [])
        self.assertEqual(rep["archiveFiles"], ["t-20261106-101500-abc.discarded.json"])
        marker = json.loads(rep["markerText"])
        self.assertEqual(set(marker), {"task_id", "role_id", "task_type", "discarded_by", "discarded_at"})
        self.assertEqual((marker["task_id"], marker["role_id"], marker["task_type"], marker["discarded_by"]),
                         ("t-20261106-101500-abc", "R034", "social_post", "Tester"))
        self.assertNotIn("SECRET", rep["markerText"])
        self.assertEqual(rep["ops"][0].split(":")[0], "write")            # marker is written BEFORE the draft is removed
        self.assertEqual(rep["ops"][1].split(":")[0], "remove")
        self.assertEqual(rep["cancel"], {"archive": [], "drafts": ["t-x.json"]})   # cancelled dialog changes nothing
        self.assertIn("Please tell the office manager", rep["locked"]["notices"])
        self.assertTrue(rep["locked"]["sendDisabled"])
        self.assertIn("Please tell the office manager", rep["reviewHold"]["notices"])
        self.assertTrue(rep["reviewHold"]["sendDisabled"])
        self.assertNotIn("Please tell the office manager", rep["paused"]["notices"])

    def test_discard_marker_is_counted_by_usage(self):
        with open(os.path.join(self.folder("R034", "archive"), "t-z.discarded.json"), "w") as f:
            json.dump({"task_id": "t-z", "role_id": "R034", "task_type": "x", "discarded_by": "A",
                       "discarded_at": datetime.datetime.now().astimezone().isoformat()}, f)
        r = {x["rid"]: x for x in admin.compute_usage(self.ws, 7, config.default_config())["seats"]}["R034"]
        self.assertEqual(r["discarded"], 1)
        self.assertEqual(r["approval_rate"], 0.0)


class T10_Launchers(Base):
    def test_launchers_exist_and_show_workspace_line_first(self):
        for name in ("start-runner.bat", "start-admin.bat", "start-runner.command", "start-admin.command"):
            text = (PROJECT / name).read_text(encoding="utf-8")
            first = [l for l in text.splitlines() if "WORKSPACE" in l][0]
            self.assertIn("WORKSPACE=", first, name)
            self.assertLess(text.index("WORKSPACE="), text.index("runner.py" if "runner" in name else "admin.py"), name)
            self.assertIn("outside the shared folder", text, name)


class T11_Hardening(Base):
    def test_root_is_required(self):
        with self.assertRaises(SystemExit) as cm:
            runner.main([])
        self.assertEqual(cm.exception.code, 2)
        with self.assertRaises(SystemExit) as cm:
            admin.main([])
        self.assertEqual(cm.exception.code, 2)

    def test_refuses_when_program_folder_is_inside_workspace(self):
        app = os.path.join(self.ws, "program")
        os.makedirs(app)
        config.set_app_dir(app)
        self.add_task("R033", "t-1")
        self.assertEqual(runner.main(["--root", self.ws, "--once"]), 2)
        self.assertIn("will not start", self.out.getvalue())
        self.assertEqual(self.names("R033", "inbox"), ["t-1.json"])
        self.assertEqual(self.names("R033", "drafts"), [])
        with mock.patch.object(admin, "make_server", side_effect=AssertionError("server must not start")):
            self.assertEqual(admin.main(["--root", self.ws, "--port", "0"]), 2)

    def test_refuses_when_workspace_is_inside_program_folder(self):
        config.set_app_dir(self.tmp)
        self.add_task("R033", "t-1")
        self.assertEqual(runner.main(["--root", self.ws, "--once"]), 2)
        self.assertEqual(self.names("R033", "inbox"), ["t-1.json"])
        self.assertFalse(self.names("R033", "drafts"))
        with mock.patch.object(admin, "make_server", side_effect=AssertionError("server must not start")):
            self.assertEqual(admin.main(["--root", self.ws, "--port", "0"]), 2)

    def test_resolved_symlink_is_not_a_false_overlap(self):
        program = os.path.join(self.tmp, "program")
        os.makedirs(program)
        link = os.path.join(program, "workspace-link")
        try:
            kwargs = {"target_is_directory": True} if os.name == "nt" else {}
            os.symlink(self.ws, link, **kwargs)
        except (OSError, NotImplementedError, TypeError) as e:
            self.skipTest("symlink creation is not permitted on this platform: %s" % e)
        config.set_app_dir(program)
        self.assertEqual(runner.main(["--root", link, "--once"]), 0, self.out.getvalue())

    def test_first_run_stub_works_and_real_backend_is_refused(self):
        self.assertFalse(os.path.isdir(self.cfgdir))
        self.add_task("R033", "t-stub")
        self.assertEqual(self.sweep(), 1)
        self.assertEqual(self.names("R033", "drafts"), ["t-stub.json"])
        st = self.status()
        self.assertEqual(st["runner_state"], "first_run")
        self.assertEqual(st["message"], "")
        self.assertFalse(os.path.isdir(self.cfgdir))
        self.add_task("R034", "t-real")
        calls = self.mock_anthropic()
        self.assertEqual(self.sweep("anthropic"), 0)
        self.assertEqual(calls, [])
        self.assertEqual(self.names("R034", "inbox"), ["t-real.json"])
        self.assertIn("REFUSED", self.out.getvalue())
        self.assertEqual(self.status()["runner_state"], "first_run")

    def test_missing_config_locks_and_recovers(self):
        os.makedirs(self.cfgdir)
        self.add_task("R033", "t-1")
        self.sweep()
        self.assertEqual(self.status()["runner_state"], "locked")
        self.assertIn("office manager", self.status()["message"])
        self.assertEqual(self.names("R033", "inbox"), ["t-1.json"])
        self.assertEqual(self.names("R033", "drafts"), [])
        config.save(config.default_config(), self.ws)
        self.sweep()
        self.assertEqual(self.names("R033", "drafts"), ["t-1.json"])
        self.assertEqual(self.status()["runner_state"], "ready")

    def test_invalid_config_without_last_good_locks_and_recovers(self):
        os.makedirs(self.cfgdir)
        config.config_path().write_text("{", encoding="utf-8")
        self.add_task("R033", "t-1")
        self.sweep()
        self.assertEqual(self.status()["runner_state"], "locked")
        self.assertEqual(self.names("R033", "inbox"), ["t-1.json"])
        config.save(config.default_config(), self.ws)
        self.sweep()
        self.assertEqual(self.status()["runner_state"], "ready")
        self.assertEqual(self.names("R033", "drafts"), ["t-1.json"])

    def test_corrupt_config_keeps_last_good_settings(self):
        self.save_cfg(provider="stub")
        self.add_task("R033", "t-1")
        self.sweep()
        config.config_path().write_text("{", encoding="utf-8")
        self.add_task("R033", "t-2")
        self.sweep()
        st = self.status()
        self.assertEqual(st["runner_state"], "ready")
        self.assertEqual(st.get("settings_note"), "using last good settings")
        self.assertEqual(sorted(self.names("R033", "drafts")), ["t-1.json", "t-2.json"])
        self.assertIn("using last good settings", self.out.getvalue())

    def test_admin_starts_when_config_is_invalid_and_shows_last_good(self):
        os.makedirs(self.cfgdir)
        config.config_path().write_text("{", encoding="utf-8")
        fake = mock.Mock()
        fake.serve_forever = lambda: None
        fake.server_close = lambda: None
        fake.app = mock.Mock(port=9)
        with mock.patch.object(admin, "make_server", return_value=fake) as made:
            self.assertEqual(admin.main(["--root", self.ws, "--port", "9"]), 0)
        made.assert_called_once()
        self.assertIn("will still start", self.out.getvalue())
        config.save(config.default_config(), self.ws)
        srv, c = self.logged_in()
        self.assertEqual(c.get("/api/state")[0], 200)
        config.config_path().write_text("{", encoding="utf-8")
        status, data, _ = c.get("/api/state")
        self.assertEqual(status, 200, data)
        self.assertEqual(data["hold"]["settings_note"], "using last good settings")
        self.assertIn("settings_note", admin.PAGE)

    def _baseline(self):
        self.save_cfg()
        self.sweep()

    def test_one_agent_file_pauses_only_that_seat(self):
        self._baseline()
        path = os.path.join(self.ws, "_agents", "R033.json")
        text = Path(path).read_text(encoding="utf-8")
        self.assertIn("Digital Marketing", text)
        Path(path).write_text(text.replace("Digital Marketing", "Digital MarketingX"), encoding="utf-8")
        self.add_task("R033", "t-a")
        self.add_task("R034", "t-b")
        self.sweep()
        self.assertEqual(self.names("R033", "inbox"), ["t-a.json"])
        self.assertEqual(self.names("R033", "drafts"), [])
        self.assertEqual(self.names("R034", "drafts"), ["t-b.json"])
        st = self.status()
        self.assertEqual(st["runner_state"], "paused_for_review")
        self.assertEqual(st["message"], runner.STAFF_HOLD_MESSAGE)
        self.assertFalse(st["seats"]["R033"]["enabled"])
        self.assertTrue(st["seats"]["R034"]["enabled"])
        self.assertNotIn(self.ws, json.dumps(st))

    def test_decision_rights_change_pauses_every_seat(self):
        self._baseline()
        path = os.path.join(self.ws, "_agents", "_decision_rights.json")
        with open(path, "a", encoding="utf-8") as f:
            f.write("\n")
        self.add_task("R033", "t-a")
        self.add_task("R034", "t-b")
        self.sweep()
        self.assertEqual(self.names("R033", "inbox"), ["t-a.json"])
        self.assertEqual(self.names("R034", "inbox"), ["t-b.json"])
        self.assertEqual(self.status()["runner_state"], "paused_for_review")
        self.assertTrue(all(not seat["enabled"] for seat in self.status()["seats"].values()))

    def test_approve_needs_a_session_and_then_seats_resume(self):
        self._baseline()
        path = os.path.join(self.ws, "_agents", "R033.json")
        Path(path).write_text(Path(path).read_text(encoding="utf-8") + "\n", encoding="utf-8")
        self.add_task("R033", "t-a")
        self.sweep()
        self.assertEqual(self.names("R033", "drafts"), [])
        srv, c = self.logged_in()
        self.assertEqual(self.client(srv).req("POST", "/api/manifest/approve", {})[0], 401)
        self.assertEqual(c.req("POST", "/api/manifest/approve", {}, csrf=False)[0], 403)
        status, data, _ = c.post("/api/manifest/approve", {})
        self.assertEqual(status, 200, data)
        self.assertEqual(data["hold"]["changes"], [])
        self.sweep()
        self.assertEqual(self.names("R033", "drafts"), ["t-a.json"])
        self.assertEqual(self.status()["runner_state"], "ready")

    def test_added_and_removed_sop_files_are_detected(self):
        self._baseline()
        sop = os.path.join(self.ws, "_sops", "R042", "extra.txt")
        os.makedirs(os.path.dirname(sop))
        with open(sop, "w", encoding="utf-8") as f:
            f.write("Approved steps.\n")
        self.add_task("R042", "t-s")
        self.add_task("R033", "t-other")
        self.sweep()
        self.assertEqual(self.names("R042", "inbox"), ["t-s.json"])
        self.assertEqual(self.names("R033", "drafts"), ["t-other.json"])
        srv, c = self.logged_in()
        status, state, _ = c.get("/api/state")
        self.assertEqual(status, 200)
        added = next(ch for ch in state["hold"]["changes"] if ch["name"].endswith("extra.txt"))
        self.assertEqual(added["change"], "added")
        self.assertIsNone(added["old_hash"])
        self.assertTrue(added["new_hash"])
        self.assertIsNotNone(added["size"])
        self.assertEqual(c.post("/api/manifest/approve", {})[0], 200)
        os.remove(sop)
        self.sweep()
        self.assertEqual(self.names("R042", "inbox"), ["t-s.json"])
        removed = next(ch for ch in c.get("/api/state")[1]["hold"]["changes"] if ch["name"].endswith("extra.txt"))
        self.assertEqual(removed["change"], "removed")
        self.assertIsNone(removed["new_hash"])
        self.assertTrue(removed["old_hash"])

    def test_symlink_in_inbox_is_quarantined_without_touching_the_target(self):
        target = os.path.join(self.tmp, "secret.txt")
        with open(target, "w", encoding="utf-8") as f:
            f.write("do-not-touch-target")
        link = os.path.join(self.folder("R033", "inbox"), "t-link.json")
        try:
            os.symlink(target, link)
        except (OSError, NotImplementedError, AttributeError) as e:
            self.skipTest("symlink creation is not permitted on this platform: %s" % e)
        self.sweep()
        self.assertEqual(Path(target).read_text(encoding="utf-8"), "do-not-touch-target")
        self.assertFalse(os.path.lexists(link))
        events = self.log_events("input_quarantined")
        self.assertTrue(events)
        self.assertEqual(events[0]["kind"], "symlink")
        blob = json.dumps(events)
        self.assertNotIn("do-not-touch-target", blob)
        self.assertNotIn("secret.txt", blob)

    def test_oversize_and_task_id_mismatch_are_rejected(self):
        big = self.add_task("R033", "t-big", "short")
        with open(big, "wb") as f:
            f.write(b"{" + b"X" * (64 * 1024))
        os.utime(big, (1, 1))
        self.add_task("R033", "t-long", "A" * 8001)
        self.add_task("R033", "t-oklen", "B" * 8000)
        self.add_task("R033", "t-file")
        mismatch = os.path.join(self.folder("R033", "inbox"), "t-file.json")
        with open(mismatch, encoding="utf-8") as f:
            data = json.load(f)
        data["task_id"] = "t-other"
        with open(mismatch, "w", encoding="utf-8") as f:
            json.dump(data, f)
        os.utime(mismatch, (1, 1))
        self.add_task("R034", "t-path", **{"folder": "..\\..\\outside"})
        self.sweep()
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "outside")))
        self.assertEqual(self.names("R033", "inbox"), [])
        with open(os.path.join(self.folder("R033", "drafts"), "t-big.json"), encoding="utf-8") as f:
            big_draft = json.load(f)
        self.assertEqual(big_draft["status"], "rejected_input")
        self.assertIn("64 KB", big_draft["rejection_note"])
        self.assertNotIn("XXX", json.dumps(big_draft))
        with open(os.path.join(self.folder("R033", "drafts"), "t-long.json"), encoding="utf-8") as f:
            long_draft = json.load(f)
        self.assertIn("8,000", long_draft["rejection_note"])
        self.assertEqual(long_draft["instructions"], "[REMOVED]")
        self.assertNotIn("AAA", json.dumps(long_draft))
        self.assertIn("t-file.rejected.json", self.names("R033", "archive"))
        self.assertNotIn("t-file.json", self.names("R033", "drafts"))
        self.assertIn("t-oklen.json", self.names("R033", "drafts"))
        self.assertEqual(self.names("R034", "drafts"), ["t-path.json"])
        reasons = {e["reason"] for e in self.log_events("input_rejected")}
        self.assertIn("too_large", reasons)
        self.assertIn("instructions_too_long", reasons)

    def test_public_status_has_state_and_no_secrets(self):
        self.save_cfg(provider="anthropic", api_key=FAKE_KEY)
        calls = self.mock_anthropic()
        self.add_task("R033", "t-1", "Please draft a note about Monday hours.")
        self.sweep()
        self.assertTrue(calls)
        st = self.status()
        self.assertIn(st["runner_state"], ("ready", "first_run", "locked", "paused_for_review"))
        self.assertIn("message", st)
        blob = json.dumps(st)
        for needle in (FAKE_KEY, PIN, self.tmp, "config.json", "Monday hours", "sk-"):
            self.assertNotIn(needle, blob)

    def test_safety_check_items_and_graceful_degrade(self):
        self.save_cfg()
        items = {i["id"]: i for i in admin.build_safety(self.ws)}
        self.assertEqual(items["app_outside_workspace"]["status"], "pass")
        self.assertEqual(items["app_outside_workspace"]["level"], "hard")
        self.assertEqual(items["settings_outside_workspace"]["status"], "pass")
        self.assertEqual(items["config_permissions"]["status"], "pass")
        self.assertEqual(items["manual_staff_account"]["status"], "manual")
        self.assertEqual(items["manual_staff_account"]["text"],
                         "Log in as a staff account and confirm you cannot edit _agents or the app folder.")
        self.assertEqual(items["perm_dashboard"]["status"], "unknown")
        self.assertIn("could not verify", items["perm_dashboard"]["text"].lower())
        config.set_app_dir(self.ws)
        self.assertEqual({i["id"]: i for i in admin.build_safety(self.ws)}["app_outside_workspace"]["status"], "fail")
        config.set_app_dir(None)
        warned = admin.build_safety(self.ws, platform="nt", icacls=lambda path: "Everyone:(OI)(CI)(F)\n")
        agents_item = next(i for i in warned if i["id"] == "perm_agents")
        self.assertEqual(agents_item["status"], "warn")
        self.assertEqual(agents_item["level"], "advisory")
        self.assertIn("Everyone", agents_item["text"])
        unknown = admin.build_safety(self.ws, platform="nt", icacls=lambda path: (_ for _ in ()).throw(OSError("no icacls")))
        advisory = [i for i in unknown if i["id"] in ("perm_agents", "perm_sops", "perm_app")]
        self.assertTrue(all(i["status"] == "unknown" for i in advisory))
        self.assertTrue(all("could not verify" in i["text"].lower() for i in advisory))
        broken = admin.build_safety(self.ws, platform="posix", stat_mode=lambda path: (_ for _ in ()).throw(OSError("no stat")))
        self.assertEqual(next(i for i in broken if i["id"] == "perm_sops")["status"], "unknown")
        open_mode = admin.build_safety(self.ws, platform="posix", stat_mode=lambda path: 0o100777)
        self.assertEqual(next(i for i in open_mode if i["id"] == "perm_app")["status"], "warn")
        srv, c = self.logged_in()
        status, data, _ = c.get("/api/safety")
        self.assertEqual(status, 200)
        ids = {i["id"] for i in data["items"]}
        for needed in ("app_outside_workspace", "settings_outside_workspace", "config_permissions",
                       "perm_agents", "perm_sops", "perm_app", "perm_dashboard", "manual_staff_account"):
            self.assertIn(needed, ids)
        self.assertEqual(self.client(srv).req("GET", "/api/safety")[0], 401)
        self.assertIn("Approve current version", admin.PAGE)
        self.assertIn("Safety check", admin.PAGE)
        self.assertIn("Could not verify", admin.PAGE)

    def test_init_workspace_copies_the_template_only(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location("init_workspace", str(PROJECT / "init-workspace.py"))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        dest = os.path.join(self.tmp, "fresh-ws")
        mod.create_workspace(dest)
        self.assertEqual(len(list(Path(dest, "_agents").glob("R*.json"))), 7)
        self.assertTrue(os.path.isfile(os.path.join(dest, "_agents", "_decision_rights.json")))
        self.assertTrue(os.path.isfile(os.path.join(dest, "R033-digital-marketing", "inbox", ".keep")))
        self.assertTrue(os.path.isfile(os.path.join(dest, "_sops", "R033", "PUT-APPROVED-SOP-TEXT-HERE.txt")))
        self.assertFalse(os.path.exists(os.path.join(dest, "runner.py")))
        self.assertFalse(os.path.exists(os.path.join(dest, "dashboard.html")))
        with self.assertRaises(SystemExit):
            mod.create_workspace(PROJECT / "nested-ws")
        self.assertFalse((PROJECT / "nested-ws").exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
