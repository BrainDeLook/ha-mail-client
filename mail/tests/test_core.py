"""Offline tests: no Gmail credentials or network needed."""

import base64
from email.message import EmailMessage
import json
import queue
from contextlib import closing
from datetime import datetime, timedelta, timezone
from http.server import ThreadingHTTPServer
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
import unittest
from urllib.request import Request, urlopen
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parents[1] / "app"))
import core
import server


SAMPLE = (b"From: Alice <alice@example.com>\r\nTo: Test <test@gmail.com>\r\n"
          b"Subject: Hello\r\nDate: Wed, 23 Sep 2026 08:00:00 +0000\r\n"
          b"MIME-Version: 1.0\r\nContent-Type: text/html; charset=utf-8\r\n\r\n"
          b"<p>Welcome <b>home</b>.</p><script>bad()</script>")


class FakeImap:
    calls = 0
    metadata_calls = 0
    uids = [90393]
    flags = "\\Seen"

    def __init__(self, *args, **kwargs):
        self.selected = ""

    def login(self, address, password):
        assert address == "test@gmail.com" and password == "secret"
        return "OK", []

    def list(self):
        return "OK", [b'(\\HasNoChildren) "/" "INBOX"',
                      b'(\\HasNoChildren \\Sent) "/" "[Gmail]/Sent Mail"',
                      b'(\\HasNoChildren \\NoSelect) "/" "[Gmail]"']

    def select(self, name, readonly=False):
        self.selected = name
        return "OK", [str(len(self.uids)).encode()]

    def response(self, name):
        return name, [str(max(self.uids) + 1).encode() if name == "UIDNEXT" else b"123"]

    def fetch(self, range_, query):
        FakeImap.metadata_calls += 1
        start, end = (int(value) for value in range_.split(":"))
        return "OK", [f"1 (UID {uid} FLAGS ({self.flags}) RFC822.SIZE 300)".encode()
                      for uid in self.uids[start - 1:end]]

    def uid(self, action, uid, query):
        if action == "SEARCH":
            minimum = int(query.split()[1].split(":")[0])
            return "OK", [b" ".join(str(value).encode() for value in self.uids if value >= minimum)]
        if query == "(BODY.PEEK[])":
            FakeImap.calls += 1
            return "OK", [(f"1 (UID {uid} BODY[] {{300}}".encode(), SAMPLE)]
        FakeImap.metadata_calls += 1
        requested = {int(value) for value in uid.split(",")}
        return "OK", [f"1 (UID {value} FLAGS ({self.flags}) RFC822.SIZE 300)".encode()
                      for value in self.uids if value in requested]

    def logout(self):
        pass


class CoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.addCleanup(self.temp.cleanup)
        self.data = Path(self.temp.name)
        self.old_data, self.old_db, self.old_options = core.DATA_DIR, core.DB_FILE, core.OPTIONS_FILE
        core.DATA_DIR = self.data
        core.DB_FILE = self.data / "mail.db"
        core.OPTIONS_FILE = self.data / "options.json"
        self.addCleanup(self.restore)
        FakeImap.calls = 0
        FakeImap.metadata_calls = 0
        FakeImap.uids = [90393]
        FakeImap.flags = "\\Seen"
        self.cfg = {"email": "test@gmail.com", "password": "secret", "interval": 5, "limit": 50}

    def restore(self):
        core.DATA_DIR, core.DB_FILE, core.OPTIONS_FILE = self.old_data, self.old_db, self.old_options

    def test_sync_persists_and_reuses_message(self):
        with patch.object(core.imaplib, "IMAP4_SSL", FakeImap):
            self.assertEqual(core.sync_all(self.cfg), 2)
            self.assertEqual(core.sync_all(self.cfg), 0)
        self.assertEqual(FakeImap.calls, 2)
        self.assertEqual(FakeImap.metadata_calls, 2)
        with closing(core.connect_db()) as conn:
            row = conn.execute("SELECT body FROM messages WHERE folder='INBOX' AND uid=90393").fetchone()
            self.assertIn("Welcome home", row[0])
            self.assertNotIn("bad()", row[0])
            folders = conn.execute("SELECT role FROM folders ORDER BY name").fetchall()
            self.assertEqual({item[0] for item in folders}, {"INBOX", "SENT"})
            self.assertEqual(core.get_meta(conn, "cache_revision"), "2")

    def test_incremental_sync_fetches_only_new_uids(self):
        with patch.object(core.imaplib, "IMAP4_SSL", FakeImap):
            core.sync_all(self.cfg)
            FakeImap.uids.append(90394)
            self.assertEqual(core.sync_all(self.cfg), 2)
            self.assertEqual(core.sync_all(self.cfg), 0)
        self.assertEqual(FakeImap.calls, 4)
        self.assertEqual(FakeImap.metadata_calls, 4)
        with closing(core.connect_db()) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM messages WHERE folder='INBOX'").fetchone()[0], 2)
            self.assertEqual(conn.execute("SELECT last_uid FROM folders WHERE name='INBOX'").fetchone()[0], 90394)

    def test_smaller_cache_limit_prunes_locally_without_refetch(self):
        FakeImap.uids = [90393, 90394]
        with patch.object(core.imaplib, "IMAP4_SSL", FakeImap):
            core.sync_all(self.cfg)
            smaller = {**self.cfg, "limit": 1}
            self.assertEqual(core.sync_all(smaller), 0)
        self.assertEqual(FakeImap.calls, 4)
        self.assertEqual(FakeImap.metadata_calls, 2)
        with closing(core.connect_db()) as conn:
            self.assertEqual(conn.execute("SELECT uid FROM messages WHERE folder='INBOX'").fetchone()[0], 90394)

    def test_sync_requests_are_coalesced(self):
        with patch.object(server, "SYNC_QUEUE", queue.Queue(maxsize=2)), \
             patch.object(server, "SYNC_PENDING", set()), \
             patch.object(server, "SYNC_STATE", {"running": False, "folder": None}):
            self.assertTrue(server.enqueue_sync("INBOX"))
            self.assertFalse(server.enqueue_sync("INBOX"))
            self.assertTrue(server.enqueue_sync(None))
            self.assertFalse(server.enqueue_sync("[Gmail]/Sent Mail"))

    def test_empty_remote_folder_clears_cached_messages(self):
        with patch.object(core.imaplib, "IMAP4_SSL", FakeImap):
            core.sync_all(self.cfg)
            FakeImap.uids = []
            self.assertEqual(core.sync_all(self.cfg), 0)
        with closing(core.connect_db()) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0], 0)
            self.assertEqual(core.get_meta(conn, "cache_revision"), "3")

    def test_rare_reconciliation_updates_flags_without_refetching_bodies(self):
        with patch.object(core.imaplib, "IMAP4_SSL", FakeImap):
            core.sync_all(self.cfg)
            with closing(core.connect_db()) as conn:
                old = (datetime.now(timezone.utc) - timedelta(hours=7)).isoformat()
                core.set_meta(conn, core.reconcile_key("INBOX"), old)
                core.set_meta(conn, core.reconcile_key("[Gmail]/Sent Mail"), old)
                conn.commit()
            FakeImap.flags = "\\Flagged"
            self.assertEqual(core.sync_all(self.cfg), 0)
        self.assertEqual(FakeImap.calls, 2)
        self.assertEqual(FakeImap.metadata_calls, 4)
        with closing(core.connect_db()) as conn:
            self.assertEqual(conn.execute("SELECT flags FROM messages WHERE folder='INBOX'").fetchone()[0], "\\Flagged")
            self.assertEqual(core.get_meta(conn, "cache_revision"), "3")

    def test_account_switch_clears_old_mail(self):
        with closing(core.connect_db()) as conn:
            core.ensure_account(conn, "test@gmail.com")
            conn.execute("INSERT INTO messages VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                         ("INBOX", 1, "Private", "Sender", "Recipient", "", "", "", "Body", 0, ""))
            conn.commit()
            core.ensure_account(conn, "another@gmail.com")
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0], 0)

    def test_folder_parsing_and_html_plaintext(self):
        folders = core.parse_folders(FakeImap().list()[1])
        self.assertEqual(folders, [("INBOX", "INBOX", "Входящие"),
                                   ("[Gmail]/Sent Mail", "SENT", "Sent Mail")])
        record, parts = core.parse_message(SAMPLE, "INBOX", 90393, [])
        self.assertEqual(record[2], "Hello")
        self.assertIn("Welcome home", record[8])
        self.assertNotIn("<script>", record[8])

    def test_modified_utf7_gmail_folders(self):
        def encoded(value):
            return "&" + base64.b64encode(value.encode("utf-16-be")).decode().rstrip("=").replace("/", ",") + "-"
        important = f'[Gmail]/{encoded("Важное")}'
        line = f'(\\HasNoChildren \\Important) "/" "{important}"'.encode("ascii")
        folders = core.parse_folders([line])
        self.assertEqual(folders[1], (important, "IMPORTANT", "Важное"))
        self.assertEqual(core.decode_imap_utf7("A&-B"), "A&B")

    def test_sanitized_html_and_inline_media(self):
        source = ('<p>Hello <img src="cid:photo1" onerror="bad()"> '
                  '<img src="https://images.example/p.png"><script>alert(1)</script>'
                  '<a href="javascript:bad()">bad link</a><video src="cid:video1"></video></p>')
        blocked = core.safe_html(source, "INBOX", 42, {"photo1": 0, "video1": 1})
        self.assertIn("api/part?folder=INBOX&amp;uid=42&amp;part=0", blocked)
        self.assertIn("api/part?folder=INBOX&amp;uid=42&amp;part=1", blocked)
        self.assertNotIn("https://images.example", blocked)
        self.assertNotIn("onerror", blocked)
        self.assertNotIn("alert(1)", blocked)
        self.assertNotIn("javascript:", blocked)
        allowed = core.safe_html(source, "INBOX", 42, {"photo1": 0}, True)
        self.assertIn("https://images.example/p.png", allowed)

    def test_sender_formatting_is_preserved_without_active_css(self):
        source = ('<table width="100%" cellpadding="12" cellspacing="0" '
                  'style="width:100%;background-color:#ffffff;border-collapse:collapse">'
                  '<tr><td style="padding:20px;text-align:center;background-image:url(https://track.example/x)">'
                  '<img src="https://images.example/logo.png" style="max-width:100%;height:auto" '
                  'onerror="bad()"></td></tr></table>')
        result = core.safe_html(source, "INBOX", 4, {}, True)
        self.assertIn("width:100%", result)
        self.assertIn('width="100%"', result)
        self.assertIn('cellpadding="12"', result)
        self.assertIn("padding:20px", result)
        self.assertIn("max-width:100%", result)
        self.assertIn("https://images.example/logo.png", result)
        self.assertNotIn("background-image", result)
        self.assertNotIn("track.example", result)
        self.assertNotIn("onerror", result)

    def test_external_media_can_be_disabled_in_addon_options(self):
        core.OPTIONS_FILE.write_text(json.dumps({"show_external_media": False}), encoding="utf-8")
        self.assertFalse(core.options()["external_media"])

    def test_theme_and_log_level_options(self):
        core.OPTIONS_FILE.write_text("{}", encoding="utf-8")
        self.assertEqual(core.options()["theme"], "system")
        self.assertEqual(core.options()["log_level"], "info")
        core.OPTIONS_FILE.write_text(json.dumps({"theme": "dark", "log_level": "debug"}), encoding="utf-8")
        self.assertEqual(core.options()["theme"], "dark")
        self.assertEqual(core.options()["log_level"], "debug")
        core.OPTIONS_FILE.write_text(json.dumps({"theme": "invalid", "log_level": "invalid"}), encoding="utf-8")
        self.assertEqual(core.options()["theme"], "system")
        self.assertEqual(core.options()["log_level"], "info")

    def test_debug_http_logging_excludes_query_values(self):
        from types import SimpleNamespace
        from unittest.mock import patch

        request = SimpleNamespace(command="GET", path="/api/message?token=SECRET&folder=INBOX")
        with patch.object(server.LOGGER, "debug") as debug:
            server.Handler.log_message(request, "ignored")
        debug.assert_called_once_with("HTTP %s %s", "GET", "/api/message")

    def test_log_level_filters_http_requests(self):
        old_level = server.LOGGER.level
        try:
            server.configure_logging("info")
            self.assertFalse(server.LOGGER.isEnabledFor(10))
            server.configure_logging("debug")
            self.assertTrue(server.LOGGER.isEnabledFor(10))
            server.configure_logging("error")
            self.assertFalse(server.LOGGER.isEnabledFor(30))
        finally:
            server.LOGGER.setLevel(old_level)

    def test_sidebar_and_mail_frame_regression(self):
        app = Path(__file__).parents[1] / "app"
        styles = (app / "dark.css").read_text(encoding="utf-8")
        markup = (app / "index.html").read_text(encoding="utf-8")
        script = (app / "app.js").read_text(encoding="utf-8")
        self.assertIn("position: fixed", styles)
        self.assertIn(".brand, .list-header, .reader-toolbar { height: 64px; min-height: 64px; }", styles)
        self.assertIn("grid-template-rows: minmax(0, 1fr)", styles)
        self.assertIn(".reading-pane { overflow-y: auto", styles)
        self.assertIn("#folders { min-height: 0; overflow-y: auto", styles)
        self.assertIn('.shell.show-sidebar .sidebar-scrim', styles)
        self.assertIn('id="sidebar-scrim"', markup)
        self.assertIn("$('sidebar-scrim').addEventListener('click', () => setSidebarOpen(false))", script)
        self.assertIn('aria-expanded="false"', markup)
        self.assertIn("doc.addEventListener('wheel'", script)
        self.assertIn('sandbox="allow-same-origin allow-popups allow-popups-to-escape-sandbox"', markup)
        self.assertNotIn("allow-scripts", markup)

    def test_mime_cid_image_is_cached(self):
        mail = EmailMessage()
        mail["Subject"] = "Photo"
        mail.set_content("Plain fallback")
        mail.add_alternative('<p>Inline <img src="cid:photo1"></p>', subtype="html")
        mail.get_payload()[1].add_related(b"\x89PNG\r\n", maintype="image", subtype="png", cid="<photo1>")
        record, parts = core.parse_message(mail.as_bytes(), "INBOX", 8, [])
        self.assertEqual(record[8], "Plain fallback")
        self.assertIn("cid:photo1", record[10])
        self.assertEqual(parts[0][0], "image/png")
        self.assertEqual(parts[0][2], "photo1")
        self.assertEqual(parts[0][3], b"\x89PNG\r\n")

    def test_upgrade_old_database_schema(self):
        old = sqlite3.connect(core.DB_FILE)
        old.executescript("""CREATE TABLE folders(name TEXT PRIMARY KEY,role TEXT NOT NULL,uidvalidity TEXT NOT NULL DEFAULT '');
            CREATE TABLE messages(folder TEXT,uid INTEGER,subject TEXT,sender TEXT,recipients TEXT,sent_at TEXT,
            flags TEXT,snippet TEXT,body TEXT,has_attachments INTEGER,PRIMARY KEY(folder,uid));""")
        old.close()
        with closing(core.connect_db()) as conn:
            self.assertIn("label", [row[1] for row in conn.execute("PRAGMA table_info(folders)")])
            self.assertIn("raw_html", [row[1] for row in conn.execute("PRAGMA table_info(messages)")])
            self.assertIn("last_uid", [row[1] for row in conn.execute("PRAGMA table_info(folders)")])
            self.assertIsNotNone(conn.execute("SELECT name FROM sqlite_master WHERE name='parts'").fetchone())

    def test_http_status_and_ingress_assets(self):
        core.OPTIONS_FILE.write_text(json.dumps({"gmail_email": "test@gmail.com",
            "gmail_app_password": "secret", "sync_interval_minutes": 5, "cache_per_folder": 50,
            "theme": "dark", "show_external_media": True}), encoding="utf-8")
        with closing(core.connect_db()) as conn:
            core.ensure_account(conn, "test@gmail.com")
            conn.execute("INSERT INTO folders(name,role,label) VALUES('INBOX','INBOX','Входящие')")
            conn.execute("INSERT INTO messages VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                         ("INBOX", 7, "Photo", "Alice", "Test", "", "", "Photo", "Photo", 1,
                          '<img src="cid:pic"><img src="https://images.example/p.png">'))
            conn.execute("INSERT INTO parts VALUES(?,?,?,?,?,?,?)",
                         ("INBOX", 7, 0, "image/png", "photo.png", "pic", b"\x89PNG\r\n"))
            conn.commit()
        web = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        thread = threading.Thread(target=web.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(web.server_close)
        self.addCleanup(web.shutdown)
        url = f"http://127.0.0.1:{web.server_port}"
        with urlopen(url + "/api/status") as response:
            value = json.load(response)
            self.assertTrue(value["configured"])
            self.assertEqual(value["email"], "test@gmail.com")
            self.assertEqual(value["theme"], "dark")
            self.assertTrue(value["show_external_media"])
            self.assertEqual(value["cache_revision"], 1)
            self.assertIn("frame-ancestors 'self'", response.headers["Content-Security-Policy"])
        with urlopen(url + "/") as response:
            self.assertIn(b"Home Mail", response.read())
        with urlopen(url + "/api/folders") as response:
            self.assertEqual(json.load(response)["folders"][0]["label"], "Входящие")
        with urlopen(url + "/api/message?folder=INBOX&uid=7") as response:
            message = json.load(response)["message"]
            self.assertIn("api/part", message["html"])
            self.assertNotIn("https://images.example", message["html"])
            self.assertNotIn("raw_html", message)
        with urlopen(url + "/api/message?folder=INBOX&uid=7&remote=1") as response:
            self.assertIn("https://images.example", json.load(response)["message"]["html"])
        with urlopen(url + "/api/part?folder=INBOX&uid=7&part=0") as response:
            self.assertEqual(response.headers["Content-Type"], "image/png")
            self.assertEqual(response.read(), b"\x89PNG\r\n")
        request = Request(url + "/api/sync", data=b"{}", headers={"Content-Type": "application/json"})
        with urlopen(request) as response:
            self.assertEqual(response.status, 202)


if __name__ == "__main__":
    unittest.main()
