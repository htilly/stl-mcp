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

// Live updates via server-sent events. cb({revision, id}) is called on change.
export function subscribe(cb, statusEl) {
  let es, first = true;
  const connect = () => {
    es = new EventSource("/api/events");
    es.onopen = () => statusEl?.classList.add("on");
    es.onmessage = (e) => {
      const d = JSON.parse(e.data);
      if (first) { first = false; return; }
      cb(d);
    };
    es.onerror = () => {
      statusEl?.classList.remove("on");
      es.close();
      setTimeout(connect, 3000);
    };
  };
  connect();
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
