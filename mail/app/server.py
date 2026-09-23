"""Ingress-only HTTP interface and background sync for Home Mail."""

from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import imaplib
import json
import logging
from pathlib import Path
import queue
import sqlite3
import smtplib
import sys
import threading
import time
from contextlib import closing
from urllib.parse import parse_qs, quote, urlsplit

import core


ROOT = Path(__file__).parent
SYNC_QUEUE = queue.Queue(maxsize=5)
SYNC_STATE = {"running": False, "folder": None}
SYNC_PENDING = set()
SYNC_LOCK = threading.Lock()
MAX_REQUEST = 600_000
LOGGER = logging.getLogger("home-mail")
LOGGER.propagate = False
LOG_LEVELS = {"error": logging.ERROR, "warning": logging.WARNING,
              "info": logging.INFO, "debug": logging.DEBUG}


def configure_logging(level):
    if not LOGGER.handlers:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(logging.Formatter("[%(levelname)s] %(message)s"))
        LOGGER.addHandler(handler)
    LOGGER.setLevel(LOG_LEVELS.get(level, logging.INFO))


def enqueue_sync(folder):
    with SYNC_LOCK:
        if folder in SYNC_PENDING or (SYNC_STATE["running"] and SYNC_STATE["folder"] == folder):
            return False
        try:
            SYNC_QUEUE.put_nowait(folder)
        except queue.Full:
            return False
        SYNC_PENDING.add(folder)
        return True


def sync_worker():
    next_full = 0.0
    failures = 0
    while True:
        now = time.monotonic()
        if now >= next_full:
            requested_folder, periodic = None, True
        else:
            try:
                requested_folder = SYNC_QUEUE.get(timeout=next_full - now)
            except queue.Empty:
                continue
            periodic = False
            with SYNC_LOCK:
                SYNC_PENDING.discard(requested_folder)
        try:
            cfg = core.options()
            configure_logging(cfg["log_level"])
            with SYNC_LOCK:
                SYNC_STATE["running"] = True
                SYNC_STATE["folder"] = requested_folder
            count = core.sync_all(cfg, requested_folder)
            if count:
                LOGGER.info("Gmail sync: %d messages added to cache", count)
            else:
                LOGGER.debug("Gmail sync: no new messages")
            failures = 0
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
            LOGGER.warning("Gmail sync: %s", message)
            failures += 1
            try:
                with closing(core.connect_db()) as conn:
                    core.set_meta(conn, "last_error", message)
                    conn.commit()
            except (OSError, sqlite3.Error):
                pass
        finally:
            with SYNC_LOCK:
                SYNC_STATE["running"] = False
                SYNC_STATE["folder"] = None
        if periodic:
            try:
                interval = core.options()["interval"] * 60
            except (OSError, ValueError, json.JSONDecodeError):
                interval = 300
            delay = interval if not failures else min(900, interval * 2 ** min(failures - 1, 3))
            next_full = time.monotonic() + delay


