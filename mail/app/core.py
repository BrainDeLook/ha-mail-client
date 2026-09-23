"""Gmail synchronisation and persistent cache for the Home Mail add-on."""

from __future__ import annotations

import email
import base64
from email import policy
from contextlib import closing
import hashlib
import html
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
from urllib.parse import unquote, urlencode, urlsplit


DATA_DIR = Path(os.environ.get("HOME_MAIL_DATA", "/data"))
OPTIONS_FILE = Path(os.environ.get("HOME_MAIL_OPTIONS", "/data/options.json"))
DB_FILE = DATA_DIR / "mail.db"
MAX_TEXT = 500_000
MAX_MAIL_BYTES = 12_000_000
MAX_PART_BYTES = 10_000_000
FOLDER_ROLES = ("INBOX", "IMPORTANT", "FLAGGED", "ALL", "SENT", "DRAFTS", "JUNK", "TRASH")
AUTO_SYNC_ROLES = ("INBOX", "SENT", "DRAFTS", "JUNK", "TRASH")
INLINE_MEDIA_TYPES = ("image/png", "image/jpeg", "image/gif", "image/webp",
                      "audio/mpeg", "audio/ogg", "audio/wav", "audio/mp4",
                      "video/mp4", "video/webm")


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
    default_folder = str(raw.get("default_folder", "INBOX")).upper()
    if default_folder not in FOLDER_ROLES:
        default_folder = "INBOX"
    theme = str(raw.get("theme", "system")).lower()
    if theme not in ("system", "light", "dark", "ha_dark"):
        theme = "system"
    view_mode = str(raw.get("view_mode", "split")).lower()
    if view_mode not in ("split", "list"):
        view_mode = "split"
    log_level = str(raw.get("log_level", "info")).lower()
    if log_level not in ("error", "warning", "info", "debug"):
        log_level = "info"
    external_media = raw.get("show_external_media", True)
    return {"email": address, "password": password, "interval": interval, "limit": limit,
            "theme": theme, "view_mode": view_mode, "default_folder": default_folder,
            "external_media": external_media is True, "log_level": log_level}


