"""Local web server for the 4-layer memory chat. Standard library only.

Run:  python server.py            (opens http://127.0.0.1:8765)
      python server.py --port 9000 --no-browser
"""

import argparse
import json
import re
import threading
import time
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from engine import BusyError, Engine, LLMError, Meter, UserError

ROOT = Path(__file__).resolve().parent
STATIC = ROOT / "static"
DATA = ROOT / "data"
CONTENT_TYPES = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
                 ".css": "text/css; charset=utf-8", ".svg": "image/svg+xml"}

engine = None
_models_cache = {"at": 0, "data": None}
_models_lock = threading.Lock()


def fetch_models():
    """OpenRouter's public model list, cached for an hour. Needs no API key."""
    with _models_lock:
        if _models_cache["data"] is not None and time.time() - _models_cache["at"] < 3600:
            return _models_cache["data"]
        req = urllib.request.Request("https://openrouter.ai/api/v1/models", headers={"User-Agent": "memory-web"})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                raw = json.load(r)["data"]
        except Exception as e:  # noqa: BLE001
            raise LLMError(f"ดึงรายชื่อโมเดลไม่ได้: {e}") from None
        models = [{
            "id": m["id"],
            "name": m.get("name", m["id"]),
            "context": m.get("context_length"),
            "prompt": (m.get("pricing") or {}).get("prompt"),
            "completion": (m.get("pricing") or {}).get("completion"),
            "tools": "tools" in (m.get("supported_parameters") or []),
            "reasoning": "reasoning" in (m.get("supported_parameters") or []),
        } for m in raw]
        _models_cache.update(at=time.time(), data=models)
        return models


class Handler(BaseHTTPRequestHandler):
    server_version = "MemoryWeb/1"

    def log_message(self, fmt, *args):
        if not self.path.startswith("/api/chats/") or "/status" not in self.path:
            super().log_message(fmt, *args)

    # ----- plumbing -----

    def _send(self, code, body, ctype="application/json; charset=utf-8", extra=None):
        data = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def _host_ok(self):
        # Blocks DNS-rebinding: only answer requests addressed to this machine by name.
        host = (self.headers.get("Host") or "").rsplit(":", 1)[0].strip("[]")
        return host in ("127.0.0.1", "localhost", "::1")

    def _body(self):
        # A JSON content type forces a CORS preflight, which this server never approves,
        # so other websites cannot post here from the user's browser.
        if not (self.headers.get("Content-Type") or "").startswith("application/json"):
            raise UserError("ต้องส่งเป็น application/json")
        length = int(self.headers.get("Content-Length") or 0)
        if length > 5_000_000:
            raise UserError("ข้อมูลใหญ่เกินไป")
        return json.loads(self.rfile.read(length) or b"{}")

    def _dispatch(self, method):
        if not self._host_ok():
            return self._send(403, {"error": "forbidden host"})
        path = urlparse(self.path).path
        try:
            if method == "GET" and not path.startswith("/api/"):
                return self._static(path)
            for m, pattern, fn in ROUTES:
                if m == method:
                    match = re.fullmatch(pattern, path)
                    if match:
                        body = self._body() if method in ("POST", "PUT", "PATCH") else None
                        args = [int(g) for g in match.groups()]
                        result = fn(self, body, *args)
                        if result is not None:
                            self._send(200, result)
                        return
            self._send(404, {"error": "not found"})
        except UserError as e:
            self._send(400, {"error": str(e)})
        except BusyError as e:
            self._send(409, {"error": str(e)})
        except LLMError as e:
            self._send(502, {"error": str(e)})
        except json.JSONDecodeError:
            self._send(400, {"error": "JSON ไม่ถูกต้อง"})
        except Exception as e:  # noqa: BLE001
            self._send(500, {"error": f"{type(e).__name__}: {e}"})

    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def do_PUT(self):
        self._dispatch("PUT")

    def do_PATCH(self):
        self._dispatch("PATCH")

    def do_DELETE(self):
        self._dispatch("DELETE")

    def _static(self, path):
        name = "index.html" if path in ("/", "") else path.lstrip("/")
        file = (STATIC / name).resolve()
        if STATIC not in file.parents or not file.is_file() or file.suffix not in CONTENT_TYPES:
            return self._send(404, {"error": "not found"})
        self._send(200, file.read_bytes(), CONTENT_TYPES[file.suffix])

    # ----- API -----

    def get_settings(self, _):
        return engine.public_settings()

    def put_settings(self, body):
        return engine.save_settings(body)

    def get_models(self, _):
        return fetch_models()

    def list_chats(self, _):
        return engine.list_chats()

    def create_chat(self, body):
        return {"id": engine.create_chat()}

    def get_chat(self, _, chat_id):
        return engine.get_chat(chat_id)

    def rename_chat(self, body, chat_id):
        engine.rename_chat(chat_id, body.get("title"))
        return {"ok": True}

    def delete_chat(self, _, chat_id):
        engine.delete_chat(chat_id)
        return {"ok": True}

    def status(self, _, chat_id):
        return engine.status(chat_id)

    def send(self, body, chat_id):
        return engine.send(chat_id, body.get("text"), bool(body.get("force_scan")))

    def send_stream(self, body, chat_id):
        """Same as send, but answers with one JSON event per line while the reply is produced.

        Events: status, round (a new model call starts; drop the text shown so far), delta, tool,
        then done (the saved turn) or error. If the browser goes away the turn still finishes and is saved.
        """
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        self.close_connection = True
        lock = threading.Lock()
        gone = False

        def emit(event):
            nonlocal gone
            if gone:
                return
            with lock:
                try:
                    self.wfile.write(json.dumps(event, ensure_ascii=False).encode("utf-8") + b"\n")
                    self.wfile.flush()
                except OSError:
                    gone = True

        try:
            turn = engine.send(chat_id, body.get("text"), bool(body.get("force_scan")), on_event=emit)
            emit({"type": "done", "turn": turn})
        except (UserError, BusyError, LLMError) as e:
            emit({"type": "error", "error": str(e)})
        except Exception as e:  # noqa: BLE001
            emit({"type": "error", "error": f"{type(e).__name__}: {e}"})

    def summarize(self, body, chat_id):
        done = engine.summarize_pending(chat_id, force=bool(body.get("force")))
        st = engine.status(chat_id)
        if st["last_error"]:
            raise LLMError(st["last_error"])
        return {"summarized": done}

    def scan(self, body, chat_id):
        query = (body.get("query") or "").strip()
        if not query:
            raise UserError("ใส่คำถามที่จะสแกนก่อน")
        meter = Meter()
        result = engine.scan(chat_id, 0, query, engine.get_settings(), meter, record=False)
        result["cost"] = meter.cost
        return result

    def put_summary(self, body, chat_id, tid):
        engine.update_summary(chat_id, tid, body)
        return {"ok": True}

    def add_state(self, body, chat_id):
        src = body.get("source_tid")
        engine.add_state(chat_id, body.get("kind"), body.get("text"), int(src) if src not in (None, "") else None)
        return {"ok": True}

    def patch_state(self, body, chat_id, item_id):
        engine.update_state(chat_id, item_id, body.get("text"), body.get("active"))
        return {"ok": True}

    def put_compression(self, body, chat_id):
        compression = engine.set_compression(chat_id, body.get("compress_at"), body.get("round_size"))
        return {"compression": compression}

    def put_effort(self, body, chat_id):
        effort = engine.set_effort(chat_id, body.get("effort"))
        return {"effort": effort}

    def list_artifacts(self, _, chat_id):
        return engine.list_artifacts(chat_id)

    def get_artifact(self, _, chat_id, artifact_id, version=None):
        return engine.get_artifact(chat_id, artifact_id, version)

    def export(self, _, chat_id):
        data = json.dumps(engine.export_chat(chat_id), ensure_ascii=False, indent=2).encode("utf-8")
        self._send(200, data, "application/json; charset=utf-8",
                   {"Content-Disposition": f'attachment; filename="chat-{chat_id}.json"'})


