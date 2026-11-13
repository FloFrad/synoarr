"""Shared data structures used across synoarr modules.

Python 3.8 compatible (DSM 7 ships Python 3.8 at /usr/bin/python3).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional


@dataclass
class Release:
    """A search result returned by an indexer."""

    title: str
    indexer: str
    download_url: Optional[str] = None  # http(s) URL of a .torrent file (may redirect to a magnet)
    magnet: Optional[str] = None  # magnet:?xt=urn:btih:...
    size: int = 0  # bytes, 0 = unknown
    seeders: Optional[int] = None  # None = unknown (e.g. Internet Archive webseeds)
    leechers: Optional[int] = None
    info_hash: Optional[str] = None  # lowercase hex
    pub_date: Optional[str] = None
    info_url: Optional[str] = None
    categories: List[int] = field(default_factory=list)


@dataclass
class SearchRequest:
    """What we are looking for.

    kind: "movie" | "season" | "episode"
    """

    kind: str
    title: str
    year: Optional[int] = None
    season: Optional[int] = None
    episode: Optional[int] = None
    imdb: Optional[str] = None  # "tt1234567"
    tmdb: Optional[str] = None
    tvdb: Optional[str] = None
    alt_titles: List[str] = field(default_factory=list)
    # accepted audio languages for this request (empty = [quality] languages)
    languages: List[str] = field(default_factory=list)

    def query_string(self) -> str:
        """Free-text query used by indexers that do not support id search."""
        if self.kind == "movie":
            return "%s %s" % (self.title, self.year) if self.year else self.title
        if self.kind == "season" and self.season is not None:
            return "%s S%02d" % (self.title, self.season)
        if self.kind == "episode" and self.season is not None and self.episode is not None:
            return "%s S%02dE%02d" % (self.title, self.season, self.episode)
        return self.title

    def describe(self) -> str:
        return self.query_string()


@dataclass
class ParsedName:
    """Result of parsing a release / file name (see parser.parse)."""

    raw: str
    title: str = ""
    year: Optional[int] = None
    seasons: List[int] = field(default_factory=list)
    episodes: List[int] = field(default_factory=list)
    resolution: Optional[str] = None  # "2160p" | "1080p" | "720p" | "576p" | "480p"
    source: Optional[str] = None  # "bluray" | "webdl" | "webrip" | "hdtv" | "dvdrip" | "cam" | ...
    languages: List[str] = field(default_factory=list)  # e.g. ["fr"], ["multi"], ["en"], ["it"]
    is_season_pack: bool = False
    is_complete: bool = False  # "Complete series" / "Integrale"
    is_sample: bool = False
    extension: Optional[str] = None  # lowercase without dot, e.g. "mkv"

    @property
    def season(self) -> Optional[int]:
        return self.seasons[0] if self.seasons else None

    @property
    def episode(self) -> Optional[int]:
        return self.episodes[0] if self.episodes else None

    @property
    def is_episode(self) -> bool:
        return bool(self.episodes)


@dataclass
class WatchItem:
    """An entry of the Plex Watchlist."""

    rating_key: str
    type: str  # "movie" | "show"
    title: str
    year: Optional[int] = None
    guids: Dict[str, str] = field(default_factory=dict)  # {"imdb": "tt..", "tmdb": "123", "tvdb": "456"}
    original_title: Optional[str] = None
    genres: List[str] = field(default_factory=list)  # e.g. ["Animation", "Comedy"]


@dataclass
class EpisodeInfo:
    season: int
    episode: int
    title: Optional[str] = None
    air_date: Optional[str] = None  # "YYYY-MM-DD"


@dataclass
class DSTask:
    """A Download Station task."""

    id: str
    title: str
    status: str  # waiting|downloading|paused|finishing|finished|hash_checking|seeding|filehosting_waiting|extracting|error
    size: int = 0
    downloaded: int = 0
    destination: Optional[str] = None
    type: Optional[str] = None
    error: Optional[str] = None
    # live transfer information (None = not reported by this DSM version)
    speed_down: Optional[int] = None  # bytes/s
    speed_up: Optional[int] = None  # bytes/s
    seeders: Optional[int] = None  # connected seeders
    leechers: Optional[int] = None  # connected leechers
    peers: Optional[int] = None  # total peers known
    uploaded: Optional[int] = None  # bytes
    created: Optional[int] = None  # task creation time (epoch seconds)
    info_hash: Optional[str] = None  # lowercase hex, from a magnet URI when available

    @property
    def progress(self) -> float:
        """0..100 (100 once finished/seeding)."""
        if self.is_complete:
            return 100.0
        if self.size and self.size > 0:
            return max(0.0, min(100.0, 100.0 * float(self.downloaded or 0) / float(self.size)))
        return 0.0

    @property
    def is_complete(self) -> bool:
        return self.status in ("finished", "seeding")

    @property
    def is_active(self) -> bool:
        return self.status not in ("finished", "seeding", "error")
