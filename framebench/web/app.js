"use strict";
const $ = (s) => document.querySelector(s);
const api = async (path, opts = {}) => {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json",
               "Idempotency-Key": opts.idem || crypto.randomUUID() },
    method: opts.method || "GET",
    body: opts.body ? JSON.stringify(opts.body) : undefined,
  });
  if (!res.ok) throw new Error((await res.json()).error || res.statusText);
  return res.json();
};

let state = { captures: [], branches: [], branch: null, run: null,
              capture: null, selected: new Set(), hexRecord: 0 };

async function boot() {
  const proto = await api("/api/protocols");
  for (const p of proto.protocols) {
    for (const sel of ["#protoSel", "#migrateTo"]) {
      const o = document.createElement("option");
      o.value = p.version; o.textContent = `nbp/${p.version}`;
      $(sel).appendChild(o);
    }
  }
  $("#health").textContent = "后端正常";
  $("#btnSample").onclick = async () => {
    $("#capInput").value = (await (await fetch("/api/sample")).text());
  };
  $("#btnUpload").onclick = upload;
  $("#btnOp").onclick = applyOp;
  $("#btnCompare").onclick = compare;
  $("#btnExport").onclick = exportBranch;
  $("#btnMigrate").onclick = migrate;
  await refreshCaptures();
}

async function upload() {
  const body = { name: $("#capName").value, data: $("#capInput").value,
                 protocol: $("#protoSel").value };
  const out = await api("/api/captures", { method: "POST", body });
  await refreshCaptures();
  await selectCapture(out.capture.id);
}

async function refreshCaptures() {
  state.captures = (await api("/api/captures")).captures;
  const ul = $("#capList"); ul.innerHTML = "";
  for (const c of state.captures) {
    const li = document.createElement("li");
    li.textContent = `${c.name} (${c.frames} 帧)`;
    li.onclick = () => selectCapture(c.id);
    ul.appendChild(li);
  }
}

async function selectCapture(id) {
  state.capture = await api(`/api/captures/${id}`);
  const { branches } = await api(`/api/captures/${id}/branches`);
  state.branches = branches;
  const ul = $("#branchList"); ul.innerHTML = "";
  state.selected.clear();
  for (const b of branches) {
    const li = document.createElement("li");
    li.textContent = `${b.name} · nbp/${b.protocol_version} · ${b.ops.length} ops`;
    li.onclick = (e) => {
      if (e.shiftKey) {
        state.selected.has(b.id) ? state.selected.delete(b.id)
                                 : state.selected.add(b.id);
        li.classList.toggle("sel");
      } else { selectBranch(b.id); }
    };
    ul.appendChild(li);
  }
  if (branches.length) selectBranch(branches[branches.length - 1].id);
}

async function selectBranch(id) {
  const out = await api(`/api/branches/${id}`);
  state.branch = out.branch; state.run = out.run;
  renderAll();
}

function renderAll() {
  renderStatus(); renderEvents(); renderDiagnostics();
  renderStates(); renderHex(state.hexRecord, []);
  $("#runMeta").textContent =
    `nbp/${state.run.protocol_version} · ${state.run.events.length} 事件`;
}

function renderStatus() {
  const cols = { pending: "#colPending", accepted: "#colAccepted",
                 rejected: "#colRejected", recovering: "#colRecovering" };
  for (const k in cols) $(cols[k]).innerHTML = "";
  for (const f of state.run.frames) {
    const chip = document.createElement("span");
    chip.className = "chip";
    chip.textContent = `#${f.record}${f.reason ? " · " + f.reason : ""}`;
    chip.title = f.reason || f.status;
    chip.onclick = () => { state.hexRecord = f.record; renderHex(f.record, []); };
    $(cols[f.status]).appendChild(chip);
  }
}

function renderEvents() {
  const box = $("#events"); box.innerHTML = "";
  for (const ev of state.run.events) {
    const div = document.createElement("div");
    div.className = "ev";
    div.innerHTML = `<span class="sev ${ev.severity}">${ev.severity}</span>` +
      `<span>${ev.id} ${ev.kind} ${esc(JSON.stringify(ev.detail))}</span>`;
    div.onclick = () => showEventBytes(ev);
    box.appendChild(div);
  }
}

