/* TeleGrab front-end — vanilla JS, no build step. */
"use strict";

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

const S = {
  me: null,
  dialogs: [],
  chat: null,
  kind: "media",
  search: "",
  offset: 0,
  messages: [],
  selected: new Set(),
  selecting: false,
  loading: false,
  done: false,
  lbIndex: -1,
  rendered: 0,
  loadToken: 0,
  loginMode: "qr",
};

let qrTimer = null;

/* ------------------------------------------------------------------ utils */

function el(tag, props = {}, children = []) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(props)) {
    if (key === "class") node.className = value;
    else if (key === "html") node.innerHTML = value; // only for trusted static icons
    else if (key.startsWith("on")) node.addEventListener(key.slice(2), value);
    else if (value !== null && value !== undefined) node.setAttribute(key, value);
  }
  for (const child of [].concat(children)) {
    if (child === null || child === undefined) continue;
    node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return node;
}

function icon(name, cls = "ic") {
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("class", cls);
  const use = document.createElementNS("http://www.w3.org/2000/svg", "use");
  use.setAttribute("href", "#i-" + name);
  svg.append(use);
  return svg;
}

function fmtSize(bytes) {
  if (!bytes) return "";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let i = 0, n = bytes;
  while (n >= 1024 && i < units.length - 1) { n /= 1024; i++; }
  return `${n >= 100 || i === 0 ? Math.round(n) : n.toFixed(1)} ${units[i]}`;
}

function fmtDur(sec) {
  if (!sec) return "";
  sec = Math.round(sec);
  const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60), s = sec % 60;
  const mm = h ? String(m).padStart(2, "0") : String(m);
  return `${h ? h + ":" : ""}${mm}:${String(s).padStart(2, "0")}`;
}

function fmtDate(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  return d.toLocaleDateString(undefined, { year: "numeric", month: "short", day: "numeric" });
}

/* Telegram's chat-list clock: time today, weekday this week, else a date. */
function fmtListTime(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  const now = new Date();
  if (d.toDateString() === now.toDateString()) {
    return d.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" });
  }
  const days = (now - d) / 86400000;
  if (days < 7) return d.toLocaleDateString(undefined, { weekday: "short" });
  if (d.getFullYear() === now.getFullYear()) return d.toLocaleDateString(undefined, { day: "numeric", month: "short" });
  return d.toLocaleDateString(undefined, { day: "2-digit", month: "2-digit", year: "2-digit" });
}

function fmtMonth(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  const sameYear = d.getFullYear() === new Date().getFullYear();
  return d.toLocaleDateString(undefined, sameYear ? { month: "long" } : { month: "long", year: "numeric" });
}

function fmtUnread(n) {
  if (n >= 1000) return `${(n / 1000).toFixed(n >= 10000 ? 0 : 1)}K`;
  return String(n);
}

/* Same peer → same colour, like Telegram's placeholder avatars. */
function avatarFor(id, title) {
  const n = Math.abs(Number(String(id).replace(/^-100/, "")) || 0) % 7;
  const node = el("div", { class: `avatar c${n}` }, initials(title));
  const image = el("img", { src: `/api/avatar/${id}`, alt: "", loading: "lazy", decoding: "async" });
  image.addEventListener("error", () => image.remove());
  node.append(image);
  return node;
}

function toast(message, type = "") {
  const node = el("div", { class: "toast " + type }, message);
  $("#toasts").append(node);
  setTimeout(() => node.remove(), type === "err" ? 6500 : 3800);
}

async function api(path, options = {}) {
  const opts = { credentials: "same-origin", ...options };
  if (opts.body && typeof opts.body !== "string") {
    opts.body = JSON.stringify(opts.body);
    opts.headers = { "Content-Type": "application/json", ...(opts.headers || {}) };
  }
  const res = await fetch(path, opts);
  if (!res.ok) {
    let detail = `HTTP ${res.status}`;
    try { const data = await res.json(); if (data && data.detail) detail = data.detail; } catch (_) {}
    if (res.status === 401) showLogin();
    const err = new Error(detail);
    err.status = res.status;
    throw err;
  }
  return res.status === 204 ? null : res.json();
}

function debounce(fn, ms) {
  let timer;
  return (...args) => { clearTimeout(timer); timer = setTimeout(() => fn(...args), ms); };
}

