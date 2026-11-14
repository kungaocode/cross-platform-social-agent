"""Durable publication claims and owned comment roots shared by listeners."""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

from .online_video import platform_url


class ThreadRegistry:
    def __init__(self, path, *, clock=time.time):
        self.clock = clock
        self.db = sqlite3.connect(Path(path), timeout=20)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("""CREATE TABLE IF NOT EXISTS video_publications (
            platform TEXT, account TEXT, video_url TEXT, state TEXT NOT NULL,
            text TEXT NOT NULL, created_at REAL NOT NULL, receipt TEXT,
            PRIMARY KEY(platform,account,video_url))""")
        self.db.execute("""CREATE TABLE IF NOT EXISTS video_comment_threads (
            platform TEXT, account TEXT, root_id TEXT, video_id TEXT NOT NULL,
            video_url TEXT NOT NULL, created_at REAL NOT NULL,
            PRIMARY KEY(platform,account,root_id))""")
        self.db.commit()

    @staticmethod
    def canonical(platform, url):
        actual, url = platform_url(url)
        if actual != platform:
            raise ValueError("video_platform_mismatch")
        return url.split("?", 1)[0].rstrip("/")

    def claim(self, platform, account, url, text, *, cooldown=180):
        canonical = self.canonical(platform, url)
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            latest = self.db.execute("""SELECT MAX(created_at) FROM video_publications
                WHERE platform=? AND account=? AND state IN ('sending','published','unknown')""",
                (platform, account)).fetchone()[0]
            if latest is not None and self.clock() - latest < cooldown:
                return False
            return self.db.execute("""INSERT INTO video_publications VALUES (?,?,?,'sending',?,?,NULL)
                ON CONFLICT(platform,account,video_url) DO UPDATE SET state='sending',
                text=excluded.text,created_at=excluded.created_at,receipt=NULL
                WHERE video_publications.state='failed'""",
                                  (platform, account, canonical, text, self.clock())).rowcount == 1

    def cooldown_remaining(self, platform, account, cooldown):
        latest = self.db.execute("""SELECT MAX(created_at) FROM video_publications
            WHERE platform=? AND account=? AND state IN ('sending','published','unknown')""",
            (platform, account)).fetchone()[0]
        return max(0, cooldown - (self.clock() - latest)) if latest is not None else 0

    def finish(self, platform, account, url, receipt=None):
        canonical = self.canonical(platform, url)
        good = (isinstance(receipt, dict) and str(receipt.get("root_id", "")).isdigit()
                and int(receipt["root_id"]) > 0 and str(receipt.get("video_id", "")).isdigit()
                and int(receipt["video_id"]) > 0)
        with self.db:
            updated = self.db.execute("""UPDATE video_publications SET state=?,receipt=?
                WHERE platform=? AND account=? AND video_url=? AND state='sending'""",
                ("published" if good else "unknown", json.dumps(receipt) if good else None,
                 platform, account, canonical)).rowcount
            if good and updated:
                self.db.execute("INSERT OR IGNORE INTO video_comment_threads VALUES (?,?,?,?,?,?)",
                    (platform, account, str(receipt["root_id"]), str(receipt["video_id"]), canonical, self.clock()))
        return bool(good and updated)

    def get(self, platform, account, root):
        row = self.db.execute("SELECT * FROM video_comment_threads WHERE platform=? AND account=? AND root_id=?",
                              (platform, str(account), str(root))).fetchone()
        return dict(row) if row else None

    def fail_before_write(self, platform, account, url):
        with self.db:
            self.db.execute("""UPDATE video_publications SET state='failed'
                WHERE platform=? AND account=? AND video_url=? AND state='sending'""",
                (platform, account, self.canonical(platform, url)))

    def owns(self, platform, account, root, *, video_id=None):
        row = self.get(platform, account, root)
        return bool(row and (video_id is None or row["video_id"] == str(video_id)))

    def latest(self, platform, account, limit=10):
        return [dict(row) for row in self.db.execute("""SELECT * FROM video_comment_threads
            WHERE platform=? AND account=? ORDER BY created_at DESC LIMIT ?""", (platform, str(account), limit))]

    def state(self, platform, account, url):
        row = self.db.execute("SELECT state FROM video_publications WHERE platform=? AND account=? AND video_url=?",
            (platform, account, self.canonical(platform, url))).fetchone()
        return row[0] if row else None

    def close(self):
        self.db.close()
