// Ferry web app. Two docks (A and B), files cross between them.
// Plain modules, no build step.

const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const icon = (n, cls = "") => `<svg class="i ${cls}" aria-hidden="true"><use href="#i-${n}"/></svg>`;
const DOCK = ["A", "B"];
const MOBILE = matchMedia("(max-width: 860px)");
const PREVIEW_KINDS = new Set(["image", "raw", "video", "audio", "text", "pdf", "subtitle"]);
const BROWSER_IMG = /\.(jpe?g|png|gif|webp|bmp|avif|svg)$/i;

// ---------------------------------------------------------------- api
class ApiError extends Error {
  constructor(msg, status) { super(msg); this.status = status; }
}

function qs(q = {}) {
  const p = new URLSearchParams();
  for (const [k, v] of Object.entries(q)) {
    if (v === undefined || v === null) continue;
    if (Array.isArray(v)) v.forEach((x) => p.append(k, x));
    else p.append(k, v);
  }
  const s = p.toString();
  return s ? `?${s}` : "";
}

async function api(method, url, body, q) {
  const opts = { method, headers: { "X-Ferry": "1" }, credentials: "same-origin" };
  if (body !== undefined) {
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(body);
  }
  let r;
  try {
    r = await fetch(url + qs(q), opts);
  } catch (e) {
    throw new ApiError("Can't reach Ferry. Check that Tailscale is on.", 0);
  }
  if (r.status === 401 && url !== "/api/login" && url !== "/api/password") {
    showLogin();
    throw new ApiError("Signed out", 401);
  }
  const ct = r.headers.get("content-type") || "";
  const data = ct.includes("json") ? await r.json() : await r.text();
  if (!r.ok) {
    const msg = (data && (data.error || data.detail)) || `Request failed (${r.status})`;
    throw new ApiError(typeof msg === "string" ? msg : JSON.stringify(msg), r.status);
  }
  return data;
}
const GET = (u, q) => api("GET", u, undefined, q);
const POST = (u, b, q) => api("POST", u, b ?? {}, q);

// ---------------------------------------------------------------- format
function fmtSize(n) {
  if (n == null) return "";
  if (n < 1000) return `${n} B`;
  const u = ["KB", "MB", "GB", "TB", "PB"];
  let i = -1;
  do { n /= 1000; i++; } while (n >= 1000 && i < u.length - 1);
  return `${n < 10 ? n.toFixed(1) : Math.round(n)} ${u[i]}`;
}
function fmtDate(ts) {
  if (!ts) return "";
  const d = new Date(ts * 1000);
  const now = new Date();
  const sameYear = d.getFullYear() === now.getFullYear();
  if (d.toDateString() === now.toDateString()) return d.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
  return d.toLocaleDateString([], { month: "short", day: "numeric", ...(sameYear ? {} : { year: "numeric" }) });
}
function fmtFullDate(ts) {
  return ts ? new Date(ts * 1000).toLocaleString([], { dateStyle: "medium", timeStyle: "short" }) : "";
}
function fmtEta(sec) {
  if (!isFinite(sec) || sec <= 0) return "";
  if (sec < 60) return `${Math.ceil(sec)}s left`;
  if (sec < 3600) return `${Math.ceil(sec / 60)} min left`;
  return `${(sec / 3600).toFixed(1)} h left`;
}
const plural = (n, w) => `${n} ${w}${n === 1 ? "" : "s"}`;
const basename = (p) => p.replace(/\/+$/, "").split("/").pop() || "/";
const dirname = (p) => { const s = p.replace(/\/+$/, "").split("/"); s.pop(); return s.join("/") || "/"; };
const joinPath = (a, b) => (a.endsWith("/") ? a : a + "/") + b;

// ---------------------------------------------------------------- state
const store = {
  get(k, d) { try { const v = localStorage.getItem(k); return v ? JSON.parse(v) : d; } catch { return d; } },
  set(k, v) { try { localStorage.setItem(k, JSON.stringify(v)); } catch {} },
};

function newPane(i) {
  return { i, conn: null, path: "/", entries: [], visible: [], view: "list", sort: { key: "name", dir: 1 },
           filter: "", sel: new Set(), cursor: -1, anchor: -1, loading: false, error: null, usage: null,
           editingPath: false, loadSeq: 0, lastPaths: {} };
}

const S = {
  me: null,
  conns: [],
  panes: [newPane(0), newPane(1)],
  active: 0,
  jobs: [],
  uploads: [],
  prefs: store.get("ferry.prefs", { showHidden: false, groupLive: true, theme: "auto", conflict: "rename", listThumbs: true }),
  selectMode: false,
  sheet: null,
};

function savePanes() {
  store.set("ferry.panes", S.panes.map((p) => ({ conn: p.conn, path: p.path, view: p.view, sort: p.sort, lastPaths: p.lastPaths })));
  store.set("ferry.active", S.active);
}
function savePrefs() { store.set("ferry.prefs", S.prefs); applyTheme(); }
function applyTheme() {
  const t = S.prefs.theme;
  if (t === "auto") document.documentElement.removeAttribute("data-theme");
  else document.documentElement.setAttribute("data-theme", t);
}
const conn = (id) => S.conns.find((c) => c.id === id);
const connName = (id) => conn(id)?.name || "?";
const other = (i) => (i === 0 ? 1 : 0);

// ---------------------------------------------------------------- toasts
function toast(msg, { err = false, action = null, ms = 4200 } = {}) {
  const el = document.createElement("div");
  el.className = "toast" + (err ? " err" : "");
  el.innerHTML = `<span>${esc(msg)}</span>${action ? `<button>${esc(action.label)}</button>` : ""}`;
  if (action) el.querySelector("button").onclick = () => { action.fn(); el.remove(); };
  $("#toasts").append(el);
  setTimeout(() => el.remove(), err ? ms * 1.6 : ms);
}

// ---------------------------------------------------------------- login
function showLogin() {
  S.me = null;
  closeAll();
  const setup = S.meSetup;
  $("#app").innerHTML = `
    <div class="login"><form>
      <svg class="mark" viewBox="0 0 64 64">${markSvg()}</svg>
      <h1>Ferry</h1>
      <p>${setup ? "Pick a password to protect your files." : "Sign in to move files between your machines."}</p>
      <div class="route"></div>
      <label class="field"><span>${setup ? "New password" : "Password"}</span>
        <input type="password" name="pw" autocomplete="${setup ? "new-password" : "current-password"}" required autofocus></label>
      <div class="note err" hidden></div>
      <button class="btn primary" style="width:100%;justify-content:center">${setup ? "Set password" : "Sign in"}</button>
    </form></div>`;
  const f = $(".login form");
  f.onsubmit = async (e) => {
    e.preventDefault();
    const note = $(".note", f);
    try {
      await POST("/api/login", { password: f.pw.value });
      boot();
    } catch (err) {
      note.hidden = false;
      note.textContent = err.message;
      f.pw.select();
    }
  };
}

function markSvg() {
  return `<circle cx="32" cy="32" r="30" style="fill:var(--wash)"/>
    <path d="M8 40 Q20 36 32 40 T56 40" fill="none" style="stroke:var(--wash-ink)" stroke-width="2.5" opacity=".5"/>
    <path d="M17 31h30l-4 8H21z" style="fill:var(--ink)"/><rect x="24" y="24" width="14" height="7" rx="1.5" style="fill:var(--ink)"/>
    <rect x="27" y="26.5" width="3" height="2.4" style="fill:var(--wash)"/><rect x="32" y="26.5" width="3" height="2.4" style="fill:var(--wash)"/>
    <path d="M10 48h44" style="stroke:var(--magenta)" stroke-width="2.5" stroke-dasharray="5 4"/>`;
}

// ---------------------------------------------------------------- boot
async function boot() {
  applyTheme();
  let me;
  try { me = await GET("/api/me"); } catch (e) { $("#app").innerHTML = `<div class="login"><p>${esc(e.message)}</p></div>`; return; }
  S.meSetup = me.setup;
  if (!me.authed) return showLogin();
  S.me = me;
  await loadConns();
  const saved = store.get("ferry.panes", null);
  S.panes.forEach((p, i) => {
    const sv = saved?.[i];
    if (sv) { Object.assign(p, { view: sv.view || "list", sort: sv.sort || p.sort, lastPaths: sv.lastPaths || {} }); }
    const want = sv?.conn && conn(sv.conn) ? sv.conn : S.conns[Math.min(i, S.conns.length - 1)]?.id;
    p.conn = want || null;
    p.path = sv && sv.conn === want ? sv.path || "/" : "/";
  });
  S.active = store.get("ferry.active", 0) === 1 ? 1 : 0;
  renderFrame();
  S.panes.forEach((p) => p.conn && load(p.i));
  startEvents();
  if (!S.conns.length) openConnections();
  if (window.isSecureContext && "serviceWorker" in navigator) navigator.serviceWorker.register("/sw.js").catch(() => {});
  const shared = +new URLSearchParams(location.search).get("shared");
  if (shared) receiveShared(shared);
}

// files sent from the phone's share menu, parked by sw.js
async function receiveShared(n) {
  history.replaceState(null, "", "/");
  const cache = await caches.open("ferry-share");
  const files = [];
  for (let i = 0; i < n; i++) {
    const r = await cache.match(`/shared/${i}`);
    if (!r) continue;
    const blob = await r.blob();
    files.push({ file: new File([blob], decodeURIComponent(r.headers.get("x-name") || `shared-${i}`),
      { type: blob.type, lastModified: +r.headers.get("x-mtime") || Date.now() }), rel: null });
  }
  await caches.delete("ferry-share");
  if (!files.length) return;
  files.forEach((f) => (f.rel = f.file.name));
  const where = S.panes.filter((p) => p.conn);
  const el = sheet({ title: `Save ${plural(files.length, "file")}`, kind: "share",
    body: `<p class="muted">${esc(files.slice(0, 3).map((f) => f.file.name).join(", "))}${files.length > 3 ? ` and ${files.length - 3} more` : ""}</p>
      <div class="menu" style="padding:0">${where.map((p) => `<button data-to="${p.i}">${icon(typeIcon(conn(p.conn)?.type))}<span>${esc(connName(p.conn))} <span class="muted">${esc(p.path)}</span></span></button>`).join("")}</div>
      <p class="small muted">Go to the folder you want in either dock first if neither is right.</p>` });
  el.addEventListener("click", (e) => {
    const b = e.target.closest("[data-to]");
    if (!b) return;
    closeAll();
    uploadFiles(+b.dataset.to, files);
  });
}

async function loadConns() {
  const r = await GET("/api/connections");
  S.conns = r.connections;
}

// ---------------------------------------------------------------- frame
function renderFrame() {
  $("#app").innerHTML = `
    <header class="top">
      <div class="brand"><svg viewBox="0 0 64 64">${markSvg()}</svg><span>Ferry</span></div>
      <div class="dock-switch" role="group" aria-label="Dock"></div>
      <button class="icon-btn transfers-btn" data-act="transfers" title="Transfers">${icon("swap")}<span class="count" hidden></span></button>
      <button class="icon-btn" data-act="connections" title="Connections">${icon("server")}</button>
      <button class="icon-btn" data-act="settings" title="Settings">${icon("gear")}</button>
    </header>
    <main class="docks">
      ${paneShell(0)}
      <div class="crossing">
        <button class="cross-btn" data-act="cross" data-from="0" title="">${icon("right")}</button>
        <span class="pct" data-act="transfers" hidden></span>
        <button class="cross-btn" data-act="cross" data-from="1" title="">${icon("left")}</button>
      </div>
      ${paneShell(1)}
    </main>
    <div class="bottom">
      <div class="route-pill" data-act="transfers" hidden></div>
      <div class="actionbar idle"></div>
    </div>
    <input type="file" id="upload-input" multiple hidden>
    <input type="file" id="upload-dir-input" webkitdirectory hidden>`;
  bindFrame();
  renderAll();
}

