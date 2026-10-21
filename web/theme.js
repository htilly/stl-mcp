// Light, dark or auto (follow the system). A classic script loaded in <head>, before the page
// paints, so it never flashes the wrong theme. The choice is kept per browser in localStorage.
//
// Any element with [data-theme-switch] gets the three-way switch; buttons with [data-theme-set]
// anywhere set the theme. On every change (including the system flipping while on auto) a
// "themechange" event is dispatched on window with {theme, effective}; the 3D view rebuilds its
// background and grid from it.
(() => {
  const KEY = "theme";
  const root = document.documentElement;
  const media = matchMedia("(prefers-color-scheme: dark)");
  const CHOICES = [["light", "sun", "Light"], ["auto", "monitor", "Auto (follow the system)"], ["dark", "moon", "Dark"]];

  const get = () => {
    let t = null;
    try { t = localStorage.getItem(KEY); } catch {}
    return t === "light" || t === "dark" ? t : "auto";
  };
  const effective = (t) => (t === "auto" ? (media.matches ? "dark" : "light") : t);

  function apply(t) {
    if (t === "auto") delete root.dataset.theme;
    else root.dataset.theme = t;
    const meta = document.querySelector('meta[name="theme-color"]');
    if (meta) meta.content = effective(t) === "dark" ? "#0a0b0e" : "#ffffff";
    document.querySelectorAll("[data-theme-set]").forEach((b) => {
      const on = b.dataset.themeSet === t;
      b.classList.toggle("on", on);
      b.setAttribute("aria-pressed", String(on));
    });
    window.dispatchEvent(new CustomEvent("themechange", { detail: { theme: t, effective: effective(t) } }));
  }

  function set(t) {
    try { t === "auto" ? localStorage.removeItem(KEY) : localStorage.setItem(KEY, t); } catch {}
    apply(t);
  }

  function mount(el) {
    const labelled = el.hasAttribute("data-labelled");
    el.classList.add("theme-switch");
    el.classList.toggle("labelled", labelled);
    el.setAttribute("role", "group");
    el.setAttribute("aria-label", "Theme");
    el.innerHTML = CHOICES.map(([v, ic, title]) =>
      `<button type="button" data-theme-set="${v}" title="${title}" aria-label="${title}">` +
      `<svg class="i" aria-hidden="true"><use href="/static/icons.svg#i-${ic}"/></svg>` +
      `${labelled ? `<span>${title.split(" ")[0]}</span>` : ""}</button>`).join("");
  }

  document.addEventListener("click", (e) => {
    const b = e.target.closest?.("[data-theme-set]");
    if (b) set(b.dataset.themeSet);
  });
  media.addEventListener("change", () => { if (get() === "auto") apply("auto"); });

  const init = () => {
    document.querySelectorAll("[data-theme-switch]").forEach(mount);
    apply(get());
  };
  apply(get());
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init);
  else init();

  // Non-production environments (dev) get a colored bar and badge so they are never mistaken for
  // prod. /auth/config names the environment; the cached value avoids a flash on the next page.
  const ENV_KEY = "app-env";
  const showEnv = (env, title = true) => {
    if (env) {
      root.dataset.env = env;
      root.style.setProperty("--env-label", JSON.stringify(env.toUpperCase()));
    } else {
      delete root.dataset.env;
      root.style.removeProperty("--env-label");
    }
    if (!title) return;
    const base = document.title.replace(/^\[[^\]]+\] /, "");
    document.title = env ? `[${env.toUpperCase()}] ${base}` : base;
  };
  const cached = () => { try { return sessionStorage.getItem(ENV_KEY) || ""; } catch { return ""; } };
  if (cached()) showEnv(cached(), false);
  fetch("/auth/config").then((r) => (r.ok ? r.json() : {})).then((c) => {
    const env = typeof c.env === "string" ? c.env.replace(/[^a-z0-9-]/g, "") : "";
    try { if (env) sessionStorage.setItem(ENV_KEY, env); else sessionStorage.removeItem(ENV_KEY); } catch {}
    const go = () => showEnv(env);
    if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", go); else go();
  }).catch(() => {});

  window.autosmithTheme = { get, set, mount, effective: () => effective(get()) };
})();
