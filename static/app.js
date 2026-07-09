"use strict";

// Default AI-rewrite prompt (pre-filled into every XPath row; editable per row).
const DEFAULT_PROMPT =
  "Rewrite the following, keeping the same meaning and tone.\n" +
  "Preserve any HTML tags and formatting intact.\n" +
  "Return only the rewritten content, nothing else:\n" +
  "{{text}}";

const $ = (s, r = document) => r.querySelector(s);

/* Cross-task guard: only one write-job (AI rewrite / Find & replace) runs at a
   time. While one runs, the other button shows a hover tooltip explaining why.
   We mark it busy with a class + title (not `disabled`) so the tooltip reliably
   appears on hover even in browsers that suppress tooltips on disabled buttons;
   the click handlers below refuse to start while TASK_RUNNING is set. */
let TASK_RUNNING = "";
const BUSY_MSG = "A task is already running — retry after it completes";
function blockWriteBtn(sel, blocked) {
  const b = $(sel);
  if (!b) return;
  if (blocked) { b.classList.add("is-busy"); b.title = BUSY_MSG; }
  else { b.classList.remove("is-busy"); b.removeAttribute("title"); }
}
const $$ = (s, r = document) => Array.from(r.querySelectorAll(s));

async function api(url, data) {
  const res = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(data || {}),
  });
  return res.json();
}

/* ---------------- navigation ---------------- */
$$(".rail__item").forEach((btn) => {
  btn.addEventListener("click", () => {
    $$(".rail__item").forEach((b) => b.classList.remove("is-active"));
    btn.classList.add("is-active");
    $$(".panel").forEach((p) => p.classList.remove("is-active"));
    $("#panel-" + btn.dataset.panel).classList.add("is-active");
    refreshBanner();
  });
});
function currentPanel() {
  return $(".rail__item.is-active").dataset.panel;
}

/* ---------------- connection status ---------------- */
function setStatus(state, text) {
  const pill = $("#statusPill");
  pill.className = "status status--" + state;
  $("#statusText").textContent = text;
}
$("#statusPill").addEventListener("click", () => {
  $$(".rail__item").forEach((b) => b.classList.remove("is-active"));
  $('.rail__item[data-panel="connection"]').classList.add("is-active");
  $$(".panel").forEach((p) => p.classList.remove("is-active"));
  $("#panel-connection").classList.add("is-active");
});

/* ---------------- config ---------------- */
async function loadConfig() {
  const c = await fetch("/api/config").then((r) => r.json());
  $("#cfg-host").value = c.ssh.host;
  $("#cfg-port").value = c.ssh.port;
  $("#cfg-username").value = c.ssh.username || "";
  $("#cfg-path").value = c.wordpress.path;
  $("#cfg-wpbin").value = c.wordpress.wp_bin;
  $("#cfg-model").value = c.openai.model;
  $("#cfg-base").value = c.openai.base_url || "";
  $("#cfg-maxtokens").value = c.openai.max_tokens;
  $("#cfg-temp").value = c.openai.temperature;
  $("#cfg-password").placeholder = c.ssh.has_password ? "•••••• saved — leave blank to keep" : "SSH password";
  $("#cfg-key").placeholder = c.openai.has_api_key ? "•••••• saved — leave blank to keep" : "sk-...";
  $("#cfg-provider").value = c.openai.provider === "cli" ? "cli" : "api";
  $("#cfg-cli-cmd").value = (c.claude && c.claude.command) || "claude";
  $("#cfg-cli-model").value = (c.claude && c.claude.model) || "";
  applyProvider();
}

