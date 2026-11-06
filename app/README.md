# Office AI Workspace

Three folders. They must not be the same folder, and none of them may sit inside another.

| Folder | Who can reach it | What is in it |
| --- | --- | --- |
| **Program folder** (`app/`) | The runner computer only. The account that runs the AI. | `runner.py`, `admin.py`, `config.py`, the launchers, tests, this README. |
| **Dashboard folder** (`dashboard/`) | Staff can open the file. They must not be able to edit it. | `dashboard.html` only. It asks each person to pick the shared workspace at runtime, so it does not live inside the workspace. |
| **Workspace** (from `workspace-template/`) | The shared data folder. | Role folders (`inbox/`, `drafts/`, `approved/`, `archive/`), `_agents/`, `_sops/`, `_logs/`, and `_agents/_public_status.json`. No program code, no settings, no secrets. |

```
dashboard.html  --writes-->  Rxxx-role/inbox/      (task files)
runner.py       --reads-->   inbox/  --writes-->   drafts/
dashboard.html  --review-->  drafts/ --approve-->  approved/
```

Nothing is sent or published by an agent. A person approves every draft.

## Set up

1. On the runner computer, keep the **program folder** somewhere only that computer's runner account can read. Do not put it on the shared drive.
2. Create the shared workspace (this does not copy the program or the dashboard):

   `python init-workspace.py --dest D:\OfficeShare\AI-Workspace`

   Or copy the `workspace-template` folder by hand. You get the seven `_agents` configs, empty role folders (each with a `.keep` file), and `_sops` placeholders.
3. Edit the `WORKSPACE=` line at the top of `start-runner.bat` / `start-runner.command` and the matching admin launcher so it points at that shared folder. The launchers run the scripts from their own folder. Leave the launchers in the program folder.
4. On the runner computer (Python 3.9+), double-click **`start-runner.bat`** (Windows) or **`start-runner.command`** (Mac). Until you choose a real provider on the Admin page, it uses a **stub** model, so you can try the workflow with no AI and no cost. The first time, when no settings folder exists yet, only the stub is allowed.
5. Put `dashboard/dashboard.html` where staff can open it but not edit it. On a staff computer, open it in **Chrome or Edge**, click **Choose workspace folder**, and pick the shared workspace (the folder that contains `_agents` and the role folders).

The runner refuses to start if the program folder and the workspace are the same folder or one is inside the other. That is deliberate.

## Folder permissions

Folder permissions are the real boundary. The checks in the program notice tampering; they are not a substitute for permissions. After you set these, log in as a staff account and confirm you cannot edit `_agents` or the program folder.

| Location | Runner account | Owner / admin | Staff |
| --- | --- | --- | --- |
| Program folder (`app/`) | Read and write | No access (or only the runner account) | No access |
| Dashboard folder | Read | Write | Read only |
| Workspace `_agents/` and `_sops/` | Read and write | Write | Read only |
| Workspace `_logs/` and `_agents/_public_status.json` | Write | Read | Read only, or no access. The dashboard only needs `_public_status.json` readable. |
| Each role's `inbox/`, `drafts/`, `approved/`, `archive/` | Read and write | Read and write | Read and write (submit, review, approve, discard, revise) |

**Windows network share.** Right-click the folder, Properties, Security, and edit the access list. Or from a command prompt (change the names):

```
icacls "D:\OfficeAI\app" /inheritance:r /grant:r "RUNNER-PC\Runner:(OI)(CI)F"
icacls "D:\OfficeShare\dashboard" /inheritance:r /grant:r "DOMAIN\Owner:(OI)(CI)M" "DOMAIN\Staff:(OI)(CI)RX"
icacls "D:\OfficeShare\AI-Workspace\_agents" /inheritance:r /grant:r "RUNNER-PC\Runner:(OI)(CI)M" "DOMAIN\Staff:(OI)(CI)RX"
icacls "D:\OfficeShare\AI-Workspace\R033-digital-marketing" /grant:r "DOMAIN\Staff:(OI)(CI)M"
```

`(RX)` is read. `(M)` is change. Do not grant Everyone, Users, or Authenticated Users change rights on the program folder, `_agents`, or `_sops`.

**Synced folder (OneDrive, Google Drive, Dropbox, and similar).** Give staff the viewer role on the program folder, the dashboard file, `_agents`, and `_sops`. Give them the editor role only on the role folders they work in. Permissions on a synced folder are enforced by the sync service, not by the file system on each PC. Test them by signing in as a staff account on another computer; do not trust a permission screen you have not tried.

