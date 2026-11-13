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
import errno
import os
import sqlite3
import threading
import unicodedata
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


try:  # POSIX only (DSM, Docker)
    import fcntl as _fcntl
except ImportError:  # pragma: no cover - Windows
    _fcntl = None  # type: ignore


def try_file_lock(directory: str, name: str) -> Any:
    """Non-blocking inter-process lock (flock) on <directory>/<name>, so that a manual command
    (``sudo python3 synoarr.py process``) never races the daemon.

    Returns an fd to pass to release_file_lock(), None when locking is impossible (no fcntl, missing or
    read-only folder: proceed without lock) or False when another process holds the lock."""
    if _fcntl is None or not directory or not os.path.isdir(directory):
        return None
    try:
        fd = os.open(os.path.join(directory, name), os.O_RDWR | os.O_CREAT, 0o666)
    except OSError:
        return None
    try:
        _fcntl.flock(fd, _fcntl.LOCK_EX | _fcntl.LOCK_NB)
        return fd
    except OSError as exc:
        os.close(fd)
        if exc.errno in (errno.EAGAIN, errno.EACCES, errno.EWOULDBLOCK):
            return False
        return None


def release_file_lock(fd: Any) -> None:
    if fd is None or fd is False:
        return
    try:
        os.close(fd)  # closing the descriptor releases the flock
    except OSError:
        pass


