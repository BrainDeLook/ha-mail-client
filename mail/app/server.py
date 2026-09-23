"""Ingress-only HTTP interface and background sync for Home Mail."""

from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import imaplib
import json
from pathlib import Path
import queue
import sqlite3
import smtplib
import threading
from contextlib import closing
from urllib.parse import parse_qs, urlsplit

import core


ROOT = Path(__file__).parent
SYNC_QUEUE = queue.Queue(maxsize=5)
SYNC_STATE = {"running": False}
SYNC_LOCK = threading.Lock()
MAX_REQUEST = 600_000


def sync_worker():
    requested_folder = None
    while True:
        try:
            cfg = core.options()
            with SYNC_LOCK:
                SYNC_STATE["running"] = True
            count = core.sync_all(cfg, requested_folder)
            print(f"[INFO] Gmail sync: {count} new messages", flush=True)
        except (FileNotFoundError, json.JSONDecodeError, ValueError, OSError,
                imaplib.IMAP4.error, RuntimeError, sqlite3.Error) as exc:
            if isinstance(exc, imaplib.IMAP4.error):
                message = "Gmail authentication or IMAP error"
            elif isinstance(exc, (FileNotFoundError, json.JSONDecodeError)):
                message = "Add-on configuration unavailable"
            elif isinstance(exc, (OSError, sqlite3.Error)):
                message = "Mail server or database unavailable"
            else:
                message = str(exc)
            print(f"[WARN] Gmail sync: {message}", flush=True)
            try:
                with closing(core.connect_db()) as conn:
                    core.set_meta(conn, "last_error", message)
                    conn.commit()
            except (OSError, sqlite3.Error):
                pass
        finally:
            with SYNC_LOCK:
                SYNC_STATE["running"] = False
        try:
            timeout = core.options()["interval"] * 60
        except (OSError, ValueError, json.JSONDecodeError):
            timeout = 300
        try:
            requested_folder = SYNC_QUEUE.get(timeout=timeout)
        except queue.Empty:
            requested_folder = None