function applyProvider() {
  const cli = $("#cfg-provider").value === "cli";
  $$(".ai-api").forEach((el) => (el.style.display = cli ? "none" : ""));
  $$(".ai-cli").forEach((el) => (el.style.display = cli ? "" : "none"));
}
$("#cfg-provider").addEventListener("change", applyProvider);
function gatherConfig() {
  return {
    ssh: {
      host: $("#cfg-host").value.trim(),
      port: $("#cfg-port").value.trim(),
      username: $("#cfg-username").value.trim(),
      password: $("#cfg-password").value,
    },
    wordpress: { path: $("#cfg-path").value.trim(), wp_bin: $("#cfg-wpbin").value.trim() },
    openai: {
      api_key: $("#cfg-key").value,
      base_url: $("#cfg-base").value.trim(),
      model: $("#cfg-model").value.trim(),
      max_tokens: $("#cfg-maxtokens").value,
      temperature: $("#cfg-temp").value,
      provider: $("#cfg-provider").value,
    },
    claude: {
      command: $("#cfg-cli-cmd").value.trim(),
      model: $("#cfg-cli-model").value.trim(),
    },
  };
}
$("#saveCfg").addEventListener("click", async () => {
  await api("/api/config", gatherConfig());
  $("#cfg-key").value = "";
  $("#cfg-password").value = "";
  await loadConfig();
  flash($("#saveCfg"), "Saved");
});
$("#testConn").addEventListener("click", async () => {
  await api("/api/config", gatherConfig());
  $("#cfg-key").value = "";
  $("#cfg-password").value = "";
  setStatus("busy", "Checking…");
  const out = $("#checkOut");
  out.hidden = false;
  out.textContent = "Connecting…";
  const r = await api("/api/check", {});
  if (r.ok) {
    setStatus("ok", r.wordpress && r.wordpress !== "(path not found)" ? "WordPress " + r.wordpress : "Connected");
    out.textContent = `WP-CLI:    ${r.wp_cli}\nWordPress: ${r.wordpress}\nPrefix:    ${r.prefix}`;
    loadPostTypes();
  } else {
    setStatus("fail", "Connection failed");
    out.textContent = r.error + "\n\nCheck host, port, key, and the WordPress path.\nIf the host blocks SSH, WP-CLI must still be reachable there.";
  }
});

function flash(btn, text) {
  const old = btn.textContent;
  btn.textContent = text;
  setTimeout(() => (btn.textContent = old), 1200);
}

/* ---------------- live banner (apply/write modes) ---------------- */
function refreshBanner() {
  const b = document.getElementById("liveBanner");
  if (!b) return;
  const writing =
    currentPanel() === "replace" ||
    currentPanel() === "logo" ||
    currentPanel() === "rewrite";
  b.hidden = !writing;
}

/* ---------------- confirmation modal ---------------- */
function confirmChanges({ title, intro, pairs, notes, confirmLabel }) {
  return new Promise((resolve) => {
    const overlay = document.createElement("div");
    overlay.className = "modal-overlay";
    const rows = pairs
      .map(([o, n]) => {
        const newCell = n === ""
          ? '<span class="chip chip--empty">(removed)</span>'
          : `<span class="chip chip--new">${escapeHtml(n)}</span>`;
        return `<tr><td><span class="chip chip--old">${escapeHtml(o)}</span></td>` +
               `<td class="cmp-arrow">→</td><td>${newCell}</td></tr>`;
      })
      .join("");
    const noteHtml = (notes || []).map((t) => `<p class="modal-note">${escapeHtml(t)}</p>`).join("");
    overlay.innerHTML = `
      <div class="modal" role="dialog" aria-modal="true" aria-label="${escapeAttr(title)}">
        <div class="modal__head"><h3>${escapeHtml(title)}</h3></div>
        <div class="modal__body">
          <p class="modal-intro">${escapeHtml(intro)}</p>
          <table class="cmp">
            <thead><tr><th>Current (old)</th><th></th><th>New</th></tr></thead>
            <tbody>${rows}</tbody>
          </table>
          ${noteHtml}
        </div>
        <div class="modal__foot">
          <button class="btn btn--ghost" data-act="cancel">Cancel</button>
          <button class="btn btn--apply" data-act="ok">${escapeHtml(confirmLabel || "Confirm")}</button>
        </div>
      </div>`;
    document.body.appendChild(overlay);
    const close = (val) => {
      overlay.remove();
      document.removeEventListener("keydown", onKey);
      resolve(val);
    };
    const onKey = (e) => {
      if (e.key === "Escape") close(false);
      if (e.key === "Enter") close(true);
    };
    overlay.querySelector('[data-act="cancel"]').onclick = () => close(false);
    overlay.querySelector('[data-act="ok"]').onclick = () => close(true);
    overlay.addEventListener("click", (e) => { if (e.target === overlay) close(false); });
    document.addEventListener("keydown", onKey);
    overlay.querySelector('[data-act="ok"]').focus();
  });
}

