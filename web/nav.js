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
  return auth;
}

document.querySelectorAll("[data-usermenu]").forEach((el) => mountUserMenu(el));