def connect_db():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_FILE, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS folders (
            name TEXT PRIMARY KEY, role TEXT NOT NULL, uidvalidity TEXT NOT NULL DEFAULT '',
            label TEXT NOT NULL DEFAULT '', last_uid INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS messages (
            folder TEXT NOT NULL, uid INTEGER NOT NULL, subject TEXT NOT NULL,
            sender TEXT NOT NULL, recipients TEXT NOT NULL, sent_at TEXT NOT NULL,
            flags TEXT NOT NULL, snippet TEXT NOT NULL, body TEXT NOT NULL,
            has_attachments INTEGER NOT NULL DEFAULT 0, raw_html TEXT,
            PRIMARY KEY (folder, uid)
        );
        CREATE TABLE IF NOT EXISTS parts (
            folder TEXT NOT NULL, uid INTEGER NOT NULL, part_id INTEGER NOT NULL,
            content_type TEXT NOT NULL, filename TEXT NOT NULL, cid TEXT NOT NULL,
            content BLOB NOT NULL,
            PRIMARY KEY(folder,uid,part_id),
            FOREIGN KEY(folder,uid) REFERENCES messages(folder,uid) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS messages_order ON messages(folder, uid DESC);
    """)
    if "label" not in {row[1] for row in conn.execute("PRAGMA table_info(folders)")}:
        conn.execute("ALTER TABLE folders ADD COLUMN label TEXT NOT NULL DEFAULT ''")
    if "last_uid" not in {row[1] for row in conn.execute("PRAGMA table_info(folders)")}:
        conn.execute("ALTER TABLE folders ADD COLUMN last_uid INTEGER NOT NULL DEFAULT 0")
    if "raw_html" not in {row[1] for row in conn.execute("PRAGMA table_info(messages)")}:
        conn.execute("ALTER TABLE messages ADD COLUMN raw_html TEXT")
    conn.commit()
    return conn


def set_meta(conn, key, value):
    conn.execute("INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, str(value)))


def get_meta(conn, key, default=""):
    row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row[0] if row else default


def bump_cache_revision(conn):
    revision = int(get_meta(conn, "cache_revision", "0")) + 1
    set_meta(conn, "cache_revision", revision)
    return revision


def reconcile_key(folder):
    return "reconcile:" + hashlib.sha256(folder.encode("utf-8")).hexdigest()[:20]


def cache_limit_key(folder):
    return "cache_limit:" + hashlib.sha256(folder.encode("utf-8")).hexdigest()[:20]


def ensure_account(conn, address):
    digest = hashlib.sha256(address.encode("utf-8")).hexdigest()
    previous = get_meta(conn, "account")
    if previous != digest:
        conn.execute("DELETE FROM messages")
        conn.execute("DELETE FROM folders")
        conn.execute("DELETE FROM meta WHERE key LIKE 'reconcile:%'")
        conn.execute("DELETE FROM meta WHERE key LIKE 'cache_limit:%'")
        set_meta(conn, "account", digest)
        set_meta(conn, "last_sync", "")
        bump_cache_revision(conn)
        conn.commit()


def decode_imap_utf7(value):
    """Decode the modified UTF-7 mailbox names used by Gmail's IMAP LIST."""
    def replacement(match):
        chunk = match.group(1)
        if not chunk:
            return "&"
        try:
            encoded = chunk.replace(",", "/")
            encoded += "=" * (-len(encoded) % 4)
            return base64.b64decode(encoded).decode("utf-16-be")
        except (ValueError, UnicodeError):
            return match.group(0)
    return re.sub(r"&([A-Za-z0-9+,]*)-", replacement, value)


def parse_folders(lines):
    result = [("INBOX", "INBOX", "Входящие")]
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
        for candidate, flag in (("IMPORTANT", b"\\IMPORTANT"), ("FLAGGED", b"\\FLAGGED"),
                                ("ALL", b"\\ALL"), ("SENT", b"\\SENT"),
                                ("DRAFTS", b"\\DRAFTS"), ("JUNK", b"\\JUNK"),
                                ("TRASH", b"\\TRASH")):
            if flag in attributes:
                role = candidate
                break
        label = decode_imap_utf7(name)
        if label.startswith("[Gmail]/"):
            label = label[len("[Gmail]/"):]
        result.append((name, role, label))
    order = {role: i for i, role in enumerate(FOLDER_ROLES)}
    return sorted(result, key=lambda item: (order.get(item[1], 10), item[2].lower()))


def text_body(message: Message):
    plain = []
    rich = []
    raw_html = []
    parts = []
    attachment = False
    for part in message.walk():
        if part.is_multipart():
            continue
        disposition = part.get_content_disposition()
        content_type = part.get_content_type().lower()
        if disposition == "attachment" or part.get_filename() or content_type not in ("text/plain", "text/html"):
            attachment = True
            payload = part.get_payload(decode=True) or b""
            if len(payload) <= MAX_PART_BYTES:
                cid = (part.get("Content-ID") or "").strip().strip("<>").lower()
                parts.append((content_type, decode_mime(part.get_filename() or ""), cid, payload))
            continue
        payload = part.get_payload(decode=True) or b""
        charset = part.get_content_charset() or "utf-8"
        try:
            text = payload.decode(charset, errors="replace")
        except LookupError:
            text = payload.decode("utf-8", errors="replace")
        if content_type == "text/plain":
            plain.append(text)
        else:
            raw_html.append(text[:MAX_TEXT])
            parser = TextFromHtml()
            parser.feed(text)
            rich.append(parser.text())
    value = "\n\n".join(plain or rich).strip()
    return value[:MAX_TEXT], attachment, "\n".join(raw_html)[:MAX_TEXT], parts


def parse_message(raw, folder, uid, flags):
    message = email.message_from_bytes(raw, policy=policy.default)
    body, attachment, raw_html, parts = text_body(message)
    try:
        date = parsedate_to_datetime(str(message.get("Date", "")))
        if date.tzinfo is None:
            date = date.replace(tzinfo=timezone.utc)
        sent_at = date.isoformat()
    except (TypeError, ValueError, IndexError):
        sent_at = ""
    record = (folder, uid, decode_mime(message.get("Subject")),
              decode_mime(message.get("From")), decode_mime(message.get("To")),
              sent_at, " ".join(flags), " ".join(body.split())[:180], body, int(attachment), raw_html)
    return record, parts


class SafeMailHtml(HTMLParser):
    """Small allowlist renderer; email HTML is never trusted as application HTML."""

    TAGS = {"a", "audio", "b", "blockquote", "body", "br", "center", "code", "div", "em", "h1", "h2", "h3",
            "h4", "hr", "i", "img", "li", "ol", "p", "pre", "s", "small", "source", "span",
            "strong", "table", "tbody", "td", "th", "thead", "tr", "u", "ul", "video"}
    VOID = {"br", "hr", "img", "source"}
    HIDDEN = {"script", "style", "head", "title", "iframe", "object", "embed", "form", "svg", "math", "template"}
    STYLE_PROPERTIES = {"background", "background-color", "background-image", "background-position",
                        "background-repeat", "background-size", "border", "border-bottom", "border-collapse",
                        "border-color", "border-left", "border-radius", "border-right", "border-top",
                        "border-width", "color", "display", "font", "font-family", "font-size",
                        "font-style", "font-weight", "height", "letter-spacing", "line-height",
                        "margin", "margin-bottom", "margin-left", "margin-right", "margin-top",
                        "max-width", "min-width", "padding", "padding-bottom", "padding-left",
                        "padding-right", "padding-top", "text-align", "text-decoration", "vertical-align",
                        "white-space", "width"}

    def safe_stylesheet(self, source):
        rules = []
        # Only simple selectors can reach the isolated message body. At-rules,
        # imports, pseudo-selectors and browser-wide selectors are discarded.
        for selector, declarations in re.findall(r"([^{}]+)\{([^{}]*)\}", source[:100_000]):
            names = []
            for item in selector.split(","):
                item = item.strip()
                if not item or len(item) > 180 or not re.fullmatch(r"[A-Za-z0-9_#.\-\s>+*]+", item):
                    continue
                if item.lower() in ("html", "body"):
                    names.append(".mail-content")
                else:
                    names.append(".mail-content " + item)
            style = self.safe_style(declarations)
            if names and style:
                rules.append(",".join(names) + "{" + style + "}")
            if len(rules) >= 200:
                break
        return "".join(rules)

    def safe_style(self, value):
        declarations = []
        for item in value.split(";"):
            name, separator, content = item.partition(":")
            name, content = name.strip().lower(), content.strip()
            if not separator or name not in self.STYLE_PROPERTIES or len(content) > 1000:
                continue
            if name in ("background", "background-image") and "url(" in content.lower():
                pattern = r"url\(\s*(['\"]?)([^'\"()]+)\1\s*\)"
                match = re.search(pattern, content, re.I) if name == "background" else re.fullmatch(pattern, content, re.I)
                if match:
                    source = self.media_url(match.group(2))
                    if source:
                        declarations.append(f"background-image:url({json.dumps(source)})")
                    if name == "background":
                        color = re.search(r"#[0-9a-fA-F]{3,8}\b", content[:match.start()] + content[match.end():])
                        if color:
                            declarations.append("background-color:" + color.group())
                continue
            if len(content) > 160:
                continue
            if not re.fullmatch(r"[\w\s#.,%()/'\"+\-]*", content, flags=re.ASCII):
                continue
            if re.search(r"url\s*\(|expression\s*\(|var\s*\(|(?:image|attr)\s*\(", content, re.I):
                continue
            declarations.append(f"{name}:{content}")
        return ";".join(declarations)

    def __init__(self, folder, uid, cid_parts, allow_remote):
        super().__init__(convert_charrefs=True)
        self.folder = folder
        self.uid = uid
        self.cid_parts = cid_parts
        self.allow_remote = allow_remote
        self.parts = []
        self.hidden = 0

    def media_url(self, value):
        if not value:
            return ""
        value = value.strip()
        if any(char in value for char in "<>\\\r\n"):
            return ""
        if value.lower().startswith("cid:"):
            cid = unquote(value[4:]).strip("<>").lower()
            part_id = self.cid_parts.get(cid)
            return "api/part?" + urlencode({"folder": self.folder, "uid": self.uid, "part": part_id}) if part_id is not None else ""
        parsed = urlsplit(value)
        if self.allow_remote and parsed.scheme.lower() == "https" and parsed.netloc:
            return value
        if value.lower().startswith(("data:image/png;base64,", "data:image/jpeg;base64,",
                                     "data:image/gif;base64,", "data:image/webp;base64,")) and len(value) <= 200_000:
            return value
        return ""

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        if tag in self.HIDDEN:
            self.hidden += 1
            return
        if self.hidden or tag not in self.TAGS:
            return
        values = dict(attrs)
        safe = []
        for key in ("class", "id"):
            value = (values.get(key) or "").strip()
            if value and len(value) <= 300 and re.fullmatch(r"[A-Za-z0-9_\-\s]+", value):
                safe.append(f' {key}="{html.escape(value, quote=True)}"')
        for key in ("alt", "title"):
            if values.get(key):
                safe.append(f' {key}="{html.escape(values[key], quote=True)}"')
        for key in ("width", "height"):
            size = (values.get(key) or "").strip()
            if size.isdigit():
                safe.append(f' {key}="{min(2000, int(size))}"')
            elif re.fullmatch(r"\d{1,3}%", size) and int(size[:-1]) <= 100:
                safe.append(f' {key}="{size}"')
        for key in ("colspan", "rowspan"):
            size = (values.get(key) or "").strip()
            if size.isdigit():
                safe.append(f' {key}="{min(100, int(size))}"')
        for key in ("cellpadding", "cellspacing", "border"):
            size = (values.get(key) or "").strip()
            if tag == "table" and size.isdigit():
                safe.append(f' {key}="{min(100, int(size))}"')
        styles = []
        if values.get("bgcolor"):
            styles.append("background-color:" + values["bgcolor"])
        if values.get("background"):
            styles.append("background-image:url(" + json.dumps(values["background"]) + ")")
        if values.get("style"):
            styles.append(values["style"])
        style = self.safe_style(";".join(styles))
        if style:
            safe.append(f' style="{html.escape(style, quote=True)}"')
        if values.get("align") in ("left", "right", "center", "justify"):
            safe.append(f' align="{values["align"]}"')
        if values.get("valign") in ("top", "middle", "bottom", "baseline"):
            safe.append(f' valign="{values["valign"]}"')
        if tag == "a":
            href = values.get("href", "").strip()
            parsed = urlsplit(href)
            if parsed.scheme.lower() in ("https", "http", "mailto") and (parsed.netloc or parsed.scheme.lower() == "mailto"):
                safe.append(f' href="{html.escape(href, quote=True)}" target="_blank" rel="noopener noreferrer"')
        if tag in ("img", "audio", "video", "source"):
            source = self.media_url(values.get("src", ""))
            if not source and tag in ("img", "source"):
                if tag == "img":
                    self.parts.append(f'<span>[{html.escape(values.get("alt") or "Внешнее изображение скрыто")}]</span>')
                return
            if source:
                safe.append(f' src="{html.escape(source, quote=True)}"')
            if tag in ("audio", "video"):
                safe.append(" controls preload=\"none\"")
        self.parts.append(f"<{'div' if tag == 'body' else tag}{''.join(safe)}>")

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag.lower() not in self.VOID:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag in self.HIDDEN:
            self.hidden = max(0, self.hidden - 1)
        elif not self.hidden and tag in self.TAGS and tag not in self.VOID:
            self.parts.append(f"</{'div' if tag == 'body' else tag}>")

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(html.escape(data))

    def render(self, source):
        self.feed(source)
        return "".join(self.parts)[:1_000_000]


def safe_html(source, folder, uid, cid_parts, allow_remote=False):
    sanitizer = SafeMailHtml(folder, uid, cid_parts, allow_remote)
    styles = "".join(sanitizer.safe_stylesheet(block) for block in
                     re.findall(r"<style\b[^>]*>(.*?)</style\s*>", source, re.I | re.S)[:20])
    return ("<style>" + styles + "</style>" if styles else "") + sanitizer.render(source)


def _literal(response):
    for item in response or []:
        if isinstance(item, tuple) and len(item) == 2 and isinstance(item[1], bytes):
            return item[1]
    raise RuntimeError("Gmail did not return the message body")


def _message_entries(listing):
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
    return entries


def _prune_cached(conn, folder, limit):
    return conn.execute("""DELETE FROM messages WHERE folder=? AND uid NOT IN
        (SELECT uid FROM messages WHERE folder=? ORDER BY uid DESC LIMIT ?)""",
                        (folder, folder, limit)).rowcount > 0


def refresh_cached_flags(imap, conn, folder, limit):
    """Refresh Gmail flags without fetching cached message bodies again."""
    cached = conn.execute("SELECT uid,flags FROM messages WHERE folder=? ORDER BY uid DESC LIMIT ?",
                          (folder, limit)).fetchall()
    if not cached:
        return False
    known = {row["uid"]: row["flags"] for row in cached}
    status, listing = imap.uid("FETCH", ",".join(str(uid) for uid in known), "(UID FLAGS)")
    if status != "OK":
        raise RuntimeError("Cannot refresh Gmail message flags")
    changed = False
    for uid, flags, _ in _message_entries(listing):
        if uid not in known:
            continue
        new_flags = " ".join(flags)
        if known[uid] != new_flags:
            conn.execute("UPDATE messages SET flags=? WHERE folder=? AND uid=?", (new_flags, folder, uid))
            changed = True
    return changed


def sync_folder(imap, conn, folder, role, label, limit, reconcile=False):
    status, count_data = imap.select('"' + folder.replace('"', '\\"') + '"', readonly=True)
    if status != "OK":
        raise RuntimeError("Cannot select Gmail folder")
    count = int(count_data[0] or 0)
    validity_data = imap.response("UIDVALIDITY")[1]
    validity = validity_data[0].decode("ascii") if validity_data and validity_data[0] else ""
    if not validity:
        raise RuntimeError("Gmail did not report UIDVALIDITY")
    old = conn.execute("SELECT uidvalidity,last_uid FROM folders WHERE name=?", (folder,)).fetchone()
    changed = bool(old and old[0] != validity)
    if old and old[0] != validity:
        conn.execute("DELETE FROM messages WHERE folder=?", (folder,))
        conn.execute("UPDATE folders SET last_uid=0 WHERE name=?", (folder,))
    conn.execute("INSERT INTO folders(name,role,uidvalidity,label) VALUES(?,?,?,?) ON CONFLICT(name) DO UPDATE SET role=excluded.role,uidvalidity=excluded.uidvalidity,label=excluded.label", (folder, role, validity, label))
    conn.commit()
    last_uid = old[1] if old and old[0] == validity else 0
    if count == 0:
        changed |= conn.execute("DELETE FROM messages WHERE folder=?", (folder,)).rowcount > 0
        conn.execute("UPDATE folders SET last_uid=0 WHERE name=?", (folder,))
        conn.commit()
        return 0, changed
    full_scan = not last_uid or reconcile
    if not full_scan:
        changed |= refresh_cached_flags(imap, conn, folder, limit)
    if full_scan:
        start = max(1, count - limit + 1)
        status, listing = imap.fetch(f"{start}:{count}", "(UID FLAGS RFC822.SIZE)")
        if status != "OK":
            raise RuntimeError("Cannot list Gmail messages")
        entries = _message_entries(listing)
        if not entries:
            raise RuntimeError("Gmail returned an empty UID listing")
        latest_uid = max(uid for uid, _, _ in entries)
    else:
        next_data = imap.response("UIDNEXT")[1]
        next_uid = int(next_data[0]) if next_data and next_data[0] else 0
        if next_uid and next_uid <= last_uid + 1:
            changed |= _prune_cached(conn, folder, limit)
            conn.commit()
            return 0, changed
        status, matches = imap.uid("SEARCH", None, f"UID {last_uid + 1}:*")
        if status != "OK":
            raise RuntimeError("Cannot search new Gmail messages")
        found = {int(value) for value in b" ".join(item for item in matches or [] if isinstance(item, bytes)).split()}
        new_uids = sorted(uid for uid in found if uid > last_uid)
        latest_uid = max(last_uid, next_uid - 1, new_uids[-1] if new_uids else last_uid)
        if not new_uids:
            changed |= _prune_cached(conn, folder, limit)
            conn.execute("UPDATE folders SET last_uid=? WHERE name=?", (latest_uid, folder))
            conn.commit()
            return 0, changed
        selected = new_uids[-limit:]
        status, listing = imap.uid("FETCH", ",".join(str(uid) for uid in selected), "(UID FLAGS RFC822.SIZE)")
        if status != "OK":
            raise RuntimeError("Cannot list new Gmail messages")
        entries = [entry for entry in _message_entries(listing) if entry[0] > last_uid]
    wanted = [entry[0] for entry in entries]
    fetched = 0
    for uid, flags, size in entries:
        exists = conn.execute("SELECT raw_html,flags FROM messages WHERE folder=? AND uid=?", (folder, uid)).fetchone()
        if exists and exists[0] is not None:
            new_flags = " ".join(flags)
            if exists[1] != new_flags:
                conn.execute("UPDATE messages SET flags=? WHERE folder=? AND uid=?", (new_flags, folder, uid))
                changed = True
            continue
        if size > MAX_MAIL_BYTES:
            continue
        status, response = imap.uid("FETCH", str(uid), "(BODY.PEEK[])")
        if status != "OK":
            raise RuntimeError("Cannot fetch Gmail message")
        parsed, parts = parse_message(_literal(response), folder, uid, flags)
        conn.execute("INSERT OR REPLACE INTO messages VALUES(?,?,?,?,?,?,?,?,?,?,?)", parsed)
        for index, (content_type, filename, cid, content) in enumerate(parts):
            conn.execute("INSERT INTO parts(folder,uid,part_id,content_type,filename,cid,content) VALUES(?,?,?,?,?,?,?)",
                         (folder, uid, index, content_type, filename, cid, content))
        fetched += 1
        changed = True
    if full_scan and wanted:
        placeholders = ",".join("?" for _ in wanted)
        changed |= conn.execute(f"DELETE FROM messages WHERE folder=? AND uid NOT IN ({placeholders})", [folder, *wanted]).rowcount > 0
    else:
        changed |= _prune_cached(conn, folder, limit)
    conn.execute("UPDATE folders SET last_uid=? WHERE name=?", (latest_uid, folder))
    conn.commit()
    return fetched, changed


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
            old_folders = {row["name"]: (row["role"], row["label"])
                           for row in conn.execute("SELECT name,role,label FROM folders")}
            new_folders = {name: (role, label) for name, role, label in folders}
            cache_changed = old_folders != new_folders
            for removed in old_folders.keys() - new_folders.keys():
                conn.execute("DELETE FROM messages WHERE folder=?", (removed,))
                conn.execute("DELETE FROM folders WHERE name=?", (removed,))
            for folder, role, label in folders:
                conn.execute("INSERT INTO folders(name,role,label) VALUES(?,?,?) ON CONFLICT(name) DO UPDATE SET role=excluded.role,label=excluded.label", (folder, role, label))
            conn.commit()
            target = [item for item in folders if item[0] == requested_folder] if requested_folder else [item for item in folders if item[1] in AUTO_SYNC_ROLES]
            if requested_folder and not target:
                raise ValueError("Unknown folder")
            total = 0
            for name, role, label in target:
                key = reconcile_key(name)
                limit_key = cache_limit_key(name)
                try:
                    reconcile = (datetime.now(timezone.utc) - datetime.fromisoformat(get_meta(conn, key))).total_seconds() >= 6 * 3600
                except ValueError:
                    reconcile = True
                # A larger cache needs one metadata scan of the wider window.
                # Existing message bodies are reused; only missing UIDs are downloaded.
                reconcile |= cfg["limit"] > int(get_meta(conn, limit_key, "0"))
                fetched, changed = sync_folder(imap, conn, name, role, label, cfg["limit"], reconcile)
                total += fetched
                cache_changed |= changed
                if reconcile:
                    set_meta(conn, key, datetime.now(timezone.utc).isoformat())
                set_meta(conn, limit_key, cfg["limit"])
            set_meta(conn, "last_sync", datetime.now(timezone.utc).isoformat())
            set_meta(conn, "last_error", "")
            if cache_changed:
                bump_cache_revision(conn)
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
                bump_cache_revision(conn)
                conn.commit()
    finally:
        try:
            imap.logout()
        except (OSError, imaplib.IMAP4.error):
            pass
