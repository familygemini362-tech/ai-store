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
      .replace(/(^|[^*\w])\*([^*\s][^*\n]*?)\*(?!\w)/g, "$1<em>$2</em>")
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

const TOOL_LABEL = {
  search_memory: "ค้นความจำเก่า",
  read_full: "อ่านข้อความเต็ม",
  pin_condition: "ปักเงื่อนไข (4a)",
  record_decision: "บันทึกข้อตัดสินใจ (4a)",
  add_note: "จดโน้ต (4b)",
  retire_state: "จัดชั้น 4",
};
const toolLabel = (name) => TOOL_LABEL[name] || name;

function nearBottom(box) {
  return box.scrollHeight - box.scrollTop - box.clientHeight < 120;
}

// ---------- panel ----------

const narrow = () => window.matchMedia("(max-width: 1100px)").matches;

function setPanel(open, remember = true) {
  $("#app").classList.toggle("panel-closed", !open);
  $("#togglePanel").setAttribute("aria-expanded", String(open));
  if (remember && !narrow()) {
    try { localStorage.setItem("panelOpen", open ? "1" : "0"); } catch {}
  }
}

function initialPanel() {
  if (narrow()) return false;
  try { return localStorage.getItem("panelOpen") !== "0"; } catch { return true; }
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
  $("#app").classList.toggle("no-chat", !has);
  if (!has) {
    $("#messages").querySelectorAll(".msg, .divider, .chat-empty").forEach((n) => n.remove());
    return;
  }
  renderHeader();
  renderMessages();
  renderPanel();
}

function renderChatList() {
  const ul = $("#chatList");
  ul.innerHTML = "";
  if (!S.chats.length) ul.append(el(`<li class="none">ยังไม่มีแชต</li>`));
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
  const comp = c.compression;
  let compressSpan;
  if (comp) {
    const isCustom = comp.custom && (comp.custom.compress_at != null || comp.custom.round_size != null);
    compressSpan = `<span class="chip" title="ชั้น 1 = ข้อความล่าสุดที่โมเดลเห็นเต็ม ๆ">ชั้น 1 <b>${L.live}</b>/${comp.compress_at} เทิร์น
      · บีบอัดครั้งละ ${comp.round_size}
      <span class="badge ${isCustom ? "custom" : ""}">${isCustom ? "เฉพาะแชตนี้" : "ค่าเริ่มต้น"}</span>
      <button class="link" id="adjustCompress">ปรับ</button></span>`;
  } else {
    compressSpan = `<span class="chip">ชั้น 1 <b>${L.live}</b> เทิร์น (สรุปเมื่อครบ ${L.next_summary_at})</span>`;
  }
  const pinned = c.state.filter((i) => i.kind === "condition" || i.kind === "decision").length;
  const working = c.state.length - pinned;
  $("#layerBar").innerHTML = `
    ${compressSpan}
    <span class="chip" title="สรุปและข้อความเต็มของเทิร์นเก่า">ชั้น 2–3 <b>${L.archived}</b> ID</span>
    <span class="chip ${over ? "over" : ""}" title="4a ปักไว้ทั้งแชต · 4b ความจำทำงาน">ชั้น 4 <b>4a ${pinned}</b> · <b>4b ${working}</b> · ${fmtNum(L.state_chars)}/${fmtNum(L.state_cap)} ตัวอักษร</span>
    <span class="chip">ค่าใช้จ่าย <b>${fmtUsd(cost)}</b></span>`;
  const adjustBtn = $("#adjustCompress");
  if (adjustBtn) adjustBtn.onclick = openCompressDialog;
}

function userMsg(t, archived, pending = false) {
  const u = el(`<div class="msg user ${archived ? "archived" : ""} ${pending ? "pending" : ""}">
    <div class="meta-row">
      ${pending ? "" : `<button class="link pin" title="ปักคำของคุณเป็นเงื่อนไขในชั้น 4a">📌 ปัก</button>`}
      <span class="tag ${archived ? "arch" : "live"}">${archived ? "สรุปแล้ว" : "ชั้น 1"}</span>
      <b>${pending ? "กำลังส่ง" : "ID " + t.tid}</b></div>
    <div class="bubble md">${md(t.user)}</div></div>`);
  const pin = u.querySelector(".pin");
  if (pin) pin.onclick = () => openPin(t);
  return u;
}