/* ------------------------------------------------------------------ boot */

async function boot() {
  bindGlobal();
  let status;
  try {
    status = await api("/api/status");
  } catch (err) {
    showLogin();
    showLoginError(err.message);
    return;
  }
  if (!status.configured) {
    S.loginMode = "phone";
    showLogin();
    const warning = $("#cfg-warning");
    warning.classList.remove("hidden");
    warning.textContent = status.message || "Telegram API credentials are missing.";
    $("#step-phone button").disabled = true;
    return;
  }
  if (status.authorized) enterApp(status.me);
  else showLogin();
}

function showLogin() {
  $("#app").classList.add("hidden");
  $("#login").classList.remove("hidden");
  $("#lightbox").classList.add("hidden");
  if (S.loginMode === "phone") {
    stopQr();
    setStep("phone");
  } else {
    setLoginMode("qr");
  }
}

function showLoginError(message) {
  const box = $("#login-error");
  box.textContent = message;
  box.classList.remove("hidden");
}

function applyDelivery(res) {
  const hint = $("#code-hint");
  if (res && res.hint) {
    hint.textContent = res.hint;
    hint.dataset.channel = res.delivery || "";
  } else {
    hint.textContent = "Telegram accepted the request.";
  }
}

function showCodeStep(res) {
  setStep("code");
  applyDelivery(res);
  const label = res && res.delivery_label ? res.delivery_label : "Telegram";
  toast(`Code sent ${label}.`, "ok");
}

function setStep(step) {
  $("#login-error").classList.add("hidden");
  $("#qr-panel").classList.toggle("hidden", step !== "qr");
  $("#step-phone").classList.toggle("hidden", step !== "phone");
  $("#step-code").classList.toggle("hidden", step !== "code");
  $("#step-password").classList.toggle("hidden", step !== "password");
  const focus = { phone: "#phone", code: "#code", password: "#password" }[step];
  if (focus) setTimeout(() => $(focus)?.focus(), 60);
}

function setLoginMode(mode) {
  S.loginMode = mode;
  if (mode === "qr") {
    setStep("qr");
    startQr();
  } else {
    stopQr();
    setStep("phone");
  }
}

/* ------------------------------------------------------------ QR login */

function stopQr() {
  if (qrTimer) { clearInterval(qrTimer); qrTimer = null; }
}

async function startQr(fresh = false) {
  stopQr();
  const status = $("#qr-status");
  status.textContent = "Connecting to Telegram…";
  $("#qr-img").removeAttribute("src");
  try {
    const query = fresh ? "?fresh=1" : "";
    const res = await api(`/api/auth/qr${query}`, { method: "POST" });
    if (applyQr(res)) return;
    qrTimer = setInterval(pollQr, 2000);
  } catch (err) {
    status.textContent = err.message;
  }
}

async function pollQr() {
  try {
    applyQr(await api("/api/auth/qr"));
  } catch (_) {
    /* transient — keep polling */
  }
}

/* Returns true when the flow has moved past the QR panel. */
function applyQr(res) {
  if (!res) return false;
  if (res.status === "ok" && res.me) {
    stopQr();
    enterApp(res.me);
    return true;
  }
  if (res.status === "password_needed") {
    stopQr();
    setStep("password");
    return true;
  }
  if (res.status === "error") {
    stopQr();
    $("#qr-status").textContent = res.error || "QR login failed.";
    return false;
  }
  if (res.image) $("#qr-img").src = res.image;
  if (res.status === "pending") {
    $("#qr-status").textContent = "Waiting for you to scan…";
  } else if (res.status === "idle") {
    $("#qr-status").textContent = "No login in progress.";
  }
  return false;
}

function enterApp(me) {
  stopQr();
  S.me = me;
  $("#login").classList.add("hidden");
  $("#app").classList.remove("hidden");
  $("#login-error").classList.add("hidden");
  const account = $("#account");
  account.replaceChildren(
    avatarFor(me.id, me.name),
    el("div", { class: "who" }, [
      el("b", {}, me.name || ""),
      el("span", {}, me.username ? "@" + me.username : me.phone ? "+" + me.phone : ""),
    ])
  );
  loadDialogs();
}