function paneShell(i) {
  return `<section class="pane" data-pane="${i}" aria-label="Dock ${DOCK[i]}">
    <div class="loading-bar" hidden></div>
    <div class="pane-head"></div>
    <nav class="crumbs" aria-label="Path"></nav>
    <div class="tools"></div>
    <div class="list" tabindex="0" role="listbox" aria-multiselectable="true"></div>
    <footer class="pane-foot"></footer>
  </section>`;
}

function renderAll() {
  S.panes.forEach((p) => renderPane(p.i));
  renderDockSwitch();
  renderCrossing();
  renderActionbar();
}

function paneEl(i) { return $(`.pane[data-pane="${i}"]`); }

function renderDockSwitch() {
  const el = $(".dock-switch");
  if (!el) return;
  el.innerHTML = S.panes.map((p) => `<button data-act="dock" data-pane="${p.i}" aria-pressed="${S.active === p.i}">
      <span class="dock-mark">${DOCK[p.i]}</span><span class="nm">${esc(p.conn ? connName(p.conn) : "Pick")}</span></button>`).join("");
  S.panes.forEach((p) => paneEl(p.i).classList.toggle("off", MOBILE.matches && S.active !== p.i));
}

function typeIcon(t) { return { local: "server", sftp: "laptop", smb: "server", ftp: "server", dropbox: "cloud" }[t] || "server"; }

function renderPane(i) {
  const p = S.panes[i];
  const el = paneEl(i);
  if (!el) return;
  el.classList.toggle("active", S.active === i);
  $(".loading-bar", el).hidden = !p.loading;
  const c = conn(p.conn);
  const dot = p.error ? "down" : c?.live || p.entries.length ? "live" : "";
  $(".pane-head", el).innerHTML = `
    <span class="dock-mark" title="Dock ${DOCK[i]}">${DOCK[i]}</span>
    <button class="conn-pick" data-act="pick-conn" data-pane="${i}" title="Switch connection">
      <span class="status-dot ${dot}"></span><span class="nm">${esc(c ? c.name : "Pick a connection")}</span>${icon("right", "caret")}</button>
    <span class="usage">${p.usage && p.usage.free ? `${fmtSize(p.usage.free)} free` : ""}</span>`;
  renderCrumbs(i);
  renderTools(i);
  computeVisible(i);
  renderList(i);
  renderFoot(i);
}

function renderCrumbs(i) {
  const p = S.panes[i];
  const el = $(".crumbs", paneEl(i));
  if (p.editingPath) {
    el.innerHTML = `<input value="${esc(p.path)}" aria-label="Go to folder" spellcheck="false" autocapitalize="off">`;
    const inp = $("input", el);
    inp.focus();
    inp.select();
    inp.onkeydown = (e) => {
      if (e.key === "Enter") { p.editingPath = false; navigate(i, inp.value.trim() || "/"); }
      if (e.key === "Escape") { p.editingPath = false; renderCrumbs(i); }
      e.stopPropagation();
    };
    inp.onblur = () => { if (p.editingPath) { p.editingPath = false; renderCrumbs(i); } };
    return;
  }
  const parts = p.path.split("/").filter(Boolean);
  let acc = "";
  const btns = [`<button data-act="nav" data-pane="${i}" data-path="/">${esc(conn(p.conn)?.name || "/")}</button>`];
  for (const part of parts) {
    acc += "/" + part;
    btns.push(`<span class="sep">/</span><button data-act="nav" data-pane="${i}" data-path="${esc(acc)}">${esc(part)}</button>`);
  }
  el.innerHTML = btns.join("") + `<span style="flex:1;min-width:24px;align-self:stretch" data-act="edit-path" data-pane="${i}" title="Type a path"></span>`;
  el.scrollLeft = el.scrollWidth;
}

function renderTools(i) {
  const p = S.panes[i];
  const sortLabel = { name: "Name", mtime: "Date", size: "Size", kind: "Type" }[p.sort.key];
  $(".tools", paneEl(i)).innerHTML = `
    <button class="icon-btn small" data-act="up" data-pane="${i}" title="Up one folder" ${p.path === "/" ? "disabled" : ""}>${icon("up")}</button>
    <button class="icon-btn small" data-act="refresh" data-pane="${i}" title="Refresh">${icon("refresh")}</button>
    <button class="icon-btn small" data-act="mkdir" data-pane="${i}" title="New folder">${icon("newfolder")}</button>
    <button class="icon-btn small" data-act="upload" data-pane="${i}" title="Upload from this device">${icon("upload")}</button>
    <span class="grow"></span>
    <label class="filter">${icon("search")}<input placeholder="Filter" value="${esc(p.filter)}" data-pane="${i}" aria-label="Filter this folder"></label>
    <button class="sort-btn" data-act="sort" data-pane="${i}" title="Change sort">${sortLabel} ${p.sort.dir > 0 ? "↑" : "↓"}</button>
    <button class="icon-btn small" data-act="view" data-pane="${i}" title="${p.view === "grid" ? "List view" : "Grid view"}">${icon(p.view === "grid" ? "list" : "grid")}</button>`;
  const f = $(".filter input", paneEl(i));
  f.oninput = () => { p.filter = f.value; computeVisible(i); renderList(i); renderFoot(i); };
  f.onkeydown = (e) => { if (e.key === "Escape") { f.value = ""; f.oninput(); f.blur(); } e.stopPropagation(); };
}

function computeVisible(i) {
  const p = S.panes[i];
  const f = p.filter.trim().toLowerCase();
  let v = p.entries.filter((e) => (S.prefs.showHidden || !e.hidden) && (!S.prefs.groupLive || !e.live_of) && (!f || e.name.toLowerCase().includes(f)));
  const { key, dir } = p.sort;
  const coll = new Intl.Collator(undefined, { numeric: true, sensitivity: "base" });
  v.sort((a, b) => {
    if (a.is_dir !== b.is_dir) return a.is_dir ? -1 : 1;
    let r = 0;
    if (key === "mtime") r = a.mtime - b.mtime;
    else if (key === "size") r = a.size - b.size;
    else if (key === "kind") r = coll.compare(a.kind, b.kind);
    if (r === 0) r = coll.compare(a.name, b.name);
    return r * dir;
  });
  p.visible = v;
}

function thumbUrl(p, e, s = 320) { return `/api/thumb${qs({ conn: p.conn, path: e.path, s, m: Math.round(e.mtime) })}`; }

function kindIcon(k) {
  return { dir: "folder", image: "image", raw: "raw", video: "video", audio: "audio", archive: "archive", text: "text", pdf: "pdf", subtitle: "subtitle" }[k] || "file";
}

function rowHtml(p, e, idx) {
  const sel = p.sel.has(e.path);
  const showThumb = (e.kind === "image" || e.kind === "raw") && (p.view === "grid" || S.prefs.listThumbs);
  const ico = showThumb
    ? `<img loading="lazy" decoding="async" alt="" src="${thumbUrl(p, e)}" onerror="this.replaceWith(Object.assign(document.createElement('span'),{innerHTML:'${icon(kindIcon(e.kind)).replace(/"/g, "&quot;")}'}))">`
    : icon(kindIcon(e.kind));
  const live = e.live && S.prefs.groupLive ? `<span class="badge" title="Live Photo, includes ${esc(e.live)}">${icon("live")}Live</span>` : "";
  const size = e.is_dir ? "" : fmtSize(e.size);
  return `<div class="row${idx === p.cursor ? " cursor" : ""}${e.hidden ? " hidden-file" : ""}" role="option" draggable="true" data-i="${idx}" data-kind="${e.kind}" aria-selected="${sel}">
    <span class="ico" data-toggle>${ico}${live}<span class="check">${icon("check")}</span></span>
    <span class="nm-wrap" style="min-width:0"><span class="nm" title="${esc(e.name)}">${esc(e.name)}${live}</span>
      <span class="meta"><span>${size}</span><span>${fmtDate(e.mtime)}</span></span></span>
    <span class="sz">${size}</span><span class="dt">${fmtDate(e.mtime)}</span></div>`;
}

function renderList(i) {
  const p = S.panes[i];
  const el = $(".list", paneEl(i));
  el.classList.toggle("grid", p.view === "grid");
  if (!p.conn) {
    el.innerHTML = `<div class="empty"><strong>No connection picked</strong>Add your Mac, NAS or Dropbox to start.<br><button class="btn primary" data-act="connections">Add a connection</button></div>`;
    return;
  }
  if (p.error) {
    el.innerHTML = `<div class="empty"><strong>${esc(p.error)}</strong><button class="btn" data-act="refresh" data-pane="${i}">Try again</button></div>`;
    return;
  }
  if (!p.visible.length) {
    if (p.loading) { el.innerHTML = ""; return; }
    const why = p.entries.length && p.filter ? "Nothing matches that filter." : p.entries.length ? "Only hidden files in here." : "This folder is empty.";
    el.innerHTML = `<div class="empty"><strong>${why}</strong>${MOBILE.matches ? "" : "Drop files here to upload them."}</div>`;
    return;
  }
  el.innerHTML = p.visible.map((e, idx) => rowHtml(p, e, idx)).join("");
}

function renderFoot(i) {
  const p = S.panes[i];
  const el = $(".pane-foot", paneEl(i));
  if (!p.conn || p.error) { el.innerHTML = ""; return; }
  const files = p.visible.filter((e) => !e.is_dir);
  const dirs = p.visible.length - files.length;
  const total = files.reduce((a, e) => a + e.size, 0);
  const parts = [];
  if (dirs) parts.push(plural(dirs, "folder"));
  parts.push(plural(files.length, "file"));
  if (total) parts.push(fmtSize(total));
  el.innerHTML = parts.map((x) => `<span>${x}</span>`).join("");
}

function updateRowSelection(i) {
  const p = S.panes[i];
  $$(".row", paneEl(i)).forEach((r) => {
    const e = p.visible[+r.dataset.i];
    r.setAttribute("aria-selected", e && p.sel.has(e.path) ? "true" : "false");
    r.classList.toggle("cursor", +r.dataset.i === p.cursor);
  });
  if (!p.sel.size) S.selectMode = false;
  renderActionbar();
  renderCrossing();
}

// ---------------------------------------------------------------- loading
async function load(i, { keepSel = false } = {}) {
  const p = S.panes[i];
  if (!p.conn) { renderPane(i); return; }
  const seq = ++p.loadSeq;
  p.loading = true;
  $(".loading-bar", paneEl(i)).hidden = false;
  try {
    const r = await GET("/api/list", { conn: p.conn, path: p.path });
    if (seq !== p.loadSeq) return;
    p.entries = r.entries;
    p.path = r.path;
    p.error = null;
    if (!keepSel) { p.sel.clear(); p.cursor = -1; p.anchor = -1; }
    else { const names = new Set(p.entries.map((e) => e.path)); [...p.sel].forEach((s) => names.has(s) || p.sel.delete(s)); }
    p.lastPaths[p.conn] = p.path;
    savePanes();
    if (!p.usage || p.usageConn !== p.conn) {
      p.usageConn = p.conn;
      GET("/api/usage", { conn: p.conn }).then((u) => { p.usage = u; renderPane(i); }).catch(() => {});
    }
    const c = conn(p.conn);
    if (c) c.live = true;
  } catch (e) {
    if (seq !== p.loadSeq) return;
    if (e.status === 401) return;
    p.entries = [];
    p.error = e.status === 404 && p.path !== "/" ? `${basename(p.path)} isn't there anymore.` : e.message;
    if (e.status === 404 && p.path !== "/") { p.path = "/"; p.loading = false; return load(i); }
  } finally {
    if (seq === p.loadSeq) {
      p.loading = false;
      renderPane(i);
      renderDockSwitch();
      renderActionbar();
      renderCrossing();
    }
  }
}