The Admin page has a **Safety check** tab that reports the hard checks (program folder outside the workspace, settings folder outside the workspace, settings file locked down) and, as a best effort, whether those folders look writable by everyone. It does not change permissions. If it says "could not verify", check them yourself.

## Using a real model

The recommended way is the **Admin page**: choose *Anthropic*, paste your key, press *Test connection*. The runner notices the change on its next check; no restart needed.

Advanced alternative: set an `ANTHROPIC_API_KEY` environment variable on the runner computer (never in the dashboard or the shared folder). **It still works and takes precedence over a key saved on the Admin page**, and the Admin page tells you which one is active. `AI_MODEL` still works as the last-resort model name (used only if no default model is set on the Admin page). You can also force a provider for one run with `python runner.py --root <workspace> --backend anthropic`.

This sends request text to an outside provider. Use it only for non-PHI work, or only under a signed BAA with the provider's HIPAA-eligible configuration. Add other backends (Bedrock, Vertex, a local model) by adding a function to `BACKENDS` in `runner.py`. A backend is a function `(system, user, task, agent, model=None, api_key=None)` that returns `(text, usage)`, where `usage = {model, input_tokens, output_tokens, latency_ms}`.

## Admin page

A small page for the practice owner, used **on the runner computer only**. Staff cannot reach it, and it never appears in the shared folder.

**First-time setup**

1. On the runner computer, double-click **`start-admin.bat`** (Windows) or **`start-admin.command`** (Mac). Or run `python admin.py --root <workspace>`.
2. The first time, it asks you in the terminal to choose a **PIN** (at least 6 digits, numbers only). To change or reset it later, run `python admin.py --root <workspace> --set-pin`, or use *Change PIN* on the Connection tab.
3. Your browser opens to `http://127.0.0.1:8787/`. (If it does not, type that address in.) Enter the PIN.
4. **Connection** tab: pick *Anthropic*, paste the API key into *Replace key*, optionally type a default model name, press **Save**, then **Test connection**. You should see a green "Connected" message. A red message tells you in plain words what is wrong (bad key, no internet, rate limit).
5. Leave the runner running (`start-runner`). Changes apply on its next check.

Closing the admin window turns the admin page off; the runner keeps working with the saved settings.

**Pause or resume a seat** — *Seats* tab: flip the switch. A paused seat's requests stay waiting in its inbox (nothing is deleted or lost), the seat is shaded and marked PAUSED, and staff see a "paused" notice and cannot send it new requests. Flip it back on and the waiting requests are processed. Typing a model name on a seat makes that seat use it; leave it blank to use the default.

**Test a model** — *Test models* tab: type **fake, non-patient text only**, choose one or two model names to compare side by side. The privacy check runs first and nothing is sent if it finds personal information. Limited to 4,000 characters and a few calls per minute.

**Read the Usage tab** — pick 7, 30 or 90 days.
- *Requests* counts distinct tasks (a revision counts as a new request). *Drafts*, *privacy blocks* and *model errors* are counted from the log.
- *Approved / revised / discarded* come from the role folders. **Approval rate = approved ÷ (approved + revised + discarded).** A discard is recorded by the staff dashboard as a small file in the role's `archive/` folder (`<task id>.discarded.json`, no request or draft text).
- *Estimated spend* only shows a number once you enter prices (*Model prices* at the bottom of the Usage tab). No prices are built in because they change: confirm them on your provider's pricing page and type dollars per million input and output tokens. Until then it says "set prices to estimate cost". These are estimates, not your bill.
- *Not used recently* lists seats with no requests in the period. *Avg time to draft* is how long the model took to answer.

**Changes** — the runner keeps SHA-256 hashes of `_agents/*.json` (except the status file it writes itself), of the files under `_sops/<role>/`, and of `dashboard.html` if you set its path here. The hashes live in `manifest.json` inside the settings folder, not in the workspace. The first time, the runner saves a baseline and logs that it did. After that, a changed seat file or SOP pauses that seat with the reason "configuration changed, owner review needed". Tasks stay queued. A change to `_decision_rights.json` or to the dashboard file pauses every seat. The list shows the file name, size, modified time, and the old and new hash prefixes. **Approve current version** (PIN required, and the page asks you to confirm) saves a new baseline. Seats resume on the next check. Adding or removing an SOP file is a change; approving it is the normal step after you edit one.

**Safety check** — read-only pass / warn / fail notes. See **Folder permissions**. It never changes permissions for you.