/* ------------------------------------------------------------------ auth */

function bindAuth() {
  $("#qr-refresh").addEventListener("click", () => startQr(true));
  $("#qr-use-phone").addEventListener("click", () => setLoginMode("phone"));
  $("#use-qr").addEventListener("click", () => setLoginMode("qr"));

  $("#step-phone").addEventListener("submit", async (event) => {
    event.preventDefault();
    const phone = $("#phone").value.trim();
    if (!phone) return;
    const button = $("#step-phone button");
    button.disabled = true;
    try {
      const res = await api("/api/auth/start", { method: "POST", body: { phone } });
      if (res.status === "ok" && res.me) enterApp(res.me);
      else showCodeStep(res);
    } catch (err) {
      showLoginError(err.message);
    } finally {
      button.disabled = false;
    }
  });

  $("#step-code").addEventListener("submit", async (event) => {
    event.preventDefault();
    const code = $("#code").value.trim();
    const button = $("#step-code button.primary");
    button.disabled = true;
    try {
      const res = await api("/api/auth/code", { method: "POST", body: { code } });
      if (res.status === "password_needed") setStep("password");
      else if (res.me) enterApp(res.me);
    } catch (err) {
      showLoginError(err.message);
    } finally {
      button.disabled = false;
    }
  });

  $("#step-password").addEventListener("submit", async (event) => {
    event.preventDefault();
    const password = $("#password").value;
    const button = $("#step-password button");
    button.disabled = true;
    try {
      const res = await api("/api/auth/password", { method: "POST", body: { password } });
      if (res.me) enterApp(res.me);
    } catch (err) {
      showLoginError(err.message);
    } finally {
      button.disabled = false;
    }
  });

  $("#resend-btn").addEventListener("click", async () => {
    const button = $("#resend-btn");
    button.disabled = true;
    try {
      const res = await api("/api/auth/resend", { method: "POST" });
      applyDelivery(res);
      toast("New code requested.", "ok");
    } catch (err) {
      showLoginError(err.message);
    } finally {
      button.disabled = false;
    }
  });

  $("#diag-btn").addEventListener("click", async () => {
    const out = $("#diag-out");
    out.classList.remove("hidden");
    out.textContent = "Testing…";
    try {
      const res = await api("/api/diag");
      out.textContent = JSON.stringify(res, null, 2);
    } catch (err) {
      out.textContent = err.message;
    }
  });

  $$("[data-restart]").forEach((button) =>
    button.addEventListener("click", () => {
      $("#phone").value = "";
      $("#code").value = "";
      $("#password").value = "";
      $("#code-hint").textContent = "";
      S.loginMode = "phone";
      stopQr();
      setStep("phone");
    })
  );

  $("#logout-btn").addEventListener("click", async () => {
    if (!confirm("Log out and revoke this login on Telegram?")) return;
    try { await api("/api/auth/logout", { method: "POST" }); } catch (_) {}
    location.reload();
  });
}

/* ------------------------------------------------------------------ dialogs */

async function loadDialogs() {
  const list = $("#dialog-list");
  list.replaceChildren(el("div", { class: "list-empty" }, "Loading chats…"));
  try {
    const res = await api("/api/dialogs");
    S.dialogs = res.dialogs || [];
    renderDialogs();
  } catch (err) {
    list.replaceChildren(el("div", { class: "list-empty" }, err.message));
  }
}

