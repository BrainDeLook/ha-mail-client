"""Offline tests: no Gmail credentials or network needed."""

import json
from contextlib import closing
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
        return "OK", [b"1"]

    def response(self, name):
        return name, [b"123"]

    def fetch(self, range_, query):
        return "OK", [b"1 (UID 90393 FLAGS (\\Seen) RFC822.SIZE 300)"]

    def uid(self, action, uid, query):
        FakeImap.calls += 1
        return "OK", [(b"1 (UID 90393 BODY[] {300}", SAMPLE)]

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
        self.cfg = {"email": "test@gmail.com", "password": "secret", "interval": 5, "limit": 50}

    def restore(self):
        core.DATA_DIR, core.DB_FILE, core.OPTIONS_FILE = self.old_data, self.old_db, self.old_options

    def test_sync_persists_and_reuses_message(self):
        with patch.object(core.imaplib, "IMAP4_SSL", FakeImap):
            self.assertEqual(core.sync_all(self.cfg), 2)
            self.assertEqual(core.sync_all(self.cfg), 0)
        self.assertEqual(FakeImap.calls, 2)
        with closing(core.connect_db()) as conn:
            row = conn.execute("SELECT body FROM messages WHERE folder='INBOX' AND uid=90393").fetchone()
            self.assertIn("Welcome home", row[0])
            self.assertNotIn("bad()", row[0])
            folders = conn.execute("SELECT role FROM folders ORDER BY name").fetchall()
            self.assertEqual({item[0] for item in folders}, {"INBOX", "SENT"})

    def test_account_switch_clears_old_mail(self):
        with closing(core.connect_db()) as conn:
            core.ensure_account(conn, "test@gmail.com")
            conn.execute("INSERT INTO messages VALUES(?,?,?,?,?,?,?,?,?,?)",
                         ("INBOX", 1, "Private", "Sender", "Recipient", "", "", "", "Body", 0))
            conn.commit()
            core.ensure_account(conn, "another@gmail.com")
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0], 0)

    def test_folder_parsing_and_html_plaintext(self):
        folders = core.parse_folders(FakeImap().list()[1])
        self.assertEqual(folders, [("INBOX", "INBOX"), ("[Gmail]/Sent Mail", "SENT")])
        record = core.parse_message(SAMPLE, "INBOX", 90393, [])
        self.assertEqual(record[2], "Hello")
        self.assertIn("Welcome home", record[8])
        self.assertNotIn("<script>", record[8])

    def test_http_status_and_ingress_assets(self):
        core.OPTIONS_FILE.write_text(json.dumps({"gmail_email": "test@gmail.com",
            "gmail_app_password": "secret", "sync_interval_minutes": 5, "cache_per_folder": 50}), encoding="utf-8")
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
            self.assertIn("frame-ancestors 'self'", response.headers["Content-Security-Policy"])
        with urlopen(url + "/") as response:
            self.assertIn(b"Home Mail", response.read())
        with urlopen(url + "/api/folders") as response:
            self.assertEqual(json.load(response)["folders"], [])
        request = Request(url + "/api/sync", data=b"{}", headers={"Content-Type": "application/json"})
        with urlopen(request) as response:
            self.assertEqual(response.status, 202)


if __name__ == "__main__":
    unittest.main()