**Where the key is stored** — in `config.json` inside a `.office-ai` folder in the runner computer's own user folder (for example `C:\Users\<you>\.office-ai\` or `~/.office-ai/`). It is created readable only by that user (on Windows the page tells you if that lock-down failed). The key is never written to the shared folder, never logged, and the browser only ever sees it masked (`sk-...abcd`). The admin page refuses to use a settings folder that is inside the workspace.

If the settings folder exists but `config.json` is missing, unreadable, or not valid JSON, the runner processes nothing and leaves every task queued. It keeps running and tries again each check, so it picks up when you fix the file. If a good config was already loaded in this run and the file then becomes unreadable, the runner keeps using those last good settings and says so on the Admin page. The very first run (no `.office-ai` folder at all) is the exception: the stub backend still works so you can try the folders with no AI. A real provider is refused until setup is finished.

A banner on the Admin page says "using last good settings" when that fallback is active. Staff never see the reason. Their dashboard only shows: "AI runner is paused. Please tell the office manager." The New request button is disabled while the runner is locked or waiting for you to approve a change.

> **Rule: the API key must never be placed in the shared folder, and never in a `.env` file inside it.** Anyone who can open the shared folder could read it. The admin page warns you if it finds a `.env` file in the workspace.

**Staff view:** the dashboard reads `_agents/_public_status.json`, which the runner rewrites after each check with `runner_state`, a short `message`, and `seats` (`enabled`, `last_draft_at` only). It contains no key, model, cost, or file path. If it is older than 10 minutes the dashboard shows "The AI runner may be offline". Editing that file does **not** un-pause a seat; the runner decides from its own private settings.

On a Mac, the first time you may need to right-click the `.command` file and choose Open (or run `chmod +x start-*.command`).

## Run the tests

From the program folder:

`python tests/test_acceptance.py`

or, from the repository root:

`python app/tests/test_acceptance.py`

(or `python -m unittest discover -s app/tests -v`). No real API calls are made. One test needs Node.js to run the dashboard's script and is skipped if Node is not installed. A symlink test is skipped, with a message, on computers that do not allow creating symlinks.

## Where the agent definitions come from

`_agents/R0xx.json` were generated from `Role_and_SOP_Master.xlsx`:

- `from_workbook`: purpose, decision authority, KPIs, duties, and the SOPs on record with their status (copied as listed).
- `authored_for_prototype`: task types, the "must not" rules, and review-flag phrases. **These were written for this prototype, are not in your workbook, and need your review.**
- `_agents/_decision_rights.json`: your Decision Rights sheet, given to every agent as escalation rules.

To give an agent real procedures, put plain-text copies of the **validated** SOPs in `_sops/<role id>/`. The runner includes them as reference material. Until then the agent says no approved SOP text is available. After you add or edit one, approve it on the Admin page **Changes** tab or the seat stays paused.

## Safeguards built in

- The program folder must be outside the shared workspace or the runner and the admin page will not start.
- Dashboard has a Content-Security-Policy that blocks all network access (view the first lines of the file). A changed dashboard file is noticed if its path is set on the Admin page.
- Privacy tripwire in both the dashboard and the runner blocks requests with SSN-like numbers, phone numbers, DOB/MRN/member-ID words, and full dates. Flagged text is redacted from every file the runner writes.
- Inbox entries that are not real files (symlinks, folders, devices) are quarantined. A symlink is removed without opening its target. Files larger than 64 KB, and requests longer than 8,000 characters, are rejected with a short note that does not copy the contents. The file name must match the task id inside the file.
- The runner logs metadata only (time, role, task id, model, token counts, response time, and an estimated cost if prices are set), never request or draft text.
- The API key, PIN hash, and integrity manifest live outside the shared folder. The admin page listens on `127.0.0.1` only and needs a PIN.
- Task files cannot choose their own folder: the role id must match the agent, and paths are built only from the checked task id and the role folder named in the agent config.
- R038 drafts are flagged if they mention appointments, visits, exams, or prescriptions.
- Pausing is fail-closed. A settings problem does not silently turn every seat back on.

## Limits to know

- The tripwire is a safety net, not a guarantee. It cannot catch a name written on its own. Staff training matters more.
- Folder permissions are the real security boundary. The manifest only detects changes; it does not undo them.
- If the folder syncs through a cloud service, it is no longer purely local. Test permissions as a staff user.
- Chrome or Edge only (folder access). Safari and Firefox are not supported.
- Approved items are not posted anywhere automatically; staff copy the text out.