def _fold(text: str) -> str:
    return unicodedata.normalize("NFC", text or "").strip().casefold()


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
            self._migrate()
            self._conn.commit()

    # ----------------------------------------------------------- primitives
    def execute(self, sql: str, params: Iterable[Any] = ()) -> int:
        """Run a write statement, commit, return lastrowid (or rowcount for UPDATE/DELETE)."""
        with self._lock:
            try:
                cur = self._conn.execute(sql, tuple(params))
                self._conn.commit()
            except BaseException:
                # never leave an implicit transaction open (it would hold the write lock / be committed later)
                try:
                    self._conn.rollback()
                except sqlite3.Error:
                    pass
                raise
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

    def list_media(self, type_: Optional[str] = None, on_watchlist_only: bool = False,
                   include_hidden: bool = False) -> List[Dict[str, Any]]:
        sql, params = "SELECT * FROM media WHERE 1=1", []  # type: str, List[Any]
        if type_:
            sql += " AND type = ?"
            params.append(type_)
        if on_watchlist_only:
            sql += " AND on_watchlist = 1"
        if not include_hidden:
            sql += " AND COALESCE(hidden, 0) = 0"
        return self.query(sql + " ORDER BY added_at DESC", params)

    # columns added after 1.0 (ALTER TABLE keeps existing databases working)
    _MEDIA_EXTRA_COLUMNS = (
        ("collection", "TEXT"),        # movie collection name (TMDb), e.g. "The Matrix Collection"
        ("genres", "TEXT"),            # comma separated genres from Plex/TMDb
        ("is_animation", "INTEGER"),   # 1 = animated (languages_animation applies), NULL = unknown
        ("language_override", "TEXT"), # manual languages for this item, e.g. "fr" or "en,fr"
        ("hidden", "INTEGER"),         # 1 = removed by the user but still in the Plex Watchlist (kept hidden)
    )

    def _migrate(self) -> None:
        cols = {r[1] for r in self._conn.execute("PRAGMA table_info(media)").fetchall()}
        for name, sqltype in self._MEDIA_EXTRA_COLUMNS:
            if name not in cols:
                self._conn.execute("ALTER TABLE media ADD COLUMN %s %s" % (name, sqltype))
        self._conn.commit()

    def set_media_details(self, media_id: int, collection: Optional[str] = None,
                          genres: Optional[List[str]] = None, is_animation: Optional[bool] = None) -> None:
        """Store optional metadata (only non-None values are written)."""
        sets, params = [], []  # type: List[str], List[Any]
        if collection is not None:
            sets.append("collection = ?")
            params.append(collection)
        if genres is not None:
            sets.append("genres = ?")
            params.append(",".join(genres))
        if is_animation is not None:
            sets.append("is_animation = ?")
            params.append(1 if is_animation else 0)
        if sets:
            self.execute("UPDATE media SET %s WHERE id = ?" % ", ".join(sets), params + [media_id])

    def set_language_override(self, media_id: int, languages: Optional[str]) -> None:
        self.execute("UPDATE media SET language_override = ? WHERE id = ?", (languages or None, media_id))

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
        """Plex items missing from *keep_keys* leave the watchlist. Local entries ("local:...") are never
        touched: they are not in the Plex Watchlist."""
        keys = list(keep_keys)
        local = " AND plex_key NOT LIKE 'local:%'"
        if keys:
            placeholders = ",".join("?" * len(keys))
            self.execute("UPDATE media SET on_watchlist = 0 WHERE plex_key NOT IN (%s)%s" % (placeholders, local),
                         keys)
        else:
            self.execute("UPDATE media SET on_watchlist = 0 WHERE 1=1" + local)

    # ---- watchlist add/delete
    def find_media_by_ids(self, type_: Optional[str], imdb: Optional[str] = None, tmdb: Optional[str] = None,
                          tvdb: Optional[str] = None, include_hidden: bool = True) -> Optional[Dict[str, Any]]:
        """Media row sharing one external id (imdb, then tmdb, then tvdb) with the same type, or None."""
        for col, value in (("imdb", imdb), ("tmdb", tmdb), ("tvdb", tvdb)):
            if not value:
                continue
            sql = "SELECT * FROM media WHERE %s = ?" % col
            params = [str(value)]  # type: List[Any]
            if type_:
                sql += " AND type = ?"
                params.append(type_)
            if not include_hidden:
                sql += " AND COALESCE(hidden, 0) = 0"
            row = self.query_one(sql + " ORDER BY id LIMIT 1", params)
            if row:
                return row
        return None

    def list_local_media(self) -> List[Dict[str, Any]]:
        """Entries added by hand in synoarr (not in the Plex Watchlist), visible ones only."""
        return self.query("SELECT * FROM media WHERE plex_key LIKE 'local:%' AND COALESCE(hidden, 0) = 0 "
                          "ORDER BY id")

    def set_media_hidden(self, media_id: int, hidden: bool) -> None:
        self.execute("UPDATE media SET hidden = ?, updated_at = ? WHERE id = ?",
                     (1 if hidden else None, now_iso(), media_id))

    def set_media_key(self, media_id: int, plex_key: str) -> None:
        self.execute("UPDATE media SET plex_key = ?, on_watchlist = 1, updated_at = ? WHERE id = ?",
                     (plex_key, now_iso(), media_id))

    def delete_media(self, media_id: int) -> int:
        """Delete a media row: its episodes go with it (ON DELETE CASCADE), its grabs stay in the history
        with media_id NULL (ON DELETE SET NULL)."""
        return self.execute("DELETE FROM media WHERE id = ?", (media_id,))

    def purge_hidden_off_watchlist(self) -> int:
        """Hidden Plex entries that the Plex Watchlist no longer returns are deleted for good."""
        return self.execute("DELETE FROM media WHERE COALESCE(hidden, 0) = 1 AND on_watchlist = 0 "
                            "AND plex_key NOT LIKE 'local:%'")
    # ---- end watchlist add/delete

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
        row = self.query_one(
            "SELECT * FROM grabs WHERE lower(torrent_name) = lower(?) OR lower(release_title) = lower(?) "
            "ORDER BY id DESC LIMIT 1", (name, name))
        if row is not None or not name:
            return row
        # SQLite lower() only folds ASCII, and macOS clients may write names in NFD: compare in Python
        wanted = _fold(name)
        for g in self.query("SELECT * FROM grabs ORDER BY id DESC LIMIT 1000"):
            if wanted in (_fold(g.get("torrent_name") or ""), _fold(g.get("release_title") or "")):
                return g
        return None

    def reset_grab_episodes(self, grab: Dict[str, Any]) -> int:
        """Episodes of *grab* still marked 'grabbed' go back to 'wanted' (failed download, or imported
        season pack that did not contain them), unless another active download covers them."""
        media_id, season, episode = grab.get("media_id"), grab.get("season"), grab.get("episode")
        if not media_id or season is None:
            return 0
        sql = ("UPDATE episodes SET status = 'wanted', last_search_at = NULL "
               "WHERE media_id = ? AND season = ? AND status = 'grabbed'")
        params = [media_id, season]  # type: List[Any]
        if episode is not None:
            eps = {int(episode)}
            from .parser import parse  # local import: parser does not depend on db
            parsed = parse(grab.get("release_title") or "")
            if parsed.season == season:
                eps.update(parsed.episodes)  # "S01E01E02" grabbed for E01 also covers E02
            sql += " AND episode IN (%s)" % ",".join("?" * len(eps))
            params.extend(sorted(eps))
        sql += (" AND NOT EXISTS (SELECT 1 FROM grabs g WHERE g.id != ? AND g.status = 'sent' "
                "AND g.media_id = episodes.media_id AND g.season = episodes.season "
                "AND (g.episode IS NULL OR g.episode = episodes.episode))")
        params.append(grab.get("id") or 0)
        return self.execute(sql, params)

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