class Handler(BaseHTTPRequestHandler):
    server_version = "HomeMail/0.1.0"

    def log_message(self, format, *args):
        # Avoid logging Ingress tokens, search terms or message details.
        print(f"[HTTP] {self.client_address[0]} {self.command} {urlsplit(self.path).path}", flush=True)

    def common_headers(self, content_type, length):
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(length))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'self'; base-uri 'none'; form-action 'none'")

    def reply(self, code, value):
        payload = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(code)
        self.common_headers("application/json; charset=utf-8", len(payload))
        self.end_headers()
        self.wfile.write(payload)

    def static(self, name, content_type):
        payload = (ROOT / name).read_bytes()
        self.send_response(200)
        self.common_headers(content_type, len(payload))
        self.end_headers()
        self.wfile.write(payload)

    def account_db(self):
        cfg = core.options()
        conn = core.connect_db()
        core.ensure_account(conn, cfg["email"])
        return cfg, conn

    def do_GET(self):
        try:
            url = urlsplit(self.path)
            if url.path in ("/", "/index.html"):
                return self.static("index.html", "text/html; charset=utf-8")
            if url.path == "/app.css":
                return self.static("app.css", "text/css; charset=utf-8")
            if url.path == "/app.js":
                return self.static("app.js", "text/javascript; charset=utf-8")
            if not url.path.startswith("/api/"):
                return self.reply(404, {"error": "Not found"})
            query = parse_qs(url.query)
            cfg, conn = self.account_db()
            with closing(conn):
                if url.path == "/api/status":
                    return self.reply(200, {"email": cfg["email"], "configured": bool(cfg["email"] and cfg["password"]),
                                            "last_sync": core.get_meta(conn, "last_sync"),
                                            "last_error": core.get_meta(conn, "last_error"),
                                            "syncing": SYNC_STATE["running"]})
                if url.path == "/api/folders":
                    rows = conn.execute("""SELECT f.name,f.role,COUNT(m.uid) AS total,
                        COALESCE(SUM(CASE WHEN m.flags NOT LIKE '%\\Seen%' THEN 1 ELSE 0 END),0) AS unread
                        FROM folders f LEFT JOIN messages m ON m.folder=f.name
                        GROUP BY f.name ORDER BY CASE f.role WHEN 'INBOX' THEN 0 WHEN 'SENT' THEN 1
                        WHEN 'DRAFTS' THEN 2 WHEN 'JUNK' THEN 3 WHEN 'TRASH' THEN 4 ELSE 5 END,f.name""").fetchall()
                    return self.reply(200, {"folders": [dict(row) for row in rows]})
                if url.path in ("/api/messages", "/api/message"):
                    folder = query.get("folder", ["INBOX"])[0]
                    if len(folder) > 255 or not conn.execute("SELECT 1 FROM folders WHERE name=?", (folder,)).fetchone():
                        raise ValueError("Unknown folder")
                    if url.path == "/api/messages":
                        search = query.get("search", [""])[0][:100]
                        if search:
                            pattern = "%" + search.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
                            rows = conn.execute("""SELECT uid,subject,sender,recipients,sent_at,flags,snippet,has_attachments
                                FROM messages WHERE folder=? AND (subject LIKE ? ESCAPE '\\' OR sender LIKE ? ESCAPE '\\' OR body LIKE ? ESCAPE '\\')
                                ORDER BY uid DESC LIMIT 200""", (folder, pattern, pattern, pattern)).fetchall()
                        else:
                            rows = conn.execute("""SELECT uid,subject,sender,recipients,sent_at,flags,snippet,has_attachments
                                FROM messages WHERE folder=? ORDER BY uid DESC LIMIT 200""", (folder,)).fetchall()
                        return self.reply(200, {"messages": [dict(row) for row in rows]})
                    uid = int(query.get("uid", ["0"])[0])
                    row = conn.execute("SELECT * FROM messages WHERE folder=? AND uid=?", (folder, uid)).fetchone()
                    return self.reply(200, {"message": dict(row)}) if row else self.reply(404, {"error": "Message is not cached"})
            return self.reply(404, {"error": "Not found"})
        except (ValueError, KeyError) as exc:
            return self.reply(400, {"error": str(exc)})
        except (OSError, sqlite3.Error, json.JSONDecodeError) as exc:
            print(f"[WARN] GET failed: {type(exc).__name__}", flush=True)
            return self.reply(503, {"error": "Mail data unavailable"})

    def do_POST(self):
        if self.headers.get("Content-Type", "").split(";")[0].strip().lower() != "application/json":
            return self.reply(415, {"error": "JSON required"})
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= MAX_REQUEST:
                return self.reply(413, {"error": "Invalid request size"})
            value = json.loads(self.rfile.read(length))
            if not isinstance(value, dict):
                raise ValueError("Invalid request")
            path = urlsplit(self.path).path
            if path == "/api/sync":
                folder = value.get("folder")
                if folder is not None:
                    if not isinstance(folder, str) or len(folder) > 255:
                        raise ValueError("Invalid folder")
                    _, conn = self.account_db()
                    with closing(conn):
                        if not conn.execute("SELECT 1 FROM folders WHERE name=?", (folder,)).fetchone():
                            raise ValueError("Unknown folder")
                try:
                    SYNC_QUEUE.put_nowait(folder)
                except queue.Full:
                    pass
                return self.reply(202, {"queued": True})
            cfg, conn = self.account_db()
            conn.close()
            if path == "/api/send":
                core.send_mail(cfg, str(value.get("to", "")), str(value.get("subject", "")), str(value.get("body", "")))
                try:
                    SYNC_QUEUE.put_nowait(None)
                except queue.Full:
                    pass
                return self.reply(200, {"sent": True})
            if path == "/api/flag":
                folder = str(value.get("folder", ""))
                uid = int(value.get("uid", 0))
                flag = str(value.get("flag", ""))
                if uid < 1:
                    raise ValueError("Invalid message")
                core.set_flag(cfg, folder, uid, flag, value.get("enabled") is True)
                return self.reply(200, {"updated": True})
            return self.reply(404, {"error": "Not found"})
        except (ValueError, TypeError, KeyError) as exc:
            return self.reply(400, {"error": str(exc)})
        except (imaplib.IMAP4.error, smtplib.SMTPException):
            return self.reply(502, {"error": "Gmail authentication or mail server error"})
        except (OSError, sqlite3.Error, RuntimeError, json.JSONDecodeError) as exc:
            print(f"[WARN] POST failed: {type(exc).__name__}", flush=True)
            return self.reply(503, {"error": "Mail server unavailable"})


if __name__ == "__main__":
    with closing(core.connect_db()):
        pass
    threading.Thread(target=sync_worker, name="gmail-sync", daemon=True).start()
    print("[INFO] Home Mail listening on 8099", flush=True)
    ThreadingHTTPServer(("0.0.0.0", 8099), Handler).serve_forever()