function confirmDialog({ title, message, confirmLabel }) {
  return new Promise((resolve) => {
    const overlay = document.createElement("div");
    overlay.className = "modal-overlay";
    overlay.innerHTML = `
      <div class="modal" role="dialog" aria-modal="true" aria-label="${escapeAttr(title)}">
        <div class="modal__head"><h3>${escapeHtml(title)}</h3></div>
        <div class="modal__body"><p class="modal-intro">${escapeHtml(message)}</p></div>
        <div class="modal__foot">
          <button class="btn btn--ghost" data-act="cancel">Cancel</button>
          <button class="btn btn--apply" data-act="ok">${escapeHtml(confirmLabel || "Confirm")}</button>
        </div>
      </div>`;
    document.body.appendChild(overlay);
    const close = (val) => {
      overlay.remove();
      document.removeEventListener("keydown", onKey);
      resolve(val);
    };
    const onKey = (e) => {
      if (e.key === "Escape") close(false);
      if (e.key === "Enter") close(true);
    };
    overlay.querySelector('[data-act="cancel"]').onclick = () => close(false);
    overlay.querySelector('[data-act="ok"]').onclick = () => close(true);
    overlay.addEventListener("click", (e) => { if (e.target === overlay) close(false); });
    document.addEventListener("keydown", onKey);
    overlay.querySelector('[data-act="ok"]').focus();
  });
}

/* ---------------- find & replace ---------------- */
function pairsFromText() {
  const olds = $("#rep-old").value.split(/\r?\n/).map((s) => s.trim());
  const news = $("#rep-new").value.split(/\r?\n/).map((s) => s.trim());
  const pairs = [];
  for (let i = 0; i < olds.length; i++) {
    if (olds[i] !== "") pairs.push([olds[i], news[i] || ""]);
  }
  return pairs;
}
$("#runReplace").addEventListener("click", async () => {
  if (TASK_RUNNING) return;   // a write-job is already running
  const pairs = pairsFromText();
  const out = $("#replaceOut");
  if (!pairs.length) {
    out.hidden = false;
    out.textContent = "Add at least one value in the Find column.";
    return;
  }
  const urls = $("#rep-url").checked;
  const ok = await confirmChanges({
    title: "Confirm the changes below",
    intro: pairs.length === 1 ? "Apply this replacement across the site?"
                              : `Apply these ${pairs.length} replacements across the site?`,
    pairs,
    confirmLabel: "Apply changes",
  });
  if (!ok) return;
  out.hidden = false;
  out.textContent = "Running…";
  TASK_RUNNING = "replace";
  $("#runReplace").disabled = true;
  blockWriteBtn("#runRewrite", true);
  try {
    const r = await api("/api/replace", {
      pairs,
      scope: "prefix",
      apply: true,
      smart_case: $("#rep-ci").checked,
      include_guid: urls,
      rename_media: urls,
    });
    out.textContent = r.report || r.error || "(no output)";
  } finally {
    TASK_RUNNING = "";
    $("#runReplace").disabled = false;
    blockWriteBtn("#runRewrite", false);
  }
});

/* ---------------- posts ---------------- */
/* Populate the Post type dropdown from the post types registered on the site.
   Falls back silently to the static page + post options if not connected. */
