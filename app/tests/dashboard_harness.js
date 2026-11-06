// Runs the REAL script from dashboard.html in Node against a tiny fake DOM and an in-memory
// "File System Access" folder, then prints a JSON report. Used by test_acceptance.py (test 9).
// usage: node dashboard_harness.js <path-to-dashboard.html> <path-to-_agents-folder>
'use strict';
const fs = require('fs'), path = require('path'), vm = require('vm');
const html = fs.readFileSync(process.argv[2], 'utf8');
const script = html.match(/<script>([\s\S]*?)<\/script>/)[1];
const agentsDir = process.argv[3];

// ---------- fake DOM ----------
class Ev { constructor(type) { this.type = type; } }
class FNode {
  constructor(tag) { this.nodeType = 1; this.tag = tag; this.children = []; this.attrs = {}; this.listeners = {};
    this.className = ''; this._text = ''; this.dataset = {}; this.id = ''; this.disabled = false; this._value = undefined;
    const cls = new Set(); this.classList = { add: c => cls.add(c), remove: c => cls.delete(c), contains: c => cls.has(c) }; }
  append(...kids) { for (const k of kids) this.children.push(k); }
  replaceChildren(...kids) { this.children = []; this.append(...kids); }
  setAttribute(k, v) { this.attrs[k] = v; if (k === 'id') this.id = v; if (k === 'value') this._value = v; }
  addEventListener(t, f) { (this.listeners[t] = this.listeners[t] || []).push(f); }
  dispatchEvent(ev) { for (const f of this.listeners[ev.type] || []) f(ev); }
  get options() { return this.children.filter(c => c.tag === 'option'); }
  get value() { if (this.tag === 'select' && this._value === undefined) { const o = this.options[0]; return o ? o.attrs.value : ''; } return this._value === undefined ? '' : this._value; }
  set value(v) { this._value = v; }
  get textContent() { return this._text + this.children.map(c => c.textContent).join(''); }
  set textContent(v) { this._text = String(v); this.children = []; }
  select() {}
  find(pred) { if (pred(this)) return this; for (const c of this.children) { if (c.find) { const r = c.find(pred); if (r) return r; } } return null; }
  findAll(pred, out = []) { if (pred(this)) out.push(this); for (const c of this.children) if (c.findAll) c.findAll(pred, out); return out; }
  querySelector(sel) { const id = sel.replace(/^#/, ''); return this.find(n => n.id === id); }
}
function makeDocument() {
  const statics = {};
  for (const id of ['unsupported', 'banner', 'notices', 'welcome', 'app', 'tabs', 'view', 'who', 'pick']) { const n = new FNode('div'); n.id = id; statics[id] = n; }
  return { statics,
    getElementById: id => statics[id] || Object.values(statics).map(r => r.find(n => n.id === id)).find(Boolean) || null,
    createElement: t => new FNode(t),
    createTextNode: s => ({ nodeType: 3, textContent: String(s) }),
    activeElement: null };
}

// ---------- fake File System Access folder ----------
const opLog = [];
class FakeFile { constructor(name) { this.kind = 'file'; this.name = name; this.text = ''; }
  async getFile() { const t = this.text; return { text: async () => t }; }
  async createWritable() { let buf = ''; const self = this; return { write: async s => { buf += s; }, close: async () => { self.text = buf; } }; } }
class FakeDir { constructor(name) { this.kind = 'directory'; this.name = name; this.map = new Map(); }
  async getDirectoryHandle(n, o) { let e = this.map.get(n); if (!e) { if (!(o && o.create)) throw new Error('NotFound'); e = new FakeDir(n); this.map.set(n, e); } return e; }
  async getFileHandle(n, o) { let e = this.map.get(n); if (!e) { if (!(o && o.create)) throw new Error('NotFound'); e = new FakeFile(n); this.map.set(n, e); } return e; }
  async removeEntry(n) { if (!this.map.has(n)) throw new Error('NotFound'); this.map.delete(n); opLog.push('remove:' + this.name + '/' + n); }
  async *values() { for (const e of this.map.values()) yield e; } }
// wrap writes so we can see ordering
const origCW = FakeFile.prototype.createWritable;
FakeFile.prototype.createWritable = async function () { const w = await origCW.call(this); const c = w.close; w.close = async () => { await c(); opLog.push('write:' + this.name); }; return w; };

async function put(dir, rel, text) { const parts = rel.split('/'); let d = dir; for (const p of parts.slice(0, -1)) d = await d.getDirectoryHandle(p, { create: true });
  const f = await d.getFileHandle(parts[parts.length - 1], { create: true }); f.text = text; }
async function get(dir, rel) { let d = dir; const parts = rel.split('/'); for (const p of parts.slice(0, -1)) d = await d.getDirectoryHandle(p);
  return (await d.getFileHandle(parts[parts.length - 1])).text; }
function listNames(dir, rel) { let d = dir; for (const p of rel.split('/')) { d = d.map.get(p); if (!d) return []; } return [...d.map.keys()]; }

async function buildWorkspace(status) {
  const root = new FakeDir('AI-Workspace');
  for (const fn of fs.readdirSync(agentsDir)) {
    const text = fs.readFileSync(path.join(agentsDir, fn), 'utf8');
    await put(root, '_agents/' + fn, text);
    if (/^R\d{3}\.json$/.test(fn)) { const a = JSON.parse(text); for (const s of ['inbox', 'drafts', 'approved', 'archive']) await root.getDirectoryHandle(a.folder, { create: true }).then(d => d.getDirectoryHandle(s, { create: true })); }
  }
  if (status) await put(root, '_agents/_public_status.json', JSON.stringify(status));
  return root;
}

async function boot(status, confirmAnswer = true) {
  const root = await buildWorkspace(status);
  const document = makeDocument();
  const ctx = { document, navigator: {}, confirm: () => confirmAnswer, setInterval: () => 0, clearInterval: () => {}, setTimeout: () => 0,
    Event: Ev, queueMicrotask, console, Date, JSON, Math, Object, Array, String, Number, Promise, Set, RegExp, Error, isNaN, parseInt };
  ctx.window = ctx; ctx.showDirectoryPicker = async () => root;
  vm.createContext(ctx); vm.runInContext(script, ctx);
  document.statics.who.value = 'Tester';
  await ctx.window.__ws.pick();
  await new Promise(r => setTimeout(r, 0));
  return { ctx, root, document, ws: ctx.window.__ws };
}

function formState(document) {
  const sel = document.getElementById('f-role'), send = document.getElementById('f-send');
  return { options: sel ? sel.options.map(o => ({ value: o.attrs.value, text: o.textContent, disabled: !!o.disabled })) : null,
           selected: sel ? sel.value : null, sendDisabled: send ? send.disabled : null,
           notices: document.statics.notices.textContent };
}

(async () => {
  const report = {};
  const iso = ms => new Date(Date.now() - ms).toISOString();
  const seats = rids => Object.fromEntries(rids.map(r => [r.id, { enabled: r.enabled, last_draft_at: null }]));
  const all = fs.readdirSync(agentsDir).filter(f => /^R\d{3}\.json$/.test(f)).map(f => f.slice(0, -5));

  // A. fresh status, R033 paused
  { const e = await boot({ updated: iso(30 * 1000), seats: seats(all.map(r => ({ id: r, enabled: r !== 'R033' }))) });
    report.paused = formState(e.document); }
  // A2. everything paused -> send disabled
  { const e = await boot({ updated: iso(1000), seats: seats(all.map(r => ({ id: r, enabled: false }))) });
    report.allPaused = formState(e.document); }
  // B. stale status
  { const e = await boot({ updated: iso(11 * 60 * 1000), seats: seats(all.map(r => ({ id: r, enabled: true }))) });
    report.stale = formState(e.document); }
  // C. no status file at all
  { const e = await boot(null); report.none = formState(e.document); }
  // E. runner locked or waiting on the owner: banner, and New request cannot be sent
  const holdMsg = 'AI runner is paused. Please tell the office manager.';
  { const e = await boot({ updated: iso(1000), runner_state: 'locked', message: holdMsg,
      seats: seats(all.map(r => ({ id: r, enabled: true }))) });
    report.locked = formState(e.document); }
  { const e = await boot({ updated: iso(1000), runner_state: 'paused_for_review', message: holdMsg,
      seats: seats(all.map(r => ({ id: r, enabled: true }))) });
    report.reviewHold = formState(e.document); }

  // D. discard writes a metadata-only marker, then deletes the draft
  { const e = await boot(null);
    const draftName = 't-20261106-101500-abc.json';
    const folder = JSON.parse(fs.readFileSync(path.join(agentsDir, 'R034.json'), 'utf8')).folder;
    await put(e.root, folder + '/drafts/' + draftName, JSON.stringify({ task_id: 't-20261106-101500-abc', role_id: 'R034', task_type: 'social_post',
      requester: 'Pat', instructions: 'SECRET REQUEST TEXT', draft: 'SECRET DRAFT TEXT', status: 'pending_review', created_draft: iso(1000) }));
    e.ws.S.tab = 'review'; await e.ws.refresh();
    const btn = e.document.statics.view.find(n => n.tag === 'button' && n.textContent === 'Discard');
    report.discardButtonFound = !!btn;
    opLog.length = 0;
    await btn.listeners.click[0](); await new Promise(r => setTimeout(r, 0));
    report.archiveFiles = listNames(e.root, folder + '/archive');
    report.draftsFiles = listNames(e.root, folder + '/drafts');
    const markerName = 't-20261106-101500-abc.discarded.json';
    report.markerText = report.archiveFiles.includes(markerName) ? await get(e.root, folder + '/archive/' + markerName) : null;
    report.ops = opLog.slice();
  }
  // D2. cancelled confirmation changes nothing
  { const e = await boot(null, false);
    const folder = JSON.parse(fs.readFileSync(path.join(agentsDir, 'R034.json'), 'utf8')).folder;
    await put(e.root, folder + '/drafts/t-x.json', JSON.stringify({ task_id: 't-x', role_id: 'R034', draft: 'D', status: 'pending_review' }));
    e.ws.S.tab = 'review'; await e.ws.refresh();
    const btn = e.document.statics.view.find(n => n.tag === 'button' && n.textContent === 'Discard');
    await btn.listeners.click[0]();
    report.cancel = { archive: listNames(e.root, folder + '/archive'), drafts: listNames(e.root, folder + '/drafts') };
  }
  process.stdout.write(JSON.stringify(report));
})().catch(err => { console.error(err && err.stack || err); process.exit(1); });