function turnChips(t) {
  const m = t.meta || {};
  const scanIds = (m.scans || []).flatMap((s) => s.ids);
  const failed = (m.scans || []).some((s) => s.shards.some((x) => x.status === "failed"));
  const chips = [];
  if ((m.scans || []).length) chips.push(`สแกน ${m.scans.length} ครั้ง: ${scanIds.length ? "ID " + [...new Set(scanIds)].join(", ") : "ไม่พบ"}${failed ? " ⚠ มีชุดล้มเหลว" : ""}`);
  const memTools = t.tools.filter((x) => ["pin_condition", "record_decision", "add_note", "retire_state"].includes(x.name)
    && !String(x.result).startsWith("ERROR"));
  if (memTools.length) chips.push(`อัปเดตชั้น 4 ${memTools.length} ครั้ง`);
  else if (t.tools.length) chips.push(`tool ${t.tools.length} ครั้ง`);
  if (m.stopped) chips.push(`⚠ หยุด: ${m.stopped}`);
  chips.push(fmtUsd(m.cost));
  return chips;
}

function renderMessages() {
  const box = $("#messages");
  // A reply being streamed stays put when something else re-renders the chat (e.g. a summary finishing).
  const inFlight = S.sendingChat === S.chatId ? [...box.querySelectorAll(".msg.pending, .msg.streaming")] : [];
  box.querySelectorAll(".msg, .divider, .chat-empty").forEach((n) => n.remove());
  const turns = S.chat.turns;
  if (!turns.length && !inFlight.length) {
    box.append(el(`<div class="chat-empty"><b>เริ่มคุยได้เลย</b>
      บอกเป้าหมายหรือเงื่อนไขของงานไว้ตั้งแต่ต้น โมเดลจะปักไว้ในชั้น 4a เอง</div>`));
  }
  let dividerDone = false;
  for (const t of turns) {
    const archived = !!t.summary;
    if (!archived && !dividerDone && turns.some((x) => x.summary)) {
      box.append(el(`<div class="divider">ด้านบนถูกบีบอัดแล้ว (ชั้น 2–3) · ด้านล่างอยู่ในชั้น 1</div>`));
      dividerDone = true;
    }
    box.append(userMsg(t, archived));
    const a = el(`<div class="msg assistant ${archived ? "archived" : ""}">
      <div class="bubble md">${md(t.answer)}</div>
      <div class="footer-chips">${turnChips(t).map((c) => `<span>${esc(c)}</span>`).join("")}
        <button class="link insp">ตรวจสอบ</button></div></div>`);
    a.querySelector(".insp").onclick = () => { S.inspectTid = t.tid; setPanel(true); setTab("inspect"); };
    box.append(a);
  }
  box.append(...inFlight);
  box.style.scrollBehavior = "auto";
  box.scrollTop = box.scrollHeight;
  box.style.scrollBehavior = "";
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
  condition: ["เงื่อนไข", "C", "คำของคุณตรงตัว ไม่หมดอายุ"],
  decision: ["ข้อตัดสินใจ", "D", "สิ่งที่คุณตกลงแล้ว ใช้จนมีข้อใหม่มาแทน"],
  note: ["โน้ต", "N", "โมเดลจดระหว่างทำงาน"],
  summary: ["สรุปที่ดึงมา", "S", "จากชั้น 2"],
  full: ["ข้อความเต็มที่ดึงมา", "F", "จากชั้น 3 แทนสรุปของ ID นั้น"],
};
const ORIGIN = { model: "โมเดล", user: "คุณ", system: "ระบบ" };