function renderDialogs() {
  const query = $("#chat-search").value.trim().toLowerCase();
  const list = $("#dialog-list");
  const items = S.dialogs.filter((d) => !query || d.title.toLowerCase().includes(query));
  list.replaceChildren();
  if (!items.length) {
    list.append(el("div", { class: "list-empty" }, "No chats match."));
    return;
  }
  const frag = document.createDocumentFragment();
  for (const dialog of items) {
    const typeIcon = { channel: "mega", group: "users" }[dialog.type];
    const nameParts = [];
    if (typeIcon) nameParts.push(icon(typeIcon));
    nameParts.push(el("span", { class: "t" }, dialog.title));
    if (dialog.muted) nameParts.push(icon("mute", "ic mute"));

    const node = el("div", { class: "dialog", "data-id": dialog.id }, [
      avatarFor(dialog.id, dialog.title),
      el("div", { class: "meta" }, [
        el("div", { class: "line" }, [
          el("div", { class: "name" }, nameParts),
          el("span", { class: "time" }, fmtListTime(dialog.date)),
        ]),
        el("div", { class: "line" }, [
          el("div", { class: "sub" }, dialog.preview || labelFor(dialog)),
          dialog.noforwards ? el("span", { title: "Saving disabled" }, icon("lock", "ic lock")) : null,
          dialog.unread
            ? el("span", { class: "unread" + (dialog.muted ? " muted" : "") }, fmtUnread(dialog.unread))
            : null,
        ]),
      ]),
    ]);
    if (S.chat && String(S.chat.id) === String(dialog.id)) node.classList.add("active");
    node.addEventListener("click", () => {
      $$(".dialog").forEach((d) => d.classList.remove("active"));
      node.classList.add("active");
      openChat(dialog);
    });
    frag.append(node);
  }
  list.append(frag);
}

function labelFor(dialog) {
  if (dialog.type === "channel") return "channel";
  if (dialog.type === "group") return "group";
  if (dialog.type === "user") return "private chat";
  return "chat";
}

function initials(title) {
  return (title || "?").trim().slice(0, 1).toUpperCase();
}

/* ------------------------------------------------------------------ chat */

function openChat(dialog) {
  S.chat = dialog;
  S.messages = [];
  S.selected.clear();
  S.selecting = false;
  S.offset = 0;
  S.done = false;
  S.lbIndex = -1;
  S.rendered = 0;
  S.lastMonth = "";
  $("#empty").classList.add("hidden");
  $("#chat").classList.remove("hidden");
  $("#app").classList.add("chat-open");
  $("#chat-name").textContent = dialog.title;
  $("#chat-type").textContent = dialog.username ? "@" + dialog.username : labelFor(dialog);
  $("#chat-badge").classList.toggle("hidden", !dialog.noforwards);
  $("#grid").replaceChildren();
  $("#grid-status").textContent = "";
  $("#dl-loaded").disabled = true;
  $("#select-btn").classList.remove("active");
  $(".chat-head").classList.remove("searching");
  document.body.classList.remove("selecting");
  updateBulkBar();

  $("#chat-avatar").replaceWith(Object.assign(avatarFor(dialog.id, dialog.title), { id: "chat-avatar" }));

  loadMedia(true);
}

async function loadMedia(reset = false) {
  if (!S.chat) return;
  if (!reset && (S.loading || S.done)) return;
  const chatId = S.chat.id;
  const token = (S.loadToken = (S.loadToken || 0) + 1);
  S.loading = true;
  if (reset) {
    S.offset = 0;
    S.done = false;
    S.messages = [];
    S.selected.clear();
    S.rendered = 0;
    S.lastMonth = "";
    $("#grid").replaceChildren();
    $("#grid-status").textContent = "Loading…";
  } else {
    $("#grid-status").textContent = "Loading more…";
  }
  try {
    const params = new URLSearchParams({
      limit: "48",
      offset_id: String(S.offset),
      kind: S.kind,
      search: S.search,
    });
    const res = await api(`/api/dialogs/${chatId}/messages?${params}`);
    if (token !== S.loadToken || S.chat?.id !== chatId) return; // superseded
    const raw = res.messages || [];
    const known = new Set(S.messages.map((m) => m.id));
    const incoming = raw.filter((m) => !known.has(m.id));
    S.offset = res.next_offset || 0;
    S.done = !S.offset || raw.length === 0;
    S.messages.push(...incoming);
    appendTiles();
    $("#grid-status").textContent = "";
    if (S.done && !S.messages.length) $("#grid").replaceChildren(el("div", { class: "grid-empty" }, "No media here yet."));
    $("#dl-loaded").disabled = !S.messages.length;
  } catch (err) {
    if (token === S.loadToken) $("#grid-status").textContent = err.message;
  } finally {
    if (token === S.loadToken) S.loading = false;
  }
}

