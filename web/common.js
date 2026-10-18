// Shared helpers for the library and the 3D view.

export async function api(path, opts = {}) {
  const r = await fetch(path, {
    credentials: "same-origin",
    headers: opts.body ? { "Content-Type": "application/json" } : {},
    ...opts,
    body: opts.body ? JSON.stringify(opts.body) : undefined,
  });
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.error || `HTTP ${r.status}`);
  return data;
}

// Live updates via server-sent events. cb({revision, id}) is called on every change.
//
// Phones suspend pages: a locked screen, a backgrounded tab or a network switch drops the stream,
// often without any error event, and changes made meanwhile would never be fetched. So the server's
// revision is remembered, and whenever the stream (re)connects and the revision moved, cb gets
// {revision, resync: true} - "something changed, re-read everything". The stream is also re-opened
// whenever the page becomes visible again.
export function subscribe(cb, statusEl) {
  let es, last = null, retry;
  const setStatus = (on) => {
    if (!statusEl) return;
    statusEl.classList.toggle("on", on);
    statusEl.title = on
      ? "Connected: this page updates automatically when Claude changes something"
      : "Connection lost, retrying. Changes will not appear until it is back (reload if it persists)";
  };
  const connect = () => {
    clearTimeout(retry);
    es?.close();
    es = new EventSource("/api/events");
    es.onopen = () => setStatus(true);
    es.onmessage = (e) => {
      const d = JSON.parse(e.data);
      const before = last;
      last = d.revision;
      if (before === null) return;                     // first connection: nothing to catch up on
      if (d.id) cb(d);                                 // a live change
      else if (d.revision !== before) cb({ revision: d.revision, resync: true });  // hello after a gap
    };
    es.onerror = () => {
      setStatus(false);
      es.close();
      retry = setTimeout(connect, 3000);
    };
  };
  connect();
  document.addEventListener("visibilitychange", () => { if (!document.hidden) connect(); });
  window.addEventListener("pageshow", (e) => { if (e.persisted) connect(); });
}

export function ago(ts) {
  const s = Math.max(0, Date.now() / 1000 - ts);
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.floor(s / 60)} min ago`;
  if (s < 86400) return `${Math.floor(s / 3600)} h ago`;
  return new Date(ts * 1000).toLocaleDateString("en-GB");
}

export const fmt = (e) => e.map((v) => (+v).toFixed(1).replace(/\.0$/, "")).join(" × ") + " mm";

export const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => (
  { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

export const PART_COLORS = ["#4f8cff", "#ff9a3c", "#5ac878", "#e65a78", "#aa78ff", "#3cc8d2"];

// Print-check warnings of one part ({warnings: [{severity, message}]}); older versions only have `watertight`.
// With `part` each warning gets a Fix button (data-part / data-i) that the page wires up.
export function warnHtml(p, part) {
  const ws = p.warnings ?? (p.watertight ? [] : [{ message: "not watertight - may cause problems in the slicer" }]);
  return ws.map((w, i) => `<div class="warn" title="${esc(w.code ?? "")}">${icon("warning", "sm")}<span>${esc(w.message)}</span>${
    part ? `<button class="btn sm fixbtn" data-part="${esc(part)}" data-i="${i}" title="Ask Claude to fix this">Fix</button>` : ""}</div>`).join("");
}

// ---- passkeys (WebAuthn). The server sends options as JSON with base64url binaries.

const b64u = {
  dec: (s) => Uint8Array.from(atob(s.replace(/-/g, "+").replace(/_/g, "/") + "===".slice((s.length + 3) % 4)),
    (c) => c.charCodeAt(0)),
  enc: (b) => btoa(String.fromCharCode(...new Uint8Array(b)))
    .replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, ""),
};

function credentialJSON(c) {
  const r = c.response;
  const out = { id: c.id, rawId: b64u.enc(c.rawId), type: c.type,
    clientExtensionResults: c.getClientExtensionResults?.() ?? {},
    response: { clientDataJSON: b64u.enc(r.clientDataJSON) } };
  if (c.authenticatorAttachment) out.authenticatorAttachment = c.authenticatorAttachment;
  if (r.attestationObject) {
    out.response.attestationObject = b64u.enc(r.attestationObject);
    out.response.transports = r.getTransports?.() ?? [];
  }
  if (r.authenticatorData) {
    out.response.authenticatorData = b64u.enc(r.authenticatorData);
    out.response.signature = b64u.enc(r.signature);
    if (r.userHandle) out.response.userHandle = b64u.enc(r.userHandle);
  }
  return out;
}

const descriptors = (list) => (list || []).map((c) => ({ ...c, id: b64u.dec(c.id) }));

export const passkeysSupported = () => !!window.PublicKeyCredential;

export async function passkeyLogin() {
  const { flow, options: o } = await api("/auth/passkey/login/options", { method: "POST", body: {} });
  o.challenge = b64u.dec(o.challenge);
  o.allowCredentials = descriptors(o.allowCredentials);
  const cred = await navigator.credentials.get({ publicKey: o });
  const res = await api("/auth/passkey/login/verify", { method: "POST", body: { flow, credential: credentialJSON(cred) } });
  // "cross-platform": the passkey came from another device, usually a phone via the QR code
  return { ...res, crossDevice: cred.authenticatorAttachment === "cross-platform" };
}

async function createPasskey(prefix, body, name) {
  const { flow, options: o } = await api(`${prefix}/options`, { method: "POST", body });
  o.challenge = b64u.dec(o.challenge);
  o.user.id = b64u.dec(o.user.id);
  o.excludeCredentials = descriptors(o.excludeCredentials);
  const cred = await navigator.credentials.create({ publicKey: o });
  return api(`${prefix}/verify`, { method: "POST", body: { flow, name, credential: credentialJSON(cred) } });
}

// Adds a passkey to the logged-in account.
export const passkeyRegister = (name) => createPasskey("/auth/passkey/register", {}, name);

// Creates a new account whose login is this passkey (open signup).
export const passkeySignup = (displayName, turnstile, name) =>
  createPasskey("/auth/signup", { display_name: displayName, turnstile }, name);

// Copies text and briefly says so on the button.
export async function copyText(btn, text) {
  try {
    await navigator.clipboard.writeText(text);
    const label = btn.textContent;
    btn.textContent = "Copied";
    setTimeout(() => { btn.textContent = label; }, 1500);
  } catch {}
}

// ---- icons: the sprite in /static/icons.svg (see docs/DESIGN.md#icons)
export const icon = (name, cls = "") =>
  `<svg class="i${cls ? " " + cls : ""}" aria-hidden="true"><use href="/static/icons.svg#i-${name}"/></svg>`;