function stateCard(it) {
  const kind = it.kind;
  const [, letter] = KIND[kind];
  const turn = S.chat.turns.find((t) => t.tid === it.source_tid);
  let text = it.text;
  if (kind === "summary" && turn?.summary) text = `user: ${turn.summary.user}\nanswer: ${turn.summary.answer}`;
  if (kind === "full" && turn) text = `user: ${turn.user.slice(0, 400)}${turn.user.length > 400 ? "…" : ""}`;
  const pinned = kind === "condition" || kind === "decision";
  const card = el(`<div class="card ${pinned ? "pinned" : ""}">
    <div class="row"><span><b>[${letter}${it.id}]</b>
        <span class="origin ${esc(it.origin)}">${esc(ORIGIN[it.origin] || it.origin)}</span>
        <span class="muted small">${it.source_tid != null ? "ID " + it.source_tid : "พิมพ์เอง"}${it.expires_tid ? " · หมดหลัง ID " + it.expires_tid : ""}</span></span>
      <span class="actions">${["condition", "decision", "note"].includes(kind) ? '<button class="small edit">แก้</button>' : ""}
        <button class="small off">เอาออก</button></span></div>
    <div class="body">${esc(text)}</div></div>`);
  card.querySelector(".off").onclick = async () => {
    if (pinned && !confirm("เอารายการนี้ออกจากชั้น 4a?")) return;
    await act(() => api("PATCH", `/api/chats/${S.chatId}/state/${it.id}`, { active: false }));
  };
  const edit = card.querySelector(".edit");
  if (edit) edit.onclick = async () => {
    const v = prompt("แก้ข้อความ", it.text);
    if (v != null && v.trim() && v !== it.text) await act(() => api("PATCH", `/api/chats/${S.chatId}/state/${it.id}`, { text: v }));
  };
  return card;
}