function appendTiles() {
  const grid = $("#grid");
  if (!S.messages.length) {
    if (!grid.querySelector(".grid-empty")) {
      grid.append(el("div", { class: "grid-empty" }, "Nothing matched."));
    }
    return;
  }
  grid.querySelector(".grid-empty")?.remove();
  const frag = document.createDocumentFragment();
  for (; S.rendered < S.messages.length; S.rendered++) {
    const item = S.messages[S.rendered];
    const month = fmtMonth(item.date);
    if (month && month !== S.lastMonth) {
      S.lastMonth = month;
      frag.append(el("div", { class: "date-sep" }, month));
    }
    frag.append(tile(item));
  }
  grid.append(frag);
  updateBulkBar();
}

const KIND_ICON = {
  photo: "img",
  video: "vid",
  animation: "play",
  voice: "music",
  audio: "music",
  file: "file",
  sticker: "img",
};

function tile(item) {
  const media = item.media;
  const kind = media.kind;
  const paid = media.paid || null;
  const locked = !!(paid && !paid.unlocked);
  const node = el("div", { class: "tile" + (locked ? " paid" : ""), "data-id": item.id });
  if (S.selected.has(item.id)) node.classList.add("selected");

  const placeholder = () => {
    const ph = el("div", { class: "ph" }, icon(KIND_ICON[kind] || "file"));
    node.querySelector("img")?.remove();
    node.prepend(ph);
    node.classList.add("ready");
  };

  if (kind === "photo" || kind === "video" || kind === "animation" || kind === "sticker") {
    // Paid posts get a versioned URL: ?p=1 for the blur, ?u=1 once bought. The
    // thumbnail response is cached for a day, so reusing one URL would keep
    // showing the old blur after the post was paid for.
    const q = paid ? (locked ? "?p=1" : "?u=1") : "";
    const thumbBase = `/api/thumb/${S.chat.id}/${item.id}${q}`;
    const img = el("img", { src: thumbBase, alt: "", loading: "lazy", decoding: "async" });
    img.addEventListener("load", () => { img.classList.add("loaded"); node.classList.add("ready"); });
    let attempts = 0;
    img.addEventListener("error", () => {
      // A single transient failure (Telegram flood wait, dropped connection)
      // should not blank the tile: retry once, then fall back to the icon.
      if (attempts++ === 0) {
        setTimeout(() => { img.src = thumbBase + (q ? "&r=1" : "?r=1"); }, 400);
        return;
      }
      placeholder();
    });
    node.append(img);
  } else {
    node.append(el("div", { class: "ph" }, icon(KIND_ICON[kind] || "file")));
    node.classList.add("ready");
  }

  if (kind === "video" || kind === "animation") {
    node.append(el("div", { class: "play" }, el("span", {}, icon("play"))));
    if (media.duration) node.append(el("div", { class: "dur" }, fmtDur(media.duration)));
  }

  const tagLabel = { photo: "PHOTO", video: "VIDEO", animation: "GIF", voice: "VOICE", audio: "AUDIO", sticker: "STICKER", file: "FILE" }[kind];
  if (locked) {
    node.append(el("div", { class: "stars" }, [icon("lock"), `⭐ ${paid.stars}`]));
  } else if (tagLabel && kind !== "photo") {
    node.append(el("div", { class: "tag" }, [icon(KIND_ICON[kind]), tagLabel]));
  }

  if (kind === "file" && media.name) node.append(el("div", { class: "fname" }, media.name));

  node.append(el("div", { class: "check" }, icon("check")));

  if (locked) {
    // Nothing to download until the post is paid for — the button opens the
    // explanation instead of firing a request Telegram will refuse.
    const quick = el("div", { class: "quick locked", title: `Paid post · ${paid.stars} Stars` }, icon("lock"));
    quick.addEventListener("click", (event) => {
      event.stopPropagation();
      openLightbox(item);
    });
    node.append(quick);
  } else {
    const quick = el("div", { class: "quick", title: "Download" }, icon("dl"));
    quick.addEventListener("click", (event) => {
      event.stopPropagation();
      downloadItem(item);
    });
    node.append(quick);
  }

  node.addEventListener("click", () => {
    if (S.selecting) toggleSelect(item);
    else openLightbox(item);
  });

  return node;
}

/* ------------------------------------------------------------------ selection */

function refreshTile(item) {
  const node = $(`.tile[data-id="${item.id}"]`);
  if (!node) return;
  node.replaceWith(tile(item));
}

