// The user menu at the right of the app bar, on every logged-in page: who you are, account
// settings, connecting an AI app, the theme, admin, and links. Mounts into [data-usermenu].
// Account settings live in a dialog on the library page; elsewhere the menu links to /#account.
import { api, esc, icon } from "/static/common.js";

const initials = (name) => (name || "?").split(/[\s@._-]+/).filter(Boolean).slice(0, 2)
  .map((w) => w[0]).join("").toUpperCase() || "?";
const hue = (s) => [...(s || "")].reduce((h, c) => (h * 31 + c.charCodeAt(0)) % 360, 210);
export const avatarHtml = (u) =>
  `<span class="avatar" aria-hidden="true" style="--hue:${hue(u?.id || u?.email || "")}">${esc(initials(u?.display_name || u?.email))}</span>`;

function menuHtml(auth, cfg) {
  const u = auth?.user;
  const signedIn = !!auth?.login_required;
  const item = (href, ic, label, extra = "") => `<a role="menuitem" href="${href}"${extra}>${icon(ic)}${label}</a>`;
  const out = [];
  if (u) {
    out.push(`<div class="mhead">${avatarHtml(u)}<div><b>${esc(u.display_name || "You")}</b>` +
      `<span>${esc(u.email || (u.role === "admin" ? "Administrator" : ""))}</span></div></div><hr>`);
  }
  if (signedIn) {
    out.push(item("/#account", "user", "Account settings"));
    out.push(item("/#security", "key", "Passkeys and sign-in"));
    out.push(item("/#connect", "plug", "Connect an AI app"));
    out.push("<hr>");
  }
  out.push(`<div class="mrow"><span>Theme</span><div data-theme-switch></div></div>`);
  out.push("<hr>");
  if (u?.role === "admin") out.push(item("/admin", "settings", "Admin"));
  out.push(item("/demo", "play", "Watch the demo"));
  out.push(item("/privacy", "lock", "Privacy"));
  out.push(item("https://github.com/htilly/stl-mcp", "external", "GitHub",
    ` target="_blank" rel="noopener noreferrer"`));
  if (cfg?.donate_url) {
    out.push(item(esc(cfg.donate_url), "heart", "Donate", ` target="_blank" rel="noopener noreferrer"`));
  }
  if (signedIn) out.push(`<hr><button type="button" role="menuitem" data-logout>${icon("logout")}Log out</button>`);
  return out.join("");
}

export async function mountUserMenu(el) {
  el.classList.add("umenu");
  el.innerHTML = `<button type="button" aria-haspopup="menu" aria-expanded="false" aria-label="Account menu" title="Account, theme and settings">
    <span class="avatar" aria-hidden="true">${icon("user", "sm")}</span>${icon("chevron")}</button>
    <div class="menu" role="menu" hidden></div>`;
  const btn = el.querySelector("button"), menu = el.querySelector(".menu");
  const [auth, cfg] = await Promise.all([api("/api/auth").catch(() => null), api("/auth/config").catch(() => null)]);
  if (auth?.user) btn.querySelector(".avatar").outerHTML = avatarHtml(auth.user);
  menu.innerHTML = menuHtml(auth, cfg);
  menu.querySelectorAll("[data-theme-switch]").forEach((s) => window.autosmithTheme?.mount(s));
  window.autosmithTheme?.set(window.autosmithTheme.get());  // mark the current choice

  const open = (on) => {
    menu.hidden = !on;
    btn.setAttribute("aria-expanded", String(on));
    if (on) menu.querySelector("a, button")?.focus({ preventScroll: true });
  };
  btn.addEventListener("click", () => open(menu.hidden));
  document.addEventListener("click", (e) => { if (!el.contains(e.target)) open(false); });
  el.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && !menu.hidden) { open(false); btn.focus(); }
    if ((e.key === "ArrowDown" || e.key === "ArrowUp") && !menu.hidden) {
      const items = [...menu.querySelectorAll("a, button")];
      const i = items.indexOf(document.activeElement);
      items[(i + (e.key === "ArrowDown" ? 1 : -1) + items.length) % items.length]?.focus();
      e.preventDefault();
    }
  });
  // links into the account dialog close the menu (the library page opens the dialog on the hash)
  menu.addEventListener("click", async (e) => {
    if (e.target.closest("a")) open(false);
    if (e.target.closest("[data-logout]")) {
      await api("/auth/logout", { method: "POST", body: {} }).catch(() => {});
      location.href = "/";
    }
  });
  if (auth?.user?.role === "admin") mountPulse(el.closest("header.top"));
  return auth;
}

