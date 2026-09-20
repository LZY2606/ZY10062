/* framebench web UI — all data comes from the API; nothing is hard-coded. */
"use strict";

const SEV_RANK = { fatal: 0, error: 1, warning: 2, info: 3 };
const STATUS_LABELS = {
  pending: "待确认", accepted: "已接受", rejected: "被拒绝", recovering: "恢复中",
};

const state = {
  protocols: [],
  captures: [],
  capture: null,        // capture detail
  selectedEvent: null,
  selectedFrame: null,
  branch: null,         // branch detail
  idemKey: null,
};

const $ = (id) => document.getElementById(id);

async function api(path, opts = {}) {
  const headers = { "Content-Type": "application/json" };
  if (opts.idem) headers["Idempotency-Key"] = opts.idem;
  const res = await fetch(path, {
    method: opts.method || "GET",
    headers,
    body: opts.body ? JSON.stringify(opts.body) : undefined,
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    const err = new Error(data.error || ("HTTP " + res.status));
    err.status = res.status;
    err.data = data;
    throw err;
  }
  return data;
}

function esc(text) {
  return String(text).replace(/[&<>"]/g,
    (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
}

/* ---------------------------------------------------------------- init */
async function init() {
  state.protocols = await api("/api/protocols");
  const sel = $("cap-protocol");
  sel.innerHTML = state.protocols
    .map((p) => `<option value="${p.name}@${p.version}">${p.name}@${p.version}</option>`)
    .join("");
  const mig = $("migrate-version");
  mig.innerHTML = sel.innerHTML;
  await refreshCaptures();
  bind();
}

async function refreshCaptures() {
  state.captures = await api("/api/captures");
  const ul = $("capture-list");
  ul.innerHTML = "";
  for (const cap of state.captures) {
    const li = document.createElement("li");
    const badges = Object.entries(cap.statuses)
      .map(([k, v]) => `${STATUS_LABELS[k] || k}×${v}`).join(" ");
    li.innerHTML = `<b>${esc(cap.name)}</b> <span class="mono">${esc(cap.id)}</span>
      · ${esc(cap.protocol)} · ${cap.frames} 帧 / ${cap.events} 事件
      <div>${esc(badges)}</div>`;
    li.onclick = () => selectCapture(cap.id, li);
    ul.appendChild(li);
  }
  $("store-status").textContent = `捕获 ${state.captures.length} 个`;
}

async function selectCapture(id, li) {
  document.querySelectorAll("#capture-list li").forEach((n) => n.classList.remove("active"));
  if (li) li.classList.add("active");
  state.capture = await api(`/api/captures/${id}`);
  state.selectedEvent = null;
  state.branch = null;
  $("branch-detail").hidden = true;
  renderCapture();
}

/* -------------------------------------------------------------- render */
function renderCapture() {
  const cap = state.capture;
  $("detail-panel").hidden = false;
  $("cap-title").textContent = `捕获 ${cap.capture.name}（${cap.capture.protocol}）`;
  $("cap-digest").textContent = "digest " + cap.digest.slice(0, 16) + "…";

  // sessions board: pending / accepted / rejected / recovering
  for (const col of document.querySelectorAll("#session-board .col")) {
    const ul = col.querySelector("ul");
    ul.innerHTML = "";
    const status = col.dataset.status;
    for (const [name, sess] of Object.entries(cap.sessions)) {
      if (sess.status !== status) continue;
      const li = document.createElement("li");
      li.textContent = `${name} · ${sess.state}` +
        (sess.warnings ? ` · ⚠${sess.warnings}` : "");
      ul.appendChild(li);
    }
  }

  // events
  const evBox = $("events");
  evBox.innerHTML = "";
  for (const ev of cap.events) {
    evBox.appendChild(eventNode(ev, () => onEventClick(ev)));
  }

  // diagnostics sorted by severity priority
  const diag = $("diagnostics");
  diag.innerHTML = "";
  const diags = cap.events
    .filter((e) => e.severity !== "info")
    .sort((a, b) => SEV_RANK[a.severity] - SEV_RANK[b.severity] || a.idx - b.idx);
  if (!diags.length) diag.innerHTML = '<div class="mono">无诊断，一切正常。</div>';
  for (const ev of diags) {
    diag.appendChild(eventNode(ev, () => onEventClick(ev)));
  }

  // state transitions
  const trans = $("transitions");
  trans.innerHTML = "";
  for (const ev of cap.events.filter((e) => e.kind === "state_transition")) {
    const div = document.createElement("div");
    div.className = "trans mono";
    div.innerHTML = `${esc(ev.session)}: ${esc(ev.state_from)}
      <span class="arrow">→</span> ${esc(ev.state_to)}
      <span class="sev info">@${esc(ev.id)}</span>`;
    div.onclick = () => onEventClick(ev);
    trans.appendChild(div);
  }

  // frame selector for hex view
  const sel = $("frame-select");
  sel.innerHTML = "";
  for (const fr of cap.frames) {
    const opt = document.createElement("option");
    opt.value = fr.id;
    opt.textContent = `${fr.id} · ${fr.session} · ${fr.dir} · ${fr.data.length / 2}B`;
    sel.appendChild(opt);
  }
  if (cap.frames.length) {
    state.selectedFrame = state.selectedFrame || cap.frames[0].id;
    sel.value = state.selectedFrame;
    renderHex(null);
  }

  // branches
  const bl = $("branch-list");
  bl.innerHTML = "";
  for (const br of cap.branches) {
    const li = document.createElement("li");
    li.innerHTML = `<b>${esc(br.name)}</b> <span class="mono">${esc(br.id)}</span>
      · 绑定 ${esc(br.protocol)} · ${br.mutations.length} 个变更
      · 锚点 ${esc(br.anchor_event || "-")}`;
    li.onclick = () => selectBranch(br.id);
    bl.appendChild(li);
  }
}

function eventNode(ev, onclick) {
  const div = document.createElement("div");
  div.className = `event sev-${ev.severity}`;
  div.dataset.eid = ev.id;
  const range = ev.byte_start != null
    ? ` [${ev.frame_id}:${ev.byte_start}–${ev.byte_end ?? "?"}]` : "";
  div.innerHTML = `<span class="sev ${ev.severity}">${ev.severity}</span>
    <span class="mono">${esc(ev.id)}</span> ${esc(ev.kind)}
    · ${esc(ev.message)}<span class="mono">${esc(range)}</span>`;
  div.onclick = onclick;
  return div;
}

function onEventClick(ev) {
  state.selectedEvent = ev;
  document.querySelectorAll(".event.selected")
    .forEach((n) => n.classList.remove("selected"));
  document.querySelectorAll(`.event[data-eid="${ev.id}"]`)
    .forEach((n) => n.classList.add("selected"));
  $("branch-anchor").textContent = ev.id;
  if (ev.frame_id) {
    state.selectedFrame = ev.frame_id;
    $("frame-select").value = ev.frame_id;
    renderHex(ev);
  }
}

function renderHex(highlight) {
  const cap = state.capture;
  const fr = cap.frames.find((f) => f.id === state.selectedFrame);
  if (!fr) return;
  const bytes = fr.data.match(/../g) || [];
  const hlStart = highlight && highlight.frame_id === fr.id ? highlight.byte_start : null;
  const hlEnd = highlight && highlight.frame_id === fr.id ? highlight.byte_end : null;
  let out = "";
  for (let row = 0; row < bytes.length; row += 16) {
    const chunk = bytes.slice(row, row + 16);
    const hexCells = chunk.map((b, i) => {
      const off = row + i;
      const hl = hlStart !== null && hlEnd !== null && off >= hlStart && off < hlEnd;
      return hl ? `<span class="hl">${b}</span>` : b;
    }).join(" ");
    const ascii = chunk.map((b) => {
      const c = parseInt(b, 16);
      return c >= 32 && c < 127 ? String.fromCharCode(c) : "·";
    }).join("");
    out += `<span class="off">${row.toString(16).padStart(4, "0")}</span>  ` +
      hexCells.padEnd(47, " ") + `  ${esc(ascii)}\n`;
  }
  $("hex-title").textContent = `${fr.id} @${fr.ts}`;
  $("hex-view").innerHTML = out || "(空帧)";
}

/* -------------------------------------------------------------- branch */
async function selectBranch(bid) {
  state.branch = await api(`/api/branches/${bid}`);
  const br = state.branch;
  $("branch-detail").hidden = false;
  $("branch-title").textContent =
    `分支 ${br.branch.name}（绑定 ${br.branch.protocol}，锚点 ${br.branch.anchor_event || "-"}）`;
  const ml = $("mutation-list");
  ml.innerHTML = "";
  for (const m of br.branch.mutations) {
    const li = document.createElement("li");
    li.textContent = JSON.stringify(m);
    ml.appendChild(li);
  }
  const be = $("branch-events");
  be.innerHTML = "";
  for (const ev of br.events) {
    be.appendChild(eventNode(ev, () => {}));
  }
  $("branch-result").textContent =
    `分支 digest: ${br.digest}\n事件数: ${br.events.length}`;
}

/* ---------------------------------------------------------------- bind */
function bind() {
  $("cap-create").onclick = async () => {
    await api("/api/captures", {
      method: "POST",
      body: { name: $("cap-name").value || undefined,
              protocol: $("cap-protocol").value },
      idem: "create-" + Date.now(),
    });
    await refreshCaptures();
  };

  $("gen-example").onclick = async () => {
    const doc = await api("/api/example");
    $("frames-json").value = JSON.stringify(doc, null, 1);
  };

  $("frames-upload").onclick = async () => {
    const out = $("upload-result");
    if (!state.capture) { out.textContent = "请先选择捕获"; out.className = "err"; return; }
    let doc;
    try {
      doc = JSON.parse($("frames-json").value);
    } catch {
      out.textContent = "JSON 解析失败"; out.className = "err"; return;
    }
    if (!state.idemKey) state.idemKey = "up-" + crypto.randomUUID();
    $("idem-key").value = state.idemKey;
    try {
      const res = await api(`/api/captures/${state.capture.capture.id}/frames`, {
        method: "POST", body: doc, idem: state.idemKey,
      });
      out.textContent = `已接受 ${res.accepted} 帧` + (res.replayed ? "（幂等重放）" : "");
      out.className = "ok";
      state.idemKey = null;
      await selectCapture(state.capture.capture.id);
    } catch (err) {
      out.textContent = "失败：" + err.message + "（可原键重试）";
      out.className = "err";
    }
  };

  $("frame-select").onchange = (e) => {
    state.selectedFrame = e.target.value;
    renderHex(state.selectedEvent);
  };

  $("export-btn").onclick = async () => {
    const bundle = await api(`/api/captures/${state.capture.capture.id}/export`);
    const blob = new Blob([JSON.stringify(bundle, null, 1)],
      { type: "application/json" });
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = `framebench-${state.capture.capture.id}.json`;
    a.click();
  };

  $("branch-create").onclick = async () => {
    if (!state.capture) return;
    await api(`/api/captures/${state.capture.capture.id}/branches`, {
      method: "POST",
      body: { name: $("branch-name").value || undefined,
              anchor_event: state.selectedEvent ? state.selectedEvent.id : null },
      idem: "br-" + Date.now(),
    });
    await selectCapture(state.capture.capture.id);
  };

  $("mutation-add").onclick = async () => {
    if (!state.branch) return;
    const type = $("mutation-type").value;
    const mutation = { type, frame_id: $("mutation-frame").value.trim() };
    if (type === "retime") mutation.ts = parseFloat($("mutation-arg1").value);
    if (type === "replace_payload") {
      mutation.offset = parseInt($("mutation-arg1").value, 10);
      mutation.data = $("mutation-arg2").value.trim();
    }
    try {
      await api(`/api/branches/${state.branch.branch.id}/mutations`, {
        method: "POST", body: { mutation }, idem: "mut-" + Date.now(),
      });
      await selectBranch(state.branch.branch.id);
    } catch (err) {
      $("branch-result").textContent = "变更被拒绝：" + err.message;
    }
  };

  $("diff-btn").onclick = async () => {
    const diff = await api(`/api/branches/${state.branch.branch.id}/diff`);
    $("branch-result").textContent = diff.identical
      ? "两分支事件流完全一致。"
      : `从事件 ${diff.diverges_at.event_id} 开始分歧：\n` +
        `基线: ${JSON.stringify(diff.diverges_at.base && diff.diverges_at.base.message)}\n` +
        `分支: ${JSON.stringify(diff.diverges_at.branch && diff.diverges_at.branch.message)}`;
  };

  $("migrate-btn").onclick = async () => {
    const bid = state.branch.branch.id;
    try {
      const res = await api(`/api/branches/${bid}/migrate`, {
        method: "POST",
        body: { version: $("migrate-version").value,
                force: $("migrate-force").checked },
        idem: "mig-" + Date.now(),
      });
      $("branch-result").textContent =
        `迁移完成 → ${res.protocol}\n` + JSON.stringify(res.report, null, 1);
      await selectBranch(bid);
    } catch (err) {
      $("branch-result").textContent =
        "兼容检查未通过：\n" + JSON.stringify(err.data, null, 1);
    }
  };
}

init().catch((err) => {
  $("store-status").textContent = "加载失败: " + err.message;
});