function panelState(body) {
  const items = S.chat.state;
  const groups = [
    ["a", "4a · ปักไว้ทั้งแชต", "โมเดลปักเงื่อนไขจากคำของคุณและบันทึกสิ่งที่ตกลงกันเอง เอาออกได้เมื่อคุณขอ", ["condition", "decision"]],
    ["b", "4b · ความจำระหว่างทำงาน", "โมเดลจดโน้ตและดึงความจำเก่ามาใช้เอง หมดอายุเอง และถูกตัดอัตโนมัติเมื่อเกินเพดาน", ["note", "summary", "full"]],
  ];
  for (const [cls, title, hint, kinds] of groups) {
    const count = items.filter((i) => kinds.includes(i.kind)).length;
    body.append(el(`<div class="layer-head ${cls}"><b>${title} (${count})</b><span class="muted">${hint}</span></div>`));
    for (const kind of kinds) {
      const [name, , kindHint] = KIND[kind];
      const list = items.filter((i) => i.kind === kind);
      body.append(el(`<h3>${name} <span class="muted small">${list.length} · ${kindHint}</span></h3>`));
      if (!list.length) body.append(el(`<div class="none-yet">ยังไม่มี</div>`));
      for (const it of list) body.append(stateCard(it));
    }
  }
  const form = el(`<details class="card manual">
    <summary>เพิ่มเองด้วยมือ (ปกติไม่ต้อง โมเดลจัดการให้)</summary>
    <div class="form-row" style="margin-top:6px">
      <select class="kind"><option value="condition">condition</option><option value="decision">decision</option></select>
      <input class="src" type="number" min="1" placeholder="ID ต้นทาง (ไม่ใส่ก็ได้)">
    </div>
    <textarea class="txt" rows="3" style="margin-top:6px" placeholder="ข้อความ"></textarea>
    <div class="row" style="margin-top:6px"><span class="error"></span><button class="primary small add">เพิ่ม</button></div>
  </details>`);
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
    body.append(el(`<div class="card"><b>${esc(toolLabel(tool.name))}</b> <code>${esc(tool.name)}(${esc(JSON.stringify(tool.args))})</code>
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

// Reads the newline-delimited JSON events of /send_stream and hands each one to onEvent.
async function streamEvents(path, body, onEvent) {
  const res = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!res.ok || !res.body) {
    const data = await res.json().catch(() => ({}));
    throw new Error(data.error || `HTTP ${res.status}`);
  }
  const reader = res.body.getReader();
  const dec = new TextDecoder();
  let buf = "";
  let finished = false;
  for (;;) {
    const { value, done } = await reader.read();
    if (value) buf += dec.decode(value, { stream: true });
    let nl;
    while ((nl = buf.indexOf("\n")) >= 0) {
      const line = buf.slice(0, nl).trim();
      buf = buf.slice(nl + 1);
      if (!line) continue;
      const ev = JSON.parse(line);
      if (ev.type === "done" || ev.type === "error") finished = true;
      onEvent(ev);
    }
    if (done) break;
  }
  if (!finished) throw new Error("การเชื่อมต่อกับเซิร์ฟเวอร์ขาดก่อนตอบเสร็จ (เซิร์ฟเวอร์ยังเปิดอยู่ไหม?)");
}

function autoGrow() {
  const t = $("#input");
  t.style.height = "auto";
  t.style.height = Math.min(t.scrollHeight, 220) + "px";
}

async function send() {
  const input = $("#input");
  const text = input.value.trim();
  if (!text || S.sending || !S.chat) return;
  const forceScan = $("#forceScan").checked;
  S.sending = true;
  S.sendingChat = S.chatId;
  $("#sendBtn").disabled = true;
  input.value = "";
  autoGrow();
  $("#messages").querySelector(".chat-empty")?.remove();

  const box = $("#messages");
  const userEl = userMsg({ user: text }, false, true);
  const live = el(`<div class="msg assistant streaming">
    <div class="steps"></div>
    <div class="bubble md"><span class="typing"><i></i><i></i><i></i></span></div></div>`);
  box.append(userEl, live);
  box.scrollTop = box.scrollHeight;
  setStatus("กำลังเริ่ม…");

  const bubble = live.querySelector(".bubble");
  const steps = live.querySelector(".steps");
  let answer = "";
  let frame = 0;
  const paint = () => {
    frame = 0;
    const stick = nearBottom(box);
    bubble.innerHTML = answer ? md(answer) : `<span class="typing"><i></i><i></i><i></i></span>`;
    if (stick) box.scrollTop = box.scrollHeight;
  };
  // A short timer rather than requestAnimationFrame, which stalls while the tab is in the background.
  const schedule = () => { if (!frame) frame = setTimeout(paint, 40); };

  const id = S.chatId;
  let failure = null;
  try {
    await streamEvents(`/api/chats/${id}/send_stream`, { text, force_scan: forceScan }, (ev) => {
      if (ev.type === "status") setStatus(ev.text + "…");
      else if (ev.type === "round") { answer = ""; schedule(); }
      else if (ev.type === "delta") { answer += ev.text; schedule(); }
      else if (ev.type === "tool") {
        const bad = String(ev.result).startsWith("ERROR");
        steps.append(el(`<span class="step ${bad ? "err" : "done"}" title="${esc(ev.result)}">${esc(toolLabel(ev.name))}</span>`));
      } else if (ev.type === "done") { answer = ev.turn.answer; paint(); }
      else if (ev.type === "error") failure = ev.error;
    });
  } catch (e) {
    failure = e.message === "Failed to fetch" ? "ติดต่อเซิร์ฟเวอร์ไม่ได้ ตรวจว่ายังรัน server.py อยู่" : e.message;
  }
  if (frame) clearTimeout(frame);
  S.sending = false;
  S.sendingChat = null;
  $("#sendBtn").disabled = false;
  if (failure) {
    userEl.remove();
    live.remove();
    if (!input.value) { input.value = text; autoGrow(); }
    setStatus(failure, true);
  } else {
    $("#forceScan").checked = false;
    setStatus("");
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

// ---------- compression dialog ----------

function openCompressDialog() {
  const c = S.chat;
  const comp = c.compression;
  if (!comp) return;
  const atInput = $("#compressAtInput");
  const roundInput = $("#compressRoundInput");
  atInput.value = comp.custom.compress_at ?? "";
  roundInput.value = comp.custom.round_size ?? "";
  atInput.placeholder = `ค่าเริ่มต้น ${comp.default.compress_at}`;
  roundInput.placeholder = `ค่าเริ่มต้น ${comp.default.round_size}`;
  $("#compressError").textContent = "";
  updateCompressHint();
  $("#compressDialog").showModal();
}

function updateCompressHint() {
  const comp = S.chat?.compression;
  if (!comp) return;
  const atInput = $("#compressAtInput");
  const roundInput = $("#compressRoundInput");
  const at = atInput.value.trim() ? Number(atInput.value) : comp.default.compress_at;
  const round = roundInput.value.trim() ? Number(roundInput.value) : comp.default.round_size;
  const err = $("#compressError");
  const bad = compressProblem(at, round);
  if (bad) {
    $("#compressHint").textContent = "";
    err.textContent = bad;
  } else {
    err.textContent = "";
    $("#compressHint").textContent = `หลังบีบอัด ชั้น 1 จะเหลือ ${at - round} เทิร์น`;
  }
}

function compressProblem(at, round) {
  if (!Number.isInteger(at) || !Number.isInteger(round)) return "ใส่เป็นจำนวนเต็มเท่านั้น";
  if (at < 2 || round < 1) return "เริ่มบีบอัดต้องอย่างน้อย 2 และบีบอัดครั้งละอย่างน้อย 1";
  if (round >= at) return "บีบอัดครั้งละต้องน้อยกว่าเริ่มบีบอัดเมื่อครบ";
  return "";
}

$("#compressAtInput").addEventListener("input", updateCompressHint);
$("#compressRoundInput").addEventListener("input", updateCompressHint);

async function saveCompression(body) {
  const buttons = $("#compressDialog").querySelectorAll("button");
  buttons.forEach((b) => { b.disabled = true; });
  try {
    await api("PUT", `/api/chats/${S.chatId}/compression`, body);
    $("#compressDialog").close();
    S.chat = await api("GET", `/api/chats/${S.chatId}`);
    render();
    watchSummarizer();
  } catch (e) { $("#compressError").textContent = e.message; }
  finally { buttons.forEach((b) => { b.disabled = false; }); }
}

$("#compressCancel").onclick = () => $("#compressDialog").close();

$("#compressUseDefault").onclick = () => saveCompression({ compress_at: null, round_size: null });

// Enter in a number field submits the form; route it through the same save path.
$("#compressForm").addEventListener("submit", (ev) => { ev.preventDefault(); $("#compressSave").click(); });

$("#compressSave").onclick = (ev) => {
  ev.preventDefault();
  if ($("#compressSave").disabled) return;
  const atInput = $("#compressAtInput");
  const roundInput = $("#compressRoundInput");
  const comp = S.chat.compression;
  const at = atInput.value.trim() ? Number(atInput.value) : comp.default.compress_at;
  const round = roundInput.value.trim() ? Number(roundInput.value) : comp.default.round_size;
  const bad = compressProblem(at, round);
  if (bad) { $("#compressError").textContent = bad; return; }
  saveCompression({
    compress_at: atInput.value.trim() ? Number(atInput.value) : null,
    round_size: roundInput.value.trim() ? Number(roundInput.value) : null,
  });
};

// ---------- settings ----------

const SETTING_FIELDS = ["model_summarizer", "model_scanner", "model_main", "compress_at", "round_size", "shard_size",
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
  if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); send(); }
});
$("#input").addEventListener("input", autoGrow);
$("#tabs").addEventListener("click", (e) => { if (e.target.dataset.tab) setTab(e.target.dataset.tab); });
$("#togglePanel").onclick = () => setPanel($("#app").classList.contains("panel-closed"));
$("#closePanel").onclick = () => setPanel(false);
$("#scrim").onclick = () => setPanel(false, false);
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && narrow() && !$("#app").classList.contains("panel-closed") && !document.querySelector("dialog[open]")) {
    setPanel(false, false);
  }
});
// Close the ⋯ menu after picking an item or clicking elsewhere.
document.addEventListener("click", (e) => {
  const menu = $("#chatMenu");
  if (menu.open && (!menu.contains(e.target) || e.target.closest(".menu-list button"))) menu.open = false;
});
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
  setPanel(initialPanel(), false);
  await loadSettings();
  await loadChats();
  let last = null;
  try { last = Number(localStorage.getItem("lastChat")); } catch {}
  if (last && S.chats.some((c) => c.id === last)) await openChat(last);
  else render();
  if (!S.settings.api_key_set) openSettings();
})();