// ---- the admin bar: server load at a glance in the app bar, on every page an admin sees

const pctOf = (part, whole) => (whole ? Math.round((100 * part) / whole) : null);
const level = (v) => (v == null ? "" : v >= 85 ? " hot" : v >= 60 ? " warm" : "");

function spark(points, seconds) {
  // the last 5 minutes on a fixed time axis, 0-100 %; gaps (no samples yet) stay empty
  const W = 64, H = 20, span = 60 * seconds, now = Date.now() / 1000;
  const xy = points.filter(([t]) => now - t <= span)
    .map(([t, v]) => `${(W - ((now - t) / span) * W).toFixed(1)},${(H - 1 - (Math.min(v, 100) / 100) * (H - 2)).toFixed(1)}`);
  if (!xy.length) return "";
  const first = xy[0].split(",")[0], last = xy[xy.length - 1].split(",")[0];
  return `<path class="area" d="M${first},${H} L${xy.join(" L")} L${last},${H} Z"/><polyline points="${xy.join(" ")}"/>`;
}

function pulseHtml(d) {
  const cpu = d.cpu.length ? d.cpu[d.cpu.length - 1][1] : null;
  const mem = d.mem_available_mb != null ? pctOf(d.mem_total_mb - d.mem_available_mb, d.mem_total_mb) : null;
  const disk = pctOf(d.disk_total_gb - d.disk_free_gb, d.disk_total_gb);
  const item = (cls, label, value, title) =>
    `<span class="pv ${cls}" title="${esc(title)}"><span class="k">${label}</span><b>${value}</b></span>`;
  return `<span class="pv cpu${level(cpu)}" title="Whole machine, ${d.cpus} cores${d.load ? ` · load ${d.load.join(" / ")}` : ""}">
      <svg class="spark" viewBox="0 0 64 20" preserveAspectRatio="none" aria-hidden="true">${spark(d.cpu, d.seconds)}</svg>
      <span class="k">CPU</span><b>${cpu == null ? "–" : `${Math.round(cpu)}%`}</b></span>` +
    (mem != null ? item(`mem${level(mem)}`, "Mem", `${mem}%`, `${Math.round((d.mem_total_mb - d.mem_available_mb) / 1024 * 10) / 10} of ${Math.round(d.mem_total_mb / 1024)} GB in use`) : "") +
    item(`builds${d.builds_waiting ? " warm" : ""}`, "Builds", d.builds_waiting ? `${d.builds_running}+${d.builds_waiting}` : d.builds_running,
      `${d.builds_running} running, ${d.builds_waiting} waiting`) +
    item("online", "Online", d.online, "Users with a page open, Claude active in the last 10 min, or a build running") +
    item(`disk${level(disk)}`, "Disk", `${d.disk_free_gb} GB`, `${d.disk_free_gb} GB free of ${d.disk_total_gb} GB (model storage)`);
}

function mountPulse(header) {
  if (!header || header.querySelector(".pulse")) return;
  const el = document.createElement("a");
  el.className = "pulse";
  el.href = "/admin/load";
  el.setAttribute("aria-label", "Server load: open the Load page");
  (header.querySelector(":scope > .grow") || header.lastElementChild).before(el);
  let timer = null;
  const tick = async () => {
    clearTimeout(timer);
    try { el.innerHTML = pulseHtml(await api("/api/admin/pulse")); el.hidden = false; }
    catch { el.hidden = true; }
    if (!document.hidden) timer = setTimeout(tick, 5000);
  };
  document.addEventListener("visibilitychange", () => { if (!document.hidden) tick(); });
  tick();
}

document.querySelectorAll("[data-usermenu]").forEach((el) => mountUserMenu(el));