function toggleSelect(item) {
  if (S.selected.has(item.id)) S.selected.delete(item.id);
  else S.selected.add(item.id);
  const node = $(`.tile[data-id="${item.id}"]`);
  node?.classList.toggle("selected", S.selected.has(item.id));
  updateBulkBar();
}

function setSelecting(on) {
  S.selecting = on;
  document.body.classList.toggle("selecting", on);
  $("#select-btn").classList.toggle("active", on);
  $("#select-btn").title = on ? "Cancel selection" : "Select";
  if (!on) S.selected.clear();
  $$(".tile").forEach((node) => node.classList.toggle("selected", S.selected.has(Number(node.dataset.id))));
  updateBulkBar();
}

function updateBulkBar() {
  const count = S.selected.size;
  const bar = $("#bulkbar");
  const visible = S.selecting && count > 0;
  bar.classList.toggle("hidden", !visible);
  $("#bulk-count").textContent = `${count} selected`;
}

/* ------------------------------------------------------------------ download */

function mediaUrl(item, dl = true, chatId = S.chat?.id) {
  return `/api/media/${chatId}/${item.id}${dl ? "" : "?dl=0"}`;
}

function isLocked(item) {
  return !!(item.media?.paid && !item.media.paid.unlocked);
}

function downloadItem(item) {
  if (isLocked(item)) return openLightbox(item);
  const link = document.createElement("a");
  link.href = mediaUrl(item);
  link.download = item.media?.name || `telegram_${item.id}`;
  link.rel = "noopener";
  document.body.append(link);
  link.click();
  link.remove();
}

let bulkRunning = false;

async function downloadMany(items) {
  const locked = items.filter(isLocked);
  if (locked.length) {
    items = items.filter((item) => !isLocked(item));
    if (!items.length) {
      return toast(`That ${locked.length === 1 ? "post is" : "posts are"} paid — pay in Telegram first.`, "err");
    }
    toast(`Skipping ${locked.length} paid post${locked.length === 1 ? "" : "s"} — pay in Telegram first.`);
  }
  if (!items.length) return;
  if (bulkRunning) return toast("A batch is already running.", "err");

  // Preferred path: stream straight into a folder the user picks.
  if (items.length > 1 && window.showDirectoryPicker) {
    let dir;
    try {
      dir = await window.showDirectoryPicker({ id: "tegrab", mode: "readwrite" });
    } catch (_) {
      return; // picker dismissed
    }
    return saveToFolder(items, dir);
  }

  bulkRunning = true;
  toast(`Starting ${items.length} download${items.length > 1 ? "s" : ""}. Allow multiple downloads if the browser asks.`, "ok");
  for (const item of items) {
    downloadItem(item);
    await new Promise((resolve) => setTimeout(resolve, 700));
  }
  bulkRunning = false;
}

async function saveToFolder(items, dir) {
  bulkRunning = true;
  const chatId = S.chat?.id;
  const status = el("div", { class: "toast" }, `Saving 0/${items.length}…`);
  $("#toasts").append(status);
  const used = new Set();
  let saved = 0;
  let failed = 0;
  for (let i = 0; i < items.length; i++) {
    const item = items[i];
    status.textContent = `Saving ${i + 1}/${items.length} · ${fmtSize(item.media?.size)}`;
    try {
      const res = await fetch(mediaUrl(item, true, chatId));
      if (!res.ok || !res.body) throw new Error(`HTTP ${res.status}`);
      let name = item.media?.name || `telegram_${item.id}`;
      if (used.has(name)) {
        const dot = name.lastIndexOf(".");
        name = dot > 0 ? `${name.slice(0, dot)}_${item.id}${name.slice(dot)}` : `${name}_${item.id}`;
      }
      used.add(name);
      const handle = await dir.getFileHandle(name, { create: true });
      await res.body.pipeTo(await handle.createWritable());
      saved++;
    } catch (_) {
      failed++;
    }
  }
  bulkRunning = false;
  status.textContent = failed ? `Saved ${saved}, ${failed} failed` : `Saved ${saved} file${saved === 1 ? "" : "s"}`;
  status.classList.add(failed ? "err" : "ok");
  setTimeout(() => status.remove(), 6000);
}

/* ------------------------------------------------------------------ lightbox */