async function loadPostTypes() {
  const sel = $("#rw-type");
  if (!sel) return;
  let r;
  try {
    r = await api("/api/posttypes", {});
  } catch {
    return;
  }
  if (!r || !r.ok || !Array.isArray(r.types) || !r.types.length) return;
  const prev = sel.value;
  sel.innerHTML = r.types
    .map((t) => `<option value="${escapeHtml(t.name)}">${escapeHtml(t.label)}</option>`)
    .join("");
  if (r.types.some((t) => t.name === prev)) sel.value = prev;
  else if (r.types.some((t) => t.name === "page")) sel.value = "page";
}

const BUILDER_LABELS = {
  elementor: "Elementor", wpbakery: "WPBakery", divi: "Divi",
  gutenberg: "Gutenberg", beaver: "Beaver", oxygen: "Oxygen",
  bricks: "Bricks", classic: "Classic",
};
function builderBadge(key) {
  if (!key) return "";
  const label = BUILDER_LABELS[key] || key;
  return `<span class="bdg bdg--${escapeAttr(key)}">${escapeHtml(label)}</span> `;
}

$("#loadPosts").addEventListener("click", async () => {
  const statuses = $$("#panel-rewrite .statuses input:checked").map((c) => c.value);
  const box = $("#postList");
  box.hidden = false;
  box.innerHTML = "Loading…";
  const r = await api("/api/posts", { post_type: $("#rw-type").value.trim(), statuses });
  if (!r.ok) {
    box.innerHTML = `<span class="meta">${escapeHtml(r.error)}</span>`;
    return;
  }
  if (!r.posts.length) {
    box.innerHTML = '<span class="meta">No posts matched.</span>';
    return;
  }
  const head = `<div class="selrow"><strong>${r.posts.length} ${r.posts.length === 1 ? "page" : "pages"} loaded</strong> · <span id="selCount">all selected</span> <button type="button" class="btn btn--ghost btn--sm" id="selAll">Select all</button><button type="button" class="btn btn--ghost btn--sm" id="selNone">None</button><button type="button" class="btn btn--ghost btn--sm" id="selHundred">Select 100</button></div>`;
  box.innerHTML =
    head +
    r.posts
      .map(
        (p) =>
          `<label><input type="checkbox" class="post-cb" value="${p.id}" checked> ${escapeHtml(p.title)} ${builderBadge(p.builder)}<span class="meta">${escapeHtml(p.type)} #${p.id}</span></label>`
      )
      .join("");
  $("#selAll").onclick = () => { visiblePostCbs().forEach((c) => (c.checked = true)); updateSelCount(); };
  $("#selNone").onclick = () => { visiblePostCbs().forEach((c) => (c.checked = false)); updateSelCount(); };
  sel100Win = 0;
  $("#selHundred").onclick = applySelect100;
  filterPostList();
});
function visiblePostCbs() {
  return $$(".post-cb").filter((c) => {
    const l = c.closest("label");
    return l && l.style.display !== "none";
  });
}
function selectedIds() {
  // Only count rows the current filter is actually showing, so searching also
  // scopes the rewrite (you can't accidentally rewrite hidden, filtered-out pages).
  return visiblePostCbs().filter((c) => c.checked).map((c) => parseInt(c.value, 10));
}
function filterPostList() {
  const q = ($("#postSearch")?.value || "").trim().toLowerCase();
  $$("#postList label").forEach((l) => {
    if (!l.querySelector(".post-cb")) return; // skip the header row
    l.style.display = !q || l.textContent.toLowerCase().includes(q) ? "" : "none";
  });
  sel100Win = 0; // the visible set changed, so restart the 100-page window
  sel100Active = null;
  updateSel100Label();
  updateSelCount();
}
// Live "N selected" count in the header. Counts the rows the current search
// shows (same set a rewrite would touch), so searching also scopes the count.
function updateSelCount() {
  const el = $("#selCount");
  if (!el) return;
  const n = visiblePostCbs().filter((c) => c.checked).length;
  el.textContent = `${n} selected`;
}
// Update the count when a checkbox is toggled by hand (programmatic changes call
// updateSelCount directly, since assigning .checked doesn't fire "change").
$("#postList")?.addEventListener("change", (e) => {
  if (e.target && e.target.classList && e.target.classList.contains("post-cb")) updateSelCount();
});
// "Select 100" walks the list one 100-page block at a time, then wraps to the
// start. It operates on the rows the current search actually shows (so it lines
// up with what a rewrite would touch). The button label shows the block that is
// currently selected ("Selected 201–300"); each click selects the next block.
let sel100Win = 0; // block the NEXT click will select
let sel100Active = null; // block currently selected via this button (null = none yet)
function updateSel100Label() {
  const btn = $("#selHundred");
  if (!btn) return;
  const total = visiblePostCbs().length;
  if (total === 0 || sel100Active === null) {
    btn.textContent = "Select 100";
    return;
  }
  const start = sel100Active * 100;
  const end = Math.min(start + 100, total);
  btn.textContent = `Selected ${start + 1}\u2013${end}`;
}
function applySelect100() {
  const vis = visiblePostCbs();
  const total = vis.length;
  if (total === 0) return;
  const windowCount = Math.ceil(total / 100);
  if (sel100Win >= windowCount) sel100Win = 0; // list shrank since last click
  const block = sel100Win;
  const start = block * 100;
  const end = start + 100;
  vis.forEach((c, i) => (c.checked = i >= start && i < end));
  sel100Active = block; // this block is what's selected now
  sel100Win = (block + 1) % windowCount; // next click selects the following block
  updateSel100Label();
  updateSelCount();
}
$("#postSearch")?.addEventListener("input", filterPostList);
function previewPostId() {
  const sel = selectedIds();
  return sel.length ? sel[0] : null;
}

