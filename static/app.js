"use strict";

const $ = (sel) => document.querySelector(sel);
const S = {
  chats: [],
  chatId: null,
  chat: null,
  settings: null,
  models: null,
  tab: "state",
  inspectTid: null,
  sending: false,
  scanResult: null,
  summaryFilter: "",
};

// ---------- helpers ----------

async function api(method, path, body) {
  const opts = { method, headers: {} };
  if (body !== undefined) {
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(body);
  }
  const res = await fetch(path, opts);
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
  return data;
}

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

function md(src) {
  return String(src || "").split("```").map((part, i) => {
    if (i % 2) {
      const nl = part.indexOf("\n");
      return `<pre><code>${esc(nl >= 0 ? part.slice(nl + 1) : part)}</code></pre>`;
    }
    const h = esc(part)
      .replace(/`([^`\n]+)`/g, "<code>$1</code>")
      .replace(/\*\*([^*\n]+)\*\*/g, "<strong>$1</strong>")
      .replace(/^#{1,4} (.+)$/gm, '<span class="h">$1</span>');
    return `<div class="txt">${h}</div>`;
  }).join("");
}

const fmtNum = (n) => Number(n || 0).toLocaleString("en-US");
const fmtUsd = (n) => "$" + Number(n || 0).toFixed(n && n < 0.01 ? 5 : 3);

function el(html) {
  const t = document.createElement("template");
  t.innerHTML = html.trim();
  return t.content.firstElementChild;
}

function setStatus(text, isErr = false) {
  const s = $("#status");
  s.textContent = text || "";
  s.classList.toggle("err", isErr);
}

// ---------- loading ----------

async function loadSettings() {
  S.settings = await api("GET", "/api/settings");
  const s = S.settings;
  const missing = [];
  if (!s.api_key_set) missing.push("API key");
  if (!s.model_summarizer) missing.push("ตัวสรุป");
  if (!s.model_scanner) missing.push("ตัววิ่งหาข้อมูล");
  if (!s.model_main) missing.push("ตัวทำงานหลัก");
  $("#setupHint").textContent = missing.length ? "ยังไม่ได้ตั้ง: " + missing.join(", ") : "";
}

async function loadChats() {
  S.chats = await api("GET", "/api/chats");
  renderChatList();
}

async function openChat(id) {
  S.chatId = id;
  S.scanResult = null;
  try { localStorage.setItem("lastChat", String(id)); } catch {}
  await refreshChat();
}

async function refreshChat() {
  if (S.chatId == null) return render();
  try {
    S.chat = await api("GET", `/api/chats/${S.chatId}`);
  } catch (e) {
    S.chatId = null;
    S.chat = null;
  }
  render();
  watchSummarizer();
}

let summaryWatch = null;
function watchSummarizer() {
  if (summaryWatch || !S.chat) return;
  const id = S.chatId;
  const tick = async () => {
    if (S.chatId !== id) { summaryWatch = null; return; }
    let st;
    try { st = await api("GET", `/api/chats/${id}/status`); } catch { summaryWatch = null; return; }
    if (st.summarizing) {
      if (!S.sending) setStatus("กำลังสรุปเทิร์นเก่าเข้าชั้น 2…");
      summaryWatch = setTimeout(tick, 1500);
    } else {
      summaryWatch = null;
      if (!S.sending) setStatus(st.last_error || "", !!st.last_error);
      const before = S.chat?.layers?.archived;
      S.chat = await api("GET", `/api/chats/${id}`);
      if (S.chat.layers.archived !== before) render();
      else renderHeader();
    }
  };
  if (S.chat.status.summarizing) tick();
  else if (S.chat.status.last_error && !S.sending) setStatus(S.chat.status.last_error, true);
}

// ---------- rendering ----------

function render() {
  renderChatList();
  const has = !!S.chat;
  $("#chatHead").hidden = !has;
  $("#composer").hidden = !has;
  $("#panel").hidden = !has;
  $("#emptyState").hidden = has;
  if (!has) {
    $("#messages").querySelectorAll(".msg, .divider").forEach((n) => n.remove());
    return;
  }
  renderHeader();
  renderMessages();
  renderPanel();
}

function renderChatList() {
  const ul = $("#chatList");
  ul.innerHTML = "";
  for (const c of S.chats) {
    const li = el(`<li class="${c.id === S.chatId ? "active" : ""}">
      <span class="t">${esc(c.title)}</span>
      <span class="m">${c.turns} เทิร์น · ${fmtUsd(c.cost)}</span></li>`);
    li.onclick = () => openChat(c.id);
    ul.append(li);
  }
}

function renderHeader() {
  const c = S.chat;
  const L = c.layers;
  $("#chatTitle").textContent = c.title;
  const cost = c.usage.reduce((a, u) => a + (u.cost || 0), 0);
  const over = L.state_chars > L.state_cap;
  $("#layerBar").innerHTML = `
    <span>ชั้น 1: <b>${L.live}</b> เทิร์น (สรุปเมื่อครบ ${L.next_summary_at})</span>
    <span>ชั้น 2–3: <b>${L.archived}</b> ID</span>
    <span class="${over ? "over" : ""}">ชั้น 4: <b>${fmtNum(L.state_chars)}</b> / ${fmtNum(L.state_cap)} ตัวอักษร</span>
    <span>ค่าใช้จ่ายรวม: <b>${fmtUsd(cost)}</b></span>`;
}

function renderMessages() {
  const box = $("#messages");
  box.querySelectorAll(".msg, .divider").forEach((n) => n.remove());
  const turns = S.chat.turns;
  let dividerDone = false;
  for (const t of turns) {
    const archived = !!t.summary;
    if (!archived && !dividerDone && turns.some((x) => x.summary)) {
      box.append(el(`<div class="divider">ด้านบนถูกสรุปแล้ว (ชั้น 2 และ 3) · ด้านล่างอยู่ในชั้น 1</div>`));
      dividerDone = true;
    }
    const u = el(`<div class="msg user ${archived ? "archived" : ""}">
      <div class="meta-row"><b>ID ${t.tid}</b>
        <span class="tag ${archived ? "arch" : "live"}">${archived ? "สรุปแล้ว" : "ชั้น 1"}</span>
        <button class="link pin">📌 ปักเป็นเงื่อนไข</button></div>
      <div class="bubble md">${md(t.user)}</div></div>`);
    u.querySelector(".pin").onclick = () => openPin(t);
    box.append(u);

    const m = t.meta || {};
    const scanIds = (m.scans || []).flatMap((s) => s.ids);
    const failed = (m.scans || []).some((s) => s.shards.some((x) => x.status === "failed"));
    const chips = [];
    if ((m.scans || []).length) chips.push(`สแกน ${m.scans.length} ครั้ง: ${scanIds.length ? "ID " + [...new Set(scanIds)].join(", ") : "ไม่พบ"}${failed ? " ⚠ มีชุดล้มเหลว" : ""}`);
    if (t.tools.length) chips.push(`tool ${t.tools.length} ครั้ง`);
    if (m.stopped) chips.push(`⚠ หยุด: ${m.stopped}`);
    chips.push(fmtUsd(m.cost));
    const a = el(`<div class="msg assistant ${archived ? "archived" : ""}">
      <div class="bubble md">${md(t.answer)}</div>
      <div class="footer-chips">${chips.map((c) => `<span>${esc(c)}</span>`).join("·")}
        <button class="link insp">ตรวจสอบ</button></div></div>`);
    a.querySelector(".insp").onclick = () => { S.inspectTid = t.tid; setTab("inspect"); };
    box.append(a);
  }
  box.scrollTop = box.scrollHeight;
}

function setTab(tab) {
  S.tab = tab;
  document.querySelectorAll("#tabs button").forEach((b) => b.classList.toggle("active", b.dataset.tab === tab));
  renderPanel();
}

function renderPanel() {
  const body = $("#panelBody");
  body.innerHTML = "";
  ({ state: panelState, summaries: panelSummaries, inspect: panelInspect, scan: panelScan })[S.tab](body);
}

const KIND = {
  condition: ["เงื่อนไข (condition)", "C", "คำของผู้ใช้ ใช้ตลอด ไม่หมดอายุ"],
  decision: ["ข้อตัดสินใจ (decision)", "D", "ผู้ใช้ตกลงแล้ว ใช้จนมีข้อใหม่มาแทน"],
  note: ["โน้ต (note)", "N", "โมเดลหลักเขียน อ้าง ID ในชั้น 1"],
  summary: ["สรุปที่ดึงมา (summary)", "S", "จากชั้น 2"],
  full: ["ข้อความเต็มที่ดึงมา (full)", "F", "จากชั้น 3 แทนสรุปของ ID นั้น"],
};

function panelState(body) {
  const items = S.chat.state;
  body.append(el(`<p class="muted small">ชั้น 4 ถูกวางท้ายข้อความล่าสุดที่ส่งให้ตัวทำงานหลักทุกครั้ง</p>`));
  for (const [kind, [title, letter, hint]] of Object.entries(KIND)) {
    const list = items.filter((i) => i.kind === kind);
    body.append(el(`<h3>${title} <span class="muted small">${list.length} · ${hint}</span></h3>`));
    for (const it of list) {
      const turn = S.chat.turns.find((t) => t.tid === it.source_tid);
      let text = it.text;
      if (kind === "summary" && turn?.summary) text = `user: ${turn.summary.user}\nanswer: ${turn.summary.answer}`;
      if (kind === "full" && turn) text = `user: ${turn.user.slice(0, 400)}…`;
      const card = el(`<div class="card">
        <div class="row"><b>[${letter}${it.id}]</b>
          <span class="muted">${it.source_tid != null ? "ID " + it.source_tid : "พิมพ์เอง"}${it.expires_tid ? " · หมดหลัง ID " + it.expires_tid : ""} · ${it.origin}</span>
          <span>${["condition", "decision", "note"].includes(kind) ? '<button class="small edit">แก้</button>' : ""}
            <button class="small off">เอาออก</button></span></div>
        <div class="body">${esc(text)}</div></div>`);
      card.querySelector(".off").onclick = async () => {
        if (kind === "condition" && !confirm("เอาเงื่อนไขนี้ออกจากชั้น 4?")) return;
        await act(() => api("PATCH", `/api/chats/${S.chatId}/state/${it.id}`, { active: false }));
      };
      const edit = card.querySelector(".edit");
      if (edit) edit.onclick = async () => {
        const v = prompt("แก้ข้อความ", it.text);
        if (v != null && v.trim() && v !== it.text) await act(() => api("PATCH", `/api/chats/${S.chatId}/state/${it.id}`, { text: v }));
      };
      body.append(card);
    }
  }
  const form = el(`<div class="card">
    <b>เพิ่มเอง</b>
    <div class="form-row" style="margin-top:6px">
      <select class="kind"><option value="condition">condition</option><option value="decision">decision</option></select>
      <input class="src" type="number" min="1" placeholder="ID ต้นทาง (ไม่ใส่ก็ได้)">
    </div>
    <textarea class="txt" rows="3" style="margin-top:6px" placeholder="ข้อความ"></textarea>
    <div class="row" style="margin-top:6px"><span class="error"></span><button class="primary small add">เพิ่ม</button></div>
  </div>`);
  form.querySelector(".add").onclick = async () => {
    try {
      await api("POST", `/api/chats/${S.chatId}/state`, {
        kind: form.querySelector(".kind").value,
        text: form.querySelector(".txt").value,
        source_tid: form.querySelector(".src").value || null,
      });
      await refreshChat();
    } catch (e) { form.querySelector(".error").textContent = e.message; }
  };
  body.append(form);
}

function panelSummaries(body) {
  const arch = S.chat.turns.filter((t) => t.summary);
  const filter = el(`<input placeholder="กรองรายการ (ค้นคำในหน้านี้เท่านั้น ไม่ใช่ตัวสแกน)" value="${esc(S.summaryFilter)}">`);
  filter.oninput = () => { S.summaryFilter = filter.value; draw(); };
  body.append(el(`<p class="muted small">${arch.length} ID ที่สรุปแล้ว แก้สรุปได้ถ้าตัวสรุปพลาด ข้อความเต็มยังอยู่ในชั้น 3 เสมอ</p>`), filter);
  const list = el(`<div style="display:flex;flex-direction:column;gap:10px"></div>`);
  body.append(list);
  function draw() {
    list.innerHTML = "";
    const q = S.summaryFilter.toLowerCase();
    for (const t of arch) {
      const s = t.summary;
      const all = `${s.user} ${s.answer} ${s.omitted}`;
      if (q && !all.toLowerCase().includes(q)) continue;
      const card = el(`<div class="card">
        <div class="row"><b>ID ${t.tid}</b><span class="muted">${fmtNum(all.length)} ตัวอักษร</span><button class="small edit">แก้</button></div>
        <div class="body"><b>user:</b> ${esc(s.user)}\n<b>answer:</b> ${esc(s.answer)}${s.omitted ? `\n<b>omitted:</b> ${esc(s.omitted)}` : ""}</div></div>`);
      card.querySelector(".edit").onclick = () => editSummary(card, t);
      list.append(card);
    }
  }
  draw();
}

function editSummary(card, t) {
  const s = t.summary;
  const f = el(`<div class="card"><b>แก้สรุป ID ${t.tid}</b>
    <label class="small">user<textarea class="u" rows="3"></textarea></label>
    <label class="small">answer<textarea class="a" rows="6"></textarea></label>
    <label class="small">omitted<textarea class="o" rows="2"></textarea></label>
    <div class="row"><span class="error"></span><span><button class="small cancel">ยกเลิก</button> <button class="small primary save">บันทึก</button></span></div></div>`);
  f.querySelector(".u").value = s.user;
  f.querySelector(".a").value = s.answer;
  f.querySelector(".o").value = s.omitted;
  f.querySelector(".cancel").onclick = () => renderPanel();
  f.querySelector(".save").onclick = async () => {
    try {
      await api("PUT", `/api/chats/${S.chatId}/summaries/${t.tid}`, {
        user: f.querySelector(".u").value, answer: f.querySelector(".a").value, omitted: f.querySelector(".o").value,
      });
      await refreshChat();
    } catch (e) { f.querySelector(".error").textContent = e.message; }
  };
  card.replaceWith(f);
}

function shardCards(scan) {
  return scan.shards.map((s) => `<div class="card ${s.status}">
    <div class="row"><b>ชุด ID ${s.range[0]}–${s.range[1]}</b><span>${s.status}${s.attempts > 1 ? " (ลอง " + s.attempts + " ครั้ง)" : ""}</span></div>
    <div class="body">${s.ids.length ? "เลือก: ID " + s.ids.join(", ") + "\n" : ""}${esc(s.reason || "")}${s.error ? "\nerror: " + esc(s.error) : ""}</div></div>`).join("");
}

function panelInspect(body) {
  const turns = S.chat.turns;
  if (!turns.length) return body.append(el(`<p class="muted">ยังไม่มีข้อความ</p>`));
  const t = turns.find((x) => x.tid === S.inspectTid) || turns[turns.length - 1];
  const m = t.meta || {};
  const sel = el(`<select>${turns.map((x) => `<option value="${x.tid}" ${x.tid === t.tid ? "selected" : ""}>ID ${x.tid}</option>`).join("")}</select>`);
  sel.onchange = () => { S.inspectTid = Number(sel.value); renderPanel(); };
  body.append(sel);
  body.append(el(`<dl class="kv">
    <dt>prompt ทั้งหมด</dt><dd>${fmtNum(m.prompt_chars)} ตัวอักษร</dd>
    <dt>ชั้น 4 ในรอบนี้</dt><dd>${fmtNum(m.state_chars)} ตัวอักษร</dd>
    <dt>ชั้น 1 ในรอบนี้</dt><dd>${m.live_ids?.length ? "ID " + m.live_ids[0] + "–" + m.live_ids[1] : "ไม่มีเทิร์นก่อนหน้า"}</dd>
    <dt>ค่าใช้จ่าย</dt><dd>${fmtUsd(m.cost)}</dd>
    <dt>สถานะ</dt><dd>${m.stopped ? "หยุดเพราะ " + m.stopped : "ปกติ"}</dd></dl>`));

  body.append(el(`<h3>การสแกน</h3>`));
  if (!(m.scans || []).length) body.append(el(`<p class="muted small">ไม่ได้สแกนในเทิร์นนี้</p>`));
  for (const sc of m.scans || []) {
    const why = { user: "ผู้ใช้บังคับ", reference: "ข้อความอ้างถึงเรื่องเก่า", model: "โมเดลหลักเรียก" }[sc.trigger] || sc.trigger;
    body.append(el(`<div><div class="small"><b>${esc(why)}</b>: “${esc(sc.query.slice(0, 160))}”</div>${shardCards(sc)}</div>`));
  }

  body.append(el(`<h3>tool ที่โมเดลหลักเรียก</h3>`));
  if (!t.tools.length) body.append(el(`<p class="muted small">ไม่มี</p>`));
  for (const tool of t.tools) {
    body.append(el(`<div class="card"><b>${esc(tool.name)}</b> <code>${esc(JSON.stringify(tool.args))}</code>
      <div class="body">${esc(tool.result)}</div></div>`));
  }

  body.append(el(`<h3>การเรียกโมเดล</h3>`));
  for (const c of m.calls || []) {
    body.append(el(`<div class="card"><div class="row"><b>${esc(c.role)}</b><span class="muted">${esc(c.model)}</span></div>
      <div class="small">in ${fmtNum(c.prompt_tokens)} · out ${fmtNum(c.completion_tokens)} · ${fmtUsd(c.cost)}</div></div>`));
  }

  body.append(el(`<h3>รวมทั้งแชต</h3>`));
  for (const u of S.chat.usage) {
    body.append(el(`<div class="card"><div class="row"><b>${esc(u.role)}</b><span>${u.calls} ครั้ง · ${fmtUsd(u.cost)}</span></div>
      <div class="small">in ${fmtNum(u.prompt_tokens)} · out ${fmtNum(u.completion_tokens)} โทเคน</div></div>`));
  }
}

function panelScan(body) {
  body.append(el(`<p class="muted small">ลองให้ตัววิ่งหาข้อมูลค้นจากสรุปทุก ID โดยไม่เพิ่มอะไรเข้าชั้น 4 ใช้ทดสอบว่าเลือก ID ถูกไหม</p>`));
  const q = el(`<textarea rows="3" placeholder="เช่น ตอนนั้นเราตกลงช่วงเวลาแจ้งเคลมไว้เท่าไร"></textarea>`);
  const btn = el(`<button class="primary">สแกน</button>`);
  const out = el(`<div style="display:flex;flex-direction:column;gap:8px"></div>`);
  btn.onclick = async () => {
    btn.disabled = true;
    out.innerHTML = `<p class="muted small">กำลังสแกน…</p>`;
    try {
      S.scanResult = await api("POST", `/api/chats/${S.chatId}/scan`, { query: q.value });
      drawScan();
    } catch (e) {
      out.innerHTML = `<p class="error">${esc(e.message)}</p>`;
    }
    btn.disabled = false;
  };
  function drawScan() {
    const r = S.scanResult;
    if (!r) return;
    out.innerHTML = r.note ? `<p class="muted">${esc(r.note)}</p>` : `<div class="small">ค่าใช้จ่าย ${fmtUsd(r.cost)} · พบ ${r.ids.length} ID</div>${shardCards(r)}`;
    for (const id of r.ids) {
      const t = S.chat.turns.find((x) => x.tid === id);
      if (t?.summary) out.append(el(`<div class="card"><b>ID ${id}</b><div class="body">${esc(t.summary.user)}\n${esc(t.summary.answer)}</div></div>`));
    }
  }
  body.append(q, btn, out);
  if (S.scanResult) { q.value = S.scanResult.query; drawScan(); }
}

// ---------- actions ----------

async function act(fn) {
  try {
    await fn();
    await refreshChat();
  } catch (e) {
    alert(e.message);
  }
}

async function send() {
  const input = $("#input");
  const text = input.value.trim();
  if (!text || S.sending || !S.chat) return;
  S.sending = true;
  $("#sendBtn").disabled = true;
  const pending = el(`<div class="msg user pending"><div class="meta-row"><b>กำลังส่ง…</b></div><div class="bubble md">${md(text)}</div></div>`);
  $("#messages").append(pending);
  $("#messages").scrollTop = $("#messages").scrollHeight;
  setStatus("กำลังเริ่ม…");
  const id = S.chatId;
  const poll = setInterval(async () => {
    try {
      const st = await api("GET", `/api/chats/${id}/status`);
      if (st.busy) setStatus(st.busy + "…");
    } catch {}
  }, 800);
  try {
    await api("POST", `/api/chats/${id}/send`, { text, force_scan: $("#forceScan").checked });
    input.value = "";
    $("#forceScan").checked = false;
    setStatus("");
  } catch (e) {
    pending.remove();
    setStatus(e.message, true);
  } finally {
    clearInterval(poll);
    S.sending = false;
    $("#sendBtn").disabled = false;
  }
  await loadChats();
  if (S.chatId === id) await refreshChat();
}

let pinTurn = null;
function openPin(t) {
  pinTurn = t;
  $("#pinText").value = t.user;
  $("#pinError").textContent = "";
  $("#pinDialog").showModal();
}

$("#pinSave").onclick = async (ev) => {
  ev.preventDefault();
  try {
    await api("POST", `/api/chats/${S.chatId}/state`, { kind: "condition", text: $("#pinText").value.trim(), source_tid: pinTurn.tid });
    $("#pinDialog").close();
    S.tab = "state";
    setTab("state");
    await refreshChat();
  } catch (e) { $("#pinError").textContent = e.message; }
};

// ---------- settings ----------

const SETTING_FIELDS = ["model_summarizer", "model_scanner", "model_main", "live_turns", "round_size", "shard_size",
  "summary_ttl", "full_ttl", "note_ttl", "state_cap_chars", "tool_limit", "full_char_limit", "max_tool_rounds", "max_usd_per_message"];

async function openSettings() {
  await loadSettings();
  const f = $("#settingsForm");
  for (const k of SETTING_FIELDS) f.elements[k].value = S.settings[k];
  f.elements.api_key.value = "";
  $("#keyState").textContent = S.settings.api_key_set ? `ตั้งค่าแล้ว (${S.settings.api_key_hint}) เว้นว่างไว้ถ้าไม่เปลี่ยน` : "ยังไม่ได้ใส่ key";
  $("#settingsError").textContent = "";
  $("#settingsDialog").showModal();
  updateModelInfo();
  if (!S.models) {
    $("#modelsState").textContent = "กำลังโหลดรายชื่อ…";
    try {
      S.models = await api("GET", "/api/models");
      $("#modelOptions").innerHTML = S.models.map((m) => `<option value="${esc(m.id)}">${esc(m.name)}</option>`).join("");
      $("#modelsState").textContent = `${S.models.length} โมเดลจาก OpenRouter`;
      updateModelInfo();
    } catch (e) {
      $("#modelsState").textContent = "โหลดรายชื่อไม่ได้ พิมพ์ ID เองได้";
    }
  }
}

function updateModelInfo() {
  for (const box of document.querySelectorAll(".model-info")) {
    const name = box.dataset.for;
    const id = $("#settingsForm").elements[name].value.trim();
    const m = S.models?.find((x) => x.id === id);
    box.classList.remove("warn");
    if (!id) { box.textContent = ""; continue; }
    if (!S.models) { box.textContent = ""; continue; }
    if (!m) { box.textContent = "ไม่พบ ID นี้ในรายชื่อ"; box.classList.add("warn"); continue; }
    const per1m = (p) => p == null ? "?" : "$" + (Number(p) * 1e6).toFixed(2);
    let txt = `context ${fmtNum(m.context)} · in ${per1m(m.prompt)} / out ${per1m(m.completion)} ต่อล้านโทเคน`;
    if (name === "model_main" && !m.tools) { txt += " · ⚠ ไม่รองรับ tool calling"; box.classList.add("warn"); }
    box.textContent = txt;
  }
}

$("#settingsForm").addEventListener("input", updateModelInfo);

$("#saveSettings").onclick = async (ev) => {
  ev.preventDefault();
  const f = $("#settingsForm");
  const body = {};
  for (const k of SETTING_FIELDS) body[k] = f.elements[k].value;
  if (f.elements.api_key.value.trim()) body.api_key = f.elements.api_key.value.trim();
  try {
    await api("PUT", "/api/settings", body);
    $("#settingsDialog").close();
    await loadSettings();
  } catch (e) { $("#settingsError").textContent = e.message; }
};

// ---------- wiring ----------

$("#openSettings").onclick = openSettings;
$("#newChat").onclick = async () => {
  const { id } = await api("POST", "/api/chats", {});
  await loadChats();
  await openChat(id);
  $("#input").focus();
};
$("#sendBtn").onclick = send;
$("#input").addEventListener("keydown", (e) => {
  if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) { e.preventDefault(); send(); }
});
$("#tabs").addEventListener("click", (e) => { if (e.target.dataset.tab) setTab(e.target.dataset.tab); });
$("#chatTitle").onclick = async () => {
  const v = prompt("ชื่อแชต", S.chat.title);
  if (v && v.trim()) { await act(() => api("PATCH", `/api/chats/${S.chatId}`, { title: v })); await loadChats(); }
};
$("#deleteChat").onclick = async () => {
  if (!confirm(`ลบแชต "${S.chat.title}" ทั้งหมด รวมสรุปและข้อความเต็ม? ย้อนกลับไม่ได้`)) return;
  await api("DELETE", `/api/chats/${S.chatId}`);
  S.chatId = null;
  S.chat = null;
  await loadChats();
  render();
};
$("#exportChat").onclick = () => { window.location.href = `/api/chats/${S.chatId}/export`; };
$("#forceSummarize").onclick = async () => {
  const btn = $("#forceSummarize");
  btn.disabled = true;
  setStatus("กำลังสรุปรอบถัดไป…");
  try {
    const r = await api("POST", `/api/chats/${S.chatId}/summarize`, { force: true });
    setStatus(r.summarized ? `สรุปแล้ว ${r.summarized} ID` : "ไม่มีเทิร์นให้สรุป หรือกำลังสรุปอยู่");
  } catch (e) { setStatus(e.message, true); }
  btn.disabled = false;
  await refreshChat();
};

(async function init() {
  await loadSettings();
  await loadChats();
  let last = null;
  try { last = Number(localStorage.getItem("lastChat")); } catch {}
  if (last && S.chats.some((c) => c.id === last)) await openChat(last);
  else render();
  if (!S.settings.api_key_set) openSettings();
})();
