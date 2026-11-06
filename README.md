# Office AI Workspace (prototype)

A shared folder is the only connection between the staff dashboard and the AI agents.

```
dashboard.html  --writes-->  Rxxx-role/inbox/      (task files)
runner.py       --reads-->   inbox/  --writes-->   drafts/
dashboard.html  --review-->  drafts/ --approve-->  approved/
```

Nothing is sent or published by an agent. A person approves every draft.

## Set up (10 minutes)

1. Put this whole `AI-Workspace` folder where staff and the runner machine can both reach it (a network drive, or a synced folder).
2. On ONE office computer with Python 3.9+ installed, double-click **`start-runner.bat`** (Windows) or **`start-runner.command`** (Mac), or open a terminal in the folder and run:
   `python3 runner.py --root . `
   (add `--once` to process the inbox a single time and exit). It uses a **stub** model until you choose a real provider on the Admin page, so you can test the workflow with no AI and no cost.
3. On any staff computer, open `dashboard.html` in **Chrome or Edge**, click **Choose workspace folder**, and pick the `AI-Workspace` folder. Type your name, make a request, and watch the draft appear under **Review drafts**.

## Using a real model

The recommended way is the **Admin page** (next section): choose *Anthropic*, paste your key, press *Test connection*. The runner notices the change on its next check; no restart needed.

Advanced alternative: set an `ANTHROPIC_API_KEY` environment variable on the runner computer (never in the dashboard or the shared folder). **It still works and takes precedence over a key saved on the Admin page**, and the Admin page tells you which one is active. `AI_MODEL` still works as the last-resort model name (used only if no default model is set on the Admin page). You can also force a provider for one run with `python3 runner.py --root . --backend anthropic`.

This sends request text to an outside provider. Use it only for non-PHI work, or only under a signed BAA with the provider's HIPAA-eligible configuration. Add other backends (Bedrock, Vertex, a local model) by adding a function to `BACKENDS` in `runner.py`. A backend is a function `(system, user, task, agent, model=None, api_key=None)` that returns `(text, usage)`, where `usage = {model, input_tokens, output_tokens, latency_ms}`.

## Admin page

A small page for the practice owner, used **on the runner computer only**. Staff cannot reach it, and it never appears in the shared folder's staff dashboard.

**First-time setup**

1. On the runner computer, double-click **`start-admin.bat`** (Windows) or **`start-admin.command`** (Mac). Or run `python3 admin.py --root <workspace folder>`.
2. The first time, it asks you in the terminal to choose a **PIN** (at least 6 digits, numbers only). To change or reset it later, run `python3 admin.py --root <workspace folder> --set-pin`, or use *Change PIN* on the Connection tab.
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

**Where the key is stored** — in `config.json` inside a `.office-ai` folder in the runner computer's own user folder (for example `C:\Users\<you>\.office-ai\` or `~/.office-ai/`). It is created readable only by that user (on Windows the page tells you if that lock-down failed). The key is never written to the shared folder, never logged, and the browser only ever sees it masked (`sk-...abcd`). The admin page refuses to use a settings folder that is inside the workspace.

> **Rule: the API key must never be placed in the shared folder, and never in a `.env` file inside it.** Anyone who can open the shared folder could read it. The admin page warns you if it finds a `.env` file in the workspace.

**Staff view:** the dashboard reads `_agents/_public_status.json`, which the runner rewrites after each check with only `{updated, seats: {R0xx: {enabled, last_draft_at}}}`. It contains no key, model, or cost. If it is older than 10 minutes the dashboard shows "The AI runner may be offline". Editing that file does **not** un-pause a seat; the runner decides from its own private settings.

**Launchers and the shared folder:** in this prototype the code (`runner.py`, `admin.py`, `config.py`) and the launchers sit in the same folder as the shared workspace, so staff can see the program files (but never any secret). For a tidier setup, keep the program files in a separate folder on the runner computer and point the `WORKSPACE=` line at the top of each launcher to the shared folder. The settings folder is always the runner computer's own `~/.office-ai`, never in either place. On a Mac, the first time you may need to right-click the `.command` file and choose Open (or run `chmod +x start-*.command`).

## Run the tests

`python tests/test_acceptance.py` (or `python -m unittest discover -s tests -v`). No real API calls are made. One test needs Node.js to run the dashboard's script and is skipped if Node is not installed.

## Where the agent definitions come from

`_agents/R0xx.json` were generated from `Role_and_SOP_Master.xlsx`:

- `from_workbook`: purpose, decision authority, KPIs, duties, and the SOPs on record with their status (copied as listed).
- `authored_for_prototype`: task types, the "must not" rules, and review-flag phrases. **These were written for this prototype, are not in your workbook, and need your review.**
- `_agents/_decision_rights.json`: your Decision Rights sheet, given to every agent as escalation rules.

To give an agent real procedures, put plain-text copies of the **validated** SOPs in `_sops/<role id>/`. The runner includes them as reference material. Until then the agent says no approved SOP text is available.

## Safeguards built in

- Dashboard has a Content-Security-Policy that blocks all network access (view the first lines of the file).
- Privacy tripwire in both the dashboard and the runner blocks requests with SSN-like numbers, phone numbers, DOB/MRN/member-ID words, and full dates. Flagged text is redacted from every file the runner writes.
- The runner logs metadata only (time, role, task id, model, token counts, response time, and an estimated cost if prices are set), never request or draft text.
- The API key and PIN hash live outside the shared folder; the admin page listens on `127.0.0.1` only and needs a PIN (see **Admin page**).
- Task files cannot choose their own folder: the role id must match the agent and ids are validated.
- R038 drafts are flagged if they mention appointments, visits, exams, or prescriptions.

## Limits to know

- The tripwire is a safety net, not a guarantee. It cannot catch a name written on its own. Staff training matters more.
- Folder permissions are the real security boundary. Give each person access only to the roles they need, and run the runner under its own account.
- If the folder syncs through a cloud service, it is no longer purely local.
- Chrome or Edge only (folder access). Safari and Firefox are not supported.
- Approved items are not posted anywhere automatically; staff copy the text out.
