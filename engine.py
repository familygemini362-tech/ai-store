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

DEFAULTS = {
    "api_key": "",
    "model_summarizer": "",
    "model_scanner": "",
    "model_main": "",
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
    compress_at INTEGER, round_size INTEGER);
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
CREATE INDEX IF NOT EXISTS state_chat ON state(chat_id, active);
CREATE INDEX IF NOT EXISTS usage_chat ON usage(chat_id);
"""

ROLE_NAME = {"main": "ตัวทำงานหลัก", "scan": "ตัววิ่งหาข้อมูล", "summarize": "ตัวสรุป"}

KIND_LABEL = {"condition": "C", "decision": "D", "note": "N", "summary": "S", "full": "F"}


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

    def chat(self, model, messages, tools=None, tool_choice=None):
        body = {"model": model, "messages": messages, "usage": {"include": True}}
        if tools:
            body["tools"] = tools
        if tool_choice:
            body["tool_choice"] = tool_choice
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
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                data = json.load(r)
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:600]
            raise LLMError(f"OpenRouter {e.code} ({model}): {detail}") from None
        except urllib.error.URLError as e:
            raise LLMError(f"เชื่อมต่อ OpenRouter ไม่ได้: {e.reason}") from None
        if data.get("error"):
            raise LLMError(f"OpenRouter ({model}): {data['error'].get('message', data['error'])}")
        if not data.get("choices"):
            raise LLMError(f"OpenRouter ({model}) ไม่ส่งคำตอบกลับมา")
        return data["choices"][0]["message"], data.get("usage") or {}


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
        with self.db() as con:
            con.executescript(SCHEMA)
            # Migration for DBs created before per-chat compression overrides existed.
            cols = {r["name"] for r in con.execute("PRAGMA table_info(chats)")}
            if "compress_at" not in cols:
                con.execute("ALTER TABLE chats ADD COLUMN compress_at INTEGER")
            if "round_size" not in cols:
                con.execute("ALTER TABLE chats ADD COLUMN round_size INTEGER")

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
            for table, col in (("turns", "chat_id"), ("state", "chat_id"), ("usage", "chat_id"), ("chats", "id")):
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

    def get_chat(self, chat_id):
        s = self.get_settings()
        with self.db() as con:
            chat = dict(self._require_chat(con, chat_id))
            comp = self._compression(con, chat_id, s)
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
        return chat

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

    def _call(self, chat_id, tid, role, model, messages, meter, settings, **kw):
        if not settings["api_key"]:
            raise UserError("ยังไม่ได้ใส่ OpenRouter API key")
        if not model:
            raise UserError(f"ยังไม่ได้เลือกโมเดลของ{ROLE_NAME[role]}")
        msg, usage = self.client_factory(settings["api_key"]).chat(model, messages, **kw)
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

    def _render_state(self, con, chat_id, cur, settings, scans):
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
        titles = {
            "condition": "Conditions (from the user, always apply)",
            "decision": "Decisions (agreed by the user)",
            "note": "Notes",
            "summary": "Retrieved summaries (layer 2)",
            "full": "Retrieved full text (layer 3)",
        }
        body = "\n".join(head)
        for k, lines in sections.items():
            if lines:
                body += f"\n\n## {titles[k]}\n" + "\n".join(lines)
        cap = settings["state_cap_chars"]
        if len(body) > cap:
            body += f"\n\nWARNING: layer 4 is {len(body):,} chars, over the cap of {cap:,}. Suggest items to retire."
        return f"<memory_state>\n{body}\n</memory_state>"

    # ---------- main turn ----------

    def send(self, chat_id, text, force_scan=False):
        text = (text or "").strip()
        if not text:
            raise UserError("ข้อความว่าง")
        settings = self.get_settings()
        lock = self._lock(self._send_locks, chat_id)
        if not lock.acquire(blocking=False):
            raise BusyError("แชตนี้กำลังตอบอยู่")
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
                state_text = self._render_state(con, chat_id, cur, settings, scans)
            messages = [{"role": "system", "content": prompts.MAIN_SYSTEM}]
            for r in live:
                messages.append({"role": "user", "content": f"[ID {r['tid']}] {r['user']}"})
                messages.append({"role": "assistant", "content": r["answer"] or "(ไม่มีคำตอบ)"})
            messages.append({"role": "user", "content": f"[ID {cur}] {text}\n\n{state_text}"})
            prompt_chars = sum(len(m["content"]) for m in messages)

            answer, stopped = self._run_main(chat_id, cur, messages, settings, meter, trace, scans)
            meta = {
                "scans": scans,
                "prompt_chars": prompt_chars,
                "state_chars": len(state_text),
                "live_ids": [live[0]["tid"], live[-1]["tid"]] if live else [],
                "cost": meter.cost,
                "calls": meter.calls,
                "stopped": stopped,
            }
            with self.db() as con:
                con.execute(
                    "INSERT INTO turns(chat_id, tid, user, answer, tools, meta, summary, created_at) "
                    "VALUES(?, ?, ?, ?, ?, ?, NULL, ?)",
                    (chat_id, cur, text, answer, json.dumps(trace, ensure_ascii=False),
                     json.dumps(meta, ensure_ascii=False), time.time()))
        finally:
            self._set_busy(chat_id, None)
            lock.release()
        self._after_turn(chat_id)
        return {"tid": cur, "user": text, "answer": answer, "tools": trace, "meta": meta, "summary": None}

    def _run_main(self, chat_id, cur, messages, settings, meter, trace, scans):
        model = settings["model_main"]
        last_text = ""
        for _ in range(settings["max_tool_rounds"]):
            if meter.cost >= settings["max_usd_per_message"]:
                return (last_text + "\n\n⚠️ หยุดเพราะใช้งบถึงเพดานต่อข้อความแล้ว").strip(), "budget"
            self._set_busy(chat_id, "โมเดลหลักกำลังคิด")
            msg = self._call(chat_id, cur, "main", model, messages, meter, settings, tools=prompts.TOOLS)
            last_text = message_text(msg)
            calls = msg.get("tool_calls") or []
            if not calls:
                return last_text, None
            messages.append({"role": "assistant", "content": last_text, "tool_calls": calls})
            for c in calls:
                name = c.get("function", {}).get("name", "")
                args = {}
                try:
                    args = json.loads(c["function"].get("arguments") or "{}")
                    self._set_busy(chat_id, f"ใช้เครื่องมือ {name}")
                    result = self._tool(chat_id, cur, name, args, settings, meter, scans)
                except UserError:
                    raise
                except Exception as e:  # noqa: BLE001 - reported back to the model as the tool result
                    result = f"ERROR: {e}"
                trace.append({"name": name, "args": args, "result": clip(result, settings["tool_limit"])})
                messages.append({"role": "tool", "tool_call_id": c.get("id", ""), "content": result})
        self._set_busy(chat_id, "โมเดลหลักกำลังสรุปคำตอบ")
        msg = self._call(chat_id, cur, "main", model, messages, meter, settings,
                         tools=prompts.TOOLS, tool_choice="none")
        return message_text(msg), "max_tool_rounds"

    # ---------- tools for the main model ----------

    def _tool(self, chat_id, cur, name, args, settings, meter, scans):
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

        if name == "record_decision":
            text = str(args.get("text") or "").strip()
            src = int(args.get("source_id"))
            if not text:
                return "ERROR: text is empty"
            if not 1 <= src <= cur:
                return f"ERROR: source_id must be between 1 and {cur}"
            with self.db() as con:
                if args.get("replaces") is not None:
                    old = con.execute("SELECT id FROM state WHERE id = ? AND chat_id = ? AND kind = 'decision' "
                                      "AND active = 1", (int(args["replaces"]), chat_id)).fetchone()
                    if not old:
                        return f"ERROR: no active decision {args['replaces']}"
                    con.execute("UPDATE state SET active = 0 WHERE id = ?", (old["id"],))
                new = con.execute(
                    "INSERT INTO state(chat_id, kind, text, source_tid, created_tid, expires_tid, origin) "
                    "VALUES(?, 'decision', ?, ?, ?, NULL, 'model')", (chat_id, text, src, cur)).lastrowid
            return f"Recorded as [D{new}]."

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
                if row["kind"] == "condition":
                    return "ERROR: conditions can only be removed by the user."
                if row["kind"] == "decision":
                    return "ERROR: replace decisions with record_decision(replaces=...), or ask the user."
                con.execute("UPDATE state SET active = 0 WHERE id = ?", (item,))
            return f"Retired item {item}."

        return f"ERROR: unknown tool {name}"
