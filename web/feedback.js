// The feedback dialog behind "Send feedback" in the user menu: the user writes, Claude drafts an
// issue, the user looks at it (and may edit it), then it is created on GitHub.
import { api, esc, icon } from "/static/common.js";

let dlg = null;

function build() {
  const el = document.createElement("dialog");
  el.className = "feedback-dlg";
  el.setAttribute("aria-labelledby", "fbtitle");
  el.innerHTML = `
    <div class="dlg-head"><h2 id="fbtitle">Send feedback</h2>
      <button type="button" class="icon quiet" data-close title="Close" aria-label="Close">${icon("close")}</button></div>
    <form class="fb-body" data-step="write">
      <div class="fb-write">
        <p class="help">Tell us what went wrong, what is missing or what you would like. Claude turns it into
          a clear issue and you can edit it before it is sent.</p>
        <label class="lbl" for="fb-text">Your feedback</label>
        <textarea id="fb-text" rows="6" maxlength="4000" required></textarea>
      </div>
      <div class="fb-review" hidden>
        <p class="help" id="fb-note"></p>
        <label class="lbl" for="fb-title">Title</label>
        <input type="text" id="fb-title" maxlength="120">
        <label class="lbl" for="fb-desc">Description</label>
        <textarea id="fb-desc" rows="8" maxlength="6000"></textarea>
        <label class="lbl" for="fb-label">Label</label>
        <select id="fb-label"></select>
        <p class="help">The issue is created in a public GitHub repository, so do not include personal details.</p>
      </div>
      <div class="fb-done" hidden></div>
      <div class="acts"><span class="msg" id="fb-msg" role="status"></span>
        <button type="button" class="quiet" data-back hidden>Back</button>
        <button type="submit" class="primary" id="fb-go">Draft the issue</button></div>
    </form>`;
  document.body.append(el);
  return el;
}

export function openFeedback() {
  dlg ||= build();
  const $ = (s) => dlg.querySelector(s);
  const form = $("form"), msg = $("#fb-msg"), go = $("#fb-go"), back = $("[data-back]");
  const say = (t, err) => { msg.textContent = t; msg.className = "msg" + (err ? " err" : ""); };
  const show = (step) => {
    form.dataset.step = step;
    $(".fb-write").hidden = step !== "write";
    $(".fb-review").hidden = step !== "review";
    $(".fb-done").hidden = step !== "done";
    back.hidden = step !== "review";
    go.hidden = step === "done";
    go.textContent = step === "review" ? "Create the issue" : "Draft the issue";
    say("");
  };
  show("write");
  $("#fb-text").value = "";
  $("#fb-label").innerHTML = "";

  const busy = async (text, fn) => {
    go.disabled = true; say(text);
    try { await fn(); } catch (err) { say(err.message, true); } finally { go.disabled = false; }
  };

  form.onsubmit = (e) => {
    e.preventDefault();
    if (form.dataset.step === "write") {
      return busy("Claude is writing a draft…", async () => {
        const d = await api("/api/feedback/refine", { method: "POST",
          body: { text: $("#fb-text").value, page: location.pathname } });
        $("#fb-title").value = d.title; $("#fb-desc").value = d.body;
        $("#fb-note").textContent = d.refined ? "Here is the draft. Change anything you like."
          : "Claude could not tidy this up, so this is your own text. Change anything you like.";
        $("#fb-label").innerHTML = ["", ...d.labels].map((l) => `<option value="${esc(l)}">${esc(l || "No label")}</option>`).join("");
        $("#fb-label").value = d.label;
        show("review");
      });
    }
    return busy("Creating the issue…", async () => {
      const d = await api("/api/feedback", { method: "POST", body: {
        title: $("#fb-title").value, body: $("#fb-desc").value, label: $("#fb-label").value } });
      $(".fb-done").innerHTML = `<p>Thank you. Your feedback is issue
        <a href="${esc(d.url)}" target="_blank" rel="noopener noreferrer">#${esc(d.number)}</a> on GitHub.</p>`;
      show("done");
    });
  };
  back.onclick = () => show("write");
  dlg.querySelector("[data-close]").onclick = () => dlg.close();
  dlg.showModal();
  $("#fb-text").focus();
}