function renderDiagnostics() {
  const box = $("#diagList"); box.innerHTML = "";
  const diags = state.run.events
    .filter((e) => e.severity !== "info")
    .sort((a, b) => sevRank(b.severity) - sevRank(a.severity));
  if (!diags.length) box.innerHTML = '<div class="sub">无告警或错误</div>';
  for (const ev of diags) {
    const d = document.createElement("div");
    d.className = `d ${ev.severity}`;
    d.textContent = `[${ev.severity}] ${ev.kind} — ${JSON.stringify(ev.detail)}`;
    d.onclick = () => showEventBytes(ev);
    box.appendChild(d);
  }
}

function renderStates() {
  const box = $("#states"); box.innerHTML = "";
  for (const ev of state.run.events.filter((e) => e.kind === "state_transition")) {
    const div = document.createElement("div");
    div.className = "tr";
    div.textContent = `s${ev.detail.session} ${ev.detail.from} --${ev.detail.msg}--> ${ev.detail.to}`;
    div.onclick = () => showEventBytes(ev);
    box.appendChild(div);
  }
}

function showEventBytes(ev) {
  if (!ev.byte_ranges.length) return;
  const rec = ev.byte_ranges[0].record;
  state.hexRecord = rec;
  renderHex(rec, ev.byte_ranges.filter((r) => r.record === rec));
}

function renderHex(recId, ranges) {
  const box = $("#hexview");
  const rec = state.capture.records[recId];
  if (!rec) { box.textContent = ""; return; }
  const bytes = rec.data.match(/../g).map((h) => parseInt(h, 16));
  const hl = new Set();
  for (const r of ranges) for (let i = r.start; i < r.end; i++) hl.add(i);
  let html = `<div class="sub">记录 #${recId} · ${rec.dir} · ts=${rec.ts} · ${bytes.length}B</div>`;
  for (let off = 0; off < bytes.length; off += 16) {
    let line = off.toString(16).padStart(4, "0") + "  ";
    for (let i = off; i < Math.min(off + 16, bytes.length); i++) {
      const h = bytes[i].toString(16).padStart(2, "0");
      line += hl.has(i) ? `<span class="hl">${h}</span> ` : h + " ";
    }
    html += line + "\n";
  }
  box.innerHTML = html;
}

async function applyOp() {
  const kind = $("#opKind").value;
  const op = { op: kind, record: parseInt($("#opRecord").value, 10) };
  if (kind === "retime") op.ts = parseFloat($("#opValue").value);
  if (kind === "replace_payload") op.data = $("#opValue").value.trim();
  const out = await api(`/api/branches/${state.branch.id}/ops`,
                        { method: "POST", body: op });
  state.branch = out.branch; state.run = out.run;
  renderAll();
}

async function compare() {
  const ids = [...state.selected];
  if (ids.length !== 2) { $("#diffOut").textContent = "Shift+点击选择两个分支"; return; }
  const d = await api(`/api/branches/${ids[0]}/diff/${ids[1]}`);
  $("#diffOut").textContent = d.diverges
    ? `从事件 #${d.first_divergent_index} 开始分歧\nA: ${JSON.stringify(d.event_a && d.event_a.kind)}\nB: ${JSON.stringify(d.event_b && d.event_b.kind)}`
    : `两分支一致（${d.events} 个事件）`;
}

async function exportBranch() {
  window.location = `/api/branches/${state.branch.id}/export`;
}

async function migrate() {
  const to = $("#migrateTo").value;
  const out = await api(`/api/branches/${state.branch.id}/migrate`,
                        { method: "POST", body: { to } });
  const r = out.report;
  let txt = `迁移检查 ${r.from} -> ${r.to}\n` +
    (r.static_findings.length ? r.static_findings.join("\n") : "静态检查通过") +
    `\n拒绝帧: ${r.rejected_frames}, 错误事件: ${r.error_events}`;
  if (r.compatible && !out.branch.migrated) {
    txt += "\n兼容 — 再次点击确认迁移";
    if (state._migrateArmed) {
      const done = await api(`/api/branches/${state.branch.id}/migrate`,
                             { method: "POST", body: { to, confirm: true } });
      txt += "\n已迁移到 " + done.branch.protocol_version;
      state.branch = done.branch;
      state._migrateArmed = false;
    } else state._migrateArmed = true;
  }
  $("#migrateOut").textContent = txt;
}

const sevRank = (s) => ({ info: 0, warning: 1, error: 2, fatal: 3 }[s]);
const esc = (s) => s.replace(/[&<>]/g, (c) =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" }[c]));

boot().catch((e) => { $("#health").textContent = "后端异常: " + e.message; });
