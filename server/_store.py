"""Durable cache: transcripts, translations, LLM romanizations, video library.

SQLite (stdlib) in config/yume_cache.db. Replaces three volatile caches: the
server's in-memory subtitle cache, the extension service worker's translation
cache (lost whenever Chrome suspended the worker) and the per-browser
chrome.storage "library". Everything here survives restarts and is shared by
every browser that talks to this server.

Transcripts are stored RAW (before the hallucination filter and user blacklist)
so filter or blacklist changes apply retroactively when they are served.
"""

import json
import os
import sqlite3
import threading
import time

MAX_VIDEOS = 200
MAX_TRANSLATIONS = 50_000
MAX_ROMANIZATIONS = 50_000
PRUNE_EVERY = 50  # new videos between prunes (it also runs at startup)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS videos (
    video_key TEXT PRIMARY KEY,
    url TEXT, title TEXT, duration REAL,
    regions TEXT,            -- JSON [[start, end], ...] planned from the full audio
    last_used REAL
);
CREATE TABLE IF NOT EXISTS transcripts (
    video_key TEXT, language TEXT, model TEXT,
    region_start REAL, region_end REAL,
    segments TEXT,           -- JSON [{"start", "end", "text", "confidence"}, ...]
    created REAL,
    PRIMARY KEY (video_key, language, model, region_start, region_end)
);
CREATE TABLE IF NOT EXISTS translations (
    src TEXT, tgt TEXT, model TEXT, text TEXT, translation TEXT, created REAL,
    PRIMARY KEY (src, tgt, model, text)
);
CREATE TABLE IF NOT EXISTS romanizations (
    language TEXT, text TEXT, romaji TEXT, created REAL,
    PRIMARY KEY (language, text)
);
"""


class Store:
    def __init__(self, path):
        self.path = str(path)
        self._lock = threading.Lock()
        self._writes = 0
        try:
            self._db = self._open()
        except sqlite3.DatabaseError as e:
            # A corrupt cache must not keep the server from starting: it only
            # holds work that can be redone. Keep the file aside for inspection.
            aside = f"{self.path}.corrupt-{int(time.time())}"
            print(f"[Yume] Cache database unreadable ({e}) — moved to {aside}, starting a new one")
            for suffix in ("", "-wal", "-shm", "-journal"):
                if os.path.exists(self.path + suffix):
                    os.replace(self.path + suffix, aside + suffix)
            self._db = self._open()
        self.prune()

    def _open(self):
        db = sqlite3.connect(self.path, check_same_thread=False)
        try:
            db.execute("PRAGMA quick_check").fetchone()
            db.executescript(_SCHEMA)
            db.commit()
        except sqlite3.DatabaseError:
            db.close()
            raise
        return db

    def _q(self, sql, args=()):
        with self._lock:
            return self._db.execute(sql, args).fetchall()

    def _w(self, sql, args=()):
        with self._lock:
            self._db.execute(sql, args)
            self._db.commit()

    # ── videos ────────────────────────────────────────────────────────────────

    def get_video(self, video_key):
        rows = self._q("SELECT url, title, duration, regions FROM videos WHERE video_key=?", (video_key,))
        if not rows:
            return None
        url, title, duration, regions = rows[0]
        return {
            "url": url,
            "title": title,
            "duration": duration,
            "regions": [tuple(r) for r in json.loads(regions)] if regions else None,
        }

    def put_video(self, video_key, url, title, duration, regions):
        self._writes += 1
        if self._writes % PRUNE_EVERY == 0:  # a long-running server must not grow forever
            self.prune()
        self._w(
            "INSERT INTO videos (video_key, url, title, duration, regions, last_used) VALUES (?,?,?,?,?,?) "
            "ON CONFLICT(video_key) DO UPDATE SET url=excluded.url, title=COALESCE(NULLIF(excluded.title,''), title), "
            "duration=COALESCE(excluded.duration, duration), regions=COALESCE(excluded.regions, regions), "
            "last_used=excluded.last_used",
            (video_key, url, title or "", duration, json.dumps(regions) if regions else None, time.time()),
        )

    def touch_video(self, video_key):
        self._w("UPDATE videos SET last_used=? WHERE video_key=?", (time.time(), video_key))

    def delete_video(self, video_key):
        with self._lock:
            self._db.execute("DELETE FROM videos WHERE video_key=?", (video_key,))
            self._db.execute("DELETE FROM transcripts WHERE video_key=?", (video_key,))
            self._db.commit()

    def library(self):
        """Videos with at least one cached region, newest first."""
        rows = self._q(
            "SELECT v.video_key, v.url, v.title, v.duration, v.regions, v.last_used, t.language, t.model, "
            "COUNT(t.region_start) FROM videos v JOIN transcripts t ON t.video_key = v.video_key "
            "GROUP BY v.video_key, t.language, t.model ORDER BY v.last_used DESC"
        )
        out = []
        for key, url, title, duration, regions, last_used, lang, model, n in rows:
            total = len(json.loads(regions)) if regions else 0
            out.append(
                {
                    "video_key": key,
                    "url": url,
                    "title": title,
                    "duration": duration,
                    "last_used": last_used,
                    "language": lang,
                    "model": model,
                    "regions_done": n,
                    "regions_total": total,
                }
            )
        return out

    # ── transcripts ───────────────────────────────────────────────────────────

    def get_transcripts(self, video_key, language, model):
        """{(start, end): [segment, ...]} for every cached region."""
        rows = self._q(
            "SELECT region_start, region_end, segments FROM transcripts WHERE video_key=? AND language=? AND model=?",
            (video_key, language, model),
        )
        return {(s, e): json.loads(segs) for s, e, segs in rows}

    def put_transcript(self, video_key, language, model, start, end, segments):
        self._w(
            "INSERT OR REPLACE INTO transcripts VALUES (?,?,?,?,?,?,?)",
            (video_key, language, model, start, end, json.dumps(segments, ensure_ascii=False), time.time()),
        )

    # ── translations / romanizations ──────────────────────────────────────────

    def get_translations(self, src, tgt, model, texts):
        if not texts:
            return {}
        out = {}
        uniq = list(dict.fromkeys(texts))
        for i in range(0, len(uniq), 500):  # SQLite variable limit
            part = uniq[i : i + 500]
            marks = ",".join("?" * len(part))
            if model is None:  # any model, newest wins
                rows = self._q(
                    f"SELECT text, translation FROM translations WHERE src=? AND tgt=? AND text IN ({marks}) "  # nosec B608 — only "?" placeholders are interpolated
                    "ORDER BY created",
                    (src, tgt, *part),
                )
            else:
                rows = self._q(
                    f"SELECT text, translation FROM translations WHERE src=? AND tgt=? AND model=? AND text IN ({marks})",  # nosec B608 — only "?" placeholders are interpolated
                    (src, tgt, model, *part),
                )
            out.update(dict(rows))
        return out

    def put_translation(self, src, tgt, model, text, translation):
        self._w(
            "INSERT OR REPLACE INTO translations VALUES (?,?,?,?,?,?)",
            (src, tgt, model, text, translation, time.time()),
        )

    def get_romanization(self, language, text):
        rows = self._q("SELECT romaji FROM romanizations WHERE language=? AND text=?", (language, text))
        return rows[0][0] if rows else None

    def put_romanization(self, language, text, romaji):
        self._w("INSERT OR REPLACE INTO romanizations VALUES (?,?,?,?)", (language, text, romaji, time.time()))

    # ── maintenance ───────────────────────────────────────────────────────────

    def clear(self):
        with self._lock:
            for table in ("videos", "transcripts", "translations", "romanizations"):
                self._db.execute(f"DELETE FROM {table}")  # nosec B608 — fixed table names
            self._db.commit()

    def prune(self):
        """Keep the newest MAX_VIDEOS videos and MAX_TRANSLATIONS translations."""
        with self._lock:
            self._db.execute(
                "DELETE FROM videos WHERE video_key NOT IN "
                "(SELECT video_key FROM videos ORDER BY last_used DESC LIMIT ?)",
                (MAX_VIDEOS,),
            )
            self._db.execute("DELETE FROM transcripts WHERE video_key NOT IN (SELECT video_key FROM videos)")
            self._db.execute(
                "DELETE FROM translations WHERE rowid NOT IN "
                "(SELECT rowid FROM translations ORDER BY created DESC LIMIT ?)",
                (MAX_TRANSLATIONS,),
            )
            self._db.execute(
                "DELETE FROM romanizations WHERE rowid NOT IN "
                "(SELECT rowid FROM romanizations ORDER BY created DESC LIMIT ?)",
                (MAX_ROMANIZATIONS,),
            )
            self._db.commit()

    def close(self):
        with self._lock:
            self._db.close()