function navigate(i, path) {
  const p = S.panes[i];
  p.path = path.startsWith("/") ? path : "/" + path;
  p.filter = "";
  p.sel.clear();
  load(i);
}

function setConn(i, cid) {
  const p = S.panes[i];
  p.conn = cid;
  p.path = p.lastPaths[cid] || "/";
  p.usage = null;
  p.error = null;
  p.entries = [];
  p.sel.clear();
  renderPane(i);
  renderDockSwitch();
  load(i);
}

function refreshWhere(cid, dir) {
  S.panes.forEach((p) => {
    if (p.conn === cid && (!dir || p.path === dir || p.path.startsWith(dir.replace(/\/$/, "") + "/") || dir.startsWith(p.path))) load(p.i, { keepSel: true });
  });
}

// ---------------------------------------------------------------- selection
function selected(i) {
  const p = S.panes[i];
  return p.visible.filter((e) => p.sel.has(e.path));
}

function selectionPaths(i) {
  // Live Photos travel together: the HEIC and its MOV
  const p = S.panes[i];
  const out = [];
  for (const e of selected(i)) {
    out.push(e.path);
    if (e.live && S.prefs.groupLive) {
      const v = p.entries.find((x) => x.name === e.live);
      if (v) out.push(v.path);
    }
  }
  return out;
}

function setActive(i) {
  if (S.active === i) return;
  S.active = i;
  savePanes();
  S.panes.forEach((p) => paneEl(p.i).classList.toggle("active", S.active === p.i));
  renderDockSwitch();
  renderActionbar();
}

function clickRow(i, idx, ev) {
  const p = S.panes[i];
  const e = p.visible[idx];
  if (!e) return;
  const touchMode = MOBILE.matches || S.selectMode;
  if (ev.shiftKey && p.anchor >= 0) {
    const [a, b] = [Math.min(p.anchor, idx), Math.max(p.anchor, idx)];
    if (!(ev.metaKey || ev.ctrlKey)) p.sel.clear();
    for (let k = a; k <= b; k++) p.sel.add(p.visible[k].path);
  } else if (ev.metaKey || ev.ctrlKey || (touchMode && S.selectMode)) {
    p.sel.has(e.path) ? p.sel.delete(e.path) : p.sel.add(e.path);
    p.anchor = idx;
  } else if (MOBILE.matches) {
    open(i, e);
    return;
  } else {
    p.sel.clear();
    p.sel.add(e.path);
    p.anchor = idx;
  }
  p.cursor = idx;
  updateRowSelection(i);
}

function toggleRow(i, idx) {
  const p = S.panes[i];
  const e = p.visible[idx];
  if (!e) return;
  p.sel.has(e.path) ? p.sel.delete(e.path) : p.sel.add(e.path);
  p.anchor = p.cursor = idx;
  S.selectMode = p.sel.size > 0;
  updateRowSelection(i);
}

function moveCursor(i, delta, extend) {
  const p = S.panes[i];
  if (!p.visible.length) return;
  let cols = 1;
  if (p.view === "grid") {
    const rows = $$(".row", paneEl(i));
    if (rows.length > 1) { const top = rows[0].offsetTop; cols = rows.findIndex((r) => r.offsetTop !== top); if (cols < 1) cols = rows.length; }
  }
  const step = Math.abs(delta) === 1 ? delta : Math.sign(delta) * cols;
  const idx = Math.max(0, Math.min(p.visible.length - 1, (p.cursor < 0 ? -1 : p.cursor) + step));
  if (extend) {
    if (p.anchor < 0) p.anchor = Math.max(0, p.cursor);
    p.sel.clear();
    const [a, b] = [Math.min(p.anchor, idx), Math.max(p.anchor, idx)];
    for (let k = a; k <= b; k++) p.sel.add(p.visible[k].path);
  } else {
    p.sel.clear();
    p.sel.add(p.visible[idx].path);
    p.anchor = idx;
  }
  p.cursor = idx;
  updateRowSelection(i);
  $(`.row[data-i="${idx}"]`, paneEl(i))?.scrollIntoView({ block: "nearest" });
}

// ---------------------------------------------------------------- open
function open(i, e) {
  const p = S.panes[i];
  if (e.is_dir) return navigate(i, e.path);
  if (e.kind === "archive") return openArchive(i, e);
  if (PREVIEW_KINDS.has(e.kind)) return openViewer(i, e);
  download(p.conn, [e]);
}

function download(cid, entries) {
  let url;
  if (entries.length === 1 && !entries[0].is_dir) url = `/api/raw${qs({ conn: cid, path: entries[0].path, dl: 1 })}`;
  else url = `/api/zip${qs({ conn: cid, path: entries.map((e) => e.path) })}`;
  const a = document.createElement("a");
  a.href = url;
  a.download = "";
  document.body.append(a);
  a.click();
  a.remove();
  if (entries.length > 1 || entries[0].is_dir) toast("Zipping as it downloads. Big folders take a while to start.");
}

// ---------------------------------------------------------------- actions
async function transfer(kind, from, dstDir, paths) {
  const src = S.panes[from];
  const dst = S.panes[other(from)];
  if (!dst.conn) return toast("Pick a connection for the other dock first.", { err: true });
  paths = paths || selectionPaths(from);
  if (!paths.length) return;
  const target = dstDir || dst.path;
  if (src.conn === dst.conn && paths.every((x) => dirname(x) === target) && kind === "move") {
    return toast("Those files are already in that folder.");
  }
  try {
    await POST("/api/transfer", { kind, src_conn: src.conn, paths, dst_conn: dst.conn, dst_dir: target, conflict: S.prefs.conflict });
    const n = selected(from).length || paths.length;
    toast(`${kind === "move" ? "Moving" : "Copying"} ${plural(n, "item")} to ${connName(dst.conn)}`, { action: { label: "Show", fn: openTransfers } });
    src.sel.clear();
    S.selectMode = false;
    updateRowSelection(from);
  } catch (e) {
    toast(e.message, { err: true });
  }
}

async function mkdirFlow(i) {
  const p = S.panes[i];
  const name = await promptSheet({ title: "New folder", label: "Folder name", value: "New folder", ok: "Create folder" });
  if (!name) return;
  try {
    const r = await POST("/api/mkdir", { conn: p.conn, path: joinPath(p.path, name) });
    await load(i);
    const idx = p.visible.findIndex((e) => e.path === r.path);
    if (idx >= 0) { p.sel = new Set([r.path]); p.cursor = idx; updateRowSelection(i); }
  } catch (e) { toast(e.message, { err: true }); }
}

async function renameFlow(i) {
  const p = S.panes[i];
  const [e] = selected(i);
  if (!e) return;
  const name = await promptSheet({ title: "Rename", label: "New name", value: e.name, ok: "Rename", selectStem: !e.is_dir });
  if (!name || name === e.name) return;
  try {
    const r = await POST("/api/rename", { conn: p.conn, path: e.path, name });
    if (e.live && S.prefs.groupLive && name.includes(".")) {
      // keep the Live Photo pair matched
      const stem = name.slice(0, name.lastIndexOf("."));
      const v = p.entries.find((x) => x.name === e.live);
      if (v) await POST("/api/rename", { conn: p.conn, path: v.path, name: stem + v.name.slice(v.name.lastIndexOf(".")) }).catch(() => {});
    }
    await load(i);
    const idx = p.visible.findIndex((x) => x.path === r.path);
    if (idx >= 0) { p.sel = new Set([r.path]); p.cursor = idx; updateRowSelection(i); }
  } catch (err) { toast(err.message, { err: true }); }
}

