"""Gmail synchronisation and persistent cache for the Home Mail add-on."""

from __future__ import annotations

import email
from email import policy
from contextlib import closing
import hashlib
from html.parser import HTMLParser
import imaplib
import json
import os
from pathlib import Path
import re
import sqlite3
import ssl
import smtplib
from email.header import decode_header, make_header
from email.message import EmailMessage, Message
from email.utils import getaddresses, parsedate_to_datetime
from datetime import datetime, timezone


DATA_DIR = Path(os.environ.get("HOME_MAIL_DATA", "/data"))
OPTIONS_FILE = Path(os.environ.get("HOME_MAIL_OPTIONS", "/data/options.json"))
DB_FILE = DATA_DIR / "mail.db"
MAX_TEXT = 500_000
MAX_MAIL_BYTES = 12_000_000
FOLDER_ROLES = ("INBOX", "SENT", "DRAFTS", "JUNK", "TRASH")


class TextFromHtml(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.hidden = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "head"):
            self.hidden += 1
        elif tag in ("br", "p", "div", "li", "tr", "h1", "h2", "h3"):
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in ("script", "style", "head"):
            self.hidden = max(0, self.hidden - 1)
        elif tag in ("p", "div", "li", "tr"):
            self.parts.append("\n")

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)

    def text(self):
        return "".join(self.parts).strip()


def decode_mime(value):
    try:
        return str(make_header(decode_header(value or "")))
    except (LookupError, UnicodeError, email.errors.HeaderParseError):
        return str(value or "")


def options():
    raw = json.loads(OPTIONS_FILE.read_text(encoding="utf-8"))
    address = str(raw.get("gmail_email", "")).strip().lower()
    password = str(raw.get("gmail_app_password", ""))
    interval = max(1, min(60, int(raw.get("sync_interval_minutes", 5))))
    limit = max(10, min(200, int(raw.get("cache_per_folder", 50))))
    return {"email": address, "password": password, "interval": interval, "limit": limit}


