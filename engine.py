"""Core of the 4-layer memory: storage, OpenRouter calls, summarizer, scanner and main loop.

Layer 1  live turns      turns WHERE summary IS NULL (full text, sent as chat messages)
Layer 2  summaries       turns.summary (one JSON object per turn ID, never merged)
Layer 3  full text       turns.user / turns.answer (always kept)
Layer 4  working state   state table, rendered at the end of the latest user message
"""

import json
import re
import sqlite3
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager

import prompts

EFFORTS = ("none", "minimal", "low", "medium", "high", "xhigh")

DEFAULTS = {
    "api_key": "",
    "model_summarizer": "",
    "model_scanner": "",
    "model_main": "",
    "main_effort": "",
    "compress_at": 25,
    "round_size": 5,
    "shard_size": 250,
    "summary_ttl": 6,
    "full_ttl": 3,
    "note_ttl": 10,
    "state_cap_chars": 30000,
    "tool_limit": 2000,
    "full_char_limit": 60000,
    "max_tool_rounds": 8,
    "max_usd_per_message": 0.5,
}

# Words that suggest the user is pointing at something said earlier (rule 13.1 of the design).
MEMORY_HINT = re.compile(
    r"เคย|ก่อนหน้า|ที่แล้ว|ตอนนั้น|เมื่อกี้|ที่คุยกัน|ที่ตกลง|ที่ตัดสิน|จำได้|ย้อนกลับไป|อันเดิม|ค่าเดิม|"
    r"\b(earlier|previous(ly)?|last time|we (discussed|agreed|decided)|remember|recall|as before)\b",
    re.IGNORECASE,
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS chats(
    id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL, created_at REAL NOT NULL,
    compress_at INTEGER, round_size INTEGER, main_effort TEXT);
CREATE TABLE IF NOT EXISTS turns(
    chat_id INTEGER NOT NULL, tid INTEGER NOT NULL,
    user TEXT NOT NULL, answer TEXT NOT NULL,
    tools TEXT NOT NULL DEFAULT '[]', meta TEXT NOT NULL DEFAULT '{}',
    summary TEXT, created_at REAL NOT NULL,
    PRIMARY KEY(chat_id, tid));
CREATE TABLE IF NOT EXISTS state(
    id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id INTEGER NOT NULL,
    kind TEXT NOT NULL, text TEXT, source_tid INTEGER,
    created_tid INTEGER NOT NULL, expires_tid INTEGER,
    active INTEGER NOT NULL DEFAULT 1, origin TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS usage(
    id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id INTEGER, tid INTEGER, role TEXT, model TEXT,
    prompt_tokens INTEGER, completion_tokens INTEGER, cost REAL, created_at REAL);
CREATE TABLE IF NOT EXISTS artifacts(
    id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id INTEGER NOT NULL, key TEXT NOT NULL,
    kind TEXT NOT NULL, language TEXT, created_tid INTEGER NOT NULL,
    UNIQUE(chat_id, key));
CREATE TABLE IF NOT EXISTS artifact_versions(
    artifact_id INTEGER NOT NULL, version INTEGER NOT NULL, title TEXT NOT NULL,
    content TEXT NOT NULL, tid INTEGER NOT NULL, created_at REAL NOT NULL,
    PRIMARY KEY(artifact_id, version));
CREATE INDEX IF NOT EXISTS state_chat ON state(chat_id, active);
CREATE INDEX IF NOT EXISTS usage_chat ON usage(chat_id);
"""

# Artifacts: standalone pieces the main model writes (pages, documents, code, images). Every edit is a
# new version. Their content stays out of the chat text, so it never fills layer 1 or the summarizer.
ARTIFACT_KINDS = ("html", "markdown", "code", "svg")
ARTIFACT_KEY = re.compile(r"[a-z0-9][a-z0-9-]{0,59}")
ARTIFACT_MAX_CHARS = 200_000
ARTIFACT_TOOLS = ("create_artifact", "update_artifact", "read_artifact")

ROLE_NAME = {"main": "ตัวทำงานหลัก", "scan": "ตัววิ่งหาข้อมูล", "summarize": "ตัวสรุป"}

KIND_LABEL = {"condition": "C", "decision": "D", "note": "N", "summary": "S", "full": "F"}

# Layer 4 has two parts. 4a holds what the user wants kept for the whole chat; it never expires.
# 4b is working memory with an expiry the model curates; the system also trims it when over the cap.
PINNED_KINDS = ("condition", "decision")
WORKING_KINDS = ("note", "summary", "full")

TOOL_STATUS = {
    "search_memory": "กำลังค้นความจำเก่า",
    "read_full": "กำลังอ่านข้อความเต็ม",
    "pin_condition": "กำลังปักเงื่อนไข",
    "record_decision": "กำลังบันทึกข้อตัดสินใจ",
    "add_note": "กำลังจดโน้ต",
    "retire_state": "กำลังจัดชั้น 4",
    "create_artifact": "กำลังสร้างชิ้นงาน",
    "update_artifact": "กำลังแก้ชิ้นงาน",
    "read_artifact": "กำลังอ่านชิ้นงาน",
}


def trace_args(name, args):
    """Tool args as kept in the turn record. Artifact bodies are replaced by their size, so the
    summarizer, the inspect tab and the stream never carry a whole page or file again."""
    if name not in ("create_artifact", "update_artifact"):
        return args
    out = dict(args)
    if isinstance(out.get("content"), str):
        out["content"] = f"<{len(out['content']):,} chars>"
    if isinstance(out.get("edits"), list):
        out["edits"] = [{k: clip(str(e.get(k, "")), 200) for k in ("old", "new")} if isinstance(e, dict) else e
                        for e in out["edits"]]
    return out


def squash(text):
    """Collapse whitespace so a quote still matches when line breaks or spaces differ."""
    return " ".join((text or "").split())


class UserError(Exception):
    """Bad input or missing setting; shown to the user as-is."""


class BusyError(Exception):
    pass


class LLMError(Exception):
    pass


def clip(text, limit):
    text = text or ""
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n…[ตัดเหลือ {limit:,} จาก {len(text):,} ตัวอักษร]"


def message_text(msg):
    content = msg.get("content")
    if isinstance(content, list):
        return "".join(p.get("text", "") for p in content if isinstance(p, dict))
    return content or ""


def parse_json(text):
    text = (text or "").strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end < start:
        raise ValueError("ไม่พบ JSON ในคำตอบ")
    return json.loads(text[start:end + 1])


def parse_count(v, name, min_val):
    """Parse a per-chat compression override: None/"" clears it, otherwise a plain int >= min_val."""
    if v is None or v == "":
        return None
    if isinstance(v, bool):
        raise UserError(f"ค่า {name} ไม่ถูกต้อง")
    if isinstance(v, int):
        n = v
    elif isinstance(v, str) and re.fullmatch(r"-?\d+", v.strip()):
        n = int(v.strip())
    else:
        raise UserError(f"ค่า {name} ไม่ถูกต้อง")
    if n < min_val:
        raise UserError(f"ค่า {name} ไม่ถูกต้อง")
    return n


def format_summary(tid, summary):
    s = json.loads(summary) if isinstance(summary, str) else summary
    parts = [f"ID {tid}", f"user: {s.get('user', '')}", f"answer: {s.get('answer', '')}"]
    if s.get("omitted"):
        parts.append(f"omitted: {s['omitted']}")
    return " | ".join(parts)


class OpenRouter:
    URL = "https://openrouter.ai/api/v1/chat/completions"

    def __init__(self, api_key, timeout=300):
        self.api_key = api_key
        self.timeout = timeout

    def _request(self, model, messages, tools, tool_choice, stream=False, reasoning=None):
        body = {"model": model, "messages": messages, "usage": {"include": True}}
        if tools:
            body["tools"] = tools
        if tool_choice:
            body["tool_choice"] = tool_choice
        if reasoning is not None:
            body["reasoning"] = reasoning
        if stream:
            body["stream"] = True
        req = urllib.request.Request(
            self.URL,
            data=json.dumps(body).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
                "HTTP-Referer": "http://localhost",
                "X-Title": "4-Layer Memory",
            },
        )
        try:
            return urllib.request.urlopen(req, timeout=self.timeout)
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:600]
            raise LLMError(f"OpenRouter {e.code} ({model}): {detail}") from None
        except urllib.error.URLError as e:
            raise LLMError(f"เชื่อมต่อ OpenRouter ไม่ได้: {e.reason}") from None

    def chat(self, model, messages, tools=None, tool_choice=None, reasoning=None):
        with self._request(model, messages, tools, tool_choice, reasoning=reasoning) as r:
            data = json.load(r)
        if data.get("error"):
            raise LLMError(f"OpenRouter ({model}): {data['error'].get('message', data['error'])}")
        if not data.get("choices"):
            raise LLMError(f"OpenRouter ({model}) ไม่ส่งคำตอบกลับมา")
        return data["choices"][0]["message"], data.get("usage") or {}

    def chat_stream(self, model, messages, tools=None, tool_choice=None, reasoning=None,
                     on_text=None, on_reasoning=None):
        """Same result as chat(), but reads server-sent events and calls on_text(piece)/on_reasoning(piece)
        as plain text and reasoning text arrive. Also rebuilds reasoning_details from the chunks."""
        text, calls, usage, got_choice = [], {}, {}, False
        reasoning_bits, reasoning_details = [], {}
        try:
            with self._request(model, messages, tools, tool_choice, stream=True, reasoning=reasoning) as r:
                for raw in r:
                    line = raw.decode("utf-8", "replace").strip()
                    if not line.startswith("data:"):
                        continue  # blank lines and ": OPENROUTER PROCESSING" keep-alives
                    payload = line[5:].strip()
                    if payload == "[DONE]":
                        break
                    chunk = json.loads(payload)
                    if chunk.get("error"):
                        err = chunk["error"]
                        raise LLMError(f"OpenRouter ({model}): {err.get('message', err) if isinstance(err, dict) else err}")
                    if chunk.get("usage"):
                        usage = chunk["usage"]
                    for choice in chunk.get("choices") or []:
                        got_choice = True
                        delta = choice.get("delta") or {}
                        piece = delta.get("content")
                        if piece:
                            text.append(piece)
                            if on_text:
                                on_text(piece)
                        rpiece = delta.get("reasoning")
                        if rpiece:
                            reasoning_bits.append(rpiece)
                            if on_reasoning:
                                on_reasoning(rpiece)
                        for rd in delta.get("reasoning_details") or []:
                            idx = rd.get("index", 0)
                            slot = reasoning_details.get(idx)
                            payload_key = {"reasoning.text": "text", "reasoning.summary": "summary",
                                           "reasoning.encrypted": "data"}.get(rd.get("type"), "text")
                            if slot is None:
                                slot = dict(rd)
                                slot[payload_key] = rd.get(payload_key) or ""
                                reasoning_details[idx] = slot
                            else:
                                slot[payload_key] = (slot.get(payload_key) or "") + (rd.get(payload_key) or "")
                                if rd.get("signature"):
                                    slot["signature"] = rd["signature"]
                            if not rpiece and payload_key in ("text", "summary") and rd.get(payload_key):
                                reasoning_bits.append(rd[payload_key])
                                if on_reasoning:
                                    on_reasoning(rd[payload_key])
                        for tc in delta.get("tool_calls") or []:
                            slot = calls.setdefault(tc.get("index", 0), {
                                "id": "", "type": "function", "function": {"name": "", "arguments": ""}})
                            if tc.get("id"):
                                slot["id"] = tc["id"]
                            fn = tc.get("function") or {}
                            slot["function"]["name"] += fn.get("name") or ""
                            slot["function"]["arguments"] += fn.get("arguments") or ""
        except (OSError, ValueError) as e:  # dropped connection or a broken chunk mid-stream
            raise LLMError(f"สตรีมจาก OpenRouter ขาดกลางทาง ({model}): {e}") from None
        if not got_choice:
            raise LLMError(f"OpenRouter ({model}) ไม่ส่งคำตอบกลับมา")
        msg = {"role": "assistant", "content": "".join(text)}
        if calls:
            msg["tool_calls"] = [calls[i] for i in sorted(calls)]
        if reasoning_details:
            msg["reasoning_details"] = [reasoning_details[i] for i in sorted(reasoning_details)]
        joined = "".join(reasoning_bits)
        if joined:
            msg["reasoning"] = joined
        return msg, usage


class Meter:
    """Cost and call log for one user message (thread-safe, scanner shards run in parallel)."""

    def __init__(self):
        self.cost = 0.0
        self.calls = []
        self._lock = threading.Lock()

    def add(self, role, model, usage):
        with self._lock:
            cost = float(usage.get("cost") or 0)
            self.cost += cost
            self.calls.append({
                "role": role, "model": model, "cost": cost,
                "prompt_tokens": usage.get("prompt_tokens") or 0,
                "completion_tokens": usage.get("completion_tokens") or 0,
            })


class Engine:
    def __init__(self, db_path, client_factory=None, summarize_async=True):
        self.db_path = str(db_path)
        self.client_factory = client_factory or OpenRouter
        self.summarize_async = summarize_async
        self._guard = threading.Lock()
        self._send_locks = {}
        self._sum_locks = {}
        self._busy = {}
        self._errors = {}
        self._listeners = {}  # chat_id -> callback(event dict) while a streamed send runs
        with self.db() as con:
            con.executescript(SCHEMA)
            # Migration for DBs created before per-chat compression overrides existed.
            cols = {r["name"] for r in con.execute("PRAGMA table_info(chats)")}
            if "compress_at" not in cols:
                con.execute("ALTER TABLE chats ADD COLUMN compress_at INTEGER")
            if "round_size" not in cols:
                con.execute("ALTER TABLE chats ADD COLUMN round_size INTEGER")
            if "main_effort" not in cols:
                con.execute("ALTER TABLE chats ADD COLUMN main_effort TEXT")

    # ---------- storage helpers ----------

    @contextmanager
    def db(self):
        con = sqlite3.connect(self.db_path, timeout=30)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA journal_mode=WAL")
        try:
            yield con
            con.commit()
        finally:
            con.close()

    def _lock(self, table, chat_id):
        with self._guard:
            return table.setdefault(chat_id, threading.Lock())

    def get_settings(self):
        s = dict(DEFAULTS)
        with self.db() as con:
            rows = {r["key"]: r["value"] for r in con.execute("SELECT key, value FROM settings")}
        for k, v in rows.items():
            if k in s:
                s[k] = json.loads(v)
        # Migration: old DBs stored "live_turns" instead of "compress_at". Translate it once;
        # the stale live_turns row itself is just ignored above (it is not in DEFAULTS anymore).
        if "compress_at" not in rows and "live_turns" in rows:
            try:
                s["compress_at"] = int(json.loads(rows["live_turns"])) + s["round_size"]
            except (TypeError, ValueError, json.JSONDecodeError):
                pass
        return s

    def public_settings(self):
        s = self.get_settings()
        key = s.pop("api_key")
        s["api_key_set"] = bool(key)
        s["api_key_hint"] = f"…{key[-4:]}" if len(key) > 8 else ""
        return s

    def save_settings(self, updates):
        clean = {}
        for k, v in updates.items():
            if k not in DEFAULTS:
                continue
            default = DEFAULTS[k]
            try:
                if isinstance(default, float):
                    v = float(v)
                    if v <= 0:
                        raise ValueError
                elif isinstance(default, int):
                    v = int(v)
                    if v < 1 or (k == "compress_at" and v < 2):
                        raise ValueError
                else:
                    v = str(v).strip()
            except (TypeError, ValueError):
                raise UserError(f"ค่า {k} ไม่ถูกต้อง") from None
            if k == "main_effort" and v and v not in EFFORTS:
                raise UserError("ระดับความคิดไม่ถูกต้อง")
            clean[k] = v
        current = self.get_settings()
        merged = {**current, **clean}
        if merged["round_size"] >= merged["compress_at"]:
            raise UserError("บีบอัดครั้งละกี่เทิร์นต้องน้อยกว่าจำนวนเทิร์นที่เริ่มบีบอัด")
        with self.db() as con:
            for k, v in clean.items():
                con.execute("INSERT OR REPLACE INTO settings(key, value) VALUES(?, ?)", (k, json.dumps(v)))
        return self.public_settings()

    def status(self, chat_id):
        return {
            "busy": self._busy.get(chat_id),
            "summarizing": self._lock(self._sum_locks, chat_id).locked(),
            "last_error": self._errors.get(chat_id),
        }

    def _set_busy(self, chat_id, text):
        self._busy[chat_id] = text
        if text:
            self._emit(chat_id, {"type": "status", "text": text})

    def _emit(self, chat_id, event):
        listener = self._listeners.get(chat_id)
        if listener:
            listener(event)

    # ---------- chats ----------

    def create_chat(self, title="แชตใหม่"):
        with self.db() as con:
            cur = con.execute("INSERT INTO chats(title, created_at) VALUES(?, ?)", (title, time.time()))
            return cur.lastrowid

    def list_chats(self):
        with self.db() as con:
            rows = con.execute("""
                SELECT c.id, c.title, c.created_at,
                       (SELECT COUNT(*) FROM turns t WHERE t.chat_id = c.id) AS turns,
                       (SELECT COALESCE(SUM(cost), 0) FROM usage u WHERE u.chat_id = c.id) AS cost
                FROM chats c ORDER BY c.id DESC""").fetchall()
        return [dict(r) for r in rows]

    def rename_chat(self, chat_id, title):
        title = (title or "").strip()[:80]
        if not title:
            raise UserError("ชื่อแชตว่างไม่ได้")
        with self.db() as con:
            con.execute("UPDATE chats SET title = ? WHERE id = ?", (title, chat_id))

    def delete_chat(self, chat_id):
        with self.db() as con:
            con.execute("DELETE FROM artifact_versions WHERE artifact_id IN "
                        "(SELECT id FROM artifacts WHERE chat_id = ?)", (chat_id,))
            for table, col in (("turns", "chat_id"), ("state", "chat_id"), ("usage", "chat_id"),
                               ("artifacts", "chat_id"), ("chats", "id")):
                con.execute(f"DELETE FROM {table} WHERE {col} = ?", (chat_id,))

    def _require_chat(self, con, chat_id):
        row = con.execute("SELECT * FROM chats WHERE id = ?", (chat_id,)).fetchone()
        if not row:
            raise UserError("ไม่พบแชตนี้")
        return row

    def _compression(self, con, chat_id, settings):
        """Effective compress_at/round_size for one chat: global settings with that chat's overrides."""
        row = con.execute("SELECT compress_at, round_size FROM chats WHERE id = ?", (chat_id,)).fetchone()
        custom_compress_at = row["compress_at"] if row else None
        custom_round_size = row["round_size"] if row else None
        compress_at = custom_compress_at if custom_compress_at is not None else settings["compress_at"]
        round_size = custom_round_size if custom_round_size is not None else settings["round_size"]
        if round_size >= compress_at:  # defaults may have changed since the override was saved
            round_size = compress_at - 1
        return {
            "compress_at": compress_at,
            "round_size": round_size,
            "custom": {"compress_at": custom_compress_at, "round_size": custom_round_size},
            "default": {"compress_at": settings["compress_at"], "round_size": settings["round_size"]},
        }

    def set_compression(self, chat_id, compress_at, round_size):
        new_compress_at = parse_count(compress_at, "compress_at", 2)
        new_round_size = parse_count(round_size, "round_size", 1)
        s = self.get_settings()
        eff_compress_at = new_compress_at if new_compress_at is not None else s["compress_at"]
        eff_round_size = new_round_size if new_round_size is not None else s["round_size"]
        if eff_round_size >= eff_compress_at:
            raise UserError("บีบอัดครั้งละกี่เทิร์นต้องน้อยกว่าจำนวนเทิร์นที่เริ่มบีบอัด")
        with self.db() as con:
            self._require_chat(con, chat_id)
            con.execute("UPDATE chats SET compress_at = ?, round_size = ? WHERE id = ?",
                        (new_compress_at, new_round_size, chat_id))
        self._after_turn(chat_id)  # lowering the threshold may mean we can compress right away
        with self.db() as con:
            return self._compression(con, chat_id, self.get_settings())

    def _effort(self, con, chat_id, settings):
        """Effective reasoning effort for one chat's main model: "" or a value in EFFORTS.

        The chat's override column is NULL (use global), "" (explicitly model default) or a level.
        """
        row = con.execute("SELECT main_effort FROM chats WHERE id = ?", (chat_id,)).fetchone()
        custom = row["main_effort"] if row else None
        value = custom if custom is not None else settings["main_effort"]
        return {"value": value, "custom": custom, "default": settings["main_effort"]}

    def set_effort(self, chat_id, effort):
        if effort is not None and effort != "" and effort not in EFFORTS:
            raise UserError("ระดับความคิดไม่ถูกต้อง")
        with self.db() as con:
            self._require_chat(con, chat_id)
            con.execute("UPDATE chats SET main_effort = ? WHERE id = ?", (effort, chat_id))
            return self._effort(con, chat_id, self.get_settings())

    def get_chat(self, chat_id):
        s = self.get_settings()
        with self.db() as con:
            chat = dict(self._require_chat(con, chat_id))
            comp = self._compression(con, chat_id, s)
            effort = self._effort(con, chat_id, s)
            turns = []
            for r in con.execute("SELECT * FROM turns WHERE chat_id = ? ORDER BY tid", (chat_id,)):
                turns.append({
                    "tid": r["tid"], "user": r["user"], "answer": r["answer"],
                    "tools": json.loads(r["tools"]), "meta": json.loads(r["meta"]),
                    "summary": json.loads(r["summary"]) if r["summary"] else None,
                })
            state = [dict(r) for r in con.execute(
                "SELECT * FROM state WHERE chat_id = ? AND active = 1 ORDER BY id", (chat_id,))]
            usage = [dict(r) for r in con.execute(
                "SELECT role, COUNT(*) AS calls, SUM(prompt_tokens) AS prompt_tokens, "
                "SUM(completion_tokens) AS completion_tokens, SUM(cost) AS cost "
                "FROM usage WHERE chat_id = ? GROUP BY role", (chat_id,))]
            cur = (turns[-1]["tid"] if turns else 0) + 1
            state_text = self._render_state(con, chat_id, cur, s, [])
            artifacts = self._artifact_list(con, chat_id)
        live = [t["tid"] for t in turns if t["summary"] is None]
        chat.update({
            "turns": turns,
            "state": state,
            "usage": usage,
            "layers": {
                "live": len(live),
                "archived": len(turns) - len(live),
                "state_chars": len(state_text),
                "state_cap": s["state_cap_chars"],
                "next_summary_at": comp["compress_at"],
            },
            "compression": comp,
            "effort": effort,
            "artifacts": artifacts,
            "status": self.status(chat_id),
        })
        return chat

    def export_chat(self, chat_id):
        with self.db() as con:
            chat = dict(self._require_chat(con, chat_id))
            chat["turns"] = [dict(r) for r in con.execute(
                "SELECT * FROM turns WHERE chat_id = ? ORDER BY tid", (chat_id,))]
            chat["state"] = [dict(r) for r in con.execute(
                "SELECT * FROM state WHERE chat_id = ? ORDER BY id", (chat_id,))]
            chat["usage"] = [dict(r) for r in con.execute(
                "SELECT * FROM usage WHERE chat_id = ? ORDER BY id", (chat_id,))]
            chat["artifacts"] = []
            for a in con.execute("SELECT * FROM artifacts WHERE chat_id = ? ORDER BY id", (chat_id,)).fetchall():
                item = dict(a)
                item["versions"] = [dict(v) for v in con.execute(
                    "SELECT version, title, content, tid, created_at FROM artifact_versions "
                    "WHERE artifact_id = ? ORDER BY version", (a["id"],))]
                chat["artifacts"].append(item)
        return chat

    # ---------- artifacts ----------

    def _artifact_list(self, con, chat_id):
        """Every artifact of a chat with its latest version's title and size, newest change first."""
        rows = con.execute("""
            SELECT a.id, a.key, a.kind, a.language, v.version, v.title, LENGTH(v.content) AS chars,
                   v.tid AS updated_tid
            FROM artifacts a
            JOIN artifact_versions v ON v.artifact_id = a.id
             AND v.version = (SELECT MAX(version) FROM artifact_versions WHERE artifact_id = a.id)
            WHERE a.chat_id = ? ORDER BY v.created_at DESC, a.id DESC""", (chat_id,)).fetchall()
        return [{**dict(r), "versions": r["version"]} for r in rows]

    def list_artifacts(self, chat_id):
        with self.db() as con:
            self._require_chat(con, chat_id)
            return self._artifact_list(con, chat_id)

    def get_artifact(self, chat_id, artifact_id, version=None):
        with self.db() as con:
            a = con.execute("SELECT * FROM artifacts WHERE id = ? AND chat_id = ?",
                            (artifact_id, chat_id)).fetchone()
            if not a:
                raise UserError("ไม่พบชิ้นงานนี้")
            versions = [dict(v) for v in con.execute(
                "SELECT version, title, tid, created_at, LENGTH(content) AS chars FROM artifact_versions "
                "WHERE artifact_id = ? ORDER BY version", (artifact_id,))]
            want = version if version is not None else versions[-1]["version"]
            v = con.execute("SELECT * FROM artifact_versions WHERE artifact_id = ? AND version = ?",
                            (artifact_id, want)).fetchone()
            if not v:
                raise UserError("ไม่พบชิ้นงานนี้")
        return {"id": a["id"], "key": a["key"], "title": v["title"], "kind": a["kind"], "language": a["language"],
                "version": v["version"], "content": v["content"], "versions": versions}

    def _artifact_tool(self, chat_id, cur, name, args, settings, touched):
        """create_artifact / update_artifact / read_artifact for the main model. Errors go back as text."""
        key = str(args.get("key") or "").strip()
        with self.db() as con:
            a = con.execute("SELECT * FROM artifacts WHERE chat_id = ? AND key = ?", (chat_id, key)).fetchone()
            latest = None
            if a:
                latest = con.execute("SELECT * FROM artifact_versions WHERE artifact_id = ? "
                                     "ORDER BY version DESC LIMIT 1", (a["id"],)).fetchone()

            if name == "read_artifact":
                if not a:
                    return f"ERROR: no artifact with key {key!r}"
                row = latest
                if args.get("version") is not None:
                    row = con.execute("SELECT * FROM artifact_versions WHERE artifact_id = ? AND version = ?",
                                      (a["id"], int(args["version"]))).fetchone()
                    if not row:
                        return f"ERROR: {key} has versions 1–{latest['version']}"
                return (f"Artifact {key}: {row['title']} [{a['kind']}] v{row['version']} "
                        f"of {latest['version']}, {len(row['content']):,} chars\n\n"
                        + clip(row["content"], settings["full_char_limit"]))

            title = str(args.get("title") or "").strip()
            if title and len(title) > 120:
                return "ERROR: title must be at most 120 characters"

            if name == "create_artifact":
                if a:
                    return f"ERROR: {key} already exists (v{latest['version']}). Use update_artifact to change it."
                if not ARTIFACT_KEY.fullmatch(key):
                    return "ERROR: key must be 1-60 chars of a-z, 0-9 and '-', starting with a letter or digit"
                kind = str(args.get("kind") or "")
                if kind not in ARTIFACT_KINDS:
                    return f"ERROR: kind must be one of {', '.join(ARTIFACT_KINDS)}"
                if not title:
                    return "ERROR: title is empty"
                content = args.get("content")
                if not isinstance(content, str) or not content.strip():
                    return "ERROR: content is empty"
                language = str(args.get("language") or "").strip()[:30] or None
                if len(content) > ARTIFACT_MAX_CHARS:
                    return f"ERROR: content is {len(content):,} chars; the limit is {ARTIFACT_MAX_CHARS:,}"
                aid = con.execute("INSERT INTO artifacts(chat_id, key, kind, language, created_tid) "
                                  "VALUES(?, ?, ?, ?, ?)", (chat_id, key, kind, language, cur)).lastrowid
                version, kind_out = 1, kind
            else:  # update_artifact
                if not a:
                    return f"ERROR: no artifact with key {key!r}. Use create_artifact first."
                content, edits = args.get("content"), args.get("edits")
                if (content is None) == (edits is None):
                    return "ERROR: pass exactly one of content (full replacement) or edits"
                if content is not None:
                    if not isinstance(content, str) or not content.strip():
                        return "ERROR: content is empty"
                else:
                    if not isinstance(edits, list) or not edits:
                        return "ERROR: edits must be a non-empty list of {old, new}"
                    content = latest["content"]
                    for i, e in enumerate(edits):
                        old = e.get("old") if isinstance(e, dict) else None
                        if not isinstance(old, str) or not old:
                            return f"ERROR: edit {i}: old text is empty"
                        count = content.count(old)
                        if count != 1:
                            return (f"ERROR: edit {i}: old text occurs {count} times in v{latest['version']}; "
                                    "it must occur exactly once. Nothing was saved. Include more context, "
                                    "or read_artifact first.")
                        content = content.replace(old, str(e.get("new") or ""), 1)
                if len(content) > ARTIFACT_MAX_CHARS:
                    return f"ERROR: content would be {len(content):,} chars; the limit is {ARTIFACT_MAX_CHARS:,}"
                aid, version, kind_out, language = a["id"], latest["version"] + 1, a["kind"], a["language"]
                title = title or latest["title"]
            con.execute("INSERT INTO artifact_versions(artifact_id, version, title, content, tid, created_at) "
                        "VALUES(?, ?, ?, ?, ?, ?)", (aid, version, title, content, cur, time.time()))
        info = {"id": aid, "key": key, "title": title, "kind": kind_out, "language": language, "version": version}
        touched[aid] = info
        self._emit(chat_id, {"type": "artifact", "artifact": info})
        verb = "Created artifact" if version == 1 else "Updated"
        return f"{verb} {key} v{version} ({len(content):,} chars). It is shown to the user in the artifact panel."

    # ---------- layer 4 edits from the user ----------

    def add_state(self, chat_id, kind, text, source_tid=None):
        text = (text or "").strip()
        if not text:
            raise UserError("ข้อความว่าง")
        if kind not in ("condition", "decision"):
            raise UserError("เพิ่มจากหน้าเว็บได้เฉพาะ condition และ decision")
        with self.db() as con:
            self._require_chat(con, chat_id)
            last = con.execute("SELECT MAX(tid) FROM turns WHERE chat_id = ?", (chat_id,)).fetchone()[0] or 0
            if source_tid is not None:
                row = con.execute("SELECT user FROM turns WHERE chat_id = ? AND tid = ?",
                                  (chat_id, source_tid)).fetchone()
                if not row:
                    raise UserError(f"ไม่พบ ID {source_tid}")
                # Rule 3.5: a condition pinned from a message must be the user's own words, verbatim.
                if kind == "condition" and text not in row["user"]:
                    raise UserError("condition ต้องเป็นคำของผู้ใช้ตรงตัวจากข้อความ ID นั้น")
            con.execute(
                "INSERT INTO state(chat_id, kind, text, source_tid, created_tid, expires_tid, origin) "
                "VALUES(?, ?, ?, ?, ?, NULL, 'user')", (chat_id, kind, text, source_tid, last))

    def update_state(self, chat_id, item_id, text=None, active=None):
        with self.db() as con:
            row = con.execute("SELECT * FROM state WHERE id = ? AND chat_id = ?", (item_id, chat_id)).fetchone()
            if not row:
                raise UserError("ไม่พบรายการนี้")
            if text is not None:
                if row["kind"] in ("summary", "full"):
                    raise UserError("แก้สรุปได้ที่แท็บชั้น 2")
                if not text.strip():
                    raise UserError("ข้อความว่าง")
                con.execute("UPDATE state SET text = ? WHERE id = ?", (text.strip(), item_id))
            if active is not None:
                con.execute("UPDATE state SET active = ? WHERE id = ?", (1 if active else 0, item_id))

    def update_summary(self, chat_id, tid, fields):
        summary = {k: str(fields.get(k, "")).strip() for k in ("user", "answer", "omitted")}
        with self.db() as con:
            cur = con.execute("UPDATE turns SET summary = ? WHERE chat_id = ? AND tid = ? AND summary IS NOT NULL",
                              (json.dumps(summary, ensure_ascii=False), chat_id, tid))
            if cur.rowcount == 0:
                raise UserError(f"ID {tid} ยังไม่ถูกสรุป")

    # ---------- model calls ----------

    def _call(self, chat_id, tid, role, model, messages, meter, settings, on_text=None,
              on_reasoning=None, reasoning=None, **kw):
        if not settings["api_key"]:
            raise UserError("ยังไม่ได้ใส่ OpenRouter API key")
        if not model:
            raise UserError(f"ยังไม่ได้เลือกโมเดลของ{ROLE_NAME[role]}")
        client = self.client_factory(settings["api_key"])
        if reasoning is not None:
            kw["reasoning"] = reasoning
        if on_text and hasattr(client, "chat_stream"):
            if on_reasoning is not None:
                kw["on_reasoning"] = on_reasoning
            msg, usage = client.chat_stream(model, messages, on_text=on_text, **kw)
        else:
            msg, usage = client.chat(model, messages, **kw)
        meter.add(role, model, usage)
        with self.db() as con:
            con.execute(
                "INSERT INTO usage(chat_id, tid, role, model, prompt_tokens, completion_tokens, cost, created_at) "
                "VALUES(?, ?, ?, ?, ?, ?, ?, ?)",
                (chat_id, tid, role, model, usage.get("prompt_tokens") or 0,
                 usage.get("completion_tokens") or 0, float(usage.get("cost") or 0), time.time()))
        return msg

    # ---------- layer 2: summarizer ----------

    def summarize_pending(self, chat_id, force=False):
        """Summarize the oldest round while layer 1 holds compress_at turns or more (global default,
        overridable per chat; see _compression).

        force=True summarizes one round now even below the threshold (for testing by hand).
        """
        lock = self._lock(self._sum_locks, chat_id)
        if not lock.acquire(blocking=False):
            return 0
        done = 0
        try:
            self._errors.pop(chat_id, None)
            while True:
                s = self.get_settings()
                with self.db() as con:
                    comp = self._compression(con, chat_id, s)
                    pending = con.execute(
                        "SELECT tid, user, answer, tools FROM turns WHERE chat_id = ? AND summary IS NULL ORDER BY tid",
                        (chat_id,)).fetchall()
                if not pending:
                    break
                if len(pending) < comp["compress_at"] and not (force and done == 0):
                    break
                batch = pending[:comp["round_size"]]
                self._summarize_round(chat_id, batch, pending[len(batch)] if len(pending) > len(batch) else None, s)
                done += len(batch)
        except Exception as e:  # noqa: BLE001 - surfaced through status()
            self._errors[chat_id] = f"สรุปไม่สำเร็จ: {e}"
        finally:
            lock.release()
        return done

    def _turn_text(self, row, settings, limit=None):
        tools = json.loads(row["tools"]) if "tools" in row.keys() else []
        parts = [f"user: {clip(row['user'], limit) if limit else row['user']}",
                 f"assistant: {clip(row['answer'], limit) if limit else row['answer']}"]
        for t in tools:
            parts.append(f"tool {t['name']}({json.dumps(t['args'], ensure_ascii=False)}): "
                         f"{clip(t['result'], settings['tool_limit'])}")
        return "\n".join(parts)

    def _summarize_round(self, chat_id, batch, after, settings):
        ids = [r["tid"] for r in batch]
        with self.db() as con:
            before = con.execute(
                "SELECT tid, summary FROM turns WHERE chat_id = ? AND summary IS NOT NULL ORDER BY tid DESC LIMIT 1",
                (chat_id,)).fetchone()
        blocks = []
        if before:
            blocks.append(f"<context_only>\n{format_summary(before['tid'], before['summary'])}\n</context_only>")
        for r in batch:
            blocks.append(f'<turn id="{r["tid"]}">\n{self._turn_text(r, settings)}\n</turn>')
        if after:
            blocks.append(f'<context_only id="{after["tid"]}">\n{self._turn_text(after, settings, 1500)}\n</context_only>')
        user = "Summarize turn IDs " + ", ".join(map(str, ids)) + ".\n\n" + "\n\n".join(blocks)
        messages = [{"role": "system", "content": prompts.SUMMARIZE_SYSTEM}, {"role": "user", "content": user}]

        last_error = None
        for _ in range(2):
            try:
                msg = self._call(chat_id, ids[-1], "summarize", settings["model_summarizer"],
                                 messages, Meter(), settings)
                data = parse_json(message_text(msg))
                got = {int(x["id"]): x for x in data.get("summaries", [])}
                if set(got) != set(ids):
                    raise ValueError(f"ได้ ID {sorted(got)} แต่ต้องการ {ids}")
                with self.db() as con:
                    for tid in ids:
                        s = {k: str(got[tid].get(k) or "").strip() for k in ("user", "answer", "omitted")}
                        con.execute("UPDATE turns SET summary = ? WHERE chat_id = ? AND tid = ?",
                                    (json.dumps(s, ensure_ascii=False), chat_id, tid))
                return
            except UserError:
                raise
            except Exception as e:  # noqa: BLE001 - retried once, then reported
                last_error = e
        raise LLMError(f"ID {ids[0]}–{ids[-1]}: {last_error}")

    def _after_turn(self, chat_id):
        if self.summarize_async:
            threading.Thread(target=self.summarize_pending, args=(chat_id,), daemon=True).start()
        else:
            self.summarize_pending(chat_id)

    # ---------- scanner ----------

    def scan(self, chat_id, cur, query, settings, meter, record=True):
        with self.db() as con:
            rows = con.execute(
                "SELECT tid, summary FROM turns WHERE chat_id = ? AND summary IS NOT NULL ORDER BY tid",
                (chat_id,)).fetchall()
        if not rows:
            return {"query": query, "shards": [], "ids": [], "note": "ยังไม่มี ID ที่ถูกสรุป"}
        size = settings["shard_size"]
        shards = [rows[i:i + size] for i in range(0, len(rows), size)]

        def run(shard):
            valid = {r["tid"] for r in shard}
            text = "\n".join(format_summary(r["tid"], r["summary"]) for r in shard)
            messages = [
                {"role": "system", "content": prompts.SCAN_SYSTEM},
                {"role": "user", "content": f"<summaries>\n{text}\n</summaries>\n\n<query>\n{query}\n</query>"},
            ]
            out = {"range": [shard[0]["tid"], shard[-1]["tid"]], "status": "failed", "ids": [], "reason": ""}
            for attempt in (1, 2):
                out["attempts"] = attempt
                try:
                    data = parse_json(message_text(self._call(
                        chat_id, cur, "scan", settings["model_scanner"], messages, meter, settings)))
                    ids = sorted({int(i) for i in data.get("ids") or [] if str(i).lstrip("-").isdigit()} & valid)
                    if data.get("status") not in ("found", "not_found"):
                        raise ValueError(f"status ไม่ถูกต้อง: {data.get('status')!r}")
                    out.update(status="found" if ids else "not_found", ids=ids,
                               reason=str(data.get("reason") or "")[:300], error=None)
                    return out
                except UserError:
                    raise
                except Exception as e:  # noqa: BLE001 - one retry, then the shard reports failed
                    out["error"] = str(e)[:300]
            return out

        with ThreadPoolExecutor(max_workers=min(8, len(shards))) as ex:
            results = list(ex.map(run, shards))
        found = sorted({i for r in results for i in r["ids"]})
        if record and found:
            with self.db() as con:
                for tid in found:
                    self._put_retrieved(con, chat_id, tid, cur, settings, "summary")
        return {"query": query, "shards": results, "ids": found}

    def _put_retrieved(self, con, chat_id, tid, cur, settings, kind):
        """Add a summary/full item for tid to layer 4. A full item replaces the summary (rule 3.4)."""
        active = {r["kind"]: r["id"] for r in con.execute(
            "SELECT id, kind FROM state WHERE chat_id = ? AND active = 1 AND source_tid = ? "
            "AND kind IN ('summary', 'full')", (chat_id, tid))}
        if kind == "summary" and "full" in active:
            kind = "full"
        ttl = settings["full_ttl"] if kind == "full" else settings["summary_ttl"]
        if kind == "full" and "summary" in active:
            con.execute("UPDATE state SET active = 0 WHERE id = ?", (active["summary"],))
        if kind in active:
            con.execute("UPDATE state SET expires_tid = ? WHERE id = ?", (cur + ttl, active[kind]))
            return active[kind]
        return con.execute(
            "INSERT INTO state(chat_id, kind, text, source_tid, created_tid, expires_tid, origin) "
            "VALUES(?, ?, NULL, ?, ?, ?, 'system')", (chat_id, kind, tid, cur, cur + ttl)).lastrowid

    # ---------- layer 4 rendering ----------

    def _render_state(self, con, chat_id, cur, settings, scans, trim=False):
        """Layer 4 as the <memory_state> block. trim=True (only when sending) retires the oldest
        retrieved 4b items until the block fits the cap; 4a and notes are never trimmed here."""
        text, trimmed = self._render_state_once(con, chat_id, cur, settings, scans, []), []
        while trim and len(text) > settings["state_cap_chars"]:
            victim = None
            for r in con.execute("SELECT id, kind, expires_tid FROM state WHERE chat_id = ? AND active = 1 "
                                 "AND kind IN ('summary', 'full') ORDER BY expires_tid, id", (chat_id,)):
                ttl = settings["full_ttl"] if r["kind"] == "full" else settings["summary_ttl"]
                if r["expires_tid"] is None or r["expires_tid"] - ttl < cur:  # skip items fetched this turn
                    victim = r
                    break
            if not victim:
                break
            con.execute("UPDATE state SET active = 0 WHERE id = ?", (victim["id"],))
            trimmed.append(f"{KIND_LABEL[victim['kind']]}{victim['id']}")
            text = self._render_state_once(con, chat_id, cur, settings, scans, trimmed)
        return text

    def _render_state_once(self, con, chat_id, cur, settings, scans, trimmed):
        items = con.execute("SELECT * FROM state WHERE chat_id = ? AND active = 1 ORDER BY id",
                            (chat_id,)).fetchall()
        sections = {k: [] for k in KIND_LABEL}
        for it in items:
            label = f"[{KIND_LABEL[it['kind']]}{it['id']}]"
            src = f"(ID {it['source_tid']})" if it["source_tid"] is not None else "(ผู้ใช้พิมพ์เอง)"
            exp = f" หมดอายุหลัง ID {it['expires_tid']}" if it["expires_tid"] else ""
            if it["kind"] in ("summary", "full"):
                t = con.execute("SELECT user, answer, summary FROM turns WHERE chat_id = ? AND tid = ?",
                                (chat_id, it["source_tid"])).fetchone()
                if not t:
                    continue
                if it["kind"] == "summary":
                    if not t["summary"]:
                        continue
                    body = format_summary(it["source_tid"], t["summary"])
                else:
                    body = (f"ID {it['source_tid']}\nuser: {clip(t['user'], settings['full_char_limit'])}\n"
                            f"assistant: {clip(t['answer'], settings['full_char_limit'])}")
                sections[it["kind"]].append(f"- {label}{exp} {body}")
            else:
                sections[it["kind"]].append(f"- {label} {src}{exp} {it['text']}")

        live = con.execute("SELECT MIN(tid), MAX(tid) FROM turns WHERE chat_id = ? AND summary IS NULL",
                           (chat_id,)).fetchone()
        archived = con.execute("SELECT COUNT(*) FROM turns WHERE chat_id = ? AND summary IS NOT NULL",
                               (chat_id,)).fetchone()[0]
        head = [f"Current turn: ID {cur}.",
                f"Layer 1 holds IDs {live[0]}–{live[1]}." if live[0] else "Layer 1 holds only this turn.",
                f"Archived IDs searchable with search_memory: {archived}."]
        for sc in scans:
            failed = [s["range"] for s in sc["shards"] if s["status"] == "failed"]
            head.append(f"Automatic scan this turn ({sc.get('trigger')}): found IDs {sc['ids'] or 'none'}"
                        + (f"; FAILED shards {failed} — tell the user the search was incomplete" if failed else ""))
        if trimmed:
            head.append(f"Layer 4 was over its cap, so the system retired the oldest retrieved items: "
                        f"{', '.join(trimmed)}. Fetch them again if you still need them.")
        arts = self._artifact_list(con, chat_id)
        if arts:
            head.append("Artifacts in this chat (open with read_artifact):\n" + "\n".join(
                f"- {a['key']}: {a['title']} [{a['kind']}{'/' + a['language'] if a['language'] else ''}] "
                f"v{a['version']}, {a['chars']:,} chars" for a in arts[:50]))
        titles = {
            "condition": "4a Conditions (the user's own words, always apply)",
            "decision": "4a Decisions (agreed by the user)",
            "note": "4b Notes",
            "summary": "4b Retrieved summaries (layer 2)",
            "full": "4b Retrieved full text (layer 3)",
        }
        body = "\n".join(head)
        for k, lines in sections.items():
            if lines:
                body += f"\n\n## {titles[k]}\n" + "\n".join(lines)
        cap = settings["state_cap_chars"]
        if len(body) > cap:
            body += (f"\n\nWARNING: layer 4 is {len(body):,} chars, over the cap of {cap:,}. "
                     "Retire notes or retrieved items you no longer need with retire_state.")
        return f"<memory_state>\n{body}\n</memory_state>"

    # ---------- main turn ----------

    def send(self, chat_id, text, force_scan=False, on_event=None):
        """Run one user message. on_event(dict) receives status/round/delta/tool events as they happen."""
        text = (text or "").strip()
        if not text:
            raise UserError("ข้อความว่าง")
        settings = self.get_settings()
        lock = self._lock(self._send_locks, chat_id)
        if not lock.acquire(blocking=False):
            raise BusyError("แชตนี้กำลังตอบอยู่")
        if on_event:
            self._listeners[chat_id] = on_event
        try:
            meter, trace, scans = Meter(), [], []
            with self.db() as con:
                chat = self._require_chat(con, chat_id)
                cur = (con.execute("SELECT MAX(tid) FROM turns WHERE chat_id = ?", (chat_id,)).fetchone()[0] or 0) + 1
                con.execute("UPDATE state SET active = 0 WHERE chat_id = ? AND active = 1 "
                            "AND expires_tid IS NOT NULL AND expires_tid < ?", (chat_id, cur))
                archived = con.execute("SELECT COUNT(*) FROM turns WHERE chat_id = ? AND summary IS NOT NULL",
                                       (chat_id,)).fetchone()[0]
                if cur == 1 and chat["title"] == "แชตใหม่":
                    con.execute("UPDATE chats SET title = ? WHERE id = ?", (text[:40], chat_id))

            trigger = "user" if force_scan else ("reference" if MEMORY_HINT.search(text) else None)
            if archived and trigger:
                self._set_busy(chat_id, "กำลังสแกนความจำ")
                result = self.scan(chat_id, cur, text, settings, meter)
                result["trigger"] = trigger
                scans.append(result)

            with self.db() as con:
                live = con.execute("SELECT tid, user, answer FROM turns WHERE chat_id = ? AND summary IS NULL "
                                   "ORDER BY tid", (chat_id,)).fetchall()
                state_text = self._render_state(con, chat_id, cur, settings, scans, trim=True)
            messages = [{"role": "system", "content": prompts.MAIN_SYSTEM}]
            for r in live:
                messages.append({"role": "user", "content": f"[ID {r['tid']}] {r['user']}"})
                messages.append({"role": "assistant", "content": r["answer"] or "(ไม่มีคำตอบ)"})
            messages.append({"role": "user", "content": f"[ID {cur}] {text}\n\n{state_text}"})
            prompt_chars = sum(len(m["content"]) for m in messages)

            with self.db() as con:
                effort = self._effort(con, chat_id, settings)["value"]
            touched = {}  # artifact id -> its latest info this turn
            answer, stopped, reasoning_text = self._run_main(
                chat_id, cur, text, messages, settings, meter, trace, scans, effort, touched)
            meta = {
                "scans": scans,
                "prompt_chars": prompt_chars,
                "state_chars": len(state_text),
                "live_ids": [live[0]["tid"], live[-1]["tid"]] if live else [],
                "cost": meter.cost,
                "calls": meter.calls,
                "stopped": stopped,
                "effort": effort,
                "artifacts": [{k: a[k] for k in ("id", "key", "title", "kind", "version")} for a in touched.values()],
            }
            if reasoning_text:
                meta["reasoning"] = clip(reasoning_text, 20000)
            with self.db() as con:
                con.execute(
                    "INSERT INTO turns(chat_id, tid, user, answer, tools, meta, summary, created_at) "
                    "VALUES(?, ?, ?, ?, ?, ?, NULL, ?)",
                    (chat_id, cur, text, answer, json.dumps(trace, ensure_ascii=False),
                     json.dumps(meta, ensure_ascii=False), time.time()))
        finally:
            self._set_busy(chat_id, None)
            self._listeners.pop(chat_id, None)
            lock.release()
        self._after_turn(chat_id)
        return {"tid": cur, "user": text, "answer": answer, "tools": trace, "meta": meta, "summary": None}

    def _main_call(self, chat_id, cur, messages, settings, meter, effort, **kw):
        """One main-model call. While a listener is attached the reply is streamed as delta events."""
        self._emit(chat_id, {"type": "round"})
        on_text = None
        on_reasoning = None
        if self._listeners.get(chat_id):
            on_text = lambda piece: self._emit(chat_id, {"type": "delta", "text": piece})  # noqa: E731
            on_reasoning = lambda piece: self._emit(chat_id, {"type": "thinking", "text": piece})  # noqa: E731
        reasoning = {"effort": effort} if effort else None
        return self._call(chat_id, cur, "main", settings["model_main"], messages, meter, settings,
                          on_text=on_text, on_reasoning=on_reasoning, reasoning=reasoning,
                          tools=prompts.TOOLS, **kw)

    def _run_main(self, chat_id, cur, user_text, messages, settings, meter, trace, scans, effort, touched):
        last_text, reasoning_bits = "", []
        for _ in range(settings["max_tool_rounds"]):
            if meter.cost >= settings["max_usd_per_message"]:
                note = "\n\n⚠️ หยุดเพราะใช้งบถึงเพดานต่อข้อความแล้ว"
                self._emit(chat_id, {"type": "delta", "text": note})
                return (last_text + note).strip(), "budget", "\n\n".join(reasoning_bits)
            self._set_busy(chat_id, "โมเดลหลักกำลังคิด")
            msg = self._main_call(chat_id, cur, messages, settings, meter, effort)
            last_text = message_text(msg)
            if msg.get("reasoning"):
                reasoning_bits.append(msg["reasoning"])
            calls = msg.get("tool_calls") or []
            if not calls:
                return last_text, None, "\n\n".join(reasoning_bits)
            assistant_msg = {"role": "assistant", "content": last_text, "tool_calls": calls}
            if msg.get("reasoning_details"):
                assistant_msg["reasoning_details"] = msg["reasoning_details"]
            elif msg.get("reasoning"):
                assistant_msg["reasoning"] = msg["reasoning"]
            messages.append(assistant_msg)
            for c in calls:
                name = c.get("function", {}).get("name", "")
                args = {}
                try:
                    args = json.loads(c["function"].get("arguments") or "{}")
                    self._set_busy(chat_id, TOOL_STATUS.get(name, f"ใช้เครื่องมือ {name}"))
                    result = self._tool(chat_id, cur, user_text, name, args, settings, meter, scans, touched)
                except UserError:
                    raise
                except Exception as e:  # noqa: BLE001 - reported back to the model as the tool result
                    result = f"ERROR: {e}"
                entry = {"name": name, "args": trace_args(name, args), "result": clip(result, settings["tool_limit"])}
                trace.append(entry)
                self._emit(chat_id, {"type": "tool", **entry})
                messages.append({"role": "tool", "tool_call_id": c.get("id", ""), "content": result})
        self._set_busy(chat_id, "โมเดลหลักกำลังสรุปคำตอบ")
        msg = self._main_call(chat_id, cur, messages, settings, meter, effort, tool_choice="none")
        if msg.get("reasoning"):
            reasoning_bits.append(msg["reasoning"])
        return message_text(msg), "max_tool_rounds", "\n\n".join(reasoning_bits)

    # ---------- tools for the main model ----------

    def _user_words(self, con, chat_id, cur, user_text, tid):
        """The user's own text for turn tid (the current turn is not stored yet)."""
        if tid == cur:
            return user_text
        row = con.execute("SELECT user FROM turns WHERE chat_id = ? AND tid = ?", (chat_id, tid)).fetchone()
        return row["user"] if row else None

    def _tool(self, chat_id, cur, user_text, name, args, settings, meter, scans, touched):
        if name in ARTIFACT_TOOLS:
            return self._artifact_tool(chat_id, cur, name, args, settings, touched)
        if name == "search_memory":
            query = str(args.get("query") or "").strip()
            if not query:
                return "ERROR: query is empty"
            self._set_busy(chat_id, "กำลังสแกนความจำ")
            result = self.scan(chat_id, cur, query, settings, meter)
            result["trigger"] = "model"
            scans.append(result)
            if not result["shards"]:
                return "No archived IDs yet. Everything is in layer 1."
            lines = [f"shard IDs {s['range'][0]}–{s['range'][1]}: {s['status']}"
                     + (f" ({s.get('error')})" if s["status"] == "failed" else "") for s in result["shards"]]
            if not result["ids"]:
                return "\n".join(lines) + "\nNothing found."
            with self.db() as con:
                rows = con.execute(
                    f"SELECT tid, summary FROM turns WHERE chat_id = ? AND tid IN ({','.join('?' * len(result['ids']))})",
                    (chat_id, *result["ids"])).fetchall()
            return "\n".join(lines) + "\n\n" + "\n".join(format_summary(r["tid"], r["summary"]) for r in rows) \
                + "\n\nThese summaries are now in layer 4. Use read_full if exact details matter."

        if name == "read_full":
            ids = [int(i) for i in (args.get("ids") or [])][:10]
            if not ids:
                return "ERROR: ids is empty"
            out = []
            with self.db() as con:
                for tid in ids:
                    t = con.execute("SELECT * FROM turns WHERE chat_id = ? AND tid = ?", (chat_id, tid)).fetchone()
                    if not t:
                        out.append(f"ID {tid}: does not exist")
                    elif t["summary"] is None:
                        out.append(f"ID {tid}: already in layer 1")
                    else:
                        self._put_retrieved(con, chat_id, tid, cur, settings, "full")
                        out.append(f"ID {tid}\nuser: {clip(t['user'], settings['full_char_limit'])}\n"
                                   f"assistant: {clip(t['answer'], settings['full_char_limit'])}")
            return "\n\n".join(out)

        if name == "pin_condition":
            # Rule 3.5: a condition is the user's own words. The code checks the quote, not the prompt.
            text = str(args.get("text") or "").strip()
            src = int(args.get("source_id"))
            if not text:
                return "ERROR: text is empty"
            if len(text) > 1000:
                return "ERROR: a condition must be under 1,000 characters. Quote only the requirement itself."
            with self.db() as con:
                words = self._user_words(con, chat_id, cur, user_text, src)
                if words is None:
                    return f"ERROR: ID {src} does not exist"
                if squash(text) not in squash(words):
                    return (f"ERROR: the text is not a verbatim quote of the user's message ID {src}. "
                            "Copy the user's exact words; do not paraphrase or translate.")
                replaced = ""
                if args.get("replaces") is not None:
                    old = con.execute("SELECT id FROM state WHERE id = ? AND chat_id = ? AND kind = 'condition' "
                                      "AND active = 1", (int(args["replaces"]), chat_id)).fetchone()
                    if not old:
                        return f"ERROR: no active condition {args['replaces']}"
                    con.execute("UPDATE state SET active = 0 WHERE id = ?", (old["id"],))
                    replaced = f" Retired [C{old['id']}]."
                for r in con.execute("SELECT id, text FROM state WHERE chat_id = ? AND kind = 'condition' "
                                     "AND active = 1", (chat_id,)):
                    if squash(r["text"]) == squash(text):
                        return f"Already pinned as [C{r['id']}].{replaced}"
                new = con.execute(
                    "INSERT INTO state(chat_id, kind, text, source_tid, created_tid, expires_tid, origin) "
                    "VALUES(?, 'condition', ?, ?, ?, NULL, 'model')", (chat_id, text, src, cur)).lastrowid
            return f"Pinned as [C{new}].{replaced}"

        if name == "record_decision":
            text = str(args.get("text") or "").strip()
            src = int(args.get("source_id"))
            if not text:
                return "ERROR: text is empty"
            if not 1 <= src <= cur:
                return f"ERROR: source_id must be between 1 and {cur}"
            with self.db() as con:
                replaced = ""
                if args.get("replaces") is not None:
                    old = con.execute("SELECT id FROM state WHERE id = ? AND chat_id = ? AND kind = 'decision' "
                                      "AND active = 1", (int(args["replaces"]), chat_id)).fetchone()
                    if not old:
                        return f"ERROR: no active decision {args['replaces']}"
                    con.execute("UPDATE state SET active = 0 WHERE id = ?", (old["id"],))
                    replaced = f" Retired [D{old['id']}]."
                for r in con.execute("SELECT id, text FROM state WHERE chat_id = ? AND kind = 'decision' "
                                     "AND active = 1", (chat_id,)):
                    if squash(r["text"]) == squash(text):
                        return f"Already recorded as [D{r['id']}].{replaced}"
                new = con.execute(
                    "INSERT INTO state(chat_id, kind, text, source_tid, created_tid, expires_tid, origin) "
                    "VALUES(?, 'decision', ?, ?, ?, NULL, 'model')", (chat_id, text, src, cur)).lastrowid
            return f"Recorded as [D{new}].{replaced}"

        if name == "add_note":
            text = str(args.get("text") or "").strip()
            src = int(args.get("source_id"))
            ttl = max(1, min(int(args.get("ttl_turns") or settings["note_ttl"]), 50))
            if not text:
                return "ERROR: text is empty"
            with self.db() as con:
                live = src == cur or con.execute(
                    "SELECT 1 FROM turns WHERE chat_id = ? AND tid = ? AND summary IS NULL", (chat_id, src)).fetchone()
                if not live:
                    return f"ERROR: ID {src} is not in layer 1. Notes must cite a live ID."
                new = con.execute(
                    "INSERT INTO state(chat_id, kind, text, source_tid, created_tid, expires_tid, origin) "
                    "VALUES(?, 'note', ?, ?, ?, ?, 'model')", (chat_id, text, src, cur, cur + ttl)).lastrowid
            return f"Added [N{new}], expires after ID {cur + ttl}."

        if name == "retire_state":
            item = int(args.get("item_id"))
            with self.db() as con:
                row = con.execute("SELECT kind FROM state WHERE id = ? AND chat_id = ? AND active = 1",
                                  (item, chat_id)).fetchone()
                if not row:
                    return f"ERROR: no active item {item}"
                if row["kind"] in PINNED_KINDS:
                    # 4a only goes when the user asks for it in this message; the quote proves it.
                    quote = squash(str(args.get("user_quote") or ""))
                    if not quote:
                        return (f"ERROR: [{KIND_LABEL[row['kind']]}{item}] is in 4a. Retire it only when the user "
                                "asks in the current message, and pass their exact words as user_quote.")
                    if quote not in squash(user_text):
                        return "ERROR: user_quote is not a verbatim quote of the user's current message."
                con.execute("UPDATE state SET active = 0 WHERE id = ?", (item,))
            return f"Retired [{KIND_LABEL[row['kind']]}{item}]."

        return f"ERROR: unknown tool {name}"