function trashInfo(p, paths) {
  const c = conn(p.conn);
  const t = c?.config?.trash ?? "";
  const inTrash = (x) => t && t !== "@synology" && (x === t || x.startsWith(t.replace(/\/$/, "") + "/"));
  const inRecycle = (x) => /^\/[^/]+\/(#recycle|delete_me)(\/|$)/.test(x);
  if (!t || (t === "@synology" && paths.every(inRecycle)) || paths.every(inTrash)) {
    return { permanent: true, where: c?.type === "dropbox" ? "Dropbox keeps deleted files for 30 days, so you can still restore them on dropbox.com." : "This can't be undone." };
  }
  if (t === "@synology") return { permanent: false, where: `They go to the #recycle folder of that shared folder on ${c.name}.` };
  return { permanent: false, where: `They go to ${t} on ${c.name}.` };
}

async function deleteFlow(i) {
  const p = S.panes[i];
  const items = selected(i);
  if (!items.length) return;
  const paths = selectionPaths(i);
  const info = trashInfo(p, paths);
  const what = items.length === 1 ? `“${items[0].name}”` : plural(items.length, "item");
  const ok = await confirmSheet({
    title: info.permanent ? `Delete ${what} for good?` : `Move ${what} to the trash?`,
    body: info.where,
    ok: info.permanent ? "Delete for good" : "Move to trash",
    danger: true,
  });
  if (!ok) return;
  try {
    const r = await POST("/api/delete", { conn: p.conn, paths });
    const errs = r.results.filter((x) => x.error);
    if (errs.length) toast(`${errs.length} couldn't be deleted: ${errs[0].error}`, { err: true });
    else toast(info.permanent ? `Deleted ${what}` : `Moved ${what} to the trash`);
    load(i);
  } catch (e) { toast(e.message, { err: true }); }
}

// ---------------------------------------------------------------- uploads
let upSeq = 0;
function uploadFiles(i, list, dstDir) {
  const p = S.panes[i];
  if (!p.conn) return;
  const base = dstDir || p.path;
  for (const { file, rel } of list) {
    S.uploads.push({ id: ++upSeq, conn: p.conn, file, path: joinPath(base, rel || file.name), name: rel || file.name, size: file.size, loaded: 0, status: "queued", error: "" });
  }
  toast(`Uploading ${plural(list.length, "file")} to ${connName(p.conn)}`, { action: { label: "Show", fn: openTransfers } });
  pumpUploads();
  renderCrossing();
}

function pumpUploads() {
  const running = S.uploads.filter((u) => u.status === "running").length;
  const next = S.uploads.filter((u) => u.status === "queued").slice(0, Math.max(0, 2 - running));
  next.forEach(startUpload);
  renderCrossing();
  if (S.sheet?.kind === "transfers") renderTransfers();
}

function startUpload(u) {
  u.status = "running";
  const xhr = new XMLHttpRequest();
  u.xhr = xhr;
  xhr.open("PUT", `/api/upload${qs({ conn: u.conn, path: u.path, mtime: u.file.lastModified || 0, conflict: S.prefs.conflict })}`);
  xhr.setRequestHeader("X-Ferry", "1");
  xhr.setRequestHeader("Content-Type", "application/octet-stream");
  let last = 0;
  xhr.upload.onprogress = (ev) => {
    u.loaded = ev.loaded;
    const now = performance.now();
    if (now - last > 400) { last = now; renderCrossing(); if (S.sheet?.kind === "transfers") renderTransfers(); }
  };
  xhr.onload = () => {
    if (xhr.status >= 200 && xhr.status < 300) { u.status = "done"; u.loaded = u.size; }
    else { u.status = "error"; try { u.error = JSON.parse(xhr.responseText).error || xhr.statusText; } catch { u.error = xhr.statusText || `HTTP ${xhr.status}`; } }
    finishUpload(u);
  };
  xhr.onerror = () => { u.status = "error"; u.error = "Connection dropped"; finishUpload(u); };
  xhr.onabort = () => { u.status = "canceled"; finishUpload(u); };
  xhr.send(u.file);
}

function finishUpload(u) {
  delete u.xhr;
  if (!S.uploads.some((x) => x.status === "running" || x.status === "queued")) {
    const done = S.uploads.filter((x) => x.status === "done").length;
    const failed = S.uploads.filter((x) => x.status === "error").length;
    if (failed) toast(`${failed} upload${failed > 1 ? "s" : ""} failed`, { err: true, action: { label: "Show", fn: openTransfers } });
    else if (done) toast(`Uploaded ${plural(done, "file")}`);
  }
  refreshWhere(u.conn, dirname(u.path));
  pumpUploads();
}

async function filesFromDrop(dt) {
  const out = [];
  const items = [...(dt.items || [])].filter((x) => x.kind === "file");
  const entries = items.map((x) => x.webkitGetAsEntry && x.webkitGetAsEntry()).filter(Boolean);
  if (!entries.length) return [...dt.files].map((f) => ({ file: f, rel: f.name }));
  const walk = async (entry, prefix) => {
    if (entry.isFile) {
      const f = await new Promise((res, rej) => entry.file(res, rej));
      out.push({ file: f, rel: prefix + f.name });
    } else if (entry.isDirectory) {
      const reader = entry.createReader();
      let batch;
      do {
        batch = await new Promise((res, rej) => reader.readEntries(res, rej));
        for (const ch of batch) await walk(ch, prefix + entry.name + "/");
      } while (batch.length);
    }
  };
  for (const e of entries) await walk(e, "");
  return out;
}

// ---------------------------------------------------------------- crossing + action bar
function activeWork() {
  const jobs = S.jobs.filter((j) => ["queued", "running", "paused", "waiting"].includes(j.status));
  const ups = S.uploads.filter((u) => u.status === "running" || u.status === "queued");
  let total = 0, done = 0;
  jobs.forEach((j) => { total += j.bytes_total; done += j.bytes_done; });
  ups.forEach((u) => { total += u.size; done += u.loaded; });
  return { count: jobs.length + ups.length, pct: total ? Math.min(99, Math.floor((done / total) * 100)) : 0, jobs, ups };
}

function renderCrossing() {
  const w = activeWork();
  const cross = $(".crossing");
  if (cross) {
    cross.classList.toggle("underway", w.count > 0);
    const pct = $(".pct", cross);
    pct.hidden = !w.count;
    pct.textContent = w.count ? `${w.pct}%` : "";
    pct.title = w.count ? `${plural(w.count, "transfer")} running` : "";
    $$(".cross-btn", cross).forEach((b) => {
      const from = +b.dataset.from;
      const n = S.panes[from].sel.size;
      const to = S.panes[other(from)].conn;
      b.disabled = !n || !to;
      b.title = n && to ? `Copy ${plural(n, "item")} to ${connName(to)}` : "Select files to copy across";
    });
  }
  const badge = $(".transfers-btn .count");
  if (badge) { badge.hidden = !w.count; badge.textContent = w.count; }
  const pill = $(".route-pill");
  if (pill) {
    pill.hidden = !w.count;
    pill.innerHTML = w.count ? `<span>${w.count === 1 ? "1 transfer" : `${w.count} transfers`}</span><span class="line"></span><b>${w.pct}%</b>` : "";
  }
}

function renderActionbar() {
  const bar = $(".actionbar");
  if (!bar) return;
  const i = S.active;
  const p = S.panes[i];
  const items = selected(i);
  const o = S.panes[other(i)];
  if (!items.length) {
    bar.className = "actionbar idle";
    bar.innerHTML = `<span class="info">${MOBILE.matches ? "" : "Select files, then copy them across. Drag between docks works too. Hold Shift to move instead."}</span>`;
    return;
  }
  bar.className = "actionbar";
  const size = items.reduce((a, e) => a + (e.is_dir ? 0 : e.size), 0);
  const dest = o.conn ? connName(o.conn) : "the other dock";
  const one = items.length === 1 ? items[0] : null;
  bar.innerHTML = `
    <span class="info"><b>${items.length} selected</b>${size ? ` <span>${fmtSize(size)}</span>` : ""}</span>
    <div class="acts">
      <button class="btn primary" data-act="copy-across" ${o.conn ? "" : "disabled"} title="Copy to ${esc(dest)}">${icon("copy")}<span>Copy<span class="lbl-long"> to ${esc(dest)}</span></span></button>
      <button class="btn" data-act="move-across" ${o.conn ? "" : "disabled"} title="Move to ${esc(dest)}">${icon("move")}<span>Move<span class="lbl-long"> to ${esc(dest)}</span></span></button>
      ${MOBILE.matches ? "" : `
      <button class="btn ghost" data-act="download" title="Download to this device">${icon("download")}<span class="lbl-long">Download</span></button>
      ${one ? `<button class="btn ghost" data-act="rename" title="Rename">${icon("pencil")}<span class="lbl-long">Rename</span></button>` : ""}
      ${one && one.kind === "archive" ? `<button class="btn ghost" data-act="extract" title="Open archive">${icon("extract")}<span class="lbl-long">Extract</span></button>` : ""}
      <button class="btn ghost danger" data-act="delete" title="Delete">${icon("trash")}</button>`}
      ${MOBILE.matches ? `<button class="btn" data-act="more" aria-label="More actions">${icon("more")}</button>` : ""}
    </div>`;
}

// ---------------------------------------------------------------- overlays
function closeAll() {
  $("#overlay-root").innerHTML = "";
  $$(".popover").forEach((x) => x.remove());
  S.sheet = null;
}

function sheet({ title, body, foot = "", wide = false, kind = "sheet", onClose }) {
  closeAll();
  const root = $("#overlay-root");
  root.innerHTML = `<div class="scrim"><div class="sheet${wide ? " wide" : ""}" role="dialog" aria-modal="true" aria-label="${esc(title)}">
    <div class="sheet-head"><h2>${esc(title)}</h2><button class="icon-btn" data-close title="Close">${icon("close")}</button></div>
    <div class="sheet-body">${body}</div>${foot ? `<div class="sheet-foot">${foot}</div>` : ""}</div></div>`;
  const scrim = $(".scrim", root);
  const close = () => { if (S.sheet?.el === scrim) { closeAll(); onClose && onClose(); } };
  scrim.addEventListener("pointerdown", (e) => { if (e.target === scrim) scrim.dataset.down = "1"; });
  scrim.addEventListener("click", (e) => { if (e.target === scrim && scrim.dataset.down) close(); delete scrim.dataset.down; });
  $("[data-close]", scrim).onclick = close;
  S.sheet = { el: scrim, kind, close };
  return scrim;
}

function promptSheet({ title, label, value = "", ok = "Save", selectStem = false }) {
  return new Promise((resolve) => {
    const el = sheet({
      title,
      body: `<form id="pf"><label class="field"><span>${esc(label)}</span><input name="v" value="${esc(value)}" autocomplete="off" spellcheck="false"></label></form>`,
      foot: `<button class="btn ghost" data-close2>Cancel</button><button class="btn primary" form="pf">${esc(ok)}</button>`,
      onClose: () => resolve(null),
    });
    const inp = $("input", el);
    setTimeout(() => {
      inp.focus();
      const dot = value.lastIndexOf(".");
      if (selectStem && dot > 0) inp.setSelectionRange(0, dot); else inp.select();
    }, 30);
    $("[data-close2]", el).onclick = () => { closeAll(); resolve(null); };
    $("#pf", el).onsubmit = (e) => { e.preventDefault(); const v = inp.value.trim(); closeAll(); resolve(v || null); };
  });
}

function confirmSheet({ title, body, ok = "OK", danger = false }) {
  return new Promise((resolve) => {
    const el = sheet({
      title,
      body: `<p>${esc(body)}</p>`,
      foot: `<button class="btn ghost" data-no>Cancel</button><button class="btn ${danger ? "primary" : "primary"}" data-yes>${esc(ok)}</button>`,
      onClose: () => resolve(false),
    });
    $("[data-no]", el).onclick = () => { closeAll(); resolve(false); };
    $("[data-yes]", el).onclick = () => { closeAll(); resolve(true); };
    setTimeout(() => $("[data-yes]", el).focus(), 30);
  });
}

function popover(anchor, html, { x, y } = {}) {
  $$(".popover").forEach((p) => p.remove());
  const el = document.createElement("div");
  el.className = "popover menu";
  el.innerHTML = html;
  document.body.append(el);
  const r = anchor ? anchor.getBoundingClientRect() : { left: x, bottom: y, top: y, right: x };
  const w = el.offsetWidth, h = el.offsetHeight;
  let left = Math.min(r.left, innerWidth - w - 8);
  let top = r.bottom + 4;
  if (top + h > innerHeight - 8) top = Math.max(8, r.top - h - 4);
  el.style.left = `${Math.max(8, left)}px`;
  el.style.top = `${top}px`;
  setTimeout(() => {
    const off = (e) => { if (!el.contains(e.target)) { el.remove(); document.removeEventListener("pointerdown", off, true); } };
    document.addEventListener("pointerdown", off, true);
  });
  el.addEventListener("click", (e) => { if (e.target.closest("button")) setTimeout(() => el.remove()); });
  return el;
}

function connPicker(i, anchor) {
  const p = S.panes[i];
  const html = S.conns.map((c) => `<button data-cid="${c.id}">${icon(typeIcon(c.type))}<span style="margin-right:auto">${esc(c.name)}</span>${c.id === p.conn ? icon("check") : ""}</button>`).join("")
    + `<hr><button data-manage>${icon("gear")}Manage connections</button>`;
  const el = popover(anchor, html);
  el.onclick = (e) => {
    const b = e.target.closest("button");
    if (!b) return;
    if (b.dataset.manage !== undefined) return openConnections();
    if (b.dataset.cid && b.dataset.cid !== p.conn) setConn(i, b.dataset.cid);
  };
}

function actionsMenuHtml(i) {
  const items = selected(i);
  const one = items.length === 1 ? items[0] : null;
  const o = S.panes[other(i)];
  const dest = o.conn ? esc(connName(o.conn)) : "other dock";
  return `
    ${one ? `<button data-m="open">${icon(one.is_dir ? "folder" : "play")}Open</button>` : ""}
    <button data-m="copy-across">${icon("copy")}Copy to ${dest}</button>
    <button data-m="move-across">${icon("move")}Move to ${dest}</button>
    <button data-m="download">${icon("download")}Download${items.length > 1 || one?.is_dir ? " as zip" : ""}</button>
    ${one ? `<button data-m="rename">${icon("pencil")}Rename</button>` : ""}
    ${one && one.kind === "archive" ? `<button data-m="extract">${icon("extract")}Extract</button>` : ""}
    ${one && !one.is_dir ? `<button data-m="copylink">${icon("link")}Copy Ferry link</button>` : ""}
    <hr><button data-m="delete" class="danger">${icon("trash")}Delete</button>`;
}

function runMenu(i, m) {
  const p = S.panes[i];
  const items = selected(i);
  if (m === "open" && items[0]) open(i, items[0]);
  if (m === "copy-across") transfer("copy", i);
  if (m === "move-across") transfer("move", i);
  if (m === "download") download(p.conn, items);
  if (m === "rename") renameFlow(i);
  if (m === "extract" && items[0]) openArchive(i, items[0]);
  if (m === "delete") deleteFlow(i);
  if (m === "copylink" && items[0]) {
    const url = location.origin + `/api/raw${qs({ conn: p.conn, path: items[0].path })}`;
    navigator.clipboard?.writeText(url).then(() => toast("Link copied. It only works where you're signed in to Ferry."), () => toast(url));
  }
}

function moreSheet(i) {
  const el = sheet({ title: plural(selected(i).length, "item"), body: `<div class="menu" style="padding:0">${actionsMenuHtml(i)}</div>` });
  $(".menu", el).onclick = (e) => { const b = e.target.closest("button[data-m]"); if (b) { closeAll(); runMenu(i, b.dataset.m); } };
}

// ---------------------------------------------------------------- transfers sheet
function openTransfers() {
  sheet({ title: "Transfers", body: `<div id="tx"></div>`, wide: true, kind: "transfers",
          foot: `<button class="btn ghost left" data-clear>Clear finished</button><button class="btn" data-close3>Done</button>` });
  $("[data-clear]").onclick = async () => {
    S.uploads = S.uploads.filter((u) => u.status === "running" || u.status === "queued");
    await POST("/api/jobs/clear").catch(() => {});
    renderTransfers();
  };
  $("[data-close3]").onclick = closeAll;
  renderTransfers();
}

function jobStatusText(j) {
  return { queued: "Queued", running: j.phase || "Copying", waiting: "Waiting for the connection", paused: "Paused",
           done: "Done", error: "Failed", canceled: "Canceled" }[j.status] || j.status;
}

function renderTransfers() {
  const el = $("#tx");
  if (!el) return;
  const ups = S.uploads.slice().reverse();
  const jobs = S.jobs;
  if (!ups.length && !jobs.length) {
    el.innerHTML = `<div class="empty"><strong>Nothing crossing right now</strong>Select files in one dock and copy them to the other.</div>`;
    return;
  }
  const upHtml = ups.map((u) => {
    const pct = u.size ? Math.floor((u.loaded / u.size) * 100) : u.status === "done" ? 100 : 0;
    const st = { queued: "Waiting", running: "Uploading", done: "Uploaded", error: "Failed", canceled: "Canceled" }[u.status];
    return `<div class="job"><div class="hd"><span class="ttl">${esc(u.name)} <span class="muted small">to ${esc(connName(u.conn))}</span></span>
      ${u.status === "running" || u.status === "queued" ? `<button class="icon-btn small" data-up-cancel="${u.id}" title="Cancel">${icon("close")}</button>` : ""}</div>
      <div class="bar"><i style="width:${pct}%"></i></div>
      <div class="sub"><span class="st ${u.status}">${st}</span><span>${fmtSize(u.loaded)} of ${fmtSize(u.size)}</span>${u.error ? `<span class="st error">${esc(u.error)}</span>` : ""}</div></div>`;
  }).join("");
  const jobHtml = jobs.map((j) => {
    const active = ["queued", "running", "paused", "waiting"].includes(j.status);
    const pct = j.bytes_total ? Math.floor((j.bytes_done / j.bytes_total) * 100) : j.status === "done" ? 100 : 0;
    const indet = j.status === "waiting" || (j.status === "running" && !j.bytes_total);
    const eta = j.speed > 0 ? fmtEta((j.bytes_total - j.bytes_done) / j.speed) : "";
    return `<div class="job"><div class="hd"><span class="ttl">${esc(j.title)}</span>
      ${active ? `<button class="icon-btn small" data-job="${j.id}" data-a="${j.status === "paused" ? "resume" : "pause"}" title="${j.status === "paused" ? "Resume" : "Pause"}">${icon(j.status === "paused" ? "play" : "pause")}</button>
        <button class="icon-btn small" data-job="${j.id}" data-a="cancel" title="Cancel">${icon("close")}</button>` : ""}
      ${j.status === "error" || j.status === "canceled" || (j.status === "done" && j.error_count) ? `<button class="btn ghost" data-job="${j.id}" data-a="retry" title="Runs it again. Files already there are skipped.">Retry</button>` : ""}</div>
      <div class="bar${indet && active ? " indet" : ""}"><i style="width:${pct}%"></i></div>
      <div class="sub"><span class="st ${j.status}">${jobStatusText(j)}</span>
        ${j.files_total ? `<span>${j.files_done + j.files_skipped} of ${plural(j.files_total, "file")}${j.counting ? ", still counting" : ""}</span>` : ""}
        ${j.bytes_total ? `<span>${fmtSize(j.bytes_done)} of ${fmtSize(j.bytes_total)}</span>` : ""}
        ${active && j.speed > 0 ? `<span>${fmtSize(j.speed)}/s</span><span>${eta}</span>` : ""}
        ${j.files_skipped ? `<span>${j.files_skipped} already there</span>` : ""}
        ${j.error ? `<span class="st error">${esc(j.error)}</span>` : ""}</div>
      ${active && j.current ? `<div class="cur">${esc(j.current)}</div>` : ""}
      ${j.error_count ? `<details><summary>${plural(j.error_count, "problem")}</summary><ul>${j.errors.map((x) => `<li>${esc(x)}</li>`).join("")}</ul></details>` : ""}
    </div>`;
  }).join("");
  const openDetails = new Set($$("details[open]", el).map((d) => d.closest(".job")?.querySelector(".ttl")?.textContent));
  el.innerHTML = upHtml + jobHtml;
  $$("details", el).forEach((d) => { if (openDetails.has(d.closest(".job")?.querySelector(".ttl")?.textContent)) d.open = true; });
  el.onclick = async (e) => {
    const b = e.target.closest("button");
    if (!b) return;
    if (b.dataset.upCancel) {
      const u = S.uploads.find((x) => x.id === +b.dataset.upCancel);
      if (u?.xhr) u.xhr.abort(); else if (u) { u.status = "canceled"; renderTransfers(); renderCrossing(); }
      return;
    }
    if (b.dataset.job) {
      try { await POST(`/api/jobs/${b.dataset.job}/${b.dataset.a}`); } catch (err) { toast(err.message, { err: true }); }
    }
  };
}

// ---------------------------------------------------------------- server events
let es = null;
const finishedSeen = new Set();
function startEvents() {
  if (es) es.close();
  es = new EventSource("/api/events");
  let first = true;
  es.onmessage = (ev) => {
    const data = JSON.parse(ev.data);
    const prev = new Map(S.jobs.map((j) => [j.id, j.status]));
    S.jobs = data.jobs;
    for (const j of S.jobs) {
      const wasActive = prev.has(j.id) && !["done", "error", "canceled"].includes(prev.get(j.id));
      const nowDone = ["done", "error", "canceled"].includes(j.status);
      if (nowDone && !finishedSeen.has(j.id)) {
        finishedSeen.add(j.id);
        if (first) continue;
        if (wasActive || !prev.has(j.id)) onJobFinished(j);
      }
    }
    first = false;
    renderCrossing();
    if (S.sheet?.kind === "transfers") renderTransfers();
  };
  es.onerror = () => {
    // EventSource reconnects on its own; a 401 shows up on the next API call
  };
}

function onJobFinished(j) {
  refreshWhere(j.dst_conn, j.dst_dir);
  if (j.kind === "move") j.src_paths.forEach((p) => refreshWhere(j.src_conn, dirname(p)));
  const dest = connName(j.dst_conn);
  if (j.status === "done") {
    const verb = { copy: "Copied", move: "Moved", extract: "Extracted" }[j.kind] || "Done";
    const n = j.files_done;
    const there = j.files_skipped ? `, ${j.files_skipped} already there` : "";
    const tail = j.error_count ? `, ${plural(j.error_count, "problem")}` : "";
    toast(`${verb} ${plural(n, "file")} to ${dest}${there}${tail}`, j.error_count ? { err: true, action: { label: "Show", fn: openTransfers } } : {});
  } else if (j.status === "error") {
    toast(`${j.title}: ${j.error}`, { err: true, action: { label: "Show", fn: openTransfers } });
  }
}

// ---------------------------------------------------------------- viewer
let viewerState = null;

function openViewer(i, e) {
  const p = S.panes[i];
  const list = p.visible.filter((x) => !x.is_dir && PREVIEW_KINDS.has(x.kind));
  let idx = list.findIndex((x) => x.path === e.path);
  if (idx < 0) { list.push(e); idx = list.length - 1; }
  closeAll();
  const root = $("#overlay-root");
  root.innerHTML = `<div class="viewer" role="dialog" aria-modal="true">
    <div class="vhead"><div class="title"></div>
      <button class="icon-btn" data-v="download" title="Download">${icon("download")}</button>
      <button class="icon-btn" data-v="close" title="Close">${icon("close")}</button></div>
    <div class="stage"></div>
    <div class="vfoot"></div></div>`;
  viewerState = { i, list, idx, offset: 0 };
  S.sheet = { el: $(".viewer", root), kind: "viewer", close: closeViewer };
  const v = $(".viewer", root);
  v.onclick = (ev) => {
    const b = ev.target.closest("[data-v]");
    if (!b) return;
    const a = b.dataset.v;
    if (a === "close") closeViewer();
    if (a === "prev") stepViewer(-1);
    if (a === "next") stepViewer(1);
    if (a === "download") download(p.conn, [viewerState.list[viewerState.idx]]);
    if (a === "copy-across") {
      const cur = viewerState.list[viewerState.idx];
      const paths = [cur.path];
      if (cur.live) { const lv = p.entries.find((x) => x.name === cur.live); if (lv) paths.push(lv.path); }
      transfer("copy", i, null, paths);
    }
    if (a === "sub-minus" || a === "sub-plus") shiftSubs(a === "sub-plus" ? 0.5 : -0.5);
  };
  // swipe between files on touch screens
  let sx = null, sy = null;
  const stage = $(".stage", v);
  stage.addEventListener("pointerdown", (ev) => { if (ev.pointerType === "touch") { sx = ev.clientX; sy = ev.clientY; } });
  stage.addEventListener("pointerup", (ev) => {
    if (sx == null) return;
    const dx = ev.clientX - sx, dy = ev.clientY - sy;
    sx = null;
    if (Math.abs(dx) > 70 && Math.abs(dx) > Math.abs(dy) * 1.5 && !ev.target.closest("video,audio,.live-btn")) stepViewer(dx < 0 ? 1 : -1);
  });
  renderViewer();
}

function closeViewer() {
  $$(".viewer video, .viewer audio").forEach((m) => { m.pause(); m.removeAttribute("src"); m.load(); });
  viewerState = null;
  closeAll();
}

function stepViewer(d) {
  if (!viewerState) return;
  const n = viewerState.list.length;
  viewerState.idx = (viewerState.idx + d + n) % n;
  viewerState.offset = 0;
  renderViewer();
}

function shiftSubs(delta) {
  const vid = $(".viewer video");
  if (!vid) return;
  viewerState.offset += delta;
  for (const t of vid.textTracks) {
    if (!t.cues) continue;
    for (const c of t.cues) { c.startTime = Math.max(0, c.startTime + delta); c.endTime = Math.max(0, c.endTime + delta); }
  }
  const lbl = $(".sub-offset");
  if (lbl) lbl.textContent = `${viewerState.offset > 0 ? "+" : ""}${viewerState.offset.toFixed(1)}s`;
}

async function renderViewer() {
  const vs = viewerState;
  const p = S.panes[vs.i];
  const e = vs.list[vs.idx];
  const v = $(".viewer");
  const stage = $(".stage", v);
  $$("video, audio", stage).forEach((m) => { m.pause(); m.removeAttribute("src"); m.load(); });
  $(".title", v).innerHTML = `<b>${esc(e.name)}</b><span>${fmtSize(e.size)}${e.mtime ? `&ensp;${esc(fmtFullDate(e.mtime))}` : ""}</span>`;
  const raw = `/api/raw${qs({ conn: p.conn, path: e.path })}`;
  const nav = vs.list.length > 1 ? `<button class="nav prev" data-v="prev" title="Previous">${icon("left")}</button><button class="nav next" data-v="next" title="Next">${icon("right")}</button>` : "";
  const o = S.panes[other(vs.i)];
  let foot = `<span class="muted small">${vs.idx + 1} of ${vs.list.length}</span><span class="spacer"></span>`;
  if (o.conn) foot += `<button class="btn primary" data-v="copy-across">${icon("copy")}Copy to ${esc(connName(o.conn))}</button>`;

  if (e.kind === "image" || e.kind === "raw") {
    const src = e.kind === "image" && BROWSER_IMG.test(e.name) ? raw : thumbUrl(p, e, 2048);
    let liveHtml = "";
    if (e.live) {
      const lv = p.entries.find((x) => x.name === e.live);
      if (lv) liveHtml = `<button class="live-btn" title="Hold to play">${icon("live")}Live</button>
        <video class="live-video" playsinline preload="none" src="/api/raw${qs({ conn: p.conn, path: lv.path })}"></video>`;
    }
    stage.innerHTML = `${liveHtml}<img alt="${esc(e.name)}" src="${src}">${nav}`;
    const img = $("img", stage);
    img.onerror = () => { stage.innerHTML = `<p class="spin">Can't show this image here. Download it instead.</p>${nav}`; };
    if (e.live) {
      const btn = $(".live-btn", stage), lvid = $(".live-video", stage);
      const play = (ev) => { ev.preventDefault(); lvid.classList.add("on"); btn.classList.add("on"); lvid.currentTime = 0; lvid.play().catch(() => {}); };
      const stop = () => { lvid.classList.remove("on"); btn.classList.remove("on"); lvid.pause(); };
      btn.addEventListener("pointerdown", play);
      btn.addEventListener("pointerup", stop);
      btn.addEventListener("pointerleave", stop);
      btn.addEventListener("contextmenu", (ev) => ev.preventDefault());
      lvid.onended = stop;
      const fit = () => { lvid.style.width = `${img.clientWidth}px`; lvid.style.height = `${img.clientHeight}px`; };
      img.addEventListener("load", fit);
      addEventListener("resize", fit);
    }
  } else if (e.kind === "video") {
    stage.innerHTML = `<video controls playsinline autoplay preload="metadata" src="${raw}"></video>${nav}`;
    const vid = $("video", stage);
    vid.onerror = () => toast("This browser can't play that video format. Download it, or open it in VLC or IINA.", { err: true });
    try {
      const subs = (await GET("/api/subs", { conn: p.conn, path: e.path })).subtitles;
      if (viewerState !== vs || vs.list[vs.idx] !== e) return;
      subs.forEach((s, k) => {
        const t = document.createElement("track");
        t.kind = "subtitles";
        t.label = s.label;
        t.srclang = /^[a-z]{2,3}$/i.test(s.label) ? s.label.toLowerCase() : "und";
        t.src = `/api/subtitle${qs({ conn: p.conn, path: s.path })}`;
        if (k === 0 && s.matched) t.default = true;
        vid.append(t);
      });
      if (subs.length) {
        foot = `<select aria-label="Subtitles" class="sub-pick"><option value="-1">Subtitles off</option>${subs.map((s, k) => `<option value="${k}" ${k === 0 && s.matched ? "selected" : ""}>${esc(s.label)}${s.matched ? "" : ` (${esc(s.name)})`}</option>`).join("")}</select>
          <button class="btn" data-v="sub-minus" title="Show subtitles earlier">−0.5s</button><span class="sub-offset muted small">0.0s</span><button class="btn" data-v="sub-plus" title="Show subtitles later">+0.5s</button>` + foot;
      }
    } catch {}
  } else if (e.kind === "audio") {
    stage.innerHTML = `<audio controls autoplay preload="metadata" src="${raw}"></audio>${nav}`;
  } else if (e.kind === "pdf") {
    stage.innerHTML = `<iframe title="${esc(e.name)}" src="${raw}"></iframe>${nav}`;
  } else {
    stage.innerHTML = `<p class="spin">Loading…</p>${nav}`;
    try {
      const r = await GET("/api/text", { conn: p.conn, path: e.path });
      if (viewerState !== vs || vs.list[vs.idx] !== e) return;
      stage.innerHTML = `<pre></pre>${nav}`;
      $("pre", stage).textContent = r.text + (r.truncated ? "\n\n… only the first 512 KB is shown. Download for the rest." : "");
    } catch (err) {
      stage.innerHTML = `<p class="spin">${esc(err.message)}</p>${nav}`;
    }
  }
  $(".vfoot", v).innerHTML = foot;
  const pick = $(".sub-pick", v);
  if (pick) {
    const vid = $("video", stage);
    const apply = () => [...vid.textTracks].forEach((t, k) => { t.mode = String(k) === pick.value ? "showing" : "disabled"; });
    pick.onchange = apply;
    setTimeout(apply, 50);
  }
}

// ---------------------------------------------------------------- archives
async function openArchive(i, e, password = "") {
  const p = S.panes[i];
  const o = S.panes[other(i)];
  const el = sheet({ title: e.name, wide: true, body: `<p class="muted">Reading the archive${p.conn && conn(p.conn)?.type !== "local" ? ". It has to download first, big ones take a moment" : ""}…</p>`, kind: "archive" });
  let data;
  try {
    data = await GET("/api/archive/list", { conn: p.conn, path: e.path, password });
  } catch (err) {
    if (S.sheet?.el !== el) return;
    const needsPw = /password/i.test(err.message);
    $(".sheet-body", el).innerHTML = `<div class="note err">${esc(err.message)}</div>${needsPw ? `<form id="apw"><label class="field"><span>Archive password</span><input name="pw" type="password" autocomplete="off"></label><button class="btn primary">Open</button></form>` : ""}`;
    const f = $("#apw", el);
    if (f) { f.pw.focus(); f.onsubmit = (ev) => { ev.preventDefault(); openArchive(i, e, f.pw.value); }; }
    return;
  }
  if (S.sheet?.el !== el) return;
  const all = data.entries;
  const sel = new Set();
  let cwd = "";
  const children = (dir) => all.filter((x) => (dir ? x.path.startsWith(dir + "/") && !x.path.slice(dir.length + 1).includes("/") : !x.path.includes("/")));
  const totalFiles = all.filter((x) => !x.is_dir).length;
  const totalSize = all.reduce((a, x) => a + (x.size || 0), 0);
  const draw = () => {
    const rows = children(cwd);
    $(".sheet-body", el).innerHTML = `
      <p class="muted small">${plural(totalFiles, "file")}, ${fmtSize(totalSize)} unpacked. Tick what you want, or leave everything unticked to extract it all.</p>
      <div class="crumbs" style="padding:0 0 8px">${cwd ? `<button data-arc-up>${icon("up")} Up</button><span class="sep">/</span><span>${esc(cwd)}</span>` : `<span>Top level</span>`}</div>
      <div class="arc-list">${rows.map((x) => `<label class="arc-row"><input type="checkbox" data-p="${esc(x.path)}" ${sel.has(x.path) ? "checked" : ""}>
        <span>${icon(x.is_dir ? "folder" : "file")}</span>
        <span class="n">${x.is_dir ? `<button type="button" data-into="${esc(x.path)}">${esc(basename(x.path))}</button>` : esc(basename(x.path))}</span>
        <span class="s">${x.is_dir ? "" : fmtSize(x.size)}</span></label>`).join("") || `<div class="empty">Empty</div>`}</div>
      <label class="field"><span>Put it in a folder named</span><input id="arc-folder" value="${esc(data.folder)}"></label>`;
    $("[data-arc-up]", el)?.addEventListener("click", () => { cwd = cwd.includes("/") ? cwd.slice(0, cwd.lastIndexOf("/")) : ""; draw(); });
    $$("[data-into]", el).forEach((b) => b.addEventListener("click", (ev) => { ev.preventDefault(); cwd = b.dataset.into; draw(); }));
    $$("input[data-p]", el).forEach((c) => c.addEventListener("change", () => { c.checked ? sel.add(c.dataset.p) : sel.delete(c.dataset.p); }));
  };
  draw();
  const foot = document.createElement("div");
  foot.className = "sheet-foot";
  foot.innerHTML = `<button class="btn left" data-x="here">${icon("extract")}Extract here</button>
    ${o.conn ? `<button class="btn primary" data-x="across">${icon("extract")}Extract to ${esc(connName(o.conn))}</button>` : ""}`;
  $(".sheet", el).append(foot);
  foot.onclick = async (ev) => {
    const b = ev.target.closest("[data-x]");
    if (!b) return;
    const across = b.dataset.x === "across";
    const dst = across ? o : p;
    try {
      await POST("/api/archive/extract", { conn: p.conn, path: e.path, dst_conn: dst.conn, dst_dir: dst.path,
        members: [...sel], password, folder: $("#arc-folder", el).value.trim(), into_folder: true, conflict: S.prefs.conflict });
      closeAll();
      toast(`Extracting to ${connName(dst.conn)}`, { action: { label: "Show", fn: openTransfers } });
    } catch (err) { toast(err.message, { err: true }); }
  };
}

// ---------------------------------------------------------------- connections
const TYPE_LABEL = { local: "This NAS", sftp: "Mac or Linux (SFTP)", smb: "Windows share (SMB)", ftp: "FTP", dropbox: "Dropbox" };

function connSummary(c) {
  const k = c.config;
  if (c.type === "local") return `Folder ${k.root}`;
  if (c.type === "dropbox") return k.has_refresh_token ? "Signed in" : "Not signed in yet";
  const who = k.username ? `${k.username}@` : "";
  const extra = c.type === "smb" ? `/${k.share || ""}` : "";
  return `${who}${k.host || ""}${k.port ? `:${k.port}` : ""}${extra}`;
}

async function openConnections() {
  try { await loadConns(); } catch {}
  const el = sheet({
    title: "Connections", wide: true, kind: "connections",
    body: `<div class="conn-list">${S.conns.map((c) => `<button class="conn-item" data-edit="${c.id}">
        <span class="type-ico">${icon(typeIcon(c.type))}</span><span class="t"><b>${esc(c.name)}</b><span>${esc(TYPE_LABEL[c.type])}, ${esc(connSummary(c))}</span></span>${icon("right")}</button>`).join("") || `<p class="muted">No connections yet.</p>`}</div>
      <button class="btn primary" data-add>${icon("plus")}Add a connection</button>`,
  });
  el.onclick = (e) => {
    const b = e.target.closest("button");
    if (!b) return;
    if (b.dataset.edit) editConnection(conn(b.dataset.edit));
    if (b.dataset.add !== undefined) editConnection(null);
  };
}

const FIELDS = {
  local: [["root", "Folder inside Ferry", "text", "/nas", "The NAS volume is mounted at /nas"]],
  sftp: [["host", "Host", "text", "100.x.x.x or mac.local"], ["port", "Port", "number", "22"], ["username", "User", "text", ""],
         ["root", "Start folder", "text", "Home folder", "Leave blank for the home folder"]],
  smb: [["host", "Host", "text", "192.168.1.20"], ["port", "Port", "number", "445"], ["share", "Share", "text", "Media"],
        ["username", "User", "text", ""], ["password", "Password", "password", ""], ["root", "Start folder", "text", "/"]],
  ftp: [["host", "Host", "text", "ftp.example.com"], ["port", "Port", "number", "21"], ["username", "User", "text", ""],
        ["password", "Password", "password", ""], ["root", "Start folder", "text", "/"]],
  dropbox: [["app_key", "Dropbox app key", "text", "", "From dropbox.com/developers/apps, see the steps below"],
            ["root", "Start folder", "text", "/"]],
};

async function editConnection(c) {
  const isNew = !c;
  let type = c?.type || "sftp";
  let cfg = { ...(c?.config || {}) };
  let dropboxFlow = null;
  let sshKey = null;
  try { sshKey = await GET("/api/ssh-key"); } catch {}
  const el = sheet({ title: isNew ? "Add a connection" : `Edit ${c.name}`, wide: true, kind: "edit-conn", body: `<form id="cf"></form>`,
    foot: `${isNew ? "" : `<button class="btn ghost danger left" data-del>${icon("trash")}Remove</button>`}
      <button class="btn" data-test>Test</button><button class="btn primary" form="cf">${isNew ? "Add connection" : "Save"}</button>` });
  const form = $("#cf", el);
  const draw = () => {
    const f = FIELDS[type];
    const val = (k) => (cfg[k] ?? "");
    let html = `
      <label class="field"><span>Name</span><input name="name" value="${esc(c?.name || "")}" placeholder="${type === "sftp" ? "MacBook" : TYPE_LABEL[type]}" required></label>
      <div class="field"><span>Type</span><div class="seg" role="group">${Object.keys(FIELDS).map((t) => `<button type="button" data-type="${t}" aria-pressed="${t === type}" ${!isNew ? "disabled" : ""}>${esc(TYPE_LABEL[t])}</button>`).join("")}</div></div>`;
    const fieldHtml = (k, label, t, ph, help) => `<label class="field"><span>${esc(label)}</span>
      <input name="${k}" type="${t}" value="${esc(t === "password" ? "" : val(k))}" placeholder="${esc(t === "password" && cfg[`has_${k}`] ? "Saved, leave blank to keep" : ph)}" autocomplete="off" spellcheck="false" autocapitalize="off">
      ${help ? `<small>${esc(help)}</small>` : ""}</label>`;
    const hostPort = f.find((x) => x[0] === "host") && f.find((x) => x[0] === "port");
    for (const fd of f) {
      if (hostPort && fd[0] === "port") continue;
      if (hostPort && fd[0] === "host") {
        const pf = f.find((x) => x[0] === "port");
        html += `<div class="field-row">${fieldHtml(...fd)}${fieldHtml(...pf)}</div>`;
        continue;
      }
      html += fieldHtml(...fd);
    }
    if (type === "sftp") {
      const auth = cfg.auth || "key";
      html += `<div class="field"><span>Sign in with</span><div class="seg"><button type="button" data-auth="key" aria-pressed="${auth === "key"}">Ferry's key</button><button type="button" data-auth="password" aria-pressed="${auth === "password"}">Password</button></div></div>`;
      if (auth === "password") html += fieldHtml("password", "Password", "password", "");
      else if (sshKey) html += `<div class="field"><span>Ferry's key</span>
        <small>On the Mac, turn on System Settings, General, Sharing, Remote Login, and tick “Allow full disk access for remote users”. Then add this line to ~/.ssh/authorized_keys. It only allows file transfer, no shell.</small>
        <div class="codebox"><code>${esc(sshKey.authorized_keys_line)}</code><button type="button" class="icon-btn small" data-copy-key title="Copy">${icon("copy")}</button></div></div>`;
      if (cfg.host_key) html += `<p class="small muted">Trusted host key saved. <button type="button" class="btn ghost" style="height:28px;padding:0 8px" data-forget-hk>Forget it</button></p>`;
    }
    if (type === "sftp" || type === "smb") {
      const via = cfg.via || "auto";
      html += `<div class="field"><span>Route</span><div class="seg" role="group">
        <button type="button" data-via="auto" aria-pressed="${via === "auto"}">Automatic</button>
        <button type="button" data-via="tailscale" aria-pressed="${via === "tailscale"}">Through Tailscale</button>
        <button type="button" data-via="direct" aria-pressed="${via === "direct"}">Direct</button></div>
        <small>${sshKey?.tailscale ? "Automatic sends 100.x and .ts.net addresses through Tailscale, everything else direct." : "Tailscale isn't mounted into Ferry, so connections go direct."}</small></div>`;
    }
    if (type === "dropbox") {
      html += `<div class="note small">One time setup: at dropbox.com/developers/apps make an app with Scoped access and Full Dropbox. Under Permissions tick files.metadata.read, files.content.read and files.content.write, then submit. Paste its App key above and sign in.</div>
        <div class="field"><span>Dropbox account</span><div>${cfg.has_refresh_token || cfg.refresh_token ? `<span class="note good" style="display:inline-block;margin:0 8px 0 0">Signed in</span>` : ""}
        <button type="button" class="btn" data-dbx>${cfg.has_refresh_token || cfg.refresh_token ? "Sign in again" : "Sign in to Dropbox"}</button></div></div>
        <div id="dbx-code" hidden><label class="field"><span>Code from Dropbox</span><input name="dbx_code" autocomplete="off"><small>Approve Ferry in the tab that opened, then paste the code here.</small></label>
        <button type="button" class="btn primary" data-dbx-finish>Finish sign in</button></div>`;
    }
    const trashDefault = { local: "@synology", sftp: "/delete_me", smb: "/delete_me", ftp: "/delete_me", dropbox: "" }[type];
    const trash = cfg.trash ?? trashDefault;
    html += `<label class="field"><span>Deleted files go to</span>
      <input name="trash" value="${esc(trash)}" placeholder="Leave blank to delete for good" spellcheck="false" autocapitalize="off">
      <small>${type === "local" ? "@synology puts them in each shared folder's #recycle. " : type === "sftp" ? "For a Mac use /.Trash so they land in its Trash. " : ""}Blank deletes for good.</small></label>
      <div id="test-out"></div>`;
    form.innerHTML = html;
  };
  draw();
  const collect = () => {
    const fd = new FormData(form);
    const out = { ...cfg };
    for (const [k] of FIELDS[type]) {
      if (fd.has(k)) out[k] = k === "port" ? (fd.get(k) ? Number(fd.get(k)) : "") : fd.get(k).trim();
    }
    if (type === "sftp") { out.auth = cfg.auth || "key"; if (fd.has("password")) out.password = fd.get("password"); }
    if (type === "sftp" || type === "smb") out.via = cfg.via || "auto";
    out.trash = (fd.get("trash") || "").trim();
    delete out.has_password; delete out.has_refresh_token;
    return { name: (fd.get("name") || "").trim(), type, config: out };
  };
  form.addEventListener("click", async (e) => {
    const b = e.target.closest("button");
    if (!b) return;
    if (b.dataset.type) { const keep = collect(); type = b.dataset.type; cfg = { name: keep.name }; draw(); form.name.value = keep.name; }
    if (b.dataset.auth) { Object.assign(cfg, collect().config); cfg.auth = b.dataset.auth; draw(); }
    if (b.dataset.via) { Object.assign(cfg, collect().config); cfg.via = b.dataset.via; draw(); }
    if (b.dataset.copyKey !== undefined) navigator.clipboard?.writeText(sshKey.authorized_keys_line).then(() => toast("Copied"), () => {});
    if (b.dataset.forgetHk !== undefined) { cfg.host_key = ""; Object.assign(cfg, collect().config); cfg.host_key = ""; draw(); toast("Host key forgotten. Save to apply."); }
    if (b.dataset.dbx !== undefined) {
      const key = form.app_key.value.trim();
      if (!key) return toast("Paste the app key first.", { err: true });
      try {
        dropboxFlow = await POST("/api/dropbox/start", { app_key: key });
        window.open(dropboxFlow.url, "_blank", "noopener");
        $("#dbx-code", form).hidden = false;
        form.dbx_code.focus();
      } catch (err) { toast(err.message, { err: true }); }
    }
    if (b.dataset.dbxFinish !== undefined) {
      try {
        const r = await POST("/api/dropbox/finish", { flow_id: dropboxFlow.flow_id, code: form.dbx_code.value });
        Object.assign(cfg, collect().config);
        cfg.refresh_token = r.refresh_token;
        draw();
        toast("Dropbox signed in. Save to keep it.");
      } catch (err) { toast(err.message, { err: true }); }
    }
  });
  $("[data-test]", el).onclick = async () => {
    const out = $("#test-out", form);
    out.innerHTML = `<p class="muted">Testing…</p>`;
    const body = collect();
    try {
      const r = await POST("/api/connections/test", body, c ? { cid: c.id } : undefined);
      out.innerHTML = r.ok ? `<div class="note good">Connected. ${plural(r.items, "item")} at the top level${r.usage?.free ? `, ${fmtSize(r.usage.free)} free` : ""}.</div>`
                           : `<div class="note err">${esc(r.error)}</div>`;
      if (r.ok && r.host_key && !cfg.host_key) cfg.host_key = r.host_key;
    } catch (err) { out.innerHTML = `<div class="note err">${esc(err.message)}</div>`; }
  };
  const del = $("[data-del]", el);
  if (del) del.onclick = async () => {
    const ok = await confirmSheet({ title: `Remove ${c.name}?`, body: "This only removes the connection from Ferry. No files are touched.", ok: "Remove", danger: true });
    if (!ok) return openConnections();
    await api("DELETE", `/api/connections/${c.id}`);
    await loadConns();
    S.panes.forEach((p) => { if (p.conn === c.id) setConn(p.i, S.conns[0]?.id || null); });
    openConnections();
  };
  form.onsubmit = async (e) => {
    e.preventDefault();
    const body = collect();
    if (!body.name) body.name = TYPE_LABEL[type];
    try {
      const saved = isNew ? await POST("/api/connections", body) : await api("PUT", `/api/connections/${c.id}`, body);
      await loadConns();
      toast(isNew ? `Added ${saved.name}` : `Saved ${saved.name}`);
      const emptyPane = S.panes.find((p) => !p.conn);
      if (emptyPane) setConn(emptyPane.i, saved.id);
      else if (!isNew) S.panes.forEach((p) => p.conn === saved.id && setConn(p.i, saved.id));
      renderAll();
      openConnections();
    } catch (err) { toast(err.message, { err: true }); }
  };
}

// ---------------------------------------------------------------- settings
function openSettings() {
  const pr = S.prefs;
  const seg = (name, opts, cur) => `<div class="seg" data-pref="${name}">${opts.map(([v, l]) => `<button type="button" data-v="${v}" aria-pressed="${cur === v}">${l}</button>`).join("")}</div>`;
  const el = sheet({ title: "Settings", kind: "settings", body: `
    <div class="field"><span>Look</span>${seg("theme", [["auto", "Match device"], ["light", "Day"], ["dark", "Night"]], pr.theme)}</div>
    <div class="field"><span>When a file is already there</span>${seg("conflict", [["rename", "Keep both"], ["overwrite", "Replace"], ["skip", "Skip it"]], pr.conflict)}</div>
    <label class="toggle"><input type="checkbox" data-bool="groupLive" ${pr.groupLive ? "checked" : ""}>Treat Live Photos as one item (the HEIC and its MOV move together)</label>
    <label class="toggle"><input type="checkbox" data-bool="listThumbs" ${pr.listThumbs ? "checked" : ""}>Show photo thumbnails in list view</label>
    <label class="toggle"><input type="checkbox" data-bool="showHidden" ${pr.showHidden ? "checked" : ""}>Show hidden files (dotfiles, @eaDir, #recycle)</label>
    <hr style="border:0;border-top:1px solid var(--contour);margin:18px 0">
    <form id="pwf"><div class="field"><span>Change password</span>
      <input type="password" name="old" placeholder="Current password" autocomplete="current-password" style="margin-bottom:8px">
      <input type="password" name="new" placeholder="New password, 8 or more characters" autocomplete="new-password"></div>
      <button class="btn">Change password</button></form>
    <p class="small muted" style="margin-top:18px">Ferry ${esc(S.me?.version || "")}. Keyboard: Tab switches docks, F5 copies across, F6 moves, F2 renames, Space previews, Delete trashes.</p>`,
    foot: `<button class="btn ghost left" data-logout>Sign out</button><button class="btn" data-done>Done</button>` });
  el.addEventListener("click", (e) => {
    const b = e.target.closest(".seg button");
    if (b) { const k = b.closest(".seg").dataset.pref; S.prefs[k] = b.dataset.v; savePrefs(); $$("button", b.closest(".seg")).forEach((x) => x.setAttribute("aria-pressed", x === b)); }
  });
  $$("[data-bool]", el).forEach((c) => c.onchange = () => { S.prefs[c.dataset.bool] = c.checked; savePrefs(); S.panes.forEach((p) => { computeVisible(p.i); renderList(p.i); renderFoot(p.i); }); });
  $("[data-done]", el).onclick = closeAll;
  $("[data-logout]", el).onclick = async () => { await POST("/api/logout").catch(() => {}); if (es) es.close(); showLogin(); };
  $("#pwf", el).onsubmit = async (e) => {
    e.preventDefault();
    try { await POST("/api/password", { old: e.target.old.value, new: e.target.new.value }); toast("Password changed. Other devices need to sign in again."); e.target.reset(); }
    catch (err) { toast(err.message, { err: true }); }
  };
}

// ---------------------------------------------------------------- events
function paneOf(el) { const p = el.closest(".pane"); return p ? +p.dataset.pane : null; }

function bindFrame() {
  const app = $("#app");
  app.addEventListener("click", (e) => {
    const t = e.target;
    const pi = paneOf(t);
    if (pi !== null) setActive(pi);
    const actEl = t.closest("[data-act]");
    const act = actEl?.dataset.act;
    const ip = actEl?.dataset.pane !== undefined ? +actEl.dataset.pane : pi ?? S.active;
    if (act) {
      switch (act) {
        case "transfers": return openTransfers();
        case "connections": return openConnections();
        case "settings": return openSettings();
        case "dock": setActive(+actEl.dataset.pane); S.panes.forEach((p) => paneEl(p.i).classList.toggle("off", S.active !== p.i)); return;
        case "pick-conn": return connPicker(ip, actEl);
        case "nav": return navigate(ip, actEl.dataset.path);
        case "edit-path": S.panes[ip].editingPath = true; return renderCrumbs(ip);
        case "up": return navigate(ip, dirname(S.panes[ip].path));
        case "refresh": S.panes[ip].usage = null; return load(ip, { keepSel: true });
        case "mkdir": return mkdirFlow(ip);
        case "upload": S.uploadTarget = ip; return $("#upload-input").click();
        case "view": S.panes[ip].view = S.panes[ip].view === "grid" ? "list" : "grid"; savePanes(); renderTools(ip); return renderList(ip);
        case "sort": return sortMenu(ip, actEl);
        case "cross": return transfer("copy", +actEl.dataset.from);
        case "copy-across": return transfer("copy", S.active);
        case "move-across": return transfer("move", S.active);
        case "download": return download(S.panes[S.active].conn, selected(S.active));
        case "rename": return renameFlow(S.active);
        case "delete": return deleteFlow(S.active);
        case "extract": { const s = selected(S.active)[0]; return s && openArchive(S.active, s); }
        case "more": return moreSheet(S.active);
      }
    }
    const row = t.closest(".row");
    if (row && pi !== null) {
      if (longPressFired) { longPressFired = false; return; }
      // on phones the icon is the checkbox; on desktop it behaves like the row
      if (MOBILE.matches && t.closest("[data-toggle]")) return toggleRow(pi, +row.dataset.i);
      return clickRow(pi, +row.dataset.i, e);
    }
    if (t.closest(".list") && pi !== null && !MOBILE.matches) {
      S.panes[pi].sel.clear();
      updateRowSelection(pi);
    }
  });
  app.addEventListener("dblclick", (e) => {
    const row = e.target.closest(".row");
    const pi = paneOf(e.target);
    if (row && pi !== null && !MOBILE.matches) open(pi, S.panes[pi].visible[+row.dataset.i]);
  });
  app.addEventListener("contextmenu", (e) => {
    const row = e.target.closest(".row");
    const pi = paneOf(e.target);
    if (!row || pi === null || MOBILE.matches) return;
    e.preventDefault();
    setActive(pi);
    const p = S.panes[pi];
    const ent = p.visible[+row.dataset.i];
    if (!p.sel.has(ent.path)) { p.sel = new Set([ent.path]); p.cursor = p.anchor = +row.dataset.i; updateRowSelection(pi); }
    const m = popover(null, actionsMenuHtml(pi), { x: e.clientX, y: e.clientY });
    m.onclick = (ev) => { const b = ev.target.closest("button[data-m]"); if (b) runMenu(pi, b.dataset.m); };
  });

  // long press to start selecting on phones
  let lpTimer = null, lpStart = null;
  app.addEventListener("pointerdown", (e) => {
    if (e.pointerType !== "touch") return;
    const row = e.target.closest(".row");
    if (!row || e.target.closest("[data-act]")) return;
    lpStart = { x: e.clientX, y: e.clientY };
    lpTimer = setTimeout(() => {
      const pi = paneOf(row);
      const p = S.panes[pi];
      const ent = p.visible[+row.dataset.i];
      if (!ent) return;
      p.sel.add(ent.path);
      p.cursor = p.anchor = +row.dataset.i;
      S.selectMode = true;
      longPressFired = true;
      navigator.vibrate?.(15);
      updateRowSelection(pi);
    }, 480);
  });
  const cancelLp = (e) => { if (lpTimer && (!lpStart || e.type !== "pointermove" || Math.hypot(e.clientX - lpStart.x, e.clientY - lpStart.y) > 10)) { clearTimeout(lpTimer); lpTimer = null; } };
  ["pointerup", "pointercancel", "pointermove"].forEach((t) => app.addEventListener(t, cancelLp, { passive: true }));
  app.addEventListener("contextmenu", (e) => { if (MOBILE.matches && e.target.closest(".row")) e.preventDefault(); });

  // drag and drop
  app.addEventListener("dragstart", (e) => {
    const row = e.target.closest(".row");
    const pi = paneOf(e.target);
    if (!row || pi === null) return;
    const p = S.panes[pi];
    const ent = p.visible[+row.dataset.i];
    if (!p.sel.has(ent.path)) { p.sel = new Set([ent.path]); updateRowSelection(pi); }
    e.dataTransfer.setData("application/x-ferry", JSON.stringify({ pane: pi, paths: selectionPaths(pi) }));
    e.dataTransfer.effectAllowed = "copyMove";
    dragFrom = pi;
  });
  app.addEventListener("dragend", () => { dragFrom = null; $$(".drop-target").forEach((x) => x.classList.remove("drop-target")); });
  app.addEventListener("dragover", (e) => {
    const pi = paneOf(e.target);
    if (pi === null) return;
    const types = [...e.dataTransfer.types];
    const internal = types.includes("application/x-ferry");
    if (internal && dragFrom === pi && !e.target.closest('.row[data-kind="dir"]')) return;
    if (!internal && !types.includes("Files")) return;
    e.preventDefault();
    e.dataTransfer.dropEffect = internal && e.shiftKey ? "move" : "copy";
    $$(".drop-target").forEach((x) => x.classList.remove("drop-target"));
    paneEl(pi).classList.add("drop-target");
  });
  app.addEventListener("dragleave", (e) => { if (!e.relatedTarget || !paneEl(paneOf(e.target) ?? 0)?.contains(e.relatedTarget)) $$(".drop-target").forEach((x) => x.classList.remove("drop-target")); });
  app.addEventListener("drop", async (e) => {
    const pi = paneOf(e.target);
    $$(".drop-target").forEach((x) => x.classList.remove("drop-target"));
    if (pi === null) return;
    e.preventDefault();
    const p = S.panes[pi];
    const dirRow = e.target.closest('.row[data-kind="dir"]');
    const dstDir = dirRow ? p.visible[+dirRow.dataset.i].path : p.path;
    const raw = e.dataTransfer.getData("application/x-ferry");
    if (raw) {
      const d = JSON.parse(raw);
      if (d.paths.includes(dstDir)) return;
      if (d.pane === pi) {
        // dropping onto a folder in the same dock moves it there
        try {
          await POST("/api/transfer", { kind: "move", src_conn: p.conn, paths: d.paths, dst_conn: p.conn, dst_dir: dstDir, conflict: S.prefs.conflict });
        } catch (err) { toast(err.message, { err: true }); }
        return;
      }
      return transfer(e.shiftKey ? "move" : "copy", d.pane, dstDir, d.paths);
    }
    const files = await filesFromDrop(e.dataTransfer);
    if (files.length) uploadFiles(pi, files, dstDir);
  });

  $("#upload-input").onchange = (e) => {
    const files = [...e.target.files].map((f) => ({ file: f, rel: f.name }));
    if (files.length) uploadFiles(S.uploadTarget ?? S.active, files);
    e.target.value = "";
  };
}

let longPressFired = false;
let dragFrom = null;

function sortMenu(i, anchor) {
  const p = S.panes[i];
  const opts = [["name", "Name"], ["mtime", "Date modified"], ["size", "Size"], ["kind", "Type"]];
  const m = popover(anchor, opts.map(([k, l]) => `<button data-k="${k}">${l}${p.sort.key === k ? ` ${p.sort.dir > 0 ? "↑" : "↓"}` : ""}</button>`).join(""));
  m.onclick = (e) => {
    const b = e.target.closest("button[data-k]");
    if (!b) return;
    const k = b.dataset.k;
    p.sort = p.sort.key === k ? { key: k, dir: -p.sort.dir } : { key: k, dir: k === "name" || k === "kind" ? 1 : -1 };
    savePanes();
    renderTools(i);
    computeVisible(i);
    renderList(i);
  };
}

document.addEventListener("keydown", (e) => {
  if (!S.me) return;
  if (viewerState) {
    if (e.key === "Escape" || e.key === " " && !$(".viewer video, .viewer audio")) { e.preventDefault(); return closeViewer(); }
    if (e.key === "ArrowRight") return stepViewer(1);
    if (e.key === "ArrowLeft") return stepViewer(-1);
    return;
  }
  if (S.sheet) { if (e.key === "Escape") S.sheet.close(); return; }
  if (e.target.closest("input, textarea, select")) return;
  const i = S.active;
  const p = S.panes[i];
  const mod = e.metaKey || e.ctrlKey;
  const cur = p.visible[p.cursor];
  switch (e.key) {
    case "Tab": e.preventDefault(); setActive(other(i)); $(".list", paneEl(S.active)).focus(); return;
    case "ArrowDown": e.preventDefault(); return moveCursor(i, p.view === "grid" ? 2 : 1, e.shiftKey);
    case "ArrowUp": e.preventDefault(); if (e.altKey) return navigate(i, dirname(p.path)); return moveCursor(i, p.view === "grid" ? -2 : -1, e.shiftKey);
    case "ArrowRight": if (p.view === "grid") { e.preventDefault(); moveCursor(i, 1, e.shiftKey); } return;
    case "ArrowLeft": if (p.view === "grid") { e.preventDefault(); moveCursor(i, -1, e.shiftKey); } return;
    case "Enter": if (cur) { e.preventDefault(); open(i, cur); } return;
    case "Backspace": if (mod) { e.preventDefault(); return deleteFlow(i); } e.preventDefault(); return navigate(i, dirname(p.path));
    case "Delete": e.preventDefault(); return deleteFlow(i);
    case "F2": e.preventDefault(); return renameFlow(i);
    case "F5": e.preventDefault(); return transfer("copy", i);
    case "F6": e.preventDefault(); return transfer("move", i);
    case " ": if (cur && !cur.is_dir) { e.preventDefault(); open(i, cur); } return;
    case "Escape": p.sel.clear(); return updateRowSelection(i);
  }
  if (mod && e.key.toLowerCase() === "a") { e.preventDefault(); p.sel = new Set(p.visible.map((x) => x.path)); updateRowSelection(i); }
  if (mod && e.key.toLowerCase() === "f") { e.preventDefault(); $(".filter input", paneEl(i)).focus(); }
});

window.addEventListener("paste", (e) => {
  if (!S.me || S.sheet || e.target.closest?.("input, textarea")) return;
  const files = [...(e.clipboardData?.files || [])].map((f) => ({ file: f, rel: f.name || `Pasted ${Date.now()}.png` }));
  if (files.length) { e.preventDefault(); uploadFiles(S.active, files); }
});

MOBILE.addEventListener("change", () => { if (S.me) renderAll(); });
window.addEventListener("beforeunload", (e) => {
  if (S.uploads.some((u) => u.status === "running" || u.status === "queued")) { e.preventDefault(); e.returnValue = ""; }
});
document.addEventListener("visibilitychange", () => {
  if (document.visibilityState === "visible" && S.me) S.panes.forEach((p) => p.conn && !p.loading && load(p.i, { keepSel: true }));
});

boot();
