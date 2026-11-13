"""SQLite persistence (stdlib sqlite3). Thread-safe through a single RLock.

Tables
------
indexers  : configured indexers
media     : Plex watchlist items (movies and shows)
episodes  : episodes of monitored shows
grabs     : torrents sent to Download Station
blocklist : release titles that failed and must not be grabbed again
events    : activity log shown in the web UI
kv        : misc key/value settings (plex client id, ...)
"""
from __future__ import annotations

import datetime as _dt
import os
import sqlite3
import threading
from typing import Any, Dict, Iterable, List, Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS indexers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    type TEXT NOT NULL,              -- torznab | rss | internet_archive
    url TEXT NOT NULL,
    api_key TEXT NOT NULL DEFAULT '',
    categories TEXT NOT NULL DEFAULT '',  -- comma separated torznab categories
    enabled INTEGER NOT NULL DEFAULT 1,
    priority INTEGER NOT NULL DEFAULT 25,  -- lower = preferred on equal score
    added_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS media (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    plex_key TEXT NOT NULL UNIQUE,
    type TEXT NOT NULL,              -- movie | show
    title TEXT NOT NULL,
    original_title TEXT,
    year INTEGER,
    imdb TEXT, tmdb TEXT, tvdb TEXT,
    status TEXT NOT NULL DEFAULT 'wanted',  -- movie: wanted|grabbed|done|ignored ; show: monitored|ignored
    on_watchlist INTEGER NOT NULL DEFAULT 1,
    last_search_at TEXT,
    added_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS episodes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    media_id INTEGER NOT NULL REFERENCES media(id) ON DELETE CASCADE,
    season INTEGER NOT NULL,
    episode INTEGER NOT NULL,
    title TEXT,
    air_date TEXT,
    status TEXT NOT NULL DEFAULT 'wanted',  -- wanted|grabbed|done|skipped
    last_search_at TEXT,
    UNIQUE(media_id, season, episode)
);
CREATE TABLE IF NOT EXISTS grabs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    media_id INTEGER REFERENCES media(id) ON DELETE SET NULL,
    season INTEGER,
    episode INTEGER,                 -- NULL with season set = season pack
    release_title TEXT NOT NULL,
    torrent_name TEXT,               -- name Download Station will use for the task / top folder
    indexer TEXT,
    info_hash TEXT,
    size INTEGER,
    status TEXT NOT NULL DEFAULT 'sent',  -- sent|imported|failed
    message TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS blocklist (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    release_title TEXT NOT NULL UNIQUE,
    reason TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    level TEXT NOT NULL,
    kind TEXT NOT NULL,
    message TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS kv (
    key TEXT PRIMARY KEY,
    value TEXT
);
CREATE INDEX IF NOT EXISTS idx_grabs_torrent ON grabs(torrent_name);
CREATE INDEX IF NOT EXISTS idx_episodes_media ON episodes(media_id);
"""


def now_iso() -> str:
    return _dt.datetime.now().replace(microsecond=0).isoformat(sep=" ")


def parse_iso(value: Optional[str]) -> Optional[_dt.datetime]:
    if not value:
        return None
    try:
        return _dt.datetime.strptime(value[:19], "%Y-%m-%d %H:%M:%S")
    except ValueError:
        try:
            return _dt.datetime.strptime(value[:10], "%Y-%m-%d")
        except ValueError:
            return None


class Database:
    def __init__(self, path: str):
        self.path = path
        if path != ":memory:":
            os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(path, check_same_thread=False, timeout=30)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute("PRAGMA foreign_keys = ON")
            try:
                self._conn.execute("PRAGMA journal_mode = WAL")
            except sqlite3.DatabaseError:  # pragma: no cover - some FS do not support WAL
                pass
            self._conn.executescript(SCHEMA)
            self._conn.commit()

    # ----------------------------------------------------------- primitives
    def execute(self, sql: str, params: Iterable[Any] = ()) -> int:
        """Run a write statement, commit, return lastrowid (or rowcount for UPDATE/DELETE)."""
        with self._lock:
            cur = self._conn.execute(sql, tuple(params))
            self._conn.commit()
            if sql.lstrip().upper().startswith("INSERT"):
                return int(cur.lastrowid or 0)
            return int(cur.rowcount)

    def query(self, sql: str, params: Iterable[Any] = ()) -> List[Dict[str, Any]]:
        with self._lock:
            cur = self._conn.execute(sql, tuple(params))
            return [dict(r) for r in cur.fetchall()]

    def query_one(self, sql: str, params: Iterable[Any] = ()) -> Optional[Dict[str, Any]]:
        rows = self.query(sql, params)
        return rows[0] if rows else None

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ------------------------------------------------------------------ kv
    def get_kv(self, key: str, default: Optional[str] = None) -> Optional[str]:
        row = self.query_one("SELECT value FROM kv WHERE key = ?", (key,))
        return row["value"] if row else default

    def set_kv(self, key: str, value: Optional[str]) -> None:
        self.execute("INSERT OR REPLACE INTO kv(key, value) VALUES(?, ?)", (key, value))

    # -------------------------------------------------------------- events
    def add_event(self, kind: str, message: str, level: str = "INFO") -> None:
        self.execute("INSERT INTO events(ts, level, kind, message) VALUES(?, ?, ?, ?)",
                     (now_iso(), level, kind, message))
        # keep the table small
        self.execute("DELETE FROM events WHERE id <= (SELECT MAX(id) FROM events) - 2000")

    def recent_events(self, limit: int = 100) -> List[Dict[str, Any]]:
        return self.query("SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,))

    # ------------------------------------------------------------ indexers
    def list_indexers(self, enabled_only: bool = False) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM indexers"
        if enabled_only:
            sql += " WHERE enabled = 1"
        return self.query(sql + " ORDER BY priority, name")

    def get_indexer(self, name_or_id: Any) -> Optional[Dict[str, Any]]:
        if isinstance(name_or_id, int) or (isinstance(name_or_id, str) and name_or_id.isdigit()):
            row = self.query_one("SELECT * FROM indexers WHERE id = ?", (int(name_or_id),))
            if row:
                return row
        return self.query_one("SELECT * FROM indexers WHERE lower(name) = lower(?)", (str(name_or_id),))

    def add_indexer(self, name: str, type_: str, url: str, api_key: str = "", categories: str = "",
                    priority: int = 25, enabled: bool = True) -> int:
        return self.execute(
            "INSERT INTO indexers(name, type, url, api_key, categories, enabled, priority, added_at) "
            "VALUES(?, ?, ?, ?, ?, ?, ?, ?)",
            (name, type_, url, api_key or "", categories or "", 1 if enabled else 0, priority, now_iso()))

    def update_indexer(self, indexer_id: int, **fields: Any) -> None:
        allowed = {"name", "type", "url", "api_key", "categories", "enabled", "priority"}
        sets = [(k, v) for k, v in fields.items() if k in allowed]
        if not sets:
            return
        self.execute("UPDATE indexers SET %s WHERE id = ?" % ", ".join("%s = ?" % k for k, _ in sets),
                     [v for _, v in sets] + [indexer_id])

    def delete_indexer(self, indexer_id: int) -> None:
        self.execute("DELETE FROM indexers WHERE id = ?", (indexer_id,))

    # --------------------------------------------------------------- media
    def get_media(self, media_id: int) -> Optional[Dict[str, Any]]:
        return self.query_one("SELECT * FROM media WHERE id = ?", (media_id,))

    def get_media_by_key(self, plex_key: str) -> Optional[Dict[str, Any]]:
        return self.query_one("SELECT * FROM media WHERE plex_key = ?", (plex_key,))

    def list_media(self, type_: Optional[str] = None, on_watchlist_only: bool = False) -> List[Dict[str, Any]]:
        sql, params = "SELECT * FROM media WHERE 1=1", []  # type: str, List[Any]
        if type_:
            sql += " AND type = ?"
            params.append(type_)
        if on_watchlist_only:
            sql += " AND on_watchlist = 1"
        return self.query(sql + " ORDER BY added_at DESC", params)

    def upsert_media(self, plex_key: str, type_: str, title: str, year: Optional[int], imdb: Optional[str],
                     tmdb: Optional[str], tvdb: Optional[str], original_title: Optional[str] = None) -> int:
        existing = self.get_media_by_key(plex_key)
        ts = now_iso()
        if existing:
            self.execute(
                "UPDATE media SET title = ?, year = COALESCE(?, year), imdb = COALESCE(?, imdb), "
                "tmdb = COALESCE(?, tmdb), tvdb = COALESCE(?, tvdb), original_title = COALESCE(?, original_title), "
                "on_watchlist = 1, updated_at = ? WHERE id = ?",
                (title, year, imdb, tmdb, tvdb, original_title, ts, existing["id"]))
            return int(existing["id"])
        status = "wanted" if type_ == "movie" else "monitored"
        return self.execute(
            "INSERT INTO media(plex_key, type, title, original_title, year, imdb, tmdb, tvdb, status, on_watchlist, "
            "added_at, updated_at) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)",
            (plex_key, type_, title, original_title, year, imdb, tmdb, tvdb, status, ts, ts))

    def set_media_status(self, media_id: int, status: str) -> None:
        self.execute("UPDATE media SET status = ?, updated_at = ? WHERE id = ?", (status, now_iso(), media_id))

    def touch_media_search(self, media_id: int) -> None:
        self.execute("UPDATE media SET last_search_at = ? WHERE id = ?", (now_iso(), media_id))

    def mark_off_watchlist(self, keep_keys: Iterable[str]) -> None:
        keys = list(keep_keys)
        if keys:
            placeholders = ",".join("?" * len(keys))
            self.execute("UPDATE media SET on_watchlist = 0 WHERE plex_key NOT IN (%s)" % placeholders, keys)
        else:
            self.execute("UPDATE media SET on_watchlist = 0")

    # ------------------------------------------------------------ episodes
    def upsert_episode(self, media_id: int, season: int, episode: int, title: Optional[str],
                       air_date: Optional[str], status: str = "wanted") -> int:
        existing = self.query_one("SELECT * FROM episodes WHERE media_id = ? AND season = ? AND episode = ?",
                                  (media_id, season, episode))
        if existing:
            self.execute("UPDATE episodes SET title = COALESCE(?, title), air_date = COALESCE(?, air_date) "
                         "WHERE id = ?", (title, air_date, existing["id"]))
            return int(existing["id"])
        return self.execute("INSERT INTO episodes(media_id, season, episode, title, air_date, status) "
                            "VALUES(?, ?, ?, ?, ?, ?)", (media_id, season, episode, title, air_date, status))

    def list_episodes(self, media_id: int, status: Optional[str] = None) -> List[Dict[str, Any]]:
        sql, params = "SELECT * FROM episodes WHERE media_id = ?", [media_id]  # type: str, List[Any]
        if status:
            sql += " AND status = ?"
            params.append(status)
        return self.query(sql + " ORDER BY season, episode", params)

    def get_episode(self, media_id: int, season: int, episode: int) -> Optional[Dict[str, Any]]:
        return self.query_one("SELECT * FROM episodes WHERE media_id = ? AND season = ? AND episode = ?",
                              (media_id, season, episode))

    def set_episode_status(self, media_id: int, season: int, episode: Optional[int], status: str,
                           only_if: Optional[Iterable[str]] = None) -> None:
        """Set status of one episode, or of a whole season when episode is None."""
        sql = "UPDATE episodes SET status = ? WHERE media_id = ? AND season = ?"
        params = [status, media_id, season]  # type: List[Any]
        if episode is not None:
            sql += " AND episode = ?"
            params.append(episode)
        if only_if:
            vals = list(only_if)
            sql += " AND status IN (%s)" % ",".join("?" * len(vals))
            params.extend(vals)
        self.execute(sql, params)

    def touch_episode_search(self, episode_id: int) -> None:
        self.execute("UPDATE episodes SET last_search_at = ? WHERE id = ?", (now_iso(), episode_id))

    # --------------------------------------------------------------- grabs
    def add_grab(self, media_id: Optional[int], season: Optional[int], episode: Optional[int], release_title: str,
                 torrent_name: Optional[str], indexer: Optional[str], info_hash: Optional[str],
                 size: Optional[int]) -> int:
        ts = now_iso()
        return self.execute(
            "INSERT INTO grabs(media_id, season, episode, release_title, torrent_name, indexer, info_hash, size, "
            "status, created_at, updated_at) VALUES(?, ?, ?, ?, ?, ?, ?, ?, 'sent', ?, ?)",
            (media_id, season, episode, release_title, torrent_name, indexer, info_hash, size, ts, ts))

    def set_grab_status(self, grab_id: int, status: str, message: Optional[str] = None) -> None:
        self.execute("UPDATE grabs SET status = ?, message = ?, updated_at = ? WHERE id = ?",
                     (status, message, now_iso(), grab_id))

    def find_grab_by_name(self, name: str) -> Optional[Dict[str, Any]]:
        """Find the most recent grab whose torrent name or release title matches *name* (case-insensitive)."""
        return self.query_one(
            "SELECT * FROM grabs WHERE lower(torrent_name) = lower(?) OR lower(release_title) = lower(?) "
            "ORDER BY id DESC LIMIT 1", (name, name))

    def list_grabs(self, status: Optional[str] = None, limit: int = 200) -> List[Dict[str, Any]]:
        sql, params = "SELECT * FROM grabs", []  # type: str, List[Any]
        if status:
            sql += " WHERE status = ?"
            params.append(status)
        return self.query(sql + " ORDER BY id DESC LIMIT ?", params + [limit])

    # ------------------------------------------------------------ blocklist
    def block_release(self, release_title: str, reason: str = "") -> None:
        self.execute("INSERT OR IGNORE INTO blocklist(release_title, reason, created_at) VALUES(?, ?, ?)",
                     (release_title, reason, now_iso()))

    def is_blocked(self, release_title: str) -> bool:
        return self.query_one("SELECT 1 AS x FROM blocklist WHERE lower(release_title) = lower(?)",
                              (release_title,)) is not None