/* ---------------- placeholders ---------------- */
$("#addPh").addEventListener("click", () => addPh());
function addPh(key = "", expr = "") {
  const tr = document.createElement("tr");
  tr.innerHTML =
    `<td><input class="ph-key" placeholder="company" value="${escapeAttr(key)}"></td>` +
    `<td><input class="mono ph-expr" placeholder="//title" value="${escapeAttr(expr)}"></td>` +
    `<td style="width:1%"><button type="button" class="btn btn--ghost btn--sm ph-del">✕</button></td>`;
  tr.querySelector(".ph-del").onclick = () => tr.remove();
  $("#phTable tbody").appendChild(tr);
}
function gatherPlaceholders() {
  const map = {};
  $$("#phTable tbody tr").forEach((tr) => {
    const k = tr.querySelector(".ph-key").value.trim();
    const v = tr.querySelector(".ph-expr").value.trim();
    if (k && v) map[k] = v;
  });
  return map;
}

/* ---------------- xpath rows ---------------- */
$("#addXp").addEventListener("click", () => addXp());
function addXp(xpath = "", prompt = DEFAULT_PROMPT) {
  const node = $("#xpRowTpl").content.firstElementChild.cloneNode(true);
  node.querySelector(".xp-xpath").value = xpath;
  node.querySelector(".xp-prompt").value = prompt;
  node.querySelector(".xp-del").onclick = () => node.remove();
  node.querySelector(".xp-fetch").onclick = () => preview(node, "fetch");
  node.querySelector(".xp-test").onclick = () => preview(node, "test");
  $("#xpRows").appendChild(node);
}
async function preview(row, action) {
  const box = row.querySelector(".xp-result");
  const pid = previewPostId();
  if (!pid) {
    box.innerHTML = `<div class="res res--err">Select a page first — tick a checkbox in the list above, then Fetch sample. The sample is taken from the first selected page.</div>`;
    return;
  }
  box.innerHTML = `<div class="res res--info">Working…</div>`;
  const r = await api("/api/preview", {
    action,
    post_id: pid,
    xpath: row.querySelector(".xp-xpath").value.trim(),
    prompt: row.querySelector(".xp-prompt").value,
    placeholders: gatherPlaceholders(),
  });
  if (action === "fetch") {
    let html = `<div class="res ${r.ok ? "res--ok" : "res--err"}">`;
    if (r.url) html += `<strong>Sample page</strong><a href="${escapeAttr(r.url)}" target="_blank" rel="noopener">${escapeHtml(r.url)}</a>`;
    html += r.ok
      ? `<strong>Matched content (post #${pid})</strong>${escapeHtml(r.message)}`
      : escapeHtml(r.message);
    html += `</div>`;
    box.innerHTML = html;
    return;
  }
  let html = `<div class="res ${r.ok ? "res--ok" : "res--err"}">`;
  if (r.url) html += `<strong>Sample page</strong><a href="${escapeAttr(r.url)}" target="_blank" rel="noopener">${escapeHtml(r.url)}</a>`;
  const evals = r.evaluations || {};
  if (Object.keys(evals).length) {
    html += `<strong>Placeholder values</strong>` + Object.entries(evals).map(([k, v]) => `${escapeHtml(k)} → ${escapeHtml(v)}`).join("\n");
  }
  html += `<strong>${r.ok ? "Model reply" : "Error"}</strong>${escapeHtml(r.message)}</div>`;
  box.innerHTML = html;
}

