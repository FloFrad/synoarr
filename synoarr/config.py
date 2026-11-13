"""Configuration loading (INI file, stdlib configparser only).

All values have defaults so that a minimal config file works. Every section is
exposed as a small dataclass so the rest of the code never touches configparser.
"""
from __future__ import annotations

import configparser
import os
from dataclasses import dataclass, field
from typing import List, Optional

DEFAULT_CONFIG_NAME = "config.ini"


class ConfigError(Exception):
    pass


def _split_list(value: str) -> List[str]:
    return [v.strip() for v in (value or "").replace(";", ",").split(",") if v.strip()]


@dataclass
class GeneralConfig:
    data_dir: str = "./data"
    log_level: str = "INFO"
    watchlist_interval_minutes: int = 15
    process_interval_minutes: int = 2
    research_interval_hours: float = 6.0
    dry_run: bool = False


@dataclass
class PlexConfig:
    token: str = ""
    server_url: str = ""  # optional local server, used to refresh libraries after an import
    server_token: str = ""  # optional, defaults to token


@dataclass
class DownloadStationConfig:
    url: str = "http://127.0.0.1:5000"
    username: str = ""
    password: str = ""
    destination: str = "Download"  # shared folder (+ optional subfolder) where Download Station saves files
    verify_ssl: bool = False  # DSM usually uses a self-signed certificate


@dataclass
class PathsConfig:
    download_dir: str = "/volume1/Download"
    movies_dir: str = "/volume1/video/Films"
    series_dir: str = "/volume1/video/Series"
    action: str = "move"  # move | copy | hardlink
    min_age_minutes: float = 5.0
    remove_task_after_import: bool = True
    delete_leftovers: bool = True
    only_known_downloads: bool = False  # True = only import downloads sent by synoarr


@dataclass
class NamingConfig:
    movie_format: str = "{title} ({year})/{title} ({year})"
    series_format: str = "{show}/Season {season:02d}/{show} - S{season:02d}E{episode:02d}"
    subtitle_extensions: List[str] = field(default_factory=lambda: ["srt", "ass", "ssa", "sub", "idx", "vtt"])
    video_extensions: List[str] = field(
        default_factory=lambda: ["mkv", "mp4", "avi", "m4v", "mov", "wmv", "ts", "m2ts", "mpg", "mpeg", "webm", "ogv"]
    )


@dataclass
class QualityConfig:
    resolutions: List[str] = field(default_factory=lambda: ["1080p", "720p"])
    allow_other_resolutions: bool = False
    languages: List[str] = field(default_factory=lambda: ["en", "fr"])
    min_seeders: int = 1
    min_size_mb: float = 100.0
    max_size_gb_movie: float = 25.0
    max_size_gb_episode: float = 6.0
    max_size_gb_season: float = 60.0
    reject_words: List[str] = field(
        default_factory=lambda: ["cam", "camrip", "hdcam", "ts", "telesync", "hdts", "tc", "telecine", "screener", "scr", "workprint"]
    )
    prefer_words: List[str] = field(default_factory=list)


@dataclass
class SeriesConfig:
    monitor: str = "all"  # all | future | latest_season
    season_packs: bool = True


@dataclass
class TmdbConfig:
    api_key: str = ""
    language: str = "fr-FR"


@dataclass
class WebConfig:
    enabled: bool = True
    host: str = "0.0.0.0"
    port: int = 8778
    username: str = ""
    password: str = ""


