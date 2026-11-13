"""Configuration loading (INI file, stdlib configparser only).

All values have defaults so that a minimal config file works. Every section is
exposed as a small dataclass so the rest of the code never touches configparser.
"""
from __future__ import annotations

import configparser
import logging
import os
import re
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
    # GitHub repository used by the "Mettre à jour" command (owner/name)
    update_repo: str = "FloFrad/synoarr"


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
    series_dir: str = "/volume1/video/TvShows"
    action: str = "move"  # move | copy | hardlink
    min_age_minutes: float = 5.0
    remove_task_after_import: bool = True
    delete_leftovers: bool = True
    only_known_downloads: bool = False  # True = only import downloads sent by synoarr


@dataclass
class NamingConfig:
    movie_format: str = "{title} ({year})"
    # used instead of movie_format when the movie belongs to a collection (TMDb key needed)
    movie_collection_format: str = "{collection}/{title} ({year})"
    series_format: str = "{show}/Saison {season:02d}/{show} - S{season:02d}E{episode:02d} - {episode_title}"
    subtitle_extensions: List[str] = field(default_factory=lambda: ["srt", "ass", "ssa", "sub", "idx", "vtt"])
    video_extensions: List[str] = field(
        default_factory=lambda: ["mkv", "mp4", "avi", "m4v", "mov", "wmv", "ts", "m2ts", "mpg", "mpeg", "webm", "ogv"]
    )


@dataclass
class QualityConfig:
    resolutions: List[str] = field(default_factory=lambda: ["1080p", "720p"])
    allow_other_resolutions: bool = False
    languages: List[str] = field(default_factory=lambda: ["en"])
    # languages for animated movies/shows (genre "Animation" on Plex or TMDb)
    languages_animation: List[str] = field(default_factory=lambda: ["fr"])
    min_seeders: int = 1
    min_size_mb: float = 100.0
    max_size_gb_movie: float = 25.0
    max_size_gb_episode: float = 6.0
    max_size_gb_season: float = 60.0
    reject_words: List[str] = field(
        default_factory=lambda: ["cam", "camrip", "hdcam", "ts", "telesync", "hdts", "tc", "telecine", "screener", "scr", "workprint"]
    )
    prefer_words: List[str] = field(default_factory=lambda: ["multisubs", "multi-subs", "subs"])


@dataclass
class SeriesConfig:
    monitor: str = "all"  # all | future | latest_season
    season_packs: bool = True


@dataclass
class TmdbConfig:
    api_key: str = ""
    language: str = "en-US"


@dataclass
class ProwlarrConfig:
    # Optional: Prowlarr instance whose indexers are kept in sync automatically. Empty = use the address/key
    # remembered by the last "Importer depuis Prowlarr" (web UI / CLI), if any.
    url: str = ""
    api_key: str = ""
    sync_interval_hours: float = 6.0  # 0 = no automatic sync


@dataclass
class JackettConfig:
    # Optional, same idea as [prowlarr] (use one OR the other). Empty = address/key remembered by the last
    # "Importer depuis Jackett". The sync frequency is [prowlarr] sync_interval_hours.
    url: str = ""
    api_key: str = ""


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
    prowlarr: ProwlarrConfig = field(default_factory=ProwlarrConfig)
    jackett: JackettConfig = field(default_factory=JackettConfig)
    warnings: List[str] = field(default_factory=list)  # non fatal problems found while loading (French)

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
        for fmt_name in ("movie_format", "movie_collection_format", "series_format"):
            fmt = getattr(self.naming, fmt_name)
            try:
                fmt.format(title="T", year=2000, show="S", collection="C", season=1, episode=2, episode_title="E", ext="mkv",
                           resolution="1080p", tmdb="1", imdb="tt1", tvdb="1")
            except (KeyError, IndexError, ValueError, AttributeError, TypeError) as exc:
                problems.append("[naming] %s invalide: %s" % (fmt_name, exc))
        if not self.downloadstation.url.startswith(("http://", "https://")):
            problems.append("[downloadstation] url doit commencer par http:// ou https://")
        return problems


def missing_share_root(path: str) -> Optional[str]:
    """For a Synology path /volumeN/<Share>/sub..., the share root if it does not exist (else None).

    A missing share must never be created by synoarr (a typo such as "Videos" instead of "Vidéos" would
    create a plain folder on the volume, invisible in File Station)."""
    m = re.match(r"^(/volume[^/]*/[^/]+)", (path or "").replace("\\", "/"))
    if not m:
        return None
    root = m.group(1)
    return None if os.path.isdir(root) else root


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
    "prowlarr": ProwlarrConfig,
    "jackett": JackettConfig,
}

_TRUE = ("1", "true", "yes", "on", "oui", "vrai")
_FALSE = ("0", "false", "no", "off", "non", "faux", "")


def _unquote(value: str) -> str:
    """'"/volume1/Vidéos"' -> '/volume1/Vidéos' (users often quote values, INI files do not need it)."""
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        return value[1:-1]
    return value


def _read_text(path: str) -> str:
    """config.ini edited from Windows over SMB may be UTF-8 with BOM, UTF-16 (old Notepad "Unicode")
    or ANSI/cp1252: accept all of them. Line endings (CRLF) are handled by universal newlines."""
    with open(path, "rb") as fh:
        data = fh.read()
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        text = data.decode("utf-16")
    else:
        try:
            text = data.decode("utf-8-sig")
        except UnicodeDecodeError:
            logging.getLogger(__name__).warning(
                "%s n'est pas enregistré en UTF-8 : lu comme Windows-1252 (ANSI). Enregistrez-le de préférence "
                "en UTF-8.", path)
            text = data.decode("cp1252", errors="replace")
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _convert(name: str, raw: str, default):
    raw = _unquote(raw.strip()).strip()
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
    # No inline comments: a password may contain " #" or " ;" or start with them ("#abc", ";abc").
    # Comments are whole lines starting with ; or # (as in config.example.ini).
    parser = configparser.ConfigParser(interpolation=None)
    if path:
        if not os.path.isfile(path):
            raise ConfigError("Fichier de configuration introuvable: %s" % path)
        try:
            text = _read_text(path)
        except OSError as exc:
            raise ConfigError("Lecture de %s impossible : %s (essayez avec sudo)" % (path, exc.strerror or exc))
        try:
            parser.read_string(text, source=path)
        except configparser.Error as exc:
            raise ConfigError("Erreur de syntaxe dans %s: %s" % (path, exc))
        for section in parser.sections():
            klass = _SECTIONS.get(section.lower()) if section.lower() == section else None
            if klass is None:
                cfg.warnings.append("Section [%s] inconnue dans %s (ignorée)%s" % (
                    section, os.path.basename(path),
                    " : les noms de section s'écrivent en minuscules" if section.lower() in _SECTIONS else ""))
                continue
            for key in parser.options(section):
                if key not in klass.__dataclass_fields__ and key not in parser.defaults():  # type: ignore
                    cfg.warnings.append("Option inconnue « %s » dans [%s] (ignorée, faute de frappe ?)"
                                        % (key, section))
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
    for attr in ("download_dir", "movies_dir", "series_dir"):
        value = getattr(cfg.paths, attr).strip()
        if len(value) > 1:
            value = value.rstrip("/")
        setattr(cfg.paths, attr, os.path.expanduser(value))
    cfg.naming.video_extensions = [e.lower().lstrip(".") for e in cfg.naming.video_extensions]
    cfg.naming.subtitle_extensions = [e.lower().lstrip(".") for e in cfg.naming.subtitle_extensions]
    return cfg