function paidCard(item, paid) {
  const image = el("img", { class: "paid-blur", src: `/api/thumb/${S.chat.id}/${item.id}?p=1`, alt: "", decoding: "async" });
  image.addEventListener("error", () => image.remove());
  const check = el("button", { class: "btn ghost", type: "button" }, "I've paid — check again");
  check.addEventListener("click", () => recheckPaid(item, check));
  return el("div", { class: "paid-card" }, [
    image,
    el("div", { class: "paid-body" }, [
      el("div", { class: "paid-head" }, [icon("lock"), el("span", {}, `Paid post · ⭐ ${paid.stars}`)]),
      el("div", { class: "paid-note" },
        "Telegram does not send this file until the post is paid for, so there is nothing here to unlock yet. " +
        "Pay it in the official app or on your phone, then check again — once the payment is on your account " +
        "this post previews and downloads like any other."),
      el("div", { class: "paid-actions" }, check),
    ]),
  ]);
}

async function recheckPaid(item, button) {
  if (button.disabled) return;
  const label = button.textContent;
  button.disabled = true;
  button.textContent = "Checking…";
  try {
    const res = await api(`/api/paid/${S.chat.id}/${item.id}/check`, { method: "POST" });
    if (res.unlocked) {
      // Swap the locked payload for the real one: the tile, the viewer and any
      // later download all read from this same object.
      item.media = res.media || item.media;
      refreshTile(item);
      openLightbox(item);
      toast("Payment found — this post is unlocked.", "ok");
      return;
    }
    toast("No payment on this post yet.", "err");
  } catch (err) {
    toast(err.message, "err");
  } finally {
    button.disabled = false;
    button.textContent = label;
  }
}

function openLightbox(item) {
  S.lbIndex = S.messages.indexOf(item);
  const box = $("#lightbox");
  const stage = $("#lb-stage");
  const media = item.media;
  stage.replaceChildren();

  const when = item.date
    ? new Date(item.date).toLocaleString(undefined, { day: "numeric", month: "short", year: "numeric", hour: "2-digit", minute: "2-digit" })
    : "";
  $("#lb-from").textContent = S.chat?.title || "";
  $("#lb-title").textContent = [when, fmtSize(media.size), media.duration ? fmtDur(media.duration) : ""].filter(Boolean).join(" · ");
  const lbAvatar = avatarFor(S.chat?.id, S.chat?.title);
  lbAvatar.id = "lb-avatar";
  lbAvatar.classList.add("sm");
  $("#lb-avatar").replaceWith(lbAvatar);
  $("#lb-caption").textContent = item.text || "";
  $("#lb-dl").onclick = () => downloadItem(item);
  const paid = media.paid || null;
  const locked = !!(paid && !paid.unlocked);
  $("#lb-dl").style.visibility = locked ? "hidden" : "";
  $("#lb-prev").disabled = S.lbIndex <= 0;
  $("#lb-next").disabled = S.lbIndex >= S.messages.length - 1;

  if (locked) {
    stage.append(paidCard(item, paid));
    box.classList.remove("hidden");
    return;
  }

  const src = mediaUrl(item, false);
  const failed = (what) => () => {
    stage.replaceChildren(
      el("div", { class: "filecard" }, [
        icon("file"),
        el("div", {}, `This ${what} could not be loaded here.`),
        el("div", { style: "margin-top:6px;font-size:12px" }, "Use the download button — the file itself is intact."),
        el("div", { style: "margin-top:16px" }, el("button", { class: "btn primary", onclick: () => downloadItem(item) }, "Download file")),
      ])
    );
  };
  if (media.kind === "photo" || media.kind === "sticker") {
    const img = el("img", { src, alt: "", decoding: "async" });
    img.addEventListener("error", failed("image"));
    // Click toggles between "fit to screen" and 1:1 pixels; the stage scrolls.
    img.addEventListener("click", () => {
      img.classList.toggle("zoomed");
      if (!img.classList.contains("zoomed")) stage.scrollTo({ top: 0, left: 0 });
    });
    stage.append(img);
  } else if (media.kind === "video" || media.kind === "animation") {
    const video = el("video", { src, controls: "", autoplay: "", playsinline: "", preload: "metadata" });
    video.addEventListener("error", failed("video"));
    stage.append(video);
  } else if (media.kind === "voice" || media.kind === "audio") {
    const audio = el("audio", { src, controls: "", autoplay: "", preload: "metadata" });
    audio.addEventListener("error", failed("audio file"));
    stage.append(audio);
  } else {
    stage.append(
      el("div", { class: "filecard" }, [
        icon("file"),
        el("div", {}, media.name),
        el("div", { style: "margin-top:6px;font-size:12px" }, fmtSize(media.size)),
        el("div", { style: "margin-top:16px" }, el("button", { class: "btn primary", onclick: () => downloadItem(item) }, "Download file")),
      ])
    );
  }
  box.classList.remove("hidden");
}