/* ---------------- rewrite (streaming) ---------------- */
function gatherRows() {
  return $$("#xpRows .xprow")
    .map((r) => ({ xpath: r.querySelector(".xp-xpath").value.trim(), prompt: r.querySelector(".xp-prompt").value }))
    .filter((r) => r.xpath !== "");
}
let rwJobId = null;
function setRewriteRunning(running) {
  const stop = $("#stopRewrite"), run = $("#runRewrite");
  if (stop) { stop.hidden = !running; stop.disabled = false; stop.textContent = "Stop"; }
  if (run) run.disabled = running;
  TASK_RUNNING = running ? "rewrite" : "";
  blockWriteBtn("#runReplace", running);
}
const _stopBtn = $("#stopRewrite");
if (_stopBtn) _stopBtn.addEventListener("click", async () => {
  if (!rwJobId) return;
  _stopBtn.disabled = true;
  _stopBtn.textContent = "Stopping…";
  appendLog("Stop requested — finishing the pages already in progress, then stopping…", "muted");
  try { await api("/api/rewrite/stop", { job_id: rwJobId }); } catch (e) {}
});

$("#runRewrite").addEventListener("click", async () => {
  if (TASK_RUNNING) return;   // a write-job is already running
  const rows = gatherRows();
  const ids = selectedIds();
  const dry = false; // preview removed — rewrites always apply
  const prog = $("#rwProgress");
  const log = $("#rwLog");
  prog.hidden = false;

  if (!rows.length) { log.textContent = "Add at least one XPath/prompt row."; return; }
  if (!ids.length) { log.textContent = "Load posts and select at least one."; return; }
  if (!(await confirmDialog({
    title: "Rewrite content?",
    message: `Rewrite ${ids.length} post(s) and save to the live database. Make sure you have a backup.`,
    confirmLabel: "Rewrite",
  }))) return;

  ["updated", "skipped", "error"].forEach((k) => ($("#c-" + k).textContent = "0"));
  log.innerHTML = "";
  const counts = { updated: 0, skipped: 0, error: 0 };
  let total = ids.length;

  rwJobId = (window.crypto && crypto.randomUUID) ? crypto.randomUUID() : ("job-" + Math.random().toString(16).slice(2));
  setRewriteRunning(true);

  let res;
  try {
    res = await fetch("/api/rewrite", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ rows, placeholders: gatherPlaceholders(), ids, dry_run: dry, elementor: $("#rw-elementor").checked, job_id: rwJobId }),
    });
  } catch (e) {
    appendLog("Couldn't start the rewrite.", "err");
    setRewriteRunning(false);
    rwJobId = null;
    return;
  }
  const reader = res.body.getReader();
  const dec = new TextDecoder();
  let buf = "";
  let processed = 0;

  try {
  while (true) {
    const { value, done } = await reader.read();
    if (done) break;
    buf += dec.decode(value, { stream: true });
    let nl;
    while ((nl = buf.indexOf("\n")) >= 0) {
      const line = buf.slice(0, nl).trim();
      buf = buf.slice(nl + 1);
      if (!line) continue;
      const ev = JSON.parse(line);
      if (ev.event === "start") {
        total = ev.total;
        appendLog(`Starting ${ev.dry_run ? "preview" : "rewrite"} of ${total} post(s)…`, "muted");
      } else if (ev.event === "post") {
        processed++;
        counts[ev.status] = (counts[ev.status] || 0) + 1;
        const cEl = $("#c-" + ev.status);
        if (cEl) cEl.textContent = counts[ev.status];
        $("#c-of").textContent = `${processed} / ${total}`;
        const cls = ev.status === "error" ? "err" : ev.status === "skipped" ? "skip" : "ok";
        const tag = { updated: "DONE", preview: "PREVIEW", skipped: "SKIP", error: "ERR" }[ev.status] || ev.status;
        appendLog(`${tag} #${ev.id} — ${ev.message}`, cls);
      } else if (ev.event === "writing") {
        appendLog(`\nWriting ${ev.count} change(s) to the database…`, "muted");
      } else if (ev.event === "written") {
        appendLog(`Saved ${ev.count} page(s).`, "ok");
      } else if (ev.event === "stopping") {
        appendLog(`\nStopping — finishing the pages already in progress; ${ev.not_started} page(s) will be left unchanged.`, "muted");
      } else if (ev.event === "done") {
        const c = ev.counts;
        if (ev.stopped) {
          appendLog(`\nStopped. ${c.updated} updated, ${c.skipped} skipped, ${c.error} error(s); ${ev.not_started} page(s) not started (left unchanged).`, "muted");
        } else {
          appendLog(`\nFinished: ${c.updated} updated, ${c.skipped} skipped, ${c.error} errors.`, "muted");
        }
        appendLog("If the live site uses a persistent or page cache, purge it.", "muted");
      } else if (ev.event === "error") {
        appendLog(ev.message, "err");
      }
    }
  }
  } finally {
    setRewriteRunning(false);
    rwJobId = null;
  }
});
function appendLog(text, cls) {
  const span = document.createElement("span");
  span.className = "ln-" + cls;
  span.textContent = text + "\n";
  $("#rwLog").appendChild(span);
  $("#rwLog").scrollTop = $("#rwLog").scrollHeight;
}