class Handler(BaseHTTPRequestHandler):
    server_version = "HomeMail/1.0.0"

    def log_message(self, format, *args):
        # Avoid logging Ingress tokens, search terms or message details.
        LOGGER.debug("HTTP %s %s", self.command, urlsplit(self.path).path)

    def common_headers(self, content_type, length):
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(length))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' http: https: data:; media-src 'self' http: https: data:; frame-src 'self' about:; connect-src 'self'; frame-ancestors 'self'; object-src 'none'; base-uri 'none'; form-action 'none'")

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

    def part(self, row):
        content_type = row["content_type"] if row["content_type"] in core.INLINE_MEDIA_TYPES else "application/octet-stream"
        payload = row["content"]
        self.send_response(200)
        self.common_headers(content_type, len(payload))
        if content_type == "application/octet-stream":
            filename = row["filename"] or "attachment"
            self.send_header("Content-Disposition", "attachment; filename=\"attachment\"; filename*=UTF-8''" + quote(filename))
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
            if url.path == "/dark.css":
                return self.static("dark.css", "text/css; charset=utf-8")
            if url.path == "/app.js":
                return self.static("app.js", "text/javascript; charset=utf-8")
            if url.path == "/icon.png":
                return self.static("icon.png", "image/png")
            if not url.path.startswith("/api/"):
                return self.reply(404, {"error": "Not found"})
            query = parse_qs(url.query)
            cfg, conn = self.account_db()
            with closing(conn):
                if url.path == "/api/status":
                    return self.reply(200, {"email": cfg["email"], "configured": bool(cfg["email"] and cfg["password"]),
                                            "theme": cfg["theme"],
                                            "show_external_media": cfg["external_media"],
                                            "last_sync": core.get_meta(conn, "last_sync"),
                                            "cache_revision": int(core.get_meta(conn, "cache_revision", "0")),
                                            "last_error": core.get_meta(conn, "last_error"),
                                            "syncing": SYNC_STATE["running"],
                                            "sync_queued": SYNC_QUEUE.qsize() > 0})
                if url.path == "/api/folders":
                    rows = conn.execute("""SELECT f.name,f.role,f.label,COUNT(m.uid) AS total,
                        COALESCE(SUM(CASE WHEN m.flags NOT LIKE '%\\Seen%' THEN 1 ELSE 0 END),0) AS unread
                        FROM folders f LEFT JOIN messages m ON m.folder=f.name
                        GROUP BY f.name ORDER BY CASE f.role WHEN 'INBOX' THEN 0 WHEN 'IMPORTANT' THEN 1
                        WHEN 'FLAGGED' THEN 2 WHEN 'ALL' THEN 3 WHEN 'SENT' THEN 4
                        WHEN 'DRAFTS' THEN 5 WHEN 'JUNK' THEN 6 WHEN 'TRASH' THEN 7 ELSE 8 END,f.label""").fetchall()
                    return self.reply(200, {"folders": [dict(row) for row in rows]})
                if url.path == "/api/part":
                    folder = query.get("folder", [""])[0]
                    uid = int(query.get("uid", ["0"])[0])
                    part_id = int(query.get("part", ["-1"])[0])
                    row = conn.execute("SELECT content_type,filename,content FROM parts WHERE folder=? AND uid=? AND part_id=?",
                                       (folder, uid, part_id)).fetchone()
                    return self.part(row) if row else self.reply(404, {"error": "Media not cached"})
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
                    row = conn.execute("""SELECT folder,uid,subject,sender,recipients,sent_at,flags,snippet,body,
                        has_attachments,raw_html FROM messages WHERE folder=? AND uid=?""", (folder, uid)).fetchone()
                    if not row:
                        return self.reply(404, {"error": "Message is not cached"})
                    message = dict(row)
                    raw_html = message.pop("raw_html") or ""
                    parts = conn.execute("SELECT part_id,filename,content_type,cid FROM parts WHERE folder=? AND uid=? ORDER BY part_id",
                                         (folder, uid)).fetchall()
                    message["parts"] = [{"part_id": part["part_id"], "filename": part["filename"],
                                         "content_type": part["content_type"]} for part in parts if part["filename"]]
                    cids = {part["cid"]: part["part_id"] for part in parts if part["cid"]}
                    message["html"] = core.safe_html(raw_html, folder, uid, cids,
                                                      query.get("remote", ["0"])[0] == "1") if raw_html else ""
                    return self.reply(200, {"message": message})
            return self.reply(404, {"error": "Not found"})
        except (ValueError, KeyError) as exc:
            return self.reply(400, {"error": str(exc)})
        except (OSError, sqlite3.Error, json.JSONDecodeError) as exc:
            LOGGER.warning("GET failed: %s", type(exc).__name__)
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
                return self.reply(202, {"queued": enqueue_sync(folder)})
            cfg, conn = self.account_db()
            conn.close()
            if path == "/api/send":
                core.send_mail(cfg, str(value.get("to", "")), str(value.get("subject", "")), str(value.get("body", "")))
                enqueue_sync(None)
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
            LOGGER.warning("POST failed: %s", type(exc).__name__)
            return self.reply(503, {"error": "Mail server unavailable"})


if __name__ == "__main__":
    try:
        configure_logging(core.options()["log_level"])
    except (OSError, ValueError, json.JSONDecodeError):
        configure_logging("info")
    with closing(core.connect_db()):
        pass
    threading.Thread(target=sync_worker, name="gmail-sync", daemon=True).start()
    LOGGER.info("Home Mail listening on 8099")
    ThreadingHTTPServer(("0.0.0.0", 8099), Handler).serve_forever()