ROUTES = [
    ("GET", r"/api/settings", Handler.get_settings),
    ("PUT", r"/api/settings", Handler.put_settings),
    ("GET", r"/api/models", Handler.get_models),
    ("GET", r"/api/chats", Handler.list_chats),
    ("POST", r"/api/chats", Handler.create_chat),
    ("GET", r"/api/chats/(\d+)", Handler.get_chat),
    ("PATCH", r"/api/chats/(\d+)", Handler.rename_chat),
    ("DELETE", r"/api/chats/(\d+)", Handler.delete_chat),
    ("GET", r"/api/chats/(\d+)/status", Handler.status),
    ("POST", r"/api/chats/(\d+)/send", Handler.send),
    ("POST", r"/api/chats/(\d+)/send_stream", Handler.send_stream),
    ("POST", r"/api/chats/(\d+)/summarize", Handler.summarize),
    ("POST", r"/api/chats/(\d+)/scan", Handler.scan),
    ("PUT", r"/api/chats/(\d+)/summaries/(\d+)", Handler.put_summary),
    ("PUT", r"/api/chats/(\d+)/compression", Handler.put_compression),
    ("PUT", r"/api/chats/(\d+)/effort", Handler.put_effort),
    ("GET", r"/api/chats/(\d+)/artifacts", Handler.list_artifacts),
    ("GET", r"/api/chats/(\d+)/artifacts/(\d+)", Handler.get_artifact),
    ("GET", r"/api/chats/(\d+)/artifacts/(\d+)/versions/(\d+)", Handler.get_artifact),
    ("POST", r"/api/chats/(\d+)/state", Handler.add_state),
    ("PATCH", r"/api/chats/(\d+)/state/(\d+)", Handler.patch_state),
    ("GET", r"/api/chats/(\d+)/export", Handler.export),
]


def main():
    global engine
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--db", default=str(DATA / "memory.db"))
    ap.add_argument("--no-browser", action="store_true")
    args = ap.parse_args()
    Path(args.db).parent.mkdir(parents=True, exist_ok=True)
    engine = Engine(args.db)
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    url = f"http://127.0.0.1:{args.port}"
    print(f"4-Layer Memory: {url}  (data: {args.db})  Ctrl+C to stop")
    if not args.no_browser:
        threading.Timer(0.5, webbrowser.open, (url,)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
