// Comments for Claude, shared by the viewer and the editor: pins on the model, the "new comment"
// popup placed by clicking a mesh, and the list with resolve / reopen / reply / delete.
// A comment's point is in its part's coordinates (the STL file's), so the spread offset is removed.
// The page supplies the markup: #pins, and the form #cpop with #ctext and #ccancel inside the stage.
import * as THREE from "three";
import { esc, ago, icon } from "/static/common.js";

export function createComments(st, {
  root,              // the stage element: gets .commenting while placing comments
  parts,             // () => [{ name, obj }]: each part's frame (an Object3D holding its meshes)
  meshes,            // () => the meshes a click can hit
  comments,          // () => the model's comments
  version,           // () => the version a new comment is about
  post,              // (body) => Promise; POSTs an action to the model (and shows its own errors)
  toast,
  list, count, hint, // optional: the comment list, its "n open" pill and the "how to" hint
}) {
  const $ = (s) => document.getElementById(s);
  const pinsEl = $("pins"), pop = $("cpop"), text = $("ctext");
  let mode = false, showResolved = false, pending = null, focusId = null;

  const visible = () => comments().filter((c) => showResolved || c.status === "open");
  const partObj = (name) => parts().find((p) => p.name === name)?.obj;

  function worldOf(c) {
    const obj = partObj(c.part);
    if (!obj || !obj.visible || !c.point) return null;
    obj.updateMatrixWorld(true);
    return new THREE.Vector3(...c.point).applyMatrix4(obj.matrixWorld);
  }

  function renderPins() {
    pinsEl.innerHTML = visible().filter((c) => c.point).map((c) => `<button class="pin${c.status !== "open" ? " done" : ""}${c.id === focusId ? " focus" : ""}"
        data-id="${c.id}" title="${esc(c.text)}">${c.id.slice(1)}</button>`).join("")
      + (pending ? '<span class="pin new" data-id="new">+</span>' : "");
    pinsEl.querySelectorAll("button.pin").forEach((b) => b.onclick = () => focus(b.dataset.id, false));
  }

  // Called every frame: moves the pins to where their points are on screen.
  function updatePins() {
    if (!pinsEl.childElementCount) return;
    for (const el of pinsEl.children) {
      const id = el.dataset.id;
      let p = null;
      if (id === "new") p = pending && pending.world;
      else { const c = comments().find((x) => x.id === id); p = c && worldOf(c); }
      const s = p && st.project(p);
      if (!s) { el.style.display = "none"; continue; }
      el.style.display = "";
      el.style.transform = `translate(${s.x}px, ${s.y}px)`;
    }
  }

  function focus(id, moveCamera) {
    focusId = id;
    render();
    list?.querySelector(`.cmt[data-id="${id}"]`)?.scrollIntoView({ block: "nearest", behavior: "smooth" });
    const c = comments().find((x) => x.id === id);
    const p = c && worldOf(c);
    if (moveCamera && p) {
      const off = st.camera.position.clone().sub(st.controls.target);
      st.controls.target.copy(p);
      st.camera.position.copy(p).add(off);
    }
  }

  function renderList() {
    if (!list) return;
    const all = comments();
    const open = all.filter((c) => c.status === "open").length;
    if (count) count.textContent = all.length ? `${open} open` : "";
    list.innerHTML = visible().map((c) => `
      <div class="cmt${c.status !== "open" ? " done" : ""}${c.id === focusId ? " focus" : ""}" data-id="${c.id}">
        <span class="badge">${c.id.slice(1)}</span>
        <div>
          <div class="ctext">${esc(c.text)}</div>
          <div class="hint">v${c.version}${c.part ? ", " + esc(c.part) : ""}, ${ago(c.created)}${c.point ? "" : ", no point"}</div>
          ${(c.replies || []).map((r) => `<div class="reply ${r.author}"><b>${r.author === "claude" ? "Claude" : "You"}:</b> ${esc(r.text)}</div>`).join("")}
          <div class="row cact">
            ${c.status === "open" ? `<button class="sm" data-a="resolve">${icon("check", "sm")}Resolve</button>` : `<button class="sm" data-a="reopen">Reopen</button>`}
            <button class="sm" data-a="reply">${icon("reply", "sm")}Reply</button>
            <button class="sm danger" data-a="delete">Delete</button>
          </div>
        </div>
      </div>`).join("");
    list.querySelectorAll(".cmt").forEach((el) => {
      const id = el.dataset.id;
      el.onclick = (e) => { if (!e.target.closest("button")) focus(id, true); };
      el.querySelectorAll("button[data-a]").forEach((b) => b.onclick = async () => {
        const a = b.dataset.a;
        let text = "";
        if (a === "reply" && !(text = prompt("Reply:") || "")) return;
        if (a === "delete" && !confirm("Delete the comment?")) return;
        await post({ action: "comment_" + a, id, text });
      });
    });
    if (hint) hint.hidden = all.length > 0;
  }

  function render() { renderList(); renderPins(); }

  function openPopup(at, value) {
    pop.hidden = false;
    const r = st.canvas.getBoundingClientRect();
    if (at) {
      const x = Math.min(at.clientX - r.left + 14, r.width - pop.offsetWidth - 8);
      const y = Math.min(at.clientY - r.top + 14, r.height - pop.offsetHeight - 8);
      pop.style.left = Math.max(8, x) + "px"; pop.style.top = Math.max(8, y) + "px";
    } else { pop.style.left = "16px"; pop.style.top = "16px"; }
    text.value = value; text.focus();
    renderPins();
  }

  function close() {
    pending = null;
    pop.hidden = true;
    renderPins();
  }

  function setMode(on) {
    mode = on;
    root.classList.toggle("commenting", on);
    if (!on) close();
  }

  // A click in the view while commenting: start a comment on the mesh under the pointer.
  function click(e) {
    if (!mode) return;
    const hit = st.ray(e).intersectObjects(meshes(), false)[0];
    if (!hit) return;
    const objs = parts().map((p) => p.obj);
    let obj = hit.object;
    while (obj && !objs.includes(obj)) obj = obj.parent;
    if (!obj) return;
    const part = parts().find((p) => p.obj === obj).name;
    pending = {
      world: hit.point.clone(),
      point: obj.worldToLocal(hit.point.clone()).toArray(),
      normal: hit.face ? hit.face.normal.clone().transformDirection(hit.object.matrixWorld).toArray() : null,
      part,
    };
    openPopup(e, "");
  }

  // A comment at a given point of a part (e.g. "Fix" on a print-check warning), prefilled.
  function startAt(part, local, value) {
    const obj = partObj(part);
    if (!obj) return;
    obj.updateMatrixWorld(true);
    pending = { world: local.clone().applyMatrix4(obj.matrixWorld), point: local.toArray(), normal: null, part };
    openPopup(null, value);
  }

  pop.onsubmit = async (e) => {
    e.preventDefault();
    const value = text.value.trim();
    if (!value || !pending) return;
    const { point, normal, part } = pending;
    try {
      await post({ action: "comment_add", text: value, point, normal, part, version: version() });
      close();
      toast("Comment saved. Ask Claude to look at the comments.");
    } catch { /* post shows the toast */ }
  };
  text.onkeydown = (e) => {
    if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) pop.requestSubmit();
    if (e.key === "Escape") close();
  };
  $("ccancel").onclick = close;

  return {
    render, updatePins, setMode, click, startAt, close,
    get mode() { return mode; },
    get open() { return !pop.hidden; },
    set showResolved(v) { showResolved = v; render(); },
  };
}