function closeLightbox() {
  const stage = $("#lb-stage");
  stage.querySelectorAll("video, audio").forEach((node) => { node.pause(); node.removeAttribute("src"); });
  stage.replaceChildren();
  stage.scrollTop = 0;
  stage.scrollLeft = 0;
  $("#lightbox").classList.add("hidden");
  S.lbIndex = -1;
}

/* ------------------------------------------------------------------ wiring */

function bindGlobal() {
  bindAuth();

  $("#chat-search").addEventListener("input", debounce(renderDialogs, 120));

  $("#media-search").addEventListener("input", debounce(() => {
    S.search = $("#media-search").value.trim();
    loadMedia(true);
  }, 420));

  $("#kind-tabs").addEventListener("click", (event) => {
    const button = event.target.closest("button[data-kind]");
    if (!button) return;
    $$("#kind-tabs button").forEach((b) => b.classList.toggle("active", b === button));
    S.kind = button.dataset.kind;
    loadMedia(true);
  });

  $("#grid").addEventListener("scroll", () => {
    const grid = $("#grid");
    if (grid.scrollTop + grid.clientHeight >= grid.scrollHeight - 400) loadMedia(false);
  });

  $("#dl-loaded").addEventListener("click", () => downloadMany(S.messages.filter((m) => m.media)));

  $("#select-btn").addEventListener("click", () => setSelecting(!S.selecting));
  $("#bulk-all").addEventListener("click", () => {
    S.messages.forEach((item) => S.selected.add(item.id));
    setSelecting(true);
    $$(".tile").forEach((node) => node.classList.add("selected"));
    updateBulkBar();
  });
  $("#bulk-clear").addEventListener("click", () => {
    S.selected.clear();
    $$(".tile").forEach((node) => node.classList.remove("selected"));
    updateBulkBar();
  });
  $("#bulk-dl").addEventListener("click", () => {
    const items = S.messages.filter((item) => S.selected.has(item.id));
    downloadMany(items);
    setSelecting(false);
  });

  $("#menu-btn").addEventListener("click", (event) => {
    event.stopPropagation();
    $("#menu").classList.toggle("hidden");
  });
  document.addEventListener("click", (event) => {
    if (!event.target.closest(".menu-wrap")) $("#menu").classList.add("hidden");
  });
  $("#back-btn").addEventListener("click", () => $("#app").classList.remove("chat-open"));
  $("#search-toggle").addEventListener("click", () => {
    const head = $(".chat-head");
    head.classList.toggle("searching");
    if (head.classList.contains("searching")) $("#media-search").focus();
  });
  $("#lb-prev").addEventListener("click", () => stepLightbox(-1));
  $("#lb-next").addEventListener("click", () => stepLightbox(1));

  $("#lb-close").addEventListener("click", closeLightbox);
  $("#lightbox").addEventListener("click", (event) => {
    if (event.target.id === "lightbox" || event.target.classList.contains("lb-stage")) closeLightbox();
  });

  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape") {
      if (!$("#lightbox").classList.contains("hidden")) closeLightbox();
      else if (S.selecting) setSelecting(false);
    }
    if ($("#lightbox").classList.contains("hidden")) return;
    if (event.key === "ArrowRight") stepLightbox(1);
    if (event.key === "ArrowLeft") stepLightbox(-1);
  });
}

function stepLightbox(delta) {
  if (S.lbIndex < 0) return;
  const next = S.lbIndex + delta;
  if (next < 0 || next >= S.messages.length) return;
  const item = S.messages[next];
  if (!item.media) return;
  openLightbox(item);
}

boot();