@dataclass
class Config:
    path: Optional[str] = None
    general: GeneralConfig = field(default_factory=GeneralConfig)
    plex: PlexConfig = field(default_factory=PlexConfig)
    downloadstation: DownloadStationConfig = field(default_factory=DownloadStationConfig)
    paths: PathsConfig = field(default_factory=PathsConfig)
    naming: NamingConfig = field(default_factory=NamingConfig)
    quality: QualityConfig = field(default_factory=QualityConfig)
    series: SeriesConfig = field(default_factory=SeriesConfig)
    tmdb: TmdbConfig = field(default_factory=TmdbConfig)
    web: WebConfig = field(default_factory=WebConfig)

    @property
    def data_dir(self) -> str:
        """Absolute data dir. Relative paths are resolved against the config file folder."""
        d = os.path.expanduser(self.general.data_dir)
        if not os.path.isabs(d):
            base = os.path.dirname(os.path.abspath(self.path)) if self.path else os.getcwd()
            d = os.path.join(base, d)
        return os.path.normpath(d)

    def validate(self) -> List[str]:
        """Return a list of human readable problems (empty = OK)."""
        problems = []
        if self.paths.action not in ("move", "copy", "hardlink"):
            problems.append("[paths] action doit valoir move, copy ou hardlink (valeur: %r)" % self.paths.action)
        if self.series.monitor not in ("all", "future", "latest_season"):
            problems.append("[series] monitor doit valoir all, future ou latest_season (valeur: %r)" % self.series.monitor)
        for fmt_name in ("movie_format", "series_format"):
            fmt = getattr(self.naming, fmt_name)
            try:
                fmt.format(title="T", year=2000, show="S", season=1, episode=2, episode_title="E", ext="mkv",
                           resolution="1080p", tmdb="1", imdb="tt1", tvdb="1")
            except (KeyError, IndexError, ValueError) as exc:
                problems.append("[naming] %s invalide: %s" % (fmt_name, exc))
        if not self.downloadstation.url.startswith(("http://", "https://")):
            problems.append("[downloadstation] url doit commencer par http:// ou https://")
        return problems


_SECTIONS = {
    "general": GeneralConfig,
    "plex": PlexConfig,
    "downloadstation": DownloadStationConfig,
    "paths": PathsConfig,
    "naming": NamingConfig,
    "quality": QualityConfig,
    "series": SeriesConfig,
    "tmdb": TmdbConfig,
    "web": WebConfig,
}

_TRUE = ("1", "true", "yes", "on", "oui", "vrai")
_FALSE = ("0", "false", "no", "off", "non", "faux", "")


def _convert(name: str, raw: str, default):
    raw = raw.strip()
    if isinstance(default, bool):
        low = raw.lower()
        if low in _TRUE:
            return True
        if low in _FALSE:
            return False
        raise ConfigError("%s: valeur booléenne attendue (true/false), reçu %r" % (name, raw))
    if isinstance(default, int):
        try:
            return int(raw)
        except ValueError:
            raise ConfigError("%s: nombre entier attendu, reçu %r" % (name, raw))
    if isinstance(default, float):
        try:
            return float(raw.replace(",", "."))
        except ValueError:
            raise ConfigError("%s: nombre attendu, reçu %r" % (name, raw))
    if isinstance(default, list):
        return [v.lower() if name.startswith("quality") else v for v in _split_list(raw)]
    return raw


def load_config(path: Optional[str]) -> Config:
    """Load the INI file at *path*. Missing file => ConfigError.

    Environment variables SYNOARR_<SECTION>_<KEY> override file values
    (e.g. SYNOARR_DOWNLOADSTATION_PASSWORD).
    """
    cfg = Config(path=os.path.abspath(path) if path else None)
    parser = configparser.ConfigParser(interpolation=None, inline_comment_prefixes=(";", "#"))
    if path:
        if not os.path.isfile(path):
            raise ConfigError("Fichier de configuration introuvable: %s" % path)
        try:
            with open(path, "r", encoding="utf-8-sig") as fh:
                parser.read_file(fh)
        except configparser.Error as exc:
            raise ConfigError("Erreur de syntaxe dans %s: %s" % (path, exc))
    for section, klass in _SECTIONS.items():
        obj = getattr(cfg, section)
        for fname in klass.__dataclass_fields__:  # type: ignore[attr-defined]
            default = getattr(obj, fname)
            env_name = "SYNOARR_%s_%s" % (section.upper(), fname.upper())
            raw = None
            if parser.has_option(section, fname):
                raw = parser.get(section, fname)
            if env_name in os.environ:
                raw = os.environ[env_name]
            if raw is None:
                continue
            setattr(obj, fname, _convert("%s.%s" % (section, fname), raw, default))
    # light normalisation
    cfg.downloadstation.url = cfg.downloadstation.url.rstrip("/")
    cfg.downloadstation.destination = cfg.downloadstation.destination.strip().strip("/")
    cfg.plex.server_url = cfg.plex.server_url.rstrip("/")
    cfg.paths.action = cfg.paths.action.lower().strip()
    cfg.series.monitor = cfg.series.monitor.lower().strip()
    cfg.naming.video_extensions = [e.lower().lstrip(".") for e in cfg.naming.video_extensions]
    cfg.naming.subtitle_extensions = [e.lower().lstrip(".") for e in cfg.naming.subtitle_extensions]
    return cfg