/* ---------------- helpers ---------------- */
function escapeHtml(s) {
  return String(s == null ? "" : s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}
function escapeAttr(s) {
  return escapeHtml(s);
}

/* ---------------- boot ---------------- */
addPh("company", "//title");
addXp("//p");
loadConfig().then(() => {
  // Quietly probe the connection so the status pill is meaningful on load.
  api("/api/check", {}).then((r) => {
    if (r.ok) {
      setStatus("ok", r.wordpress && r.wordpress !== "(path not found)" ? "WordPress " + r.wordpress : "Connected");
      loadPostTypes();
    } else setStatus("unknown", "Not connected");
  });
});

/* ---------------- logo ---------------- */
$("#logo-upload").addEventListener("click", async () => {
  const out = $("#logoOut");
  const file = $("#logo-file").files[0];
  if (!file) {
    out.hidden = false;
    out.textContent = "Choose an image file first.";
    return;
  }
  out.hidden = false;
  out.textContent = "Uploading to the media library…";
  const fd = new FormData();
  fd.append("file", file);
  try {
    const r = await fetch("/api/logo/upload", { method: "POST", body: fd }).then((res) => res.json());
    if (r.ok) {
      $("#logo-new").value = r.url || "";
      out.textContent = "Uploaded (attachment #" + r.id + "). New logo URL filled in:\n" + (r.url || "(no URL returned)");
    } else {
      out.textContent = "Upload failed: " + (r.error || "unknown error");
    }
  } catch (e) {
    out.textContent = "Upload failed: " + e;
  }
});

$("#logo-replace").addEventListener("click", async () => {
  const out = $("#logoOut");
  const old_url = $("#logo-old").value.trim();
  const new_url = $("#logo-new").value.trim();
  if (!new_url) {
    out.hidden = false;
    out.textContent = "Enter the new logo URL, or upload a logo first.";
    return;
  }
  if (!(await confirmDialog({
    title: "Replace logo?",
    message: "Replace the logo across the live database. Make sure you have a recent backup.",
    confirmLabel: "Replace logo",
  }))) return;
  out.hidden = false;
  out.textContent = "Replacing the logo everywhere…";
  const r = await api("/api/logo/replace", { old_url, new_url, set_identity: $("#logo-identity").checked });
  out.textContent = r.ok ? r.report || "Done." : "Failed: " + (r.error || "unknown error");
});

$("#logo-clear").addEventListener("click", () => {
  $("#logo-old").value = "";
  $("#logo-new").value = "";
  $("#logo-file").value = "";
  $("#logo-identity").checked = true;
  const out = $("#logoOut");
  out.hidden = true;
  out.textContent = "";
});

/* ---------------- auto-detect API provider from the key ---------------- */
function detectProvider(key) {
  const k = (key || "").trim();
  if (k.startsWith("sk-or-")) return { base: "https://openrouter.ai/api/v1", model: "openai/gpt-4o-mini" };
  if (k.startsWith("AIza"))   return { base: "https://generativelanguage.googleapis.com/v1beta/openai", model: "gemini-2.5-flash" };
  if (k.startsWith("gsk_"))   return { base: "https://api.groq.com/openai/v1", model: "llama-3.1-8b-instant" };
  if (k.startsWith("sk-ant-")) return null; // Anthropic's API isn't OpenAI-compatible
  if (k.startsWith("sk-"))    return { base: "https://api.openai.com/v1", model: "gpt-4o-mini" };
  return null;
}
$("#cfg-key").addEventListener("input", () => {
  const d = detectProvider($("#cfg-key").value);
  if (!d) return;
  if ($("#cfg-base").value.trim() !== d.base) {
    $("#cfg-base").value = d.base;
    if (d.model) $("#cfg-model").value = d.model;
  }
});

$("#testAI").addEventListener("click", async () => {
  await api("/api/config", gatherConfig()); // save current settings first
  $("#cfg-key").value = "";
  $("#cfg-password").value = "";
  const out = $("#checkOut");
  out.hidden = false;
  out.textContent = "Testing AI…";
  const r = await api("/api/ai/test", {});
  if (r.ok) {
    out.textContent = `AI OK — ${r.provider === "cli" ? "Claude CLI" : "API"}\nReply: ${r.reply}`;
  } else {
    out.textContent = "AI test failed:\n\n" + r.message;
  }
});

/* ---------------- find & replace: clear ---------------- */
$("#repClear").addEventListener("click", () => {
  $("#rep-old").value = "";
  $("#rep-new").value = "";
  $("#rep-ci").checked = true;
  $("#rep-url").checked = true;
  const out = $("#replaceOut");
  out.hidden = true;
  out.textContent = "";
});

/* ---------------- favicon ---------------- */
$("#favicon-set").addEventListener("click", async () => {
  const out = $("#faviconOut");
  const file = $("#favicon-file").files[0];
  if (!file) {
    out.hidden = false;
    out.textContent = "Choose a favicon image first.";
    return;
  }
  out.hidden = false;
  out.textContent = "Uploading and setting the favicon…";
  const fd = new FormData();
  fd.append("file", file);
  try {
    const r = await fetch("/api/favicon/upload", { method: "POST", body: fd }).then((res) => res.json());
    out.textContent = r.ok
      ? (r.report || "Favicon set.") + "\n" + (r.url || "") + "\n\nPurge your cache + CDN and hard-refresh to see it."
      : "Failed: " + (r.error || "unknown error");
  } catch (e) {
    out.textContent = "Failed: " + e;
  }
});