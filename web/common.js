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
  const connect = () => {
    clearTimeout(retry);
    es?.close();
    es = new EventSource("/api/events");
    es.onopen = () => statusEl?.classList.add("on");
    es.onmessage = (e) => {
      const d = JSON.parse(e.data);
      const before = last;
      last = d.revision;
      if (before === null) return;                     // first connection: nothing to catch up on
      if (d.id) cb(d);                                 // a live change
      else if (d.revision !== before) cb({ revision: d.revision, resync: true });  // hello after a gap
    };
    es.onerror = () => {
      statusEl?.classList.remove("on");
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
export function warnHtml(p) {
  const ws = p.warnings ?? (p.watertight ? [] : [{ message: "not watertight - may cause problems in the slicer" }]);
  return ws.map((w) => `<div class="warn" title="${esc(w.code ?? "")}">⚠ ${esc(w.message)}</div>`).join("");
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
  return api("/auth/passkey/login/verify", { method: "POST", body: { flow, credential: credentialJSON(cred) } });
}

export async function passkeyRegister(name) {
  const { flow, options: o } = await api("/auth/passkey/register/options", { method: "POST", body: {} });
  o.challenge = b64u.dec(o.challenge);
  o.user.id = b64u.dec(o.user.id);
  o.excludeCredentials = descriptors(o.excludeCredentials);
  const cred = await navigator.credentials.create({ publicKey: o });
  return api("/auth/passkey/register/verify", { method: "POST", body: { flow, name, credential: credentialJSON(cred) } });
}