def connect_db():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_FILE, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS folders (
            name TEXT PRIMARY KEY, role TEXT NOT NULL, uidvalidity TEXT NOT NULL DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS messages (
            folder TEXT NOT NULL, uid INTEGER NOT NULL, subject TEXT NOT NULL,
            sender TEXT NOT NULL, recipients TEXT NOT NULL, sent_at TEXT NOT NULL,
            flags TEXT NOT NULL, snippet TEXT NOT NULL, body TEXT NOT NULL,
            has_attachments INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (folder, uid)
        );
        CREATE INDEX IF NOT EXISTS messages_order ON messages(folder, uid DESC);
    """)
    return conn


def set_meta(conn, key, value):
    conn.execute("INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, str(value)))


def get_meta(conn, key, default=""):
    row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row[0] if row else default


def ensure_account(conn, address):
    digest = hashlib.sha256(address.encode("utf-8")).hexdigest()
    previous = get_meta(conn, "account")
    if previous != digest:
        conn.execute("DELETE FROM messages")
        conn.execute("DELETE FROM folders")
        set_meta(conn, "account", digest)
        set_meta(conn, "last_sync", "")
        conn.commit()


def parse_folders(lines):
    result = [("INBOX", "INBOX")]
    for line in lines or []:
        if not isinstance(line, bytes):
            continue
        match = re.match(rb'^\(([^)]*)\)\s+(?:"[^"]*"|NIL)\s+(?:"([^"]+)"|(.+))$', line)
        if not match:
            continue
        attributes = match.group(1).upper().split()
        if b"\\NOSELECT" in attributes:
            continue
        name = (match.group(2) or match.group(3)).decode("ascii", errors="replace")
        if name.upper() == "INBOX":
            continue
        role = "OTHER"
        for candidate, flag in (("SENT", b"\\SENT"), ("DRAFTS", b"\\DRAFTS"),
                                ("JUNK", b"\\JUNK"), ("TRASH", b"\\TRASH")):
            if flag in attributes:
                role = candidate
                break
        result.append((name, role))
    order = {role: i for i, role in enumerate(FOLDER_ROLES)}
    return sorted(result, key=lambda item: (order.get(item[1], 10), item[0].lower()))


def text_body(message: Message):
    plain = []
    rich = []
    attachment = False
    for part in message.walk():
        if part.is_multipart():
            continue
        disposition = part.get_content_disposition()
        if disposition == "attachment" or part.get_filename():
            attachment = True
            continue
        if part.get_content_type() not in ("text/plain", "text/html"):
            continue
        payload = part.get_payload(decode=True) or b""
        charset = part.get_content_charset() or "utf-8"
        try:
            text = payload.decode(charset, errors="replace")
        except LookupError:
            text = payload.decode("utf-8", errors="replace")
        if part.get_content_type() == "text/plain":
            plain.append(text)
        else:
            parser = TextFromHtml()
            parser.feed(text)
            rich.append(parser.text())
    value = "\n\n".join(plain or rich).strip()
    return value[:MAX_TEXT], attachment


def parse_message(raw, folder, uid, flags):
    message = email.message_from_bytes(raw, policy=policy.default)
    body, attachment = text_body(message)
    try:
        date = parsedate_to_datetime(str(message.get("Date", "")))
        if date.tzinfo is None:
            date = date.replace(tzinfo=timezone.utc)
        sent_at = date.isoformat()
    except (TypeError, ValueError, IndexError):
        sent_at = ""
    return (folder, uid, decode_mime(message.get("Subject")),
            decode_mime(message.get("From")), decode_mime(message.get("To")),
            sent_at, " ".join(flags), " ".join(body.split())[:180], body, int(attachment))


def _literal(response):
    for item in response or []:
        if isinstance(item, tuple) and len(item) == 2 and isinstance(item[1], bytes):
            return item[1]
    raise RuntimeError("Gmail did not return the message body")


def sync_folder(imap, conn, folder, role, limit):
    status, count_data = imap.select('"' + folder.replace('"', '\\"') + '"', readonly=True)
    if status != "OK":
        raise RuntimeError("Cannot select Gmail folder")
    count = int(count_data[0] or 0)
    validity_data = imap.response("UIDVALIDITY")[1]
    validity = validity_data[0].decode("ascii") if validity_data and validity_data[0] else ""
    if not validity:
        raise RuntimeError("Gmail did not report UIDVALIDITY")
    old = conn.execute("SELECT uidvalidity FROM folders WHERE name=?", (folder,)).fetchone()
    if old and old[0] != validity:
        conn.execute("DELETE FROM messages WHERE folder=?", (folder,))
    conn.execute("INSERT INTO folders(name,role,uidvalidity) VALUES(?,?,?) ON CONFLICT(name) DO UPDATE SET role=excluded.role,uidvalidity=excluded.uidvalidity", (folder, role, validity))
    conn.commit()
    if count == 0:
        conn.execute("DELETE FROM messages WHERE folder=?", (folder,))
        conn.commit()
        return 0
    start = max(1, count - limit + 1)
    status, listing = imap.fetch(f"{start}:{count}", "(UID FLAGS RFC822.SIZE)")
    if status != "OK":
        raise RuntimeError("Cannot list Gmail messages")
    entries = []
    for item in listing or []:
        info = item[0] if isinstance(item, tuple) else item
        if not isinstance(info, bytes):
            continue
        uid_match = re.search(rb"\bUID\s+(\d+)", info)
        flags_match = re.search(rb"\bFLAGS\s+\(([^)]*)\)", info)
        size_match = re.search(rb"\bRFC822\.SIZE\s+(\d+)", info)
        if uid_match:
            entries.append((int(uid_match.group(1)),
                            [value.decode("ascii", "replace") for value in (flags_match.group(1).split() if flags_match else [])],
                            int(size_match.group(1)) if size_match else 0))
    if count and not entries:
        raise RuntimeError("Gmail returned an empty UID listing")
    wanted = [entry[0] for entry in entries]
    fetched = 0
    for uid, flags, size in entries:
        exists = conn.execute("SELECT 1 FROM messages WHERE folder=? AND uid=?", (folder, uid)).fetchone()
        if exists:
            conn.execute("UPDATE messages SET flags=? WHERE folder=? AND uid=?", (" ".join(flags), folder, uid))
            continue
        if size > MAX_MAIL_BYTES:
            continue
        status, response = imap.uid("FETCH", str(uid), "(BODY.PEEK[])")
        if status != "OK":
            raise RuntimeError("Cannot fetch Gmail message")
        parsed = parse_message(_literal(response), folder, uid, flags)
        conn.execute("INSERT OR REPLACE INTO messages VALUES(?,?,?,?,?,?,?,?,?,?)", parsed)
        fetched += 1
    if wanted:
        placeholders = ",".join("?" for _ in wanted)
        conn.execute(f"DELETE FROM messages WHERE folder=? AND uid NOT IN ({placeholders})", [folder, *wanted])
    conn.commit()
    return fetched


def sync_all(cfg, requested_folder=None):
    if not cfg["email"] or not cfg["password"]:
        raise RuntimeError("Set Gmail address and app password in add-on configuration")
    with closing(connect_db()) as conn:
        ensure_account(conn, cfg["email"])
    imap = imaplib.IMAP4_SSL("imap.gmail.com", 993, timeout=45)
    try:
        imap.login(cfg["email"], cfg["password"])
        status, lines = imap.list()
        if status != "OK":
            raise RuntimeError("Cannot list Gmail folders")
        folders = parse_folders(lines)
        with closing(connect_db()) as conn:
            for folder, role in folders:
                conn.execute("INSERT INTO folders(name,role) VALUES(?,?) ON CONFLICT(name) DO UPDATE SET role=excluded.role", (folder, role))
            conn.commit()
            target = [(name, role) for name, role in folders if name == requested_folder] if requested_folder else [item for item in folders if item[1] in FOLDER_ROLES]
            if requested_folder and not target:
                raise ValueError("Unknown folder")
            total = 0
            for name, role in target:
                total += sync_folder(imap, conn, name, role, cfg["limit"])
                set_meta(conn, "last_sync", datetime.now(timezone.utc).isoformat())
                set_meta(conn, "last_error", "")
                conn.commit()
            return total
    finally:
        try:
            imap.logout()
        except (OSError, imaplib.IMAP4.error):
            pass


def send_mail(cfg, recipient, subject, body):
    if not cfg["email"] or not cfg["password"]:
        raise RuntimeError("Gmail is not configured")
    addresses = [address for _, address in getaddresses([recipient])]
    if not addresses or any("@" not in address for address in addresses):
        raise ValueError("Enter a valid recipient")
    if not isinstance(subject, str) or not isinstance(body, str) or len(subject) > 1000 or len(body) > MAX_TEXT:
        raise ValueError("Message is too large")
    message = EmailMessage()
    message["From"] = cfg["email"]
    message["To"] = ", ".join(addresses)
    message["Subject"] = subject
    message.set_content(body)
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=45, context=ssl.create_default_context()) as smtp:
        smtp.login(cfg["email"], cfg["password"])
        smtp.send_message(message)


def set_flag(cfg, folder, uid, flag, enabled):
    if flag not in ("\\Seen", "\\Flagged"):
        raise ValueError("Unsupported flag")
    with closing(connect_db()) as conn:
        valid = conn.execute("SELECT 1 FROM folders WHERE name=?", (folder,)).fetchone()
    if not valid:
        raise ValueError("Unknown folder")
    imap = imaplib.IMAP4_SSL("imap.gmail.com", 993, timeout=45)
    try:
        imap.login(cfg["email"], cfg["password"])
        if imap.select('"' + folder.replace('"', '\\"') + '"')[0] != "OK":
            raise RuntimeError("Cannot select Gmail folder")
        if imap.uid("STORE", str(uid), "+FLAGS" if enabled else "-FLAGS", f"({flag})")[0] != "OK":
            raise RuntimeError("Cannot update message")
        with closing(connect_db()) as conn:
            row = conn.execute("SELECT flags FROM messages WHERE folder=? AND uid=?", (folder, uid)).fetchone()
            if row:
                flags = set(row[0].split())
                flags.add(flag) if enabled else flags.discard(flag)
                conn.execute("UPDATE messages SET flags=? WHERE folder=? AND uid=?", (" ".join(sorted(flags)), folder, uid))
                conn.commit()
    finally:
        try:
            imap.logout()
        except (OSError, imaplib.IMAP4.error):
            pass
