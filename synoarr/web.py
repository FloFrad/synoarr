"""Mini interface web (http.server) + API JSON de synoarr.

Une seule page HTML embarquée (vanilla JS, CSS inline, aucun CDN) et une API JSON sous ``/api``.

Onglets : Tableau de bord, Watchlist, À venir, Indexers, Historique, Commandes (toutes les commandes de la CLI, en
tâches d'arrière-plan suivies par ``GET /api/jobs/<id>``), Journal (``data/synoarr.log``) et Configuration
(éditeur de ``config.ini`` brut, validé avant remplacement). Tout passe par l'auth Basic et la protection CSRF.

Interface ``App`` attendue (fournie par ``app.py``) ::

    class App(Protocol):
        cfg: Config
        db: Database                                 # + set_language_override(media_id, "fr" | None)
        searcher: Searcher                           # facultatif : languages_for(media) -> List[str]
        def run_watchlist_now(self) -> bool          # lance en arrière-plan, retourne tout de suite
                                                     # (False = déjà en cours)
        def run_process_now(self) -> bool            # idem (traitement des téléchargements)
        def search_now(self, media_id: int) -> None  # recherche d'un élément de la Watchlist, en arrière-plan
        def set_media_status(self, media_id: int, status: str) -> None  # "ignored" | "wanted" | "monitored"
        def status(self) -> dict  # JSON : version, started_at, last_watchlist_run, last_process_run,
                                  #        last_errors (list), dry_run, plex_configured, ds_configured,
                                  #        restart_required (list), jobs_running (list)
        indexer_manager: IndexerManager
        # --- onglet Commandes (Job : id, command, label, running, result, to_dict()) ---
        def start_command(self, command: str, force: bool = False, query: str = "") -> Tuple[Job, bool]
            # command ∈ sync | search_all | process | dry_run | check | test_indexers | search | plex_login
            #           | telegram_test (F2)
            # (tâche, créée) ; créée=False si la même commande tourne déjà. ValueError -> 400
        def get_job(self, job_id: int) -> Optional[Job]
        def list_jobs(self) -> List[Job]                       # 20 dernières, la plus récente d'abord
        def grab_search_result(self, job_id: int, index: int) -> dict   # {"ok", "message"}
        def start_plex_login(self) -> Tuple[Job, bool]         # job.result = {"code", "url", "linked"}
        def restart(self) -> None                              # os.execv après la réponse
        # --- onglets Journal et Configuration ---
        def log_file(self) -> str
        def read_config_text(self) -> dict      # {"path", "text", "encoding", "backups", "restart_reasons"}
        def save_config_text(self, text: str) -> dict          # ConfigInvalid -> 400, BusyError -> 409
        def restore_config_backup(self) -> dict
        # --- progression des téléchargements et « Changer de source » (onglet Watchlist) ---
        def downloads_status(self) -> dict    # facultatif ; {"ok", "warning", "items", "failed", "active"} :
            # items = grabs « sent » + progression Download Station (cache ~5 s), failed = échecs encore
            # recherchés ; chaque entrée : grab_id, media_id, release_title, season, episode, indexer, status
            # (statut DS, « introuvable », « inconnu » si DS injoignable, « failed »), progress, size, downloaded,
            # speed_down, speed_up, seeders, leechers, peers, eta_seconds, task_id, error. Ne lève pas.
        def start_source_search(self, media_id, season=None, episode=None) -> Tuple[Job, bool]
            # job.result = {"request", "kind", "languages", "total", "results": [{index, title, size, seeders,
            #               leechers, indexer, priority, score (None = rejeté), reason}]}
        def replace_source(self, media_id, season=None, episode=None, grab_id=None, job_id=None, index=None,
                           link=None, torrent=None, delete_old=True, blocklist_old=False,
                           accept_rejected=False) -> dict   # {"ok", "message"} ; ValueError -> 400

    class IndexerManager(Protocol):   # les méthodes peuvent lever ValueError (message français) -> HTTP 400
        def list(self) -> List[dict]                 # lignes de la table indexers
        def catalog(self) -> List[dict]              # entrées CATALOG + "installed": bool
        def add_from_catalog(self, catalog_id: str) -> dict
        def add_url(self, name: str, url: str, api_key: str = "", categories: str = "") -> dict
        def test(self, indexer_id: int) -> Tuple[bool, str]
        def set_enabled(self, indexer_id: int, enabled: bool) -> None
        def set_priority(self, indexer_id: int, priority: int) -> dict   # 1 (le plus prioritaire) à 50
        def import_prowlarr(self, base_url: str, api_key: str) -> dict  # {"added", "updated", "skipped", "message"}
        def delete(self, indexer_id: int) -> None

API ajoutée : ``GET/POST /api/jobs``, ``GET /api/jobs/<id>``, ``POST /api/jobs/<id>/grab``, ``POST /api/plex/login``,
``POST /api/restart``, ``GET /api/logs?lines=&level=&q=``, ``GET /api/logs/download[?n=1..3]``,
``GET/POST /api/config``, ``POST /api/config/restore``, ``POST /api/media/<id>/language``,
``POST /api/indexers/<id>/priority`` (``{"priority": 1..50}``), ``POST /api/indexers/import-prowlarr``
(``{"url", "api_key"}``), ``GET /api/downloads`` (progression, aussi présente
dans ``GET /api/media`` : champ ``downloads`` de chaque élément), ``POST /api/media/<id>/sources``
(``{"season", "episode"}`` -> tâche de recherche de toutes les sources) et ``POST /api/media/<id>/replace``
(``{"season", "episode", "grab_id", "delete_old", "blocklist_old"}`` + ``{"job_id", "index", "accept_rejected"}``,
``{"link"}`` (magnet ou URL .torrent) ou ``{"torrent_base64"}`` (fichier .torrent, 700 Ko max)).
Watchlist (API App en tête de app.py) : ``GET /api/watchlist/search?q=&kind=&year=`` (app.search_titles),
``POST /api/watchlist/add`` (``{"candidate", "add_to_plex", "search_now"}``), ``POST /api/watchlist/manual``
(``{"type", "title", "year", "imdb", "tmdb", "search_now"}``), ``DELETE /api/media/<id>`` ou
``POST /api/media/<id>/remove`` (``{"remove_from_plex", "delete_downloads"}``). Les entrées masquées (hidden)
n'apparaissent ni dans /api/media ni dans les compteurs du tableau de bord.
Affiches, actions groupées, À venir (méthodes App facultatives) ::

        def request_posters(self, media_ids) -> bool   # cherche en arrière-plan (TMDb, limité) les affiches manquantes
                                                        # (media.poster NULL ; "" = aucune) + media.release_date des films
        def search_many(self, media_ids) -> None       # recherche plusieurs éléments dans UN fil d'arrière-plan
        def start_source_search(..., french_only=None)  # transmis seulement si la méthode accepte ce paramètre

``POST /api/media/bulk`` (``{"ids": [...], "action": "search"|"language"|"ignore"|"unignore"|"delete",
"languages", "remove_from_plex", "delete_downloads"}`` -> ``{"ok", "done", "skipped", "errors", "message", ...}``),
``GET /api/upcoming?days=30&past=7`` (``{"today", "upcoming", "recent"}`` : épisodes des séries suivies et films
recherchés datés), ``GET /api/dashboard`` (``{"storage": espace disque de la vidéothèque, "imports": 10 derniers}``).
``GET /api/config/form`` (sections et options du formulaire de configuration, voir configform.py ; secrets jamais
renvoyés) et ``POST /api/config/form`` (``{"changes": {section: {clé: valeur}}}`` : seules les clés modifiées sont
réécrites, puis app.save_config_text). ``GET /api/grabs`` et ``/api/events`` acceptent ``?since=AAAA-MM-JJ&until=``
(inclus) et renvoient alors ``{"items", "total", "limit", "since", "until"}`` (sans ces paramètres : liste, comme
avant). ``GET /api/status`` contient ``commands`` (app.COMMANDS) : l'onglet Commandes les range par catégorie.
Fichiers publics, sans authentification ni contrôle Host (aucun secret) : ``/manifest.webmanifest``, ``/icon.svg``,
``/icon-192.png``, ``/icon-512.png``, ``/apple-touch-icon.png`` (icônes PNG dessinées en Python pur, zlib).

Les clés d'API ne sortent jamais de l'API indexers/catalogue : elles sont remplacées par "••••". Seul l'onglet
Configuration montre le vrai fichier (c'est la configuration de l'utilisateur, derrière l'authentification).
"""
from __future__ import annotations

import base64
import binascii
import datetime as _dt
import hashlib
import hmac
import json
import logging
import os
import re
import shutil
import socket
import struct
import threading
import urllib.parse
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Optional, Tuple

from .app import BusyError, ConfigInvalid
from .httpclient import HttpError, redact

log = logging.getLogger(__name__)

MASK = "••••"
MAX_BODY = 1024 * 1024
_SECRET_KEYS = ("api_key", "apikey", "password", "passwd", "token", "passkey")


class HttpProblem(Exception):
    """Erreur renvoyée telle quelle au client : ``{"error": message}``."""

    def __init__(self, status: int, message: str, extra: Optional[Dict[str, Any]] = None):
        Exception.__init__(self, message)
        self.status = status
        self.message = message
        self.extra = extra or {}


class RawResponse(object):
    """Réponse non JSON (téléchargement du journal)."""

    def __init__(self, data: bytes, ctype: str, headers: Optional[Dict[str, str]] = None):
        self.data = data
        self.ctype = ctype
        self.headers = headers or {}


LOG_LEVELS = {"DEBUG": 10, "INFO": 20, "WARNING": 30, "ERROR": 40, "CRITICAL": 50}
_LOG_LINE = re.compile(r"^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d (DEBUG|INFO|WARNING|ERROR|CRITICAL)\b")
_LANG_LIST = re.compile(r"^[a-z]{2,6}(,[a-z]{2,6})*\Z")


def tail_log(path: str, lines: int = 300, level: str = "DEBUG", text: str = "",
             max_bytes: int = 1024 * 1024) -> Dict[str, Any]:
    """Dernières *lines* entrées du journal (niveau minimal *level*, contenant *text*), lues depuis la fin du
    fichier par blocs croissants (au plus *max_bytes*) : un journal de 2 Mo n'est pas relu en entier à chaque
    rafraîchissement. Les lignes de suite (traceback) restent attachées à leur entrée."""
    min_level = LOG_LEVELS.get((level or "DEBUG").upper(), 10)
    needle = (text or "").strip().lower()
    try:
        size = os.path.getsize(path)
    except OSError:
        return {"path": path, "exists": False, "size": 0, "lines": [], "matched": 0, "partial": False}
    chunk = min(max_bytes, max(64 * 1024, lines * 400))
    while True:
        start = max(0, size - chunk)
        with open(path, "rb") as fh:
            fh.seek(start)
            data = fh.read(size - start)
        if start > 0:  # première ligne coupée
            data = data[data.find(b"\n") + 1:] if b"\n" in data else b""
        records = []  # type: List[List[str]]
        for line in data.decode("utf-8", "replace").splitlines():
            m = _LOG_LINE.match(line)
            if m:
                records.append([m.group(1), line])
            elif records:
                records[-1][1] += "\n" + line
            elif line.strip():
                records.append(["INFO", line])
        matched = [r for r in records if LOG_LEVELS.get(r[0], 20) >= min_level
                   and (not needle or needle in r[1].lower())]
        if len(matched) >= lines or start == 0 or chunk >= max_bytes:
            break
        chunk = min(max_bytes, chunk * 4)
    out = matched[-lines:]
    return {"path": path, "exists": True, "size": size, "lines": [{"level": lv, "text": tx} for lv, tx in out],
            "matched": len(matched), "partial": start > 0}


def _redact_url(url: Any) -> Any:
    if not isinstance(url, str) or not url:
        return url
    out = redact(url).replace("%2A", "*")
    try:
        parts = urllib.parse.urlsplit(out)
    except ValueError:
        return out
    if "@" in parts.netloc:  # http://user:pass@host -> http://host
        out = urllib.parse.urlunsplit((parts.scheme, parts.netloc.rsplit("@", 1)[1], parts.path,
                                       parts.query, parts.fragment))
    return out


def mask_secrets(row: Any) -> Any:
    """Copie de *row* (dict) sans aucun secret : clés masquées, URLs nettoyées."""
    if not isinstance(row, dict):
        return row
    out = dict(row)
    for k in list(out):
        if k.lower() in _SECRET_KEYS:
            out[k] = MASK if out[k] else ""
        elif k.lower() in ("url", "link"):
            out[k] = _redact_url(out[k])
    return out


def _accepts(func: Any, name: str) -> bool:
    """True when *func* takes a keyword argument *name* (optional App features)."""
    import inspect
    try:
        params = inspect.signature(func).parameters
    except (TypeError, ValueError):
        return False
    return name in params or any(p.kind == p.VAR_KEYWORD for p in params.values())


def _nearest_existing(path: str) -> Optional[str]:
    """*path* or its nearest existing parent folder (the library folder may not exist yet), None if none."""
    cur = os.path.abspath(path)
    while True:
        if os.path.isdir(cur):
            return cur
        parent = os.path.dirname(cur)
        if parent == cur:
            return None
        cur = parent


def library_storage(cfg: Any) -> List[Dict[str, Any]]:
    """Free space of the video library: one entry per volume holding [paths] movies_dir / series_dir (both on
    the same share = one entry). {"labels", "dirs", "path", "total", "used", "free", "free_pct", "low"} or
    {"labels", "dirs", "error"}. Never raises."""
    paths = getattr(cfg, "paths", None)
    out = []  # type: List[Dict[str, Any]]
    by_dev = {}  # type: Dict[Any, Dict[str, Any]]
    for label, folder in (("Films", getattr(paths, "movies_dir", "")), ("Séries", getattr(paths, "series_dir", ""))):
        if not folder:
            continue
        existing = _nearest_existing(folder)
        try:
            if existing is None:
                raise OSError("dossier introuvable")
            dev = os.stat(existing).st_dev
            if dev in by_dev:
                by_dev[dev]["labels"].append(label)
                by_dev[dev]["dirs"].append(folder)
                continue
            usage = shutil.disk_usage(existing)
        except OSError as exc:
            out.append({"labels": [label], "dirs": [folder], "error": "Espace disque illisible : %s" % (
                exc.strerror or exc)})
            continue
        total, free = int(usage.total), int(usage.free)
        pct = round(100.0 * free / total, 1) if total else None
        entry = {"labels": [label], "dirs": [folder], "path": existing, "total": total, "used": max(0, total - free),
                 "free": free, "free_pct": pct, "low": pct is not None and pct < 10}
        by_dev[dev] = entry
        out.append(entry)
    return out


# ---------------------------------------------------------------- écran d'accueil du téléphone (PWA légère)
# Pas de service worker : il exige HTTPS. « Ajouter à l'écran d'accueil » marche quand même en HTTP sur le réseau
# local (Safari iPhone, Chrome Android) : l'icône ouvre synoarr en plein écran, sans barre d'adresse.
THEME_LIGHT, THEME_DARK, ICON_COLOR = "#ffffff", "#1b1f26", "#2563eb"

ICON_SVG = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512"><defs><linearGradient id="g" x1="0" '
            'y1="0" x2="0" y2="1"><stop offset="0" stop-color="#3b82f6"/><stop offset="1" stop-color="#1d4ed8"/>'
            '</linearGradient></defs><rect width="512" height="512" rx="104" fill="url(#g)"/>'
            '<path d="M184 154L358 256L184 358Z" fill="#fff"/></svg>')


def _png_chunk(tag: bytes, data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)


def make_icon_png(size: int) -> bytes:
    """The synoarr icon (blue gradient, white « play » triangle inside the maskable safe zone) as an RGB PNG of
    *size* x *size*, drawn and encoded with the standard library only (zlib), antialiased."""
    top, bottom = (0x3B, 0x82, 0xF6), (0x1D, 0x4E, 0xD8)
    s = float(size)
    x0, x1, yc, hh = s * 0.36, s * 0.70, s * 0.5, s * 0.20
    sub = 4
    raw = bytearray()
    for y in range(size):
        k = y / max(1.0, s - 1)
        bg = tuple(int(round(top[c] + (bottom[c] - top[c]) * k)) for c in range(3))
        row = bytearray(bytes(bg) * size)
        rights = []
        for j in range(sub):
            d = abs(y + (j + 0.5) / sub - yc)
            if d < hh:
                rights.append(x0 + (x1 - x0) * (1.0 - d / hh))
        if rights:
            lo, hi = int(x0), min(size, int(max(rights)) + 1)
            for i in range(lo, hi):
                cov = 0.0
                for r in rights:
                    a = min(r, i + 1.0) - max(x0, float(i))
                    if a > 0:
                        cov += a if a < 1.0 else 1.0
                cov /= sub
                if cov > 0:
                    row[3 * i:3 * i + 3] = bytes(int(round(bg[c] + (255 - bg[c]) * cov)) for c in range(3))
        raw += b"\x00" + row
    ihdr = struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + _png_chunk(b"IHDR", ihdr) + _png_chunk(b"IDAT", zlib.compress(bytes(raw), 9))
            + _png_chunk(b"IEND", b""))


def manifest_json() -> bytes:
    icons = [{"src": "icon-192.png", "sizes": "192x192", "type": "image/png", "purpose": "any"},
             {"src": "icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any"},
             {"src": "icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "maskable"},
             {"src": "icon.svg", "sizes": "any", "type": "image/svg+xml"}]
    data = {"name": "synoarr", "short_name": "synoarr", "lang": "fr", "dir": "ltr",
            "description": "Watchlist Plex, recherche et rangement des films et séries sur le NAS",
            "start_url": "./", "scope": "./", "display": "standalone", "orientation": "any",
            "background_color": "#f4f5f7", "theme_color": ICON_COLOR, "icons": icons}
    return json.dumps(data, ensure_ascii=False, indent=1).encode("utf-8")


# fichiers publics (aucun secret) : servis sans authentification ni contrôle de l'en-tête Host, car les navigateurs
# les demandent parfois sans identifiants (manifeste, icône de l'écran d'accueil)
PUBLIC_FILES = {
    "/manifest.webmanifest": ("application/manifest+json; charset=utf-8", manifest_json),
    "/icon.svg": ("image/svg+xml", lambda: ICON_SVG.encode("utf-8")),
    "/favicon.ico": ("image/png", lambda: make_icon_png(64)),
    "/icon-192.png": ("image/png", lambda: make_icon_png(192)),
    "/icon-512.png": ("image/png", lambda: make_icon_png(512)),
    "/apple-touch-icon.png": ("image/png", lambda: make_icon_png(180)),
    "/apple-touch-icon-precomposed.png": ("image/png", lambda: make_icon_png(180)),
}
_public_cache = {}  # type: Dict[str, bytes]
_public_lock = threading.Lock()


def public_file(path: str) -> Optional[Tuple[bytes, str]]:
    entry = PUBLIC_FILES.get(path)
    if entry is None:
        return None
    with _public_lock:
        if path not in _public_cache:
            _public_cache[path] = entry[1]()
        return _public_cache[path], entry[0]


def host_is_local(host_header: str) -> bool:
    """True for an IP address, localhost, a single-label LAN name ("diskstation") or *.local / *.lan / *.home."""
    import ipaddress
    host = (host_header or "").strip().lower()
    if not host:
        return True  # HTTP/1.0 clients, tools
    if host.startswith("["):  # [::1]:8778
        host = host[1:].split("]", 1)[0]
    elif host.count(":") == 1:
        host = host.split(":", 1)[0]
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        pass
    if host == "localhost" or "." not in host:
        return True
    return host.endswith((".local", ".lan", ".home", ".localdomain", ".home.arpa"))


class WebServer:
    def __init__(self, app: Any, host: str, port: int, username: str = "", password: str = ""):
        self.app = app
        self.host = host
        self.port = port  # remplacé par le port réel après start() (utile avec port 0)
        self.username = username or ""
        self.password = password or ""
        self._httpd = None  # type: Optional[ThreadingHTTPServer]
        self._thread = None  # type: Optional[threading.Thread]
        self._routes = [
            ("GET", r"/api/status", self.api_status),
            ("GET", r"/api/media", self.api_media),
            ("GET", r"/api/media/(\d+)/episodes", self.api_episodes),
            ("POST", r"/api/media/(\d+)/search", self.api_media_search),
            ("POST", r"/api/media/(\d+)/status", self.api_media_status),
            ("GET", r"/api/indexers", self.api_indexers),
            ("POST", r"/api/indexers", self.api_indexer_add),
            ("POST", r"/api/indexers/(\d+)/test", self.api_indexer_test),
            ("POST", r"/api/indexers/(\d+)/enabled", self.api_indexer_enabled),
            ("POST", r"/api/indexers/(\d+)/priority", self.api_indexer_priority),
            ("GET", r"/api/indexers/(\d+)", self.api_indexer_get),
            ("POST", r"/api/indexers/(\d+)/edit", self.api_indexer_edit),
            ("POST", r"/api/indexers/import-prowlarr", self.api_indexer_import_prowlarr),
            ("GET", r"/api/prowlarr", self.api_prowlarr_status),
            ("POST", r"/api/prowlarr/sync", self.api_prowlarr_sync),
            ("POST", r"/api/prowlarr/forget", self.api_prowlarr_forget),
            ("POST", r"/api/indexers/import-jackett", self.api_indexer_import_jackett),
            ("GET", r"/api/jackett", self.api_jackett_status),
            ("POST", r"/api/jackett/sync", self.api_jackett_sync),
            ("POST", r"/api/jackett/forget", self.api_jackett_forget),
            ("GET", r"/api/downloads", self.api_downloads),
            ("POST", r"/api/media/(\d+)/sources", self.api_media_sources),
            ("POST", r"/api/media/(\d+)/replace", self.api_media_replace),
            ("GET", r"/api/watchlist/search", self.api_watchlist_search),
            ("POST", r"/api/watchlist/add", self.api_watchlist_add),
            ("POST", r"/api/watchlist/manual", self.api_watchlist_manual),
            ("DELETE", r"/api/media/(\d+)", self.api_media_remove),
            ("POST", r"/api/media/(\d+)/remove", self.api_media_remove),
            ("DELETE", r"/api/indexers/(\d+)", self.api_indexer_delete),
            ("GET", r"/api/catalog", self.api_catalog),
            ("GET", r"/api/grabs", self.api_grabs),
            ("GET", r"/api/events", self.api_events),
            ("POST", r"/api/run/watchlist", self.api_run_watchlist),
            ("POST", r"/api/run/process", self.api_run_process),
            ("POST", r"/api/media/(\d+)/language", self.api_media_language),
            ("POST", r"/api/media/(\d+)/title_fr", self.api_media_title_fr),
            ("GET", r"/api/jobs", self.api_jobs),
            ("POST", r"/api/jobs", self.api_job_start),
            ("GET", r"/api/jobs/(\d+)", self.api_job),
            ("POST", r"/api/jobs/(\d+)/grab", self.api_job_grab),
            ("POST", r"/api/plex/login", self.api_plex_login),
            ("POST", r"/api/restart", self.api_restart),
            ("GET", r"/api/logs", self.api_logs),
            ("GET", r"/api/logs/download", self.api_logs_download),
            ("GET", r"/api/config", self.api_config),
            ("POST", r"/api/config", self.api_config_save),
            ("POST", r"/api/config/restore", self.api_config_restore),
            ("GET", r"/api/config/form", self.api_config_form),
            ("POST", r"/api/config/form", self.api_config_form_save),
            ("POST", r"/api/media/bulk", self.api_media_bulk),
            ("GET", r"/api/upcoming", self.api_upcoming),
            ("GET", r"/api/dashboard", self.api_dashboard),
        ]  # type: List[Tuple[str, str, Any]]
        self._compiled = [(m, re.compile(p + r"/?\Z"), f) for m, p, f in self._routes]

    @property
    def auth_enabled(self) -> bool:
        return bool(self.username and self.password)

    # ------------------------------------------------------------ lifecycle
    def start(self) -> None:
        handler = _make_handler(self)
        server_cls = ThreadingHTTPServer
        if ":" in self.host:
            class _V6(ThreadingHTTPServer):
                address_family = socket.AF_INET6
            server_cls = _V6
        self._httpd = server_cls((self.host, self.port), handler)
        self._httpd.daemon_threads = True
        self.port = int(self._httpd.server_address[1])
        self._thread = threading.Thread(target=self._httpd.serve_forever, kwargs={"poll_interval": 0.2},
                                        name="synoarr-web", daemon=True)
        self._thread.start()
        log.debug("Interface web en écoute sur %s:%s%s", self.host, self.port,
                 " (authentification activée)" if self.auth_enabled else "")

    def stop(self) -> None:
        if self._httpd is None:
            return
        self._httpd.shutdown()
        self._httpd.server_close()
        if self._thread is not None:
            self._thread.join(5)
        self._httpd = None
        self._thread = None

    # -------------------------------------------------------------- helpers
    def check_auth(self, header: Optional[str]) -> bool:
        if not self.auth_enabled:
            return True
        if not header or not header.lower().startswith("basic "):
            return False
        try:
            decoded = base64.b64decode(header[6:].strip(), validate=True).decode("utf-8")
        except (binascii.Error, ValueError):
            return False
        user, sep, pwd = decoded.partition(":")
        ok_user = hmac.compare_digest(user.encode("utf-8"), self.username.encode("utf-8"))
        ok_pwd = hmac.compare_digest(pwd.encode("utf-8"), self.password.encode("utf-8"))
        return bool(sep) and ok_user and ok_pwd

    def dispatch(self, method: str, path: str, query: Dict[str, str], body: Any) -> Tuple[int, Any]:
        allowed = False
        for m, rx, func in self._compiled:
            match = rx.match(path)
            if not match:
                continue
            if m != method:
                allowed = True
                continue
            args = [int(g) for g in match.groups()]
            return 200, func(*args, query=query, body=body)
        if allowed:
            raise HttpProblem(405, "Méthode non autorisée")
        raise HttpProblem(404, "Ressource introuvable : %s" % path)

    def _media_or_404(self, media_id: int) -> Dict[str, Any]:
        media = self.app.db.get_media(media_id)
        if not media:
            raise HttpProblem(404, "Élément de la Watchlist introuvable (id %d)" % media_id)
        return media

    @staticmethod
    def _limit(query: Dict[str, str], default: int) -> int:
        try:
            return max(1, min(1000, int(query.get("limit", default))))
        except ValueError:
            raise HttpProblem(400, "Paramètre limit invalide")

    @staticmethod
    def _body_dict(body: Any) -> Dict[str, Any]:
        if body is None:
            return {}
        if not isinstance(body, dict):
            raise HttpProblem(400, "Objet JSON attendu")
        return body

    # ------------------------------------------------------------ endpoints
    def api_status(self, query: Dict[str, str], body: Any) -> Dict[str, Any]:
        status = dict(self.app.status() or {})
        db = self.app.db
        one = db.query_one(
            "SELECT "
            "(SELECT COUNT(*) FROM media WHERE type='movie' AND on_watchlist=1 AND COALESCE(hidden,0)=0) AS movies, "
            "(SELECT COUNT(*) FROM media WHERE type='show' AND on_watchlist=1 AND COALESCE(hidden,0)=0) AS shows, "
            "(SELECT COUNT(*) FROM media WHERE type='movie' AND status='wanted' AND on_watchlist=1 "
            " AND COALESCE(hidden,0)=0) AS movies_wanted, "
            "(SELECT COUNT(*) FROM episodes e JOIN media m ON m.id=e.media_id WHERE e.status='wanted' "
            " AND COALESCE(m.hidden,0)=0 "
            " AND m.status='monitored' AND m.on_watchlist=1) AS episodes_wanted, "
            "(SELECT COUNT(*) FROM grabs WHERE status='sent') AS downloading, "
            "(SELECT COUNT(*) FROM indexers WHERE enabled=1) AS indexers_enabled") or {}
        status["counts"] = one
        try:  # commandes connues du programme (onglet Commandes : seules celles-ci sont proposées)
            from .app import COMMANDS
            status.setdefault("commands", dict(COMMANDS))
        except ImportError:  # pragma: no cover
            pass
        return status

    def _downloads(self) -> Dict[str, Any]:
        func = getattr(self.app, "downloads_status", None)
        empty = {"ok": False, "warning": None, "items": [], "failed": [], "active": 0}  # type: Dict[str, Any]
        if func is None:
            return empty
        try:
            return func()
        except Exception as exc:  # never a 500 for the progress: the list must stay usable
            log.warning("Progression des téléchargements indisponible : %s", exc)
            return dict(empty, warning="Progression des téléchargements indisponible : %s" % exc)

    def api_downloads(self, query: Dict[str, str], body: Any) -> Dict[str, Any]:
        return self._downloads()

    def api_media(self, query: Dict[str, str], body: Any) -> List[Dict[str, Any]]:
        db = self.app.db
        dls = self._downloads()
        by_media = {}  # type: Dict[Any, List[Dict[str, Any]]]
        for d in list(dls.get("items") or []) + list(dls.get("failed") or []):
            by_media.setdefault(d.get("media_id"), []).append(d)
        summary = {}
        for r in db.query(
                "SELECT media_id, COUNT(*) AS total, "
                "SUM(CASE WHEN status='done' THEN 1 ELSE 0 END) AS done, "
                "SUM(CASE WHEN status='grabbed' THEN 1 ELSE 0 END) AS grabbed, "
                "SUM(CASE WHEN status='wanted' THEN 1 ELSE 0 END) AS wanted "
                "FROM episodes GROUP BY media_id"):
            summary[r["media_id"]] = {k: int(r[k] or 0) for k in ("total", "done", "grabbed", "wanted")}
        out = []
        languages_for = getattr(getattr(self.app, "searcher", None), "languages_for", None)
        default_langs = list(getattr(getattr(self.app.cfg, "quality", None), "languages", []) or [])
        for m in db.list_media():
            m = dict(m)
            try:
                m["languages"] = list(languages_for(m)) if languages_for else default_langs
            except Exception:  # pragma: no cover - never break the list for one item
                m["languages"] = default_langs
            if m.get("type") == "show":
                m["episodes"] = summary.get(m["id"], {"total": 0, "done": 0, "grabbed": 0, "wanted": 0})
            m["downloads"] = by_media.get(m["id"], [])
            out.append(m)
        self._request_posters([m["id"] for m in out if m.get("poster") is None and m.get("on_watchlist")])
        return out

    def _request_posters(self, ids: List[int]) -> None:
        """Missing posters are looked up on TMDb in the background (throttled by the App), never blocking."""
        func = getattr(self.app, "request_posters", None)
        if ids and func is not None:
            try:
                func(ids)
            except Exception as exc:  # pragma: no cover - a poster never breaks the list
                log.debug("Affiches : %s", exc)

    def api_episodes(self, media_id: int, query: Dict[str, str], body: Any) -> List[Dict[str, Any]]:
        self._media_or_404(media_id)
        return self.app.db.list_episodes(media_id)

    def api_media_search(self, media_id: int, query: Dict[str, str], body: Any) -> Dict[str, Any]:
        media = self._media_or_404(media_id)
        self.app.search_now(media_id)
        return {"ok": True, "message": "Recherche lancée pour « %s »" % media["title"]}

    def api_media_status(self, media_id: int, query: Dict[str, str], body: Any) -> Dict[str, Any]:
        media = self._media_or_404(media_id)
        status = self._body_dict(body).get("status")
        if status not in ("ignored", "wanted", "monitored"):
            raise HttpProblem(400, "Statut invalide : utiliser ignored, wanted ou monitored")
        if status != "ignored":  # le statut « actif » dépend du type
            status = "monitored" if media["type"] == "show" else "wanted"
        self.app.set_media_status(media_id, status)
        return {"ok": True, "status": status}

    def api_indexers(self, query: Dict[str, str], body: Any) -> List[Dict[str, Any]]:
        return [mask_secrets(r) for r in self.app.indexer_manager.list()]

    def api_indexer_get(self, indexer_id: int, query: Dict[str, str], body: Any = None) -> Dict[str, Any]:
        """One indexer for the edit form: real URL (behind authentication), API key masked."""
        mgr = self.app.indexer_manager
        row = mgr._row(indexer_id)
        out = dict(row)
        out["api_key"] = "••••" if row.get("api_key") else ""
        out["has_api_key"] = bool(row.get("api_key"))
        out["synced_by"] = mgr.synced_by(row)
        return out

    def api_indexer_edit(self, indexer_id: int, query: Dict[str, str], body: Any) -> Dict[str, Any]:
        data = self._body_dict(body)

        def opt(key: str) -> Optional[str]:
            v = data.get(key)
            if v is None:
                return None
            if not isinstance(v, (str, int, float)):
                raise HttpProblem(400, "Valeur invalide pour %s" % key)
            return str(v)
        row = self.app.indexer_manager.update(
            indexer_id, name=opt("name"), url=opt("url"), api_key=opt("api_key"),
            clear_api_key=bool(data.get("clear_api_key")), categories=opt("categories"), type_=opt("type"),
            priority=data.get("priority"),
            enabled=None if data.get("enabled") is None else bool(data.get("enabled")))
        return {"ok": True, "message": "Indexer « %s » modifié" % row.get("name"), "indexer": mask_secrets(row)}

    def api_catalog(self, query: Dict[str, str], body: Any) -> List[Dict[str, Any]]:
        return [mask_secrets(r) for r in self.app.indexer_manager.catalog()]

    def api_indexer_add(self, query: Dict[str, str], body: Any) -> Dict[str, Any]:
        data = self._body_dict(body)
        mgr = self.app.indexer_manager
        if data.get("catalog_id"):
            row = mgr.add_from_catalog(str(data["catalog_id"]))
        else:
            name = str(data.get("name") or "").strip()
            url = str(data.get("url") or "").strip()
            if not name or not url:
                raise HttpProblem(400, "Le nom et l'URL de l'indexer sont obligatoires")
            prio = data.get("priority")
            if prio in (None, ""):
                row = mgr.add_url(name, url, str(data.get("api_key") or "").strip(),
                                  str(data.get("categories") or "").strip())
            else:
                row = mgr.add_url(name, url, str(data.get("api_key") or "").strip(),
                                  str(data.get("categories") or "").strip(), priority=prio)
        return mask_secrets(row)

    def api_indexer_test(self, indexer_id: int, query: Dict[str, str], body: Any) -> Dict[str, Any]:
        ok, message = self.app.indexer_manager.test(indexer_id)
        return {"ok": bool(ok), "message": message}

    def api_indexer_enabled(self, indexer_id: int, query: Dict[str, str], body: Any) -> Dict[str, Any]:
        enabled = self._body_dict(body).get("enabled")
        if not isinstance(enabled, bool):
            raise HttpProblem(400, "Le champ « enabled » doit valoir true ou false")
        self.app.indexer_manager.set_enabled(indexer_id, enabled)
        return {"ok": True, "enabled": enabled}

    def api_indexer_priority(self, indexer_id: int, query: Dict[str, str], body: Any) -> Dict[str, Any]:
        prio = self._body_dict(body).get("priority")
        if isinstance(prio, bool) or not isinstance(prio, int) or not 1 <= prio <= 50:
            raise HttpProblem(400, "Priorité invalide : nombre entier de 1 (le plus prioritaire) à 50 attendu")
        row = self.app.indexer_manager.set_priority(indexer_id, prio) or {}
        return {"ok": True, "priority": prio,
                "message": "Priorité de « %s » : %d" % (row.get("name") or indexer_id, prio)}

    def api_indexer_import_prowlarr(self, query: Dict[str, str], body: Any) -> Dict[str, Any]:
        data = self._body_dict(body)
        url, key = data.get("url"), data.get("api_key")
        if not isinstance(url, str) or (key is not None and not isinstance(key, str)):
            raise HttpProblem(400, "L'adresse de Prowlarr et sa clé API sont obligatoires")
        func = getattr(self.app.indexer_manager, "import_prowlarr", None)
        if func is None:
            raise HttpProblem(501, "Import Prowlarr non disponible")
        return func(url, key or "")

    def api_prowlarr_status(self, query: Dict[str, str], body: Any = None) -> Dict[str, Any]:
        func = getattr(self.app, "prowlarr_status", None)
        return func() if func else {"configured": False}

    def api_prowlarr_sync(self, query: Dict[str, str], body: Any) -> Dict[str, Any]:
        mgr = self.app.indexer_manager
        if not mgr.prowlarr_settings(self.app.cfg)["configured"]:
            raise HttpProblem(400, "Prowlarr n'est pas configuré : utilisez d'abord « Importer depuis Prowlarr »")
        res = mgr.sync_prowlarr(self.app.cfg) or {}
        if res.get("ok") is False:
            raise HttpProblem(502, "Synchronisation Prowlarr impossible : %s" % res.get("message"))
        return res

    def api_prowlarr_forget(self, query: Dict[str, str], body: Any) -> Dict[str, Any]:
        self.app.indexer_manager.forget_prowlarr()
        return {"ok": True, "message": "Synchronisation automatique arrêtée (les indexers déjà importés sont gardés)"}

    def api_indexer_import_jackett(self, query: Dict[str, str], body: Any) -> Dict[str, Any]:
        data = self._body_dict(body)
        url, key = data.get("url"), data.get("api_key")
        if not isinstance(url, str) or (key is not None and not isinstance(key, str)):
            raise HttpProblem(400, "L'adresse de Jackett et sa clé API sont obligatoires")
        return self.app.indexer_manager.import_jackett(url, key or "")

    def api_jackett_status(self, query: Dict[str, str], body: Any = None) -> Dict[str, Any]:
        func = getattr(self.app, "jackett_status", None)
        return func() if func else {"configured": False}

    def api_jackett_sync(self, query: Dict[str, str], body: Any) -> Dict[str, Any]:
        mgr = self.app.indexer_manager
        if not mgr.jackett_settings(self.app.cfg)["configured"]:
            raise HttpProblem(400, "Jackett n'est pas configuré : utilisez d'abord « Importer depuis Jackett »")
        res = mgr.sync_jackett(self.app.cfg) or {}
        if res.get("ok") is False:
            raise HttpProblem(502, "Synchronisation Jackett impossible : %s" % res.get("message"))
        return res

    def api_jackett_forget(self, query: Dict[str, str], body: Any) -> Dict[str, Any]:
        self.app.indexer_manager.forget_jackett()
        return {"ok": True, "message": "Synchronisation automatique avec Jackett arrêtée (indexers gardés)"}

    def api_media_sources(self, media_id: int, query: Dict[str, str], body: Any) -> Dict[str, Any]:
        self._media_or_404(media_id)
        data = self._body_dict(body)
        french_only = data.get("french_only")
        if french_only is not None and not isinstance(french_only, bool):
            raise HttpProblem(400, "Le champ « french_only » doit valoir true ou false")
        func = self.app.start_source_search
        if french_only is not None and _accepts(func, "french_only"):
            job, created = func(media_id, data.get("season"), data.get("episode"), french_only=french_only)
        else:
            job, created = func(media_id, data.get("season"), data.get("episode"))
        msg = ("« %s » lancé" % job.label) if created else ("« %s » est déjà en cours" % job.label)
        return {"ok": True, "created": created, "message": msg, "job": job.to_dict()}

    def api_media_replace(self, media_id: int, query: Dict[str, str], body: Any) -> Dict[str, Any]:
        self._media_or_404(media_id)
        data = self._body_dict(body)
        torrent = None
        raw = data.get("torrent_base64")
        if raw not in (None, ""):
            if not isinstance(raw, str):
                raise HttpProblem(400, "Fichier .torrent invalide")
            if "," in raw[:100] and raw.startswith("data:"):  # data:application/x-bittorrent;base64,....
                raw = raw.split(",", 1)[1]
            try:
                torrent = base64.b64decode(raw, validate=True)
            except (binascii.Error, ValueError):
                raise HttpProblem(400, "Fichier .torrent invalide (encodage base64 incorrect)")
            if not torrent:
                raise HttpProblem(400, "Fichier .torrent vide")
        link = data.get("link")
        if link is not None and not isinstance(link, str):
            raise HttpProblem(400, "Le champ « link » doit être un lien magnet ou une adresse http(s)")
        return self.app.replace_source(
            media_id, season=data.get("season"), episode=data.get("episode"), grab_id=data.get("grab_id"),
            job_id=data.get("job_id"), index=data.get("index"), link=link, torrent=torrent,
            delete_old=data.get("delete_old", True) is not False, blocklist_old=data.get("blocklist_old") is True,
            accept_rejected=data.get("accept_rejected") is True)

    # ------------------------------------------------- ajout / suppression dans la Watchlist
    def _app_func(self, name: str) -> Any:
        func = getattr(self.app, name, None)
        if func is None:
            raise HttpProblem(501, "Fonction non disponible : %s" % name)
        return func

    def api_watchlist_search(self, query: Dict[str, str], body: Any) -> Dict[str, Any]:
        q = (query.get("q") or "").strip()
        if len(q) > 200:
            raise HttpProblem(400, "Recherche trop longue (200 caractères maximum)")
        return self._app_func("search_titles")(q, query.get("kind") or None, query.get("year") or None)

    def _added(self, row: Dict[str, Any], search_now: bool) -> Dict[str, Any]:
        label = "%s%s" % (row.get("title"), " (%s)" % row["year"] if row.get("year") else "")
        if row.get("already_present") and row.get("reactivated"):
            msg = "« %s » réactivé (déjà connu de synoarr)" % label
        elif row.get("already_present"):
            msg = "« %s » est déjà dans la liste" % label
        elif row.get("plex_added"):
            msg = "« %s » ajouté à la Watchlist Plex" % label
        else:
            msg = "« %s » ajouté (entrée locale, hors Watchlist Plex)" % label
        started = False
        if search_now and row.get("id") and row.get("status") in ("wanted", "monitored"):
            self.app.search_now(int(row["id"]))
            started = True
            msg += " — recherche lancée"
        media = {k: row.get(k) for k in ("id", "type", "title", "year", "status", "plex_key", "imdb", "tmdb", "tvdb")}
        return {"ok": True, "message": msg, "warning": row.get("warning"), "already_present":
                bool(row.get("already_present")), "reactivated": bool(row.get("reactivated")),
                "plex_added": row.get("plex_added"), "search_started": started,
                "media": media}

    def api_watchlist_add(self, query: Dict[str, str], body: Any) -> Dict[str, Any]:
        data = self._body_dict(body)
        cand = data.get("candidate")
        if not isinstance(cand, dict):
            raise HttpProblem(400, "Le champ « candidate » (résultat de la recherche) est obligatoire")
        langs = self._parse_add_languages(data.get("languages"))  # validated before anything is added
        row = self._app_func("add_to_watchlist")(cand, data.get("add_to_plex", True) is not False)
        self._apply_add_languages(row, langs)
        self._keep_candidate_poster(row, cand.get("poster_url"))
        return self._added(row, data.get("search_now", True) is not False)

    def _keep_candidate_poster(self, row: Dict[str, Any], url: Any) -> None:
        """The poster shown in the « Ajouter » window (Plex or TMDb, public https URL) becomes the Watchlist
        thumbnail: no TMDb lookup needed later."""
        if not row.get("id") or not isinstance(url, str) or not re.match(r"^https://[^\s\"'<>]{1,500}\Z", url) \
                or "token" in url.lower():
            return
        try:
            media = self.app.db.get_media(int(row["id"])) or {}
            if media.get("poster") is None:
                self.app.db.set_media_artwork(int(row["id"]), url)
        except Exception as exc:  # pragma: no cover - older database API
            log.debug("Affiche du candidat non enregistrée : %s", exc)

    def _apply_add_languages(self, row: Dict[str, Any], value: Optional[str]) -> None:
        """Languages chosen in the « Ajouter » window, applied BEFORE the first search."""
        if value and row.get("id"):
            self.app.db.set_language_override(int(row["id"]), value)

    def _parse_add_languages(self, raw: Any) -> Optional[str]:
        if raw in (None, ""):
            return None
        if not isinstance(raw, str):
            raise HttpProblem(400, "Le champ « languages » doit être une chaîne (ex. \"fr\")")
        value = ",".join(x.strip().lower() for x in raw.replace(";", ",").split(",") if x.strip())
        if value and not _LANG_LIST.match(value):
            raise HttpProblem(400, "Langues invalides : codes de 2 à 6 lettres séparés par des virgules (ex. en,fr)")
        return value or None

    def api_watchlist_manual(self, query: Dict[str, str], body: Any) -> Dict[str, Any]:
        data = self._body_dict(body)
        for k in ("type", "title", "imdb", "tmdb"):
            if data.get(k) is not None and not isinstance(data.get(k), str):
                raise HttpProblem(400, "Champ « %s » invalide" % k)
        langs = self._parse_add_languages(data.get("languages"))
        row = self._app_func("add_manual")(data.get("type") or "", data.get("title") or "", data.get("year"),
                                           (data.get("imdb") or "").strip() or None,
                                           (data.get("tmdb") or "").strip() or None)
        self._apply_add_languages(row, langs)
        return self._added(row, data.get("search_now", True) is not False)

    def api_media_remove(self, media_id: int, query: Dict[str, str], body: Any) -> Dict[str, Any]:
        media = self._media_or_404(media_id)
        data = self._body_dict(body)
        res = dict(self._app_func("remove_from_watchlist")(
            media_id, remove_from_plex=data.get("remove_from_plex", True) is not False,
            delete_downloads=data.get("delete_downloads") is True) or {})
        msg = "« %s » retiré de la liste" % media["title"]
        if res.get("plex_removed"):
            msg += " et de la Watchlist Plex"
        if res.get("downloads_deleted"):
            msg += " — %d téléchargement(s) supprimé(s) de Download Station" % res["downloads_deleted"]
        res.update({"ok": True, "message": msg})
        return res

    def api_indexer_delete(self, indexer_id: int, query: Dict[str, str], body: Any) -> Dict[str, Any]:
        self.app.indexer_manager.delete(indexer_id)
        return {"ok": True}

    @staticmethod
    def _period(query: Dict[str, str]) -> Optional[Tuple[str, str]]:
        """?since=YYYY-MM-DD&until=YYYY-MM-DD (inclusive) -> (since, until), None without these parameters."""
        if "since" not in query and "until" not in query:
            return None
        out = []
        for name, empty in (("since", "0001-01-01"), ("until", "9999-12-31")):
            raw = (query.get(name) or "").strip()
            if not raw:
                out.append(empty)
                continue
            try:
                out.append(_dt.datetime.strptime(raw, "%Y-%m-%d").date().isoformat())
            except ValueError:
                raise HttpProblem(400, "Date invalide pour « %s » : AAAA-MM-JJ attendu (ex. 2026-11-01)" % name)
        if out[0] > out[1]:
            raise HttpProblem(400, "Période invalide : la date de début est après la date de fin")
        return out[0], out[1]

    def _history(self, sql: str, count_sql: str, date_col: str, query: Dict[str, str], default: int) -> Any:
        period = self._period(query)
        if period is None:  # previous behaviour: latest *limit* rows, plain list
            return self.app.db.query(sql % "1=1", (self._limit(query, default),))
        cond = "substr(%s, 1, 10) BETWEEN ? AND ?" % date_col
        limit = self._limit(query, 500)
        rows = self.app.db.query(sql % cond, (period[0], period[1], limit))
        total = (self.app.db.query_one(count_sql % cond, period) or {}).get("n", 0)
        return {"items": rows, "total": int(total or 0), "limit": limit, "since": period[0], "until": period[1]}

    def api_grabs(self, query: Dict[str, str], body: Any) -> Any:
        """Envois, les plus récents d'abord. Avec ?since=&until= : {"items", "total", "limit", "since", "until"}."""
        return self._history(
            "SELECT g.*, m.title AS media_title, m.type AS media_type FROM grabs g "
            "LEFT JOIN media m ON m.id = g.media_id WHERE %s ORDER BY g.id DESC LIMIT ?",
            "SELECT COUNT(*) AS n FROM grabs g WHERE %s", "g.created_at", query, 200)

    def api_events(self, query: Dict[str, str], body: Any) -> Any:
        """Événements, les plus récents d'abord. Avec ?since=&until= : même format que /api/grabs."""
        return self._history("SELECT * FROM events WHERE %s ORDER BY id DESC LIMIT ?",
                             "SELECT COUNT(*) AS n FROM events WHERE %s", "ts", query, 100)

    def api_run_watchlist(self, query: Dict[str, str], body: Any) -> Dict[str, Any]:
        if self.app.run_watchlist_now() is False:
            return {"ok": True, "message": "Une synchronisation de la Watchlist est déjà en cours"}
        return {"ok": True, "message": "Synchronisation de la Watchlist lancée"}

    def api_run_process(self, query: Dict[str, str], body: Any) -> Dict[str, Any]:
        if self.app.run_process_now() is False:
            return {"ok": True, "message": "Un traitement des téléchargements est déjà en cours"}
        return {"ok": True, "message": "Traitement des téléchargements lancé"}

    def api_media_language(self, media_id: int, query: Dict[str, str], body: Any) -> Dict[str, Any]:
        media = self._media_or_404(media_id)
        raw = self._body_dict(body).get("languages")
        if raw is None:
            raw = ""
        if not isinstance(raw, str):
            raise HttpProblem(400, "Le champ « languages » doit être une chaîne (ex. \"fr\" ou \"en,fr\")")
        value = ",".join(x.strip().lower() for x in raw.replace(";", ",").split(",") if x.strip())
        if value and not _LANG_LIST.match(value):
            raise HttpProblem(400, "Langues invalides : codes de 2 à 6 lettres séparés par des virgules (ex. en,fr)")
        self.app.db.set_language_override(media_id, value or None)
        label = value or "automatique"
        title_fr = None
        searcher = getattr(self.app, "searcher", None)
        if searcher is not None and hasattr(searcher, "french_title") and "fr" in searcher.languages_for(
                self.app.db.get_media(media_id) or media):
            try:  # one TMDb call: the French title is shown at once and searched on the indexers
                title_fr = searcher.french_title(self.app.db.get_media(media_id) or media)
            except Exception:  # pragma: no cover - TMDb down: fetched again at the next search
                title_fr = None
        msg = "Langues de « %s » : %s" % (media["title"], label)
        if title_fr:
            msg += " — recherche aussi sous le titre français « %s »" % title_fr
        fresh = self.app.db.get_media(media_id) or media
        if fresh.get("on_watchlist", 1) and fresh.get("status") in ("wanted", "monitored") and \
                hasattr(self.app, "search_now"):
            self.app.search_now(media_id)
            msg += " — nouvelle recherche lancée"
        elif fresh.get("status") == "grabbed":
            msg += " — un téléchargement est déjà en cours : utilisez « Changer de source » pour une version française"
        return {"ok": True, "languages": value, "title_fr": title_fr, "message": msg}

    def api_media_title_fr(self, media_id: int, query: Dict[str, str], body: Any) -> Dict[str, Any]:
        media = self._media_or_404(media_id)
        raw = self._body_dict(body).get("title_fr")
        if raw is not None and not isinstance(raw, str):
            raise HttpProblem(400, "Le champ « title_fr » doit être une chaîne")
        value = (raw or "").strip()[:300]
        if value:
            self.app.db.set_title_fr(media_id, value)
            msg = "Titre français de « %s » : %s" % (media["title"], value)
        else:  # empty: forget it, looked up again on TMDb at the next search
            self.app.db.execute("UPDATE media SET title_fr = NULL WHERE id = ?", (media_id,))
            msg = "Titre français de « %s » effacé (recherché à nouveau sur TMDb)" % media["title"]
        return {"ok": True, "title_fr": value, "message": msg}

    # ------------------------------------------------- actions groupées, À venir, tableau de bord
    BULK_ACTIONS = ("search", "language", "ignore", "unignore", "delete")
    BULK_MAX = 500

    def _search_many(self, ids: List[int]) -> None:
        func = getattr(self.app, "search_many", None)
        if func is not None:
            func(list(ids))
        else:
            for mid in ids:
                self.app.search_now(mid)

    def api_media_bulk(self, query: Dict[str, str], body: Any) -> Dict[str, Any]:
        data = self._body_dict(body)
        action = data.get("action")
        if action not in self.BULK_ACTIONS:
            raise HttpProblem(400, "Action invalide : search, language, ignore, unignore ou delete attendu")
        raw = data.get("ids")
        if not isinstance(raw, list) or not raw:
            raise HttpProblem(400, "Le champ « ids » (liste des éléments sélectionnés) est obligatoire")
        if len(raw) > self.BULK_MAX:
            raise HttpProblem(400, "Trop d'éléments sélectionnés (%d maximum)" % self.BULK_MAX)
        ids = []  # type: List[int]
        for v in raw:
            if isinstance(v, bool) or not isinstance(v, int) or v <= 0:
                raise HttpProblem(400, "Identifiant invalide dans « ids » : %s" % json.dumps(v, ensure_ascii=False)[:40])
            if v not in ids:
                ids.append(v)
        langs = None  # type: Optional[str]
        if action == "language":
            raw_l = data.get("languages")
            if raw_l is not None and not isinstance(raw_l, str):
                raise HttpProblem(400, "Le champ « languages » doit être une chaîne (ex. \"fr\", \"en,fr\" ou \"\")")
            langs = ",".join(x.strip().lower() for x in (raw_l or "").replace(";", ",").split(",") if x.strip())
            if langs and not _LANG_LIST.match(langs):
                raise HttpProblem(400, "Langues invalides : codes de 2 à 6 lettres séparés par des virgules (ex. en,fr)")
        remove_from_plex = data.get("remove_from_plex", True) is not False
        delete_downloads = data.get("delete_downloads") is True
        if action == "delete":
            self._app_func("remove_from_watchlist")
        db = self.app.db
        done, skipped, errors, warnings = [], [], [], []  # type: List[int], List[Dict[str, Any]], List[Dict[str, Any]], List[str]
        to_search = []  # type: List[int]
        downloads_deleted = 0
        for mid in ids:
            media = db.get_media(mid)
            if not media:
                errors.append({"id": mid, "title": None, "error": "Élément introuvable (id %d)" % mid})
                continue
            title = media["title"]
            active = bool(media.get("on_watchlist")) and media.get("status") in ("wanted", "monitored")
            try:
                if action == "search":
                    if not active:
                        skipped.append({"id": mid, "title": title, "reason": "ignoré, terminé, en téléchargement "
                                        "ou retiré de la Watchlist"})
                        continue
                    to_search.append(mid)
                elif action == "language":
                    db.set_language_override(mid, langs or None)
                    if active:
                        to_search.append(mid)
                elif action == "ignore":
                    self.app.set_media_status(mid, "ignored")
                elif action == "unignore":
                    if media.get("status") != "ignored":
                        skipped.append({"id": mid, "title": title, "reason": "n'était pas ignoré"})
                        continue
                    self.app.set_media_status(mid, "monitored" if media["type"] == "show" else "wanted")
                else:  # delete
                    res = self.app.remove_from_watchlist(mid, remove_from_plex=remove_from_plex,
                                                         delete_downloads=delete_downloads) or {}
                    downloads_deleted += int(res.get("downloads_deleted") or 0)
                    if res.get("warning"):
                        warnings.append(str(res["warning"]))
                done.append(mid)
            except ValueError as exc:
                errors.append({"id": mid, "title": title, "error": str(exc)})
        if to_search:  # one background thread for the whole selection
            self._search_many(to_search)
        n = len(done)
        msg = {"search": "Recherche lancée pour %d élément(s)",
               "language": "Langues « " + (langs or "automatique") + " » appliquées à %d élément(s)",
               "ignore": "%d élément(s) ignoré(s)",
               "unignore": "%d élément(s) repris",
               "delete": "%d élément(s) retiré(s) de la liste"}[action] % n
        if action == "language" and to_search:
            msg += " — nouvelle recherche lancée pour %d" % len(to_search)
        if downloads_deleted:
            msg += " — %d téléchargement(s) supprimé(s) de Download Station" % downloads_deleted
        if skipped:
            msg += " — %d non concerné(s)" % len(skipped)
        if errors:
            msg += " — %d erreur(s) : %s" % (len(errors), "; ".join(e["error"] for e in errors[:3]))
        return {"ok": bool(done) or not errors, "action": action, "done": done, "skipped": skipped,
                "errors": errors, "searched": len(to_search), "downloads_deleted": downloads_deleted,
                "warning": " ; ".join(warnings[:5]) or None, "message": msg}

    def api_upcoming(self, query: Dict[str, str], body: Any) -> Dict[str, Any]:
        """Episodes of the monitored shows airing in the next *days* days (and those of the last *past* days, with
        their status) + wanted movies whose release date (TMDb) falls in the same window."""
        try:
            days = int(query.get("days", 30))
            past = int(query.get("past", 7))
        except ValueError:
            raise HttpProblem(400, "Paramètres days / past invalides")
        if not 1 <= days <= 365 or not 0 <= past <= 60:
            raise HttpProblem(400, "days doit être entre 1 et 365, past entre 0 et 60")
        today = _dt.date.today()
        start = (today - _dt.timedelta(days=past)).isoformat()
        end = (today + _dt.timedelta(days=days)).isoformat()
        db = self.app.db
        rows = db.query(
            "SELECT e.media_id, e.season, e.episode, e.title AS episode_title, substr(e.air_date, 1, 10) AS date, "
            "e.status, m.title, m.year, m.poster FROM episodes e JOIN media m ON m.id = e.media_id "
            "WHERE m.type = 'show' AND m.status = 'monitored' AND m.on_watchlist = 1 AND COALESCE(m.hidden, 0) = 0 "
            "AND e.air_date IS NOT NULL AND substr(e.air_date, 1, 10) BETWEEN ? AND ? "
            "ORDER BY date, m.title, e.season, e.episode LIMIT 1000", (start, end))
        items = [dict(r, kind="episode") for r in rows]
        movies = db.query(
            "SELECT id AS media_id, title, year, poster, status, substr(release_date, 1, 10) AS date FROM media "
            "WHERE type = 'movie' AND status = 'wanted' AND on_watchlist = 1 AND COALESCE(hidden, 0) = 0 "
            "AND release_date IS NOT NULL AND substr(release_date, 1, 10) BETWEEN ? AND ? "
            "ORDER BY date, title LIMIT 200", (start, end))
        items += [dict(r, kind="movie") for r in movies]
        t = today.isoformat()
        upcoming = sorted([i for i in items if i["date"] >= t],
                          key=lambda i: (i["date"], i["kind"] != "movie", i["title"] or "", i.get("season") or 0,
                                         i.get("episode") or 0))
        recent = sorted([i for i in items if i["date"] < t],
                        key=lambda i: (i["date"], i["kind"] != "movie", i["title"] or "", i.get("season") or 0,
                                       i.get("episode") or 0))
        recent.reverse()
        missing = db.query("SELECT id FROM media WHERE poster IS NULL AND on_watchlist = 1 AND COALESCE(hidden, 0) = 0 "
                           "AND ((type = 'movie' AND status = 'wanted') OR (type = 'show' AND status = 'monitored'))")
        self._request_posters([r["id"] for r in missing])
        return {"today": t, "days": days, "past_days": past, "upcoming": upcoming, "recent": recent,
                "tmdb_configured": bool(getattr(getattr(self.app, "tmdb", None), "enabled", False))}

    def api_dashboard(self, query: Dict[str, str], body: Any) -> Dict[str, Any]:
        imports = self.app.db.query(
            "SELECT g.id, g.media_id, g.season, g.episode, g.release_title, g.size, g.indexer, g.updated_at, "
            "m.title AS media_title, m.type AS media_type, m.poster FROM grabs g LEFT JOIN media m ON m.id = g.media_id "
            "WHERE g.status = 'imported' ORDER BY g.updated_at DESC, g.id DESC LIMIT ?", (self._limit(query, 10),))
        return {"storage": library_storage(self.app.cfg), "imports": imports}

    # ------------------------------------------------------------- commandes
    @staticmethod
    def _job_summary(job: Any) -> Dict[str, Any]:
        d = job.to_dict()
        d.pop("output", None)
        d.pop("result", None)
        return d

    def _job_or_404(self, job_id: int) -> Any:
        job = self.app.get_job(job_id)
        if job is None:
            raise HttpProblem(404, "Tâche introuvable (id %d) : synoarr a peut-être redémarré" % job_id)
        return job

    def api_jobs(self, query: Dict[str, str], body: Any) -> List[Dict[str, Any]]:
        return [self._job_summary(j) for j in self.app.list_jobs()]

    def api_job(self, job_id: int, query: Dict[str, str], body: Any) -> Dict[str, Any]:
        return self._job_or_404(job_id).to_dict()

    def api_job_start(self, query: Dict[str, str], body: Any) -> Dict[str, Any]:
        data = self._body_dict(body)
        command = str(data.get("command") or "")
        job, created = self.app.start_command(command, force=bool(data.get("force")),
                                              query=str(data.get("query") or ""))
        msg = ("« %s » lancé" % job.label) if created else ("« %s » est déjà en cours" % job.label)
        return {"ok": True, "created": created, "message": msg, "job": job.to_dict()}

    def api_job_grab(self, job_id: int, query: Dict[str, str], body: Any) -> Dict[str, Any]:
        self._job_or_404(job_id)
        index = self._body_dict(body).get("index")
        if not isinstance(index, int) or isinstance(index, bool):
            raise HttpProblem(400, "Le champ « index » (numéro du résultat) est obligatoire")
        return self.app.grab_search_result(job_id, index)

    def api_plex_login(self, query: Dict[str, str], body: Any) -> Dict[str, Any]:
        try:
            job, created = self.app.start_plex_login()
        except HttpError as exc:
            raise HttpProblem(502, "plex.tv injoignable : %s. Vérifiez la connexion Internet du NAS." % exc)
        result = job.result or {}
        return {"ok": True, "created": created, "code": result.get("code"), "url": result.get("url"),
                "message": "Saisissez le code %s sur %s" % (result.get("code"), result.get("url")),
                "job": job.to_dict()}

    def api_restart(self, query: Dict[str, str], body: Any) -> Dict[str, Any]:
        running = [j.label for j in self.app.list_jobs() if j.running and j.command != "plex_login"]
        self.app.restart()
        msg = "Redémarrage de synoarr…"
        if running:
            msg += " (après la fin de : %s)" % ", ".join(running)
        return {"ok": True, "message": msg}

    def api_logs(self, query: Dict[str, str], body: Any) -> Dict[str, Any]:
        try:
            lines = max(1, min(5000, int(query.get("lines", 300))))
        except ValueError:
            raise HttpProblem(400, "Paramètre lines invalide")
        level = (query.get("level") or "DEBUG").upper()
        if level not in LOG_LEVELS:
            raise HttpProblem(400, "Niveau invalide : DEBUG, INFO, WARNING ou ERROR")
        return tail_log(self.app.log_file(), lines, level, query.get("q", ""))

    def api_logs_download(self, query: Dict[str, str], body: Any) -> RawResponse:
        path = self.app.log_file()
        n = query.get("n", "")
        if n:
            if n not in ("1", "2", "3"):
                raise HttpProblem(400, "Paramètre n invalide (1 à 3)")
            path += "." + n
        try:
            with open(path, "rb") as fh:
                data = fh.read()
        except OSError:
            raise HttpProblem(404, "Journal introuvable : %s" % os.path.basename(path))
        name = os.path.basename(path)
        return RawResponse(data, "text/plain; charset=utf-8",
                           {"Content-Disposition": 'attachment; filename="%s"' % name})

    def api_config(self, query: Dict[str, str], body: Any) -> Dict[str, Any]:
        data = dict(self.app.read_config_text())
        data["auth_enabled"] = self.auth_enabled
        return data

    def _config_result(self, out: Dict[str, Any]) -> Dict[str, Any]:
        out = dict(out)
        new_web = self.app.cfg.web
        out["auth_enabled"] = self.auth_enabled
        out["auth_configured"] = bool(new_web.username and new_web.password)
        return out

    def api_config_save(self, query: Dict[str, str], body: Any) -> Dict[str, Any]:
        text = self._body_dict(body).get("text")
        if not isinstance(text, str):
            raise HttpProblem(400, "Le champ « text » (contenu de config.ini) est obligatoire")
        return self._config_result(self.app.save_config_text(text))

    def api_config_restore(self, query: Dict[str, str], body: Any) -> Dict[str, Any]:
        return self._config_result(self.app.restore_config_backup())

    def api_config_form(self, query: Dict[str, str], body: Any) -> Dict[str, Any]:
        """Formulaire de l'onglet Configuration : options des dataclasses, valeurs du fichier, aide de
        config.example.ini ; les secrets ne sont jamais renvoyés (seulement « has_value »)."""
        from . import configform
        data = self.app.read_config_text()
        out = configform.form_schema(data["text"])
        out.update({"path": data["path"], "auth_enabled": self.auth_enabled,
                    "restart_reasons": data.get("restart_reasons") or []})
        return out

    def api_config_form_save(self, query: Dict[str, str], body: Any) -> Dict[str, Any]:
        """{section: {clé: valeur}} : seules les clés modifiées sont réécrites dans config.ini (commentaires et ordre
        conservés), puis enregistrement habituel (validation, sauvegardes, rechargement). Secret : "" = effacer."""
        from . import configform
        data = self._body_dict(body)
        changes = data.get("changes", data)
        text = self.app.read_config_text()["text"]
        values, problems = configform.normalize_changes(text, changes)
        if problems:
            raise HttpProblem(400, "config.ini n'a pas été modifié : valeurs invalides", {"problems": problems})
        if not values:
            return {"ok": True, "changed": [], "message": "Aucune modification à enregistrer"}
        out = self._config_result(self.app.save_config_text(configform.apply_changes(text, values)))
        out["changed"] = ["%s.%s" % (sec, key) for sec, keys in values.items() for key in keys]
        out["message"] = "%s (%d option%s modifiée%s)" % (out.get("message") or "Configuration enregistrée",
                                                          len(out["changed"]), "s" if len(out["changed"]) > 1 else "",
                                                          "s" if len(out["changed"]) > 1 else "")
        return out


def _make_handler(server: WebServer) -> type:
    class Handler(BaseHTTPRequestHandler):
        server_version = "synoarr"
        sys_version = ""
        timeout = 60
        protocol_version = "HTTP/1.0"

        def log_message(self, fmt: str, *args: Any) -> None:
            log.debug("web: %s - %s", self.address_string(), fmt % args)

        def _send(self, status: int, payload: Any, ctype: str = "application/json; charset=utf-8",
                  extra: Optional[Dict[str, str]] = None) -> None:
            if isinstance(payload, bytes):
                data = payload
            else:
                data = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            if "Cache-Control" not in (extra or {}):
                self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Referrer-Policy", "no-referrer")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(data)

        def _handle(self) -> None:
            try:
                self._handle_inner()
            except (BrokenPipeError, ConnectionResetError) as exc:  # le navigateur a abandonné la requête
                log.debug("web: client parti avant la réponse (%s %s) : %s", self.command,
                          self.path.split("?")[0], exc)
            except Exception:  # ne jamais faire tomber le serveur
                log.exception("Erreur interne de l'interface web (%s %s)", self.command, self.path.split("?")[0])
                try:
                    self._send(500, {"error": "Erreur interne du serveur (voir le journal)"})
                except Exception:  # client déjà parti
                    pass

        def _handle_inner(self) -> None:
            if self.command in ("GET", "HEAD"):
                static = public_file(urllib.parse.urlsplit(self.path).path)
                if static is not None:  # manifeste et icônes : publics (voir PUBLIC_FILES)
                    self._send(200, static[0], static[1], {"Cache-Control": "public, max-age=86400"})
                    return
            if not server.auth_enabled and not host_is_local(self.headers.get("Host", "")):
                # protection contre le « DNS rebinding » quand aucun mot de passe n'est configuré
                self._send(403, {"error": "Accès refusé : utilisez l'adresse IP du NAS, ou configurez [web] "
                                          "username et password pour y accéder par un nom de domaine"})
                return
            if not server.check_auth(self.headers.get("Authorization")):
                self._send(401, {"error": "Authentification requise"},
                           extra={"WWW-Authenticate": 'Basic realm="synoarr", charset="UTF-8"'})
                return
            parts = urllib.parse.urlsplit(self.path)
            path = parts.path or "/"
            method = "GET" if self.command == "HEAD" else self.command
            if path in ("/", "/index.html") and method == "GET":
                self._send(200, INDEX_HTML.encode("utf-8"), "text/html; charset=utf-8",
                           {"Content-Security-Policy": CSP})
                return
            if not path.startswith("/api/"):
                self._send(404, {"error": "Page introuvable"})
                return
            if method in ("POST", "DELETE") and not self._same_origin():
                self._send(403, {"error": "Requête d'une autre origine refusée"})
                return
            try:
                body = self._read_json() if method in ("POST", "DELETE") else None
                query = dict(urllib.parse.parse_qsl(parts.query))
                status, payload = server.dispatch(method, path, query, body)
            except HttpProblem as exc:
                status, payload = exc.status, dict(exc.extra, error=exc.message)
            except ConfigInvalid as exc:
                status, payload = 400, {"error": "config.ini n'a pas été modifié : la configuration contient des "
                                                 "erreurs", "problems": exc.problems}
            except BusyError as exc:
                status, payload = 409, {"error": str(exc)}
            except ValueError as exc:  # erreurs métier (indexer_manager...) en français
                status, payload = 400, {"error": str(exc)}
            except HttpError as exc:  # indexer injoignable pendant l'ajout (détection du type...)
                status, payload = 502, {"error": "Service distant injoignable : %s" % exc}
            if isinstance(payload, RawResponse):
                self._send(status, payload.data, payload.ctype, payload.headers)
                return
            self._send(status, payload)

        def _same_origin(self) -> bool:
            """Protection CSRF. Accepté : pas d'en-tête Origin (outils, anciens navigateurs), Origin égal à Host
            ou X-Forwarded-Host (proxy inverse DSM/nginx), ou corps JSON : un navigateur n'envoie un
            Content-Type application/json vers une autre origine qu'après une requête préalable (CORS
            preflight) que ce serveur n'accepte jamais. Un formulaire piégé (text/plain, urlencoded) est refusé."""
            origin = self.headers.get("Origin")
            if not origin:
                return True
            netloc = urllib.parse.urlsplit(origin).netloc.lower()
            hosts = [self.headers.get("Host", "")]
            hosts += (self.headers.get("X-Forwarded-Host") or "").split(",")
            if netloc and netloc in [h.strip().lower() for h in hosts if h.strip()]:
                return True
            ctype = (self.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
            return ctype == "application/json"

        def _read_json(self) -> Any:
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                raise HttpProblem(400, "En-tête Content-Length invalide")
            if length > MAX_BODY:
                raise HttpProblem(413, "Requête trop volumineuse")
            raw = self.rfile.read(length) if length > 0 else b""
            if not raw.strip():
                return None
            try:
                return json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, ValueError):
                raise HttpProblem(400, "JSON invalide")

        do_GET = _handle
        do_HEAD = _handle
        do_POST = _handle
        do_DELETE = _handle

    return Handler


INDEX_JS = r"""
const $=s=>document.querySelector(s), app=$('#app');
const esc=v=>String(v==null?'':v).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const LBL={wanted:'Recherché',grabbed:'En téléchargement',done:'Terminé',ignored:'Ignoré',monitored:'Suivi',skipped:'Ignoré',
  sent:'Envoyé',imported:'Importé',failed:'Échec',movie:'Film',show:'Série',torznab:'Torznab',rss:'RSS',internet_archive:'Internet Archive',
  running:'En cours',error:'Erreur',planned:'Prévu'};
const lbl=s=>LBL[s]||s||'';
const badge=s=>`<span class="b b-${esc(s)}">${esc(lbl(s))}</span>`;
const yn=v=>v?'<span class="b b-done">Oui</span>':'<span class="b b-failed">Non</span>';
const sleep=ms=>new Promise(r=>setTimeout(r,ms));
function fdate(v){if(v==null||v==='')return 'Jamais';
  if(typeof v==='number')return new Date(v*1000).toLocaleString('fr-FR');
  if(typeof v==='object')return Object.values(v).filter(x=>x!=null&&typeof x!=='object').map(fdate).join(' · ');
  return String(v).replace('T',' ').slice(0,19)}
function size(n){if(!n)return '';const u=['o','Ko','Mo','Go','To'];let i=0;while(n>=1024&&i<4){n/=1024;i++}return n.toFixed(i>2?1:0)+' '+u[i]}
const nv=v=>v==null?'?':v, pad=n=>String(n).padStart(2,'0');
const spd=n=>n==null?'?':(size(n)||'0 o')+'/s';
function eta(s){if(s==null)return '';const h=Math.floor(s/3600),m=Math.floor(s%3600/60);
  return h>=96?'plus de 4 jours':h?h+' h '+pad(m):m?m+' min':'moins d\'une minute'}
const DSL={waiting:'En attente',downloading:'Téléchargement',paused:'En pause',finishing:'Finalisation',finished:'Terminé',
  hash_checking:'Vérification',seeding:'Partage (seeding)',filehosting_waiting:'En attente',extracting:'Extraction',error:'Erreur',
  introuvable:'Absent de Download Station',inconnu:'État inconnu',unknown:'État inconnu',failed:'Échec'};
const tgt=d=>d.season!=null?(d.episode!=null?` S${pad(d.season)}E${pad(d.episode)}`:` saison ${d.season}`):'';
const LT={torrent:['b-done','.torrent','Fichier .torrent : Download Station le télécharge directement'],magnet:['b-grabbed','magnet','Lien magnet : Download Station doit d\'abord trouver des sources (peut rester « En attente »)'],inconnu:['b-planned','lien ?','Type de lien non vérifié']};
const lt=x=>{const t=LT[x.link_type||'inconnu']||LT.inconnu;return ` <span class="b ${t[0]}" title="${esc(t[2])}">${t[1]}</span>`};
const LTF={src:'all',srch:'all'};
const ltPass=(x,f)=>f==='all'||(x.link_type||'inconnu')===f;
const ltChips=(list,scope)=>{const n=k=>list.filter(x=>k==='all'||(x.link_type||'inconnu')===k).length,f=LTF[scope];
  const c=(k,l)=>`<button type="button" class="xs${f===k?'':' sec'}" data-ltf="${k}" data-lts="${scope}" aria-pressed="${f===k}">${l} (${n(k)})</button>`;
  return `<div class="row ltf" role="group" aria-label="Filtrer par type de lien">${c('all','Tous')}${c('torrent','.torrent')}${c('magnet','magnet')}${n('inconnu')?c('inconnu','lien ?'):''}</div>`};
const pr=x=>x.priority!=null?` <span class="mut" title="Priorité de l'indexer (1 = le plus prioritaire)">· prio ${esc(x.priority)}</span>`:'';
const fpct=p=>(Math.round(p*10)/10).toLocaleString('fr-FR')+' %';
const srcBtn=(mid,s,e,g,label,rt,txt)=>`<button class="sec xs" data-src="${mid}" data-s="${s==null?'':s}" data-e="${e==null?'':e}" data-g="${g||''}" data-t="${esc(label)}" data-rt="${esc(rt||'')}">${txt||'Changer de source'}</button>`;
function dlRow(d,wm){const st=d.status,live=d.task_id!=null,m=[];
  if(live){m.push(`${size(d.downloaded)||'0 o'} / ${size(d.size)||'?'}`);m.push(`↓ ${spd(d.speed_down)}`);m.push(`↑ ${spd(d.speed_up)}`);
    m.push(`${nv(d.seeders)} seeds · ${nv(d.leechers)} leechers${d.peers!=null?' · '+esc(d.peers)+' pairs':''}`);
    if(d.eta_seconds!=null)m.push('reste '+eta(d.eta_seconds));if(d.uploaded)m.push('partagé '+size(d.uploaded))}
  else if(d.size)m.push(size(d.size));
  if(d.indexer)m.push(esc(d.indexer));
  const what=(wm?(d.media_title||''):'')+tgt(d);
  return `<div class="dl"><div class="dlh"><span class="b ds-${esc(st)}">${esc(DSL[st]||st)}</span>${live&&d.progress!=null?`<b class="pct">${fpct(d.progress)}</b>`:''}${what?`<b>${esc(what)}</b>`:''}<span class="mut small brk">${esc(d.release_title)}</span></div>
  ${live&&d.progress!=null?`<div class="pbar${st==='error'?' ko':st==='seeding'||st==='finished'?' ok':''}"><i style="width:${Math.max(0,Math.min(100,d.progress))}%"></i></div>`:''}
  <div class="dlm"><span class="mut small">${m.join(' · ')}</span>${d.media_id?srcBtn(d.media_id,d.season,d.episode,d.grab_id,(d.media_title||'')+tgt(d),d.release_title):''}</div>
  ${d.error?`<div class="small o-err">${esc(d.error)}</div>`:''}</div>`}
let tab=(location.hash||'#dash').slice(1), exp=new Set(), timer=null, mounted=null, dlActive=false;
async function api(p,method,body){
  const o={method:method||'GET',headers:{}};
  if(body!==undefined||o.method!=='GET'){o.headers['Content-Type']='application/json';o.body=JSON.stringify(body||{})}
  const r=await fetch('api/'+p,o);let d=null;try{d=await r.json()}catch(e){}
  if(!r.ok){const er=new Error((d&&d.error)||('Erreur HTTP '+r.status));er.status=r.status;er.data=d;throw er}return d}
function toast(msg,bad){const t=$('#toast');t.textContent=msg;t.className='show'+(bad?' bad':'');
  clearTimeout(t._h);t._h=setTimeout(()=>t.className='',5000)}
async function act(fn,okMsg){try{const d=await fn();toast((d&&d.message)||okMsg||'OK',d&&d.ok===false);await refresh()}
  catch(e){toast(e.message,true)}}
const rsBanner=r=>r&&r.length?`<div class="card warn"><b>Redémarrage nécessaire</b> pour appliquer : ${esc(r.join(', '))}. <button data-restart="1">Redémarrer maintenant</button></div>`:'';
const V={}, S={};
function dlBlock(dl){if(!dl)return '';const it=dl.items||[],fl=dl.failed||[];if(dl.active)dlActive=true;
  return `<div class="card"><h3>Téléchargements en cours${it.length?` <span class="mut small">(${it.length}${dl.active?' dont '+dl.active+' actif'+(dl.active>1?'s':''):''})</span>`:''}</h3>
  ${dl.warning?`<div class="msg wa">${esc(dl.warning)}</div>`:''}${it.length?it.map(d=>dlRow(d,true)).join(''):'<p class="mut">Aucun téléchargement en cours.</p>'}
  ${fl.length?`<h4 class="mut">Échecs récents (toujours recherchés)</h4>${fl.map(d=>dlRow(d,true)).join('')}`:''}</div>`}
const stoBlock=st=>!st||!st.length?'':`<div class="card"><h3>Espace disque de la vidéothèque</h3>${st.map(v=>{const lb=esc((v.labels||[]).join(' · '));
  if(v.error)return `<div class="msg wa">${lb} : ${esc(v.error)}</div>`;const used=Math.max(0,Math.min(100,100-(v.free_pct||0)));
  return `<div class="sto"><div class="stoh"><b>${lb}</b><span class="mut small brk">${esc(v.path)}</span></div>
  <div class="pbar sb${v.free_pct<5?' ko':v.low?' wa':''}" role="meter" aria-label="Espace utilisé : ${lb}" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${Math.round(used)}"><i style="width:${used}%"></i></div>
  <div class="small"><b class="${v.low?'o-warn':''}">${size(v.free)||'0 o'} libres (${fpct(v.free_pct||0)})</b> <span class="mut">· ${size(v.used)||'0 o'} utilisés sur ${size(v.total)}</span>${v.low?' <span class="b b-grabbed">espace faible</span>':''}</div></div>`}).join('')}</div>`;
const impBlock=im=>`<div class="card"><h3>Derniers imports</h3>${im&&im.length?`<ul class="ev imp">${im.map(g=>`<li><time>${esc(fdate(g.updated_at))}</time> <b>${esc((g.media_title||'')+tgt(g))||esc(g.release_title)}</b><div class="mut small brk">${esc(g.release_title)}</div></li>`).join('')}</ul>`:'<p class="mut">Aucun import pour le moment.</p>'}</div>`;
V.dash=async()=>{const [s,ev,dl,db]=await Promise.all([api('status'),api('events?limit=8'),api('downloads').catch(()=>null),api('dashboard').catch(()=>null)]);const c=s.counts||{};
  const errs=(s.last_errors||[]).map(e=>`<li>${typeof e==='object'?esc(fdate(e.ts||e.at||''))+' — '+esc(e.message||e.error||JSON.stringify(e)):esc(e)}</li>`).join('');
  return `${rsBanner(s.restart_required)}<div class="grid">
  <div class="card stat"><b>${c.movies||0}</b><span>Films suivis</span></div><div class="card stat"><b>${c.shows||0}</b><span>Séries suivies</span></div>
  <div class="card stat"><b>${(c.movies_wanted||0)+(c.episodes_wanted||0)}</b><span>Recherchés</span></div>
  <div class="card stat"><b>${c.downloading||0}</b><span>En téléchargement</span></div></div>${stoBlock(db&&db.storage)}${dlBlock(dl)}
  <div class="card"><div class="row"><button data-run="watchlist">Synchroniser la Watchlist</button><button data-run="process">Traiter les téléchargements</button><a class="btn sec" href="#cmd">Autres commandes…</a></div>
  <dl><dt>Dernière synchro Watchlist</dt><dd>${esc(fdate(s.last_watchlist_run))}</dd><dt>Dernier traitement</dt><dd>${esc(fdate(s.last_process_run))}</dd>
  <dt>Plex</dt><dd>${yn(s.plex_configured)}</dd><dt>Download Station</dt><dd>${yn(s.ds_configured)}</dd>
  <dt>Indexers actifs</dt><dd>${c.indexers_enabled||0}</dd><dt>Mode simulation</dt><dd>${s.dry_run?'<span class="b b-grabbed">Activé</span>':'Non'}</dd>
  ${s.jobs_running&&s.jobs_running.length?`<dt>En cours</dt><dd>${esc(s.jobs_running.join(', '))}</dd>`:''}
  <dt>Démarré le</dt><dd>${esc(fdate(s.started_at))}</dd><dt>Version</dt><dd>${esc(s.version)}</dd></dl></div>
  ${errs?`<div class="card err"><h3>Dernières erreurs</h3><ul>${errs}</ul></div>`:''}
  <div class="grid2">${impBlock(db&&db.imports)}<div class="card"><h3>Activité récente</h3>${evList(ev)}</div></div>`};
const evList=ev=>ev.length?`<ul class="ev">${ev.map(e=>`<li class="lv-${esc(e.level)}"><time>${esc(fdate(e.ts))}</time> <span class="k">${esc(e.kind)}</span> ${esc(e.message)}</li>`).join('')}</ul>`:'<p class="mut">Aucun événement.</p>';
const LANGS=[['','Auto'],['en','Anglais (en)'],['fr','Français (fr)'],['en,fr','Anglais + Français']];
const langSel=m=>{const v=m.language_override||'',o=LANGS.some(l=>l[0]===v)?LANGS:LANGS.concat([[v,v]]);
  return `<select class="lsel" data-lang="${m.id}" title="Langues recherchées" aria-label="Langues recherchées">${o.map(l=>`<option value="${esc(l[0])}"${l[0]===v?' selected':''}>${esc(l[1])}</option>`).join('')}</select>`};
/* onglets de statut de la Watchlist */
let WST='all';try{WST=localStorage.getItem('synoarr.wst')||'all'}catch(e){}
const WTABS=[['all','Tous'],['wanted','Recherchés'],['dl','En téléchargement'],['done','Terminés'],['ignored','Ignorés'],['off','Retirés de la Watchlist']];
const wcat=m=>{if(!m.on_watchlist)return 'off';if(m.status==='ignored')return 'ignored';const e=m.episodes,dls=m.downloads||[];
  if(dls.some(d=>d.grab_status==='sent')||m.status==='grabbed'||(e&&e.grabbed>0))return 'dl';
  if(m.type==='movie')return m.status==='done'?'done':'wanted';
  if(e&&e.total>0&&e.wanted===0&&e.done>0)return 'done';return 'wanted'};
/* affiches + mode Sélection (actions groupées) */
const BADIMG=new Set();
const okImg=u=>typeof u==='string'&&/^https:\/\/[^\s"'<>]+$/.test(u)&&!BADIMG.has(u);
const pst=(m,c)=>`<div class="pst${c?' '+c:''}" data-l="${esc(String(m.title||'?').trim().charAt(0).toUpperCase())}" aria-hidden="true">${okImg(m.poster)?`<img src="${esc(m.poster)}" alt="" loading="lazy" decoding="async" referrerpolicy="no-referrer">`:''}</div>`;
let SEL=null,WMAP={},WVIS=[];
V.watch=async()=>{const [ms,dl]=await Promise.all([api('media'),api('downloads').catch(()=>null)]);const f=$('#wf')?$('#wf').value:'',q=$('#wq')?$('#wq').value.toLowerCase():'';
  const base=ms.filter(m=>(!f||m.type===f)&&(!q||(m.title||'').toLowerCase().includes(q)));
  const cnt={};base.forEach(m=>{const c=wcat(m);cnt[c]=(cnt[c]||0)+1;if(c!=='off')cnt.all=(cnt.all||0)+1});
  if(WST!=='all'&&!cnt[WST]&&WST==='off')WST='all';
  const items=base.filter(m=>WST==='all'?wcat(m)!=='off':wcat(m)===WST);
  WMAP={};ms.forEach(m=>WMAP[m.id]=m);WVIS=items.map(m=>m.id);if(SEL)[...SEL].forEach(id=>{if(!WMAP[id])SEL.delete(id)});
  const tabs=`<div class="row wtabs" role="tablist" aria-label="Filtrer par statut">${WTABS.filter(t=>t[0]!=='off'||cnt.off).map(t=>`<button type="button" role="tab" class="xs${WST===t[0]?'':' sec'}" aria-selected="${WST===t[0]}" data-wst="${t[0]}">${t[1]} (${cnt[t[0]]||0})</button>`).join('')}${WST==='off'&&cnt.off?'<span class="mut small">Ces éléments ne sont plus dans votre Watchlist Plex : supprimez-les ou remettez-les dans la Watchlist.</span>':''}</div>`;
  const eps=await Promise.all(items.filter(m=>exp.has(m.id)).map(async m=>[m.id,await api('media/'+m.id+'/episodes').catch(()=>[])]));
  const em=Object.fromEntries(eps);
  return `<div class="row tools"><select id="wf"><option value="">Tous</option><option value="movie"${f==='movie'?' selected':''}>Films</option><option value="show"${f==='show'?' selected':''}>Séries</option></select>
  <input id="wq" type="search" placeholder="Filtrer…" value="${esc(q)}"><button class="sec" data-selmode="1" aria-pressed="${!!SEL}">${SEL?'Terminer':'Sélection'}</button><button data-wadd="1">+ Ajouter</button></div>${tabs}${dl&&dl.warning?`<div class="msg wa">${esc(dl.warning)}</div>`:''}
  ${items.length?items.map(m=>{const e=m.episodes,pct=e&&e.total?Math.round(100*e.done/e.total):0,ign=m.status==='ignored',dls=m.downloads||[];
  if(dls.some(d=>d.active))dlActive=true;
  return `<div class="card item${m.on_watchlist?'':' off'}${SEL&&SEL.has(m.id)?' sel':''}" data-id="${m.id}"><div class="wr">${SEL?`<input type="checkbox" class="wck" data-sel="${m.id}"${SEL.has(m.id)?' checked':''} aria-label="Sélectionner ${esc(m.title)}">`:''}${pst(m)}<div class="it"><div><b>${esc(m.title)}</b> ${m.year?`<span class="mut">(${esc(m.year)})</span>`:''}
  <div class="meta">${badge(m.type)} ${badge(m.status)} ${String(m.plex_key||'').startsWith('local:')?'<span class="b b-local" title="Entrée locale, hors Watchlist Plex">local</span>':''} ${m.is_animation?'<span class="b b-anim">Animation</span>':''} ${m.on_watchlist?'':'<span class="mut">retiré de la Watchlist</span>'}
  ${m.collection?`<span class="mut">Collection : ${esc(m.collection)}</span>`:''}
  ${e?`<span class="mut">${e.done}/${e.total} épisodes · ${e.wanted} recherchés${e.grabbed?' · '+e.grabbed+' en cours':''}</span>`:''}
  ${m.last_search_at?`<span class="mut">dernière recherche ${esc(fdate(m.last_search_at))}</span>`:''}
  <span class="mut">langues : ${esc((m.languages||[]).join(', ')||'—')} (${m.language_override?'manuel':'auto'})</span>${(m.languages||[]).includes('fr')?` <span class="mut">· titre FR : ${m.title_fr?esc(m.title_fr):'<i>inconnu</i>'}</span> <button class="xs sec" data-tfr="${m.id}" data-v="${esc(m.title_fr||'')}" title="Titre utilisé en plus pour chercher les releases françaises">modifier</button>`:''}</div>
  ${e&&e.total?`<div class="bar"><i style="width:${pct}%"></i></div>`:''}</div>
  <div class="acts"><button data-search="${m.id}"${ign?' disabled':''}>Rechercher</button><button class="sec" data-st="${m.id}" data-v="${ign?'wanted':'ignored'}">${ign?'Reprendre':'Ignorer'}</button>
  ${m.type==='movie'&&m.status==='wanted'&&!dls.length?srcBtn(m.id,null,null,null,m.title,'','Choisir une source'):''}
  ${m.type==='show'?`<button class="sec" data-eps="${m.id}">${exp.has(m.id)?'Masquer':'Épisodes'}</button>`:''}${langSel(m)}<button class="danger" data-wdel="${m.id}" data-n="${esc(m.title)}" data-local="${String(m.plex_key||'').startsWith('local:')?1:0}" data-dl="${dls.filter(d=>d.grab_status==='sent').length}">Supprimer</button></div></div></div>
  ${dls.length?`<div class="dls">${dls.map(d=>dlRow(d,false)).join('')}</div>`:''}
  ${exp.has(m.id)?epTable(em[m.id]||[],m):''}</div>`}).join(''):(ms.length?'<p class="mut card">Aucun élément dans cet onglet.</p>':'<p class="mut card">Watchlist vide : ajoutez des films ou séries à votre Watchlist Plex puis synchronisez.</p>')}`};
function epTable(eps,m){if(!eps.length)return '<p class="mut">Aucun épisode connu.</p>';const by={};eps.forEach(e=>(by[e.season]=by[e.season]||[]).push(e));
  return Object.keys(by).map(s=>`<details${Object.keys(by).length<3?' open':''}><summary>Saison ${esc(s)} — ${by[s].filter(e=>e.status==='done').length}/${by[s].length}</summary>
  ${by[s].filter(e=>e.status==='wanted').length>1?`<div class="row spk">${srcBtn(m.id,+s,null,null,m.title+' saison '+s,'','Choisir un pack pour la saison '+esc(s))}</div>`:''}
  <table class="eps">${by[s].map(e=>`<tr><td>E${pad(e.episode)}</td><td>${esc(e.title||'')}</td><td class="mut">${esc(e.air_date||'')}</td><td>${badge(e.status)}${e.status==='wanted'?' '+srcBtn(m.id,e.season,e.episode,null,m.title+` S${pad(e.season)}E${pad(e.episode)}`,'','Source…'):''}</td></tr>`).join('')}</table></details>`).join('')}
/* ------------------------------------------------------------------ À venir */
let CALD='30';try{CALD=localStorage.getItem('synoarr.cald')||'30'}catch(e){}
const pday=s=>{const a=String(s).split('-');return new Date(+a[0],+a[1]-1,+a[2])};
function dayLbl(d,t){const n=Math.round((pday(d)-pday(t))/864e5);if(n===0)return 'Aujourd\'hui';if(n===1)return 'Demain';if(n===-1)return 'Hier';
  const o={weekday:'long',day:'numeric',month:'long'};if(d.slice(0,4)!==t.slice(0,4))o.year='numeric';const x=pday(d).toLocaleDateString('fr-FR',o);return x.charAt(0).toUpperCase()+x.slice(1)}
function calRow(i,t){const ep=i.kind==='episode',code=ep?`S${pad(i.season)}E${pad(i.episode)}`:'',fut=i.date>t;
  const st=fut&&i.status==='wanted'?badge('planned'):badge(i.status);
  return `<div class="crow">${pst(i,'sm')}<div class="cb"><div><b>${esc(i.title)}</b>${ep?` <span class="mut nw">${code}</span>`:(i.year?` <span class="mut">(${esc(i.year)})</span>`:'')}</div>
  ${ep&&i.episode_title?`<div class="small brk">${esc(i.episode_title)}</div>`:''}<div class="meta">${badge(ep?'show':'movie')} ${st}${ep?'':' <span class="mut small">date de sortie (TMDb)</span>'}</div></div>
  ${i.status==='wanted'&&!fut?`<div class="cact">${srcBtn(i.media_id,ep?i.season:null,ep?i.episode:null,null,i.title+(ep?' '+code:''),'',ep?'Source…':'Choisir une source')}</div>`:''}</div>`}
function calList(list,t){const g=[];list.forEach(i=>{let x=g[g.length-1];if(!x||x.d!==i.date){x={d:i.date,a:[]};g.push(x)}x.a.push(i)});
  return g.map(x=>`<section class="cday"><h4${x.d===t?' class="tdy"':''}>${esc(dayLbl(x.d,t))}${/^Aujourd|^Demain|^Hier/.test(dayLbl(x.d,t))?` <span class="mut small">${esc(pday(x.d).toLocaleDateString('fr-FR',{weekday:'long',day:'numeric',month:'long'}))}</span>`:''}</h4>${x.a.map(i=>calRow(i,t)).join('')}</section>`).join('')}
V.cal=async()=>{const r=await api('upcoming?days='+encodeURIComponent(CALD));const t=r.today;
  const sel=`<select id="cald" aria-label="Période">${[['7','7 jours'],['14','14 jours'],['30','30 jours'],['60','60 jours'],['90','90 jours']].map(o=>`<option value="${o[0]}"${CALD===o[0]?' selected':''}>${o[1]}</option>`).join('')}</select>`;
  const hint=`<p class="mut small">Épisodes des séries suivies (dates de diffusion TMDb / Plex, mises à jour à chaque synchronisation de la Watchlist)${r.tmdb_configured?' et films recherchés avec une date de sortie connue':' ; configurez une clé TMDb pour voir aussi les dates de sortie des films'}.</p>`;
  return `<div class="card"><div class="cfgh"><h3>À venir <span class="mut small">(${r.upcoming.length})</span></h3>${sel}</div>${r.upcoming.length?calList(r.upcoming,t):`<p class="mut">Rien de prévu dans les ${esc(r.days)} prochains jours.</p>`}${hint}</div>
  <div class="card"><h3>${esc(r.past_days)} derniers jours <span class="mut small">(${r.recent.length})</span></h3>${r.recent.length?calList(r.recent,t):'<p class="mut">Aucune sortie récente.</p>'}</div>`};
const PRW={url:'http://127.0.0.1:9696',key:'',res:''};
async function prowlarr(){const u=$('#prwu').value.trim(),k=$('#prwk').value.trim();PRW.url=u;PRW.key=k;
  if(!u||!k){toast('Saisissez l\'adresse et la clé API de Prowlarr',true);return}
  const b=$('#prwf button');b.disabled=true;b.textContent='Import…';
  try{const r=await api('indexers/import-prowlarr','POST',{url:u,api_key:k});PRW.key='';
    const li=(t,a)=>a.length?`<div class="small"><b>${t}</b><ul>${a.map(x=>`<li>${esc(x)}</li>`).join('')}</ul></div>`:'';
    PRW.res=`<div class="msg ok">✔ ${esc(r.message)}</div>${li('Ajoutés',r.added)}${li('Mis à jour',r.updated)}${li('Ignorés',r.skipped)}`;toast(r.message)}
  catch(e){PRW.res=`<div class="msg ko">✖ ${esc(e.message)}</div>`;toast(e.message,true)}
  await refresh()}
const prioCtl=i=>{const p=Math.max(1,Math.min(50,+i.priority||25));return `<div class="prio"><label for="pr${i.id}">Priorité</label><button class="sec pb" data-pd="${i.id}" data-v="${p-1}"${p<=1?' disabled':''} title="Plus prioritaire" aria-label="Plus prioritaire">−</button><input type="number" id="pr${i.id}" class="pin" min="1" max="50" step="1" value="${p}" data-prio="${i.id}" aria-label="Priorité de ${esc(i.name)} (1 = le plus prioritaire)"><button class="sec pb" data-pd="${i.id}" data-v="${p+1}"${p>=50?' disabled':''} title="Moins prioritaire" aria-label="Moins prioritaire">+</button></div>`};
const JKT={url:'http://127.0.0.1:9117',key:'',res:''};
async function jackett(){const u=$('#jktu').value.trim(),k=$('#jktk').value.trim();JKT.url=u;JKT.key=k;
  if(!u||!k){toast('Saisissez l\'adresse et la clé API de Jackett',true);return}
  const b=$('#jktf button');b.disabled=true;b.textContent='Import…';
  try{const r=await api('indexers/import-jackett','POST',{url:u,api_key:k});JKT.key='';
    const li=(t,a)=>a&&a.length?`<div class="small"><b>${t}</b><ul>${a.map(x=>`<li>${esc(x)}</li>`).join('')}</ul></div>`:'';
    JKT.res=`<div class="msg ok">✔ ${esc(r.message)}</div>${li('Ajoutés',r.added)}${li('Mis à jour',r.updated)}${li('Ignorés',r.skipped)}`;toast(r.message)}
  catch(e){JKT.res=`<div class="msg ko">✖ ${esc(e.message)}</div>`;toast(e.message,true)}
  await refresh()}
const jktState=p=>{if(!p||!p.configured)return '<p class="mut small">Synchronisation automatique : inactive. Elle démarre dès le premier import.</p>';
  return `<div class="msg ok small">Synchronisation automatique avec <b>${esc(p.url)}</b> : ${p.interval_hours>0?'toutes les '+p.interval_hours+' h':'désactivée'}${p.last_sync?' · dernière : '+esc(p.last_sync):''}</div>${p.last_error?`<div class="msg ko small">Dernière erreur : ${esc(p.last_error)}</div>`:''}
  <div class="row"><button class="sec" data-jkts="1">Synchroniser maintenant</button><button class="danger" data-jktx="1">Arrêter la synchro</button></div>`};
async function jktSync(){try{const r=await api('jackett/sync','POST',{});toast(r.message||'Synchronisé')}catch(e){toast(e.message,true)}await refresh()}
async function jktForget(){if(!confirm('Arrêter la synchronisation automatique avec Jackett ? Les indexers déjà importés sont gardés.'))return;
  try{const r=await api('jackett/forget','POST',{});toast(r.message)}catch(e){toast(e.message,true)}await refresh()}
const prwState=p=>{if(!p||!p.configured)return '<p class="mut small">Synchronisation automatique : inactive. Elle démarre dès le premier import.</p>';
  const every=p.interval_hours>0?`toutes les ${p.interval_hours} h`:'désactivée ([prowlarr] sync_interval_hours = 0)';
  return `<div class="msg ok small">Synchronisation automatique avec <b>${esc(p.url)}</b> : ${every}${p.last_sync?' · dernière : '+esc(p.last_sync):''}${p.running?' · en cours…':''}</div>${p.last_error?`<div class="msg ko small">Dernière erreur : ${esc(p.last_error)}</div>`:''}
  <div class="row"><button class="sec" data-prws="1">Synchroniser maintenant</button><button class="danger" data-prwx="1">Arrêter la synchro</button></div>
  <p class="mut small">Un indexer ajouté dans Prowlarr apparaît ici automatiquement ; supprimé ou désactivé dans Prowlarr, il est désactivé ici ; les priorités suivent celles de Prowlarr.</p>`};
async function prwSync(){try{const r=await api('prowlarr/sync','POST',{});toast(r.message||'Synchronisé')}catch(e){toast(e.message,true)}await refresh()}
async function prwForget(){if(!confirm('Arrêter la synchronisation automatique avec Prowlarr ? Les indexers déjà importés sont gardés.'))return;
  try{const r=await api('prowlarr/forget','POST',{});toast(r.message)}catch(e){toast(e.message,true)}await refresh()}
V.idx=async()=>{const [ix,cat,prw,jkt]=await Promise.all([api('indexers'),api('catalog'),api('prowlarr').catch(()=>null),api('jackett').catch(()=>null)]);
  return `<div class="card"><h3>Indexers configurés</h3>${ix.length?`<p class="mut small">Priorité : 1 = le plus prioritaire, 50 = le moins. Les indexers sont interrogés dans cet ordre et, à qualité égale (résolution, langue, source), la release de l'indexer le plus prioritaire est choisie.</p>`:''}${ix.length?ix.map(i=>`<div class="item it"><div><b>${esc(i.name)}</b> ${badge(i.type)} ${i.enabled?'<span class="b b-done">Actif</span>':'<span class="b b-ignored">Désactivé</span>'}
  <div class="meta mut"><span class="url">${esc(i.url)}</span>${i.api_key?' · clé '+esc(i.api_key):''}${i.categories?' · catégories '+esc(i.categories):''}</div><div class="tres" id="tr${i.id}"></div></div>
  <div class="acts">${prioCtl(i)}<button class="sec" data-iedit="${i.id}">Modifier</button><button data-test="${i.id}">Tester</button><button class="sec" data-en="${i.id}" data-v="${i.enabled?0:1}">${i.enabled?'Désactiver':'Activer'}</button><button class="danger" data-del="${i.id}" data-n="${esc(i.name)}">Supprimer</button></div></div>`).join(''):'<p class="mut">Aucun indexer : ajoutez-en un depuis le catalogue ou par URL.</p>'}</div>
  <div class="card"><h3>Importer depuis Prowlarr</h3><p class="mut small">Ajoute chaque indexer torrent actif de Prowlarr comme indexer Torznab (avec sa priorité). La clé API se trouve dans Prowlarr > Settings > General > API Key. <b>Utilisez Prowlarr OU Jackett, pas les deux.</b></p>
  <form id="prwf" class="row" autocomplete="off"><input id="prwu" name="url" type="text" spellcheck="false" value="${esc(PRW.url)}" placeholder="http://127.0.0.1:9696" aria-label="Adresse de Prowlarr">
  <input id="prwk" name="api_key" type="password" autocomplete="new-password" placeholder="Clé API Prowlarr" aria-label="Clé API Prowlarr" value="${esc(PRW.key)}"><button type="submit">Importer</button></form><div id="prwr">${PRW.res}</div>${prwState(prw)}</div>
  <div class="card"><h3>Importer depuis Jackett</h3><p class="mut small">Même principe que Prowlarr : chaque indexer configuré dans Jackett devient un indexer Torznab, puis synoarr suit Jackett automatiquement. La clé API est en haut à droite de la page Jackett. <b>Utilisez Prowlarr OU Jackett, pas les deux</b> (sinon chaque site serait interrogé deux fois).</p>
  <form id="jktf" class="row" autocomplete="off"><input id="jktu" name="url" type="text" spellcheck="false" value="${esc(JKT.url)}" placeholder="http://127.0.0.1:9117" aria-label="Adresse de Jackett">
  <input id="jktk" name="api_key" type="password" autocomplete="new-password" placeholder="Clé API Jackett" aria-label="Clé API Jackett" value="${esc(JKT.key)}"><button type="submit">Importer</button></form><div id="jktr">${JKT.res}</div>${jktState(jkt)}</div>
  <div class="card"><h3>Catalogue (sources légales)</h3>${cat.map(c=>`<div class="item it"><div><b>${esc(c.name)}</b> ${badge(c.type)}<div class="meta mut">${esc(c.description||'')}</div></div>
  <div class="acts">${c.installed?'<span class="b b-done">Installé</span>':`<button data-cat="${esc(c.id)}">Ajouter</button>`}</div></div>`).join('')||'<p class="mut">Catalogue vide.</p>'}</div>`};
/* Historique : période (mémorisée dans le navigateur) */
let HP={p:'30',from:'',to:''};try{Object.assign(HP,JSON.parse(localStorage.getItem('synoarr.hist')||'{}'))}catch(e){}
const saveHP=()=>{try{localStorage.setItem('synoarr.hist',JSON.stringify(HP))}catch(e){}};
const isoD=d=>d.getFullYear()+'-'+pad(d.getMonth()+1)+'-'+pad(d.getDate());
const HPS=[['today','Aujourd\'hui'],['7','7 derniers jours'],['30','30 derniers jours'],['90','90 derniers jours'],['all','Tout'],['custom','Période personnalisée']];
function histRange(){const t=new Date(),ago=n=>{const d=new Date(t);d.setDate(d.getDate()-n);return isoD(d)};
  if(HP.p==='today')return [isoD(t),isoD(t)];if(HP.p==='all')return ['',''];if(HP.p==='custom')return [HP.from||'',HP.to||''];
  return [ago((+HP.p||30)-1),isoD(t)]}
const hcount=(d,w)=>d.total>d.items.length?`<span class="mut small">${d.items.length} affichés sur ${d.total}</span>`:`<span class="mut small">${d.total} ${w}</span>`;
V.hist=async()=>{const [a,b]=histRange(),q=`since=${encodeURIComponent(a)}&until=${encodeURIComponent(b)}`;
  const [gd,ed]=await Promise.all([api('grabs?'+q),api('events?'+q)]);const g=gd.items,ev=ed.items;
  const per=`<div class="card"><div class="row hper"><label class="mini" for="hp">Période</label><select id="hp">${HPS.map(o=>`<option value="${o[0]}"${HP.p===o[0]?' selected':''}>${o[1]}</option>`).join('')}</select>
  ${HP.p==='custom'?`<label class="mini">du <input type="date" id="hpf" value="${esc(HP.from)}" max="${esc(HP.to||'')}"></label><label class="mini">au <input type="date" id="hpt" value="${esc(HP.to)}" min="${esc(HP.from||'')}"></label>`:''}</div></div>`;
  return `${per}<div class="card"><h3>Téléchargements envoyés ${hcount(gd,'envoi(s)')}</h3>${g.length?`<div class="tw"><table><tr><th>Date</th><th>Release</th><th class="hm">Média</th><th class="hm">Indexer</th><th class="hm">Taille</th><th>Statut</th></tr>
  ${g.map(x=>`<tr><td class="nw">${esc(fdate(x.created_at))}</td><td class="brk">${esc(x.release_title)}${x.message?`<div class="mut">${esc(x.message)}</div>`:''}</td><td class="hm">${esc(x.media_title||'')}${x.season!=null?` S${String(x.season).padStart(2,'0')}${x.episode!=null?'E'+String(x.episode).padStart(2,'0'):''}`:''}</td>
  <td class="hm">${esc(x.indexer||'')}</td><td class="hm nw">${size(x.size)}</td><td>${badge(x.status)}</td></tr>`).join('')}</table></div>`:'<p class="mut">Aucun téléchargement sur cette période.</p>'}</div>
  <div class="card"><h3>Événements ${hcount(ed,'événement(s)')}</h3>${ev.length?evList(ev):'<p class="mut">Aucun événement sur cette période.</p>'}</div>`};
/* ---------------------------------------------------------------- Commandes */
const CMDS=[['sync','Synchroniser la Watchlist','Importe votre Watchlist Plex (films et séries).'],
  ['search_all','Rechercher tout','Synchronise puis cherche sur les indexers tout ce qui manque.'],
  ['process','Traiter les téléchargements','Renomme et range les téléchargements terminés.'],
  ['dry_run','Simulation du traitement','Montre ce qui serait rangé, sans rien déplacer.'],
  ['check','Vérifier la configuration','Dossiers, Download Station, Plex, TMDb et indexers.'],
  ['test_indexers','Tester tous les indexers','Vérifie que chaque indexer répond.'],
  ['rss_sync','Synchroniser les flux RSS','Lit les dernières releases de chaque indexer et envoie celles qui correspondent à un épisode, un film ou une amélioration attendus.'],
  ['ds_test','Test d\'envoi Download Station','Envoie un torrent libre très partagé (Ubuntu) des 3 façons possibles (fichier, lien, magnet) et indique lesquelles démarrent (≈ 5 min).'],
  ['ds_diag','Diagnostic Download Station','Explique pourquoi des téléchargements restent « En attente » ou en erreur.'],
  ['subtitles_scan','Rechercher les sous-titres manquants','Télécharge depuis OpenSubtitles les sous-titres manquants ([subtitles] languages) des films et séries de la bibliothèque.'],
  ['update_check','Vérifier les mises à jour','Compare votre version avec GitHub, sans rien modifier.'],
  ['update','Mettre à jour synoarr','Télécharge la dernière version, la vérifie, garde config.ini et data/, puis redémarre.'],
  ['update_rollback','Revenir à la version précédente','Restaure les fichiers sauvegardés lors de la dernière mise à jour, puis redémarre.'],
  ['telegram_test','Tester Telegram','Envoie un message de test (ou affiche votre chat_id).']];
const PFX={ok:'[OK]  ',err:'[ERR] ',warn:'[!]   ',info:''};
let jobs={}, cur=null, srch=null, plexId=null;
const polling=new Set();
/* catégories de l'onglet Commandes : une commande absente d'ici (ajoutée plus tard) va dans « Autres » */
const CMDCAT=[['Watchlist & recherche',['sync','search_all','rss_sync']],['Téléchargements',['process','dry_run','ds_test','ds_diag']],
  ['Indexers',['test_indexers']],['Bibliothèque',['subtitles_scan']],['Connexions & notifications',['check','plex_login','telegram_test']],
  ['Maintenance',['update_check','update','update_rollback','restart']]];
const CMDX={plex_login:['plex_login','Connexion Plex','Relie synoarr à votre compte Plex (code à saisir sur plex.tv/link).'],
  restart:['restart','Redémarrer synoarr','Relance le programme, après la fin des tâches en cours.']};
const cmdItem=c=>c[0]==='plex_login'?`<div class="cmd"><button data-plex="1">${esc(c[1])}</button><span class="mut">${esc(c[2])}</span></div>`
  :c[0]==='restart'?`<div class="cmd"><button class="danger" data-restart="1">${esc(c[1])}</button><span class="mut">${esc(c[2])}</span></div>`
  :`<div class="cmd"><button data-cmd="${esc(c[0])}">${esc(c[1])}</button>${c[2]?`<span class="mut">${esc(c[2])}</span>`:''}${c[0]==='search_all'?'<label class="chk"><input type="checkbox" id="force"> forcer (ignorer le délai entre deux recherches)</label>':''}</div>`;
function cmdCards(known){const desc={};CMDS.forEach(c=>desc[c[0]]=c);
  const avail=id=>id==='restart'||!known||id in known,get=id=>CMDX[id]||desc[id]||(known&&known[id]?[id,known[id],'']:null),used=new Set(['search']);
  const cards=CMDCAT.map(([t,ids])=>{const it=ids.filter(avail).map(get).filter(Boolean);it.forEach(c=>used.add(c[0]));return it.length?[t,it]:null}).filter(Boolean);
  const other=CMDS.map(c=>c[0]).concat(Object.keys(known||{})).filter((id,i,a)=>a.indexOf(id)===i&&!used.has(id)&&avail(id)).map(get).filter(Boolean);
  if(other.length)cards.push(['Autres',other]);return cards}
S.cmd=async()=>{let known=null;try{known=(await api('status')).commands||null}catch(e){}
  app.innerHTML=`<div class="cmdcats">${cmdCards(known).map(([t,it])=>`<div class="card"><h3>${esc(t)}</h3><div class="cmdl">${it.map(cmdItem).join('')}</div>${it.some(c=>c[0]==='plex_login')?'<div id="plexbox"></div>':''}</div>`).join('')}</div>
  <p class="mut small">La recherche manuelle (requête libre, résultats notés, envoi à Download Station) est sur le <a href="#dash">Tableau de bord</a>.</p>
  <div class="card" id="outc"><h3 id="outh">Résultat</h3><div id="out"><p class="mut">Lancez une commande : son résultat s'affichera ici.</p></div></div>
  <div class="card"><h3>Tâches récentes</h3><div id="jl" class="mut">…</div></div>`;
  [cur,srch,plexId].forEach(id=>{if(id&&jobs[id])draw(jobs[id])});loadJobs()};
async function startJob(body){try{const r=await api('jobs','POST',body);toast(r.message);track(r.job);
  const t=$(r.job.command==='search'?'#sres':'#outc');if(t&&t.getBoundingClientRect().top>innerHeight*0.6)t.scrollIntoView({behavior:'smooth',block:'start'})}catch(e){toast(e.message,true)}}
function track(j){jobs[j.id]=j;if(j.command==='search')srch=j.id;else if(j.command==='plex_login')plexId=j.id;else cur=j.id;
  draw(j);if(j.status==='running')poll(j.id)}
async function poll(id){if(polling.has(id))return;polling.add(id);let fails=0;
  try{while(fails<30){await sleep(1000);let j;try{j=await api('jobs/'+id);fails=0}catch(e){if(e.status===404)break;fails++;continue}
    jobs[id]=j;draw(j);if(j.status!=='running'){done(j);break}}}finally{polling.delete(id)}}
function done(j){if(j.result&&j.result.restarting){waitRestart(upS0)}
  if(j.command==='plex_login')toast(j.status==='done'?'Compte Plex connecté':(j.error||'Connexion Plex échouée'),j.status!=='done');
  else if(j.command!=='search')toast(j.label+' : '+(j.status==='done'?'terminé':'erreur'),j.status!=='done');loadJobs()}
function draw(j){if(j.id===srch&&$('#sres'))drawSearch(j);if(tab!=='cmd')return;if(j.id===cur&&$('#out'))drawOut(j);if(j.id===plexId&&$('#plexbox'))drawPlex(j)}
const con=o=>`<pre class="con">${o.map(x=>`<span class="o-${esc(x.level)}">${esc(PFX[x.level]||'')}${esc(x.text)}</span>`).join('\n')}</pre>`;
function drawOut(j){$('#outh').innerHTML=`${esc(j.label)} ${badge(j.status)} <span class="mut small">${esc(fdate(j.started_at))}</span>`;
  const r=j.result||{};let h='';
  if(r.actions){h+=r.dry_run?'<p class="mut small">Simulation : rien n\'a été déplacé.</p>':'';
    h+=r.actions.length?`<div class="tw"><table class="acts-t"><tr><th>Statut</th><th>Fichier → destination</th></tr>${r.actions.map(a=>`<tr><td>${badge(a.status)}</td><td class="brk">${esc(a.src)}${a.dst?`<div class="dst">→ ${esc(a.dst)}</div>`:''}${a.message?`<div class="mut small">${esc(a.message)}</div>`:''}</td></tr>`).join('')}</table></div>`:'';
    h+=j.output.length?`<details${r.actions.length?'':' open'}><summary>Détail</summary>${con(j.output)}</details>`:''}
  else h+=j.output.length?con(j.output):'';
  if(j.status==='running')h+='<p class="mut">En cours…</p>';
  $('#out').innerHTML=h||'<p class="mut">Aucune sortie.</p>'}
/* recherche manuelle (Tableau de bord) : tri par colonne et filtres comme la liste des sources */
const MSF={key:'score',dir:-1,q:'',ok:false};let msJob=null;
function drawSearch(j){const r=j.result,el=$('#sres');if(!el)return;
  if(j.status!=='done'||!r){msJob=null;el.innerHTML=`<p class="small">${esc(j.label)} ${badge(j.status)}</p>${j.status==='running'?'<p class="mut">Recherche en cours sur les indexers…</p>':j.status==='error'?con(j.output):''}`;return}
  if(msJob!==j.id||!$('#mstbl')){msJob=j.id;const all=r.results;
    el.innerHTML=`<p class="small">${esc(j.label)} ${badge(j.status)}</p><p class="mut small">Requête : ${esc(r.request)} (${esc(r.kind==='movie'?'film':r.kind==='season'?'saison':'épisode')}) — ${r.total} résultat(s), ${all.filter(x=>x.score!=null).length} acceptable(s)</p>
    ${all.length?`<div id="mschips"></div><div class="row sfil"><input id="msq" type="search" placeholder="Filtrer le titre (ex. 1080p, MULTi…)" value="${esc(MSF.q)}" aria-label="Filtrer le titre"><label class="chk"><input type="checkbox" id="msok"${MSF.ok?' checked':''}> Acceptables seulement</label><span id="msn" class="mut small"></span></div>`:''}<div id="mstbl"></div>`}
  drawMsTable()}
function drawMsTable(){const j=msJob&&jobs[msJob],r=j&&j.result,el=$('#mstbl');if(!r||!el)return;const all=r.results,q=MSF.q.trim().toLowerCase();
  if($('#mschips'))$('#mschips').innerHTML=ltChips(all,'srch');
  const vis=all.filter(x=>ltPass(x,LTF.srch)&&(!q||String(x.title).toLowerCase().includes(q))&&(!MSF.ok||x.score!=null));
  vis.sort((a,b)=>{const va=sval(a,MSF.key),vb=sval(b,MSF.key);return (va<vb?-1:va>vb?1:0)*MSF.dir||(a.index-b.index)});
  if($('#msn'))$('#msn').textContent=`${vis.length} / ${all.length} affiché(s)`;
  const th=c=>`<th${c[0]==='size'||c[0]==='seeders'||c[0]==='indexer'?' class="hm"':''}><button type="button" class="sortb${MSF.key===c[0]?' on':''}" data-msort="${c[0]}" title="Trier par ${c[1].toLowerCase()}">${c[1]}${MSF.key===c[0]?(MSF.dir<0?' ▼':' ▲'):''}</button></th>`;
  const row=x=>`<tr${x.score==null?' class="rej"':''}><td class="nw">${x.score==null?'rejeté':esc(x.score)}</td><td class="brk"><b>${esc(x.title)}</b>${lt(x)}<div class="mut small">${esc(x.reason)}</div>
      <div class="mut small sm">${size(x.size)} · ${nv(x.seeders)} seeders · ${esc(x.indexer)}${pr(x)}</div></td><td class="hm nw">${size(x.size)}</td><td class="hm">${nv(x.seeders)}</td><td class="hm">${esc(x.indexer)}${pr(x)}</td>
      <td>${x.score!=null?`<button data-grab="${x.index}" title="Envoyer à Download Station">Envoyer<span class="hm"> à Download Station</span></button>`:''}</td></tr>`;
  el.innerHTML=!all.length?'<p class="mut">Aucun résultat.</p>':vis.length?`<div class="tw srt"><table><tr>${SCOLS.map(th).join('')}<th></th></tr>${vis.map(row).join('')}</table></div>`:'<p class="mut">Aucun résultat ne correspond aux filtres.</p>'}
const msBox=$('#msearch');
msBox.addEventListener('submit',e=>{if(e.target.id!=='sf')return;e.preventDefault();const q=$('#sq').value.trim();if(q)startJob({command:'search',query:q})});
msBox.addEventListener('click',e=>{const b=e.target.closest('button');if(!b)return;const d=b.dataset;
  if(d.grab)grab(+d.grab,b);else if(d.ltf&&d.lts==='srch'){LTF.srch=d.ltf;drawMsTable()}
  else if(d.msort){const k=d.msort;if(MSF.key===k)MSF.dir=-MSF.dir;else{MSF.key=k;MSF.dir=(k==='title'||k==='indexer')?1:-1}drawMsTable()}});
msBox.addEventListener('input',e=>{if(e.target.id==='msq'){MSF.q=e.target.value;drawMsTable()}});
msBox.addEventListener('change',e=>{if(e.target.id==='msok'){MSF.ok=e.target.checked;drawMsTable()}});
function drawPlex(j){const r=j.result||{};let st;
  if(j.status==='running')st='<span class="mut">En attente de la validation sur plex.tv (5 minutes max)…</span>';
  else if(j.status==='done')st=`<span class="o-ok">✔ ${esc((j.output.slice(-1)[0]||{}).text||'Compte Plex connecté')}</span>`;
  else st=`<span class="o-err">✖ ${esc(j.error||'Échec')}</span>`;
  $('#plexbox').innerHTML=`<div class="plex"><div>1. Ouvrez <a href="${esc(r.url||'https://plex.tv/link')}" target="_blank" rel="noopener noreferrer">plex.tv/link</a> (connecté à votre compte Plex)</div>
  <div>2. Saisissez le code :</div><b class="code">${esc(r.code||'····')}</b><div>${st}</div></div>`}
async function loadJobs(){const el=$('#jl');if(!el)return;try{const l=await api('jobs');
  el.innerHTML=l.length?l.map(j=>`<div class="jrow"><button class="lnk" data-job="${j.id}">${esc(j.label)}</button> ${badge(j.status)} <span class="mut small">${esc(fdate(j.started_at))}</span></div>`).join(''):'Aucune tâche depuis le démarrage.'}catch(e){el.textContent=e.message}}
async function openJob(id){try{const j=await api('jobs/'+id);track(j);const t=j.command==='search'?'#sres':j.command==='plex_login'?'#plexbox':'#outc';$(t).scrollIntoView({behavior:'smooth',block:'start'})}catch(e){toast(e.message,true)}}
async function plexLogin(){try{const r=await api('plex/login','POST');toast(r.message);track(r.job)}catch(e){toast(e.message,true)}}
async function grab(i,b){b.disabled=true;b.textContent='Envoi…';try{const r=await api('jobs/'+srch+'/grab','POST',{index:i});toast(r.message,!r.ok);b.textContent=r.ok?'Envoyé ✔':'Échec';if(!r.ok)b.disabled=false}
  catch(e){toast(e.message,true);b.textContent='Envoyer';b.disabled=false}}
async function waitRestart(s0){$('#conn').textContent='Redémarrage en cours…';const t0=Date.now();
  const wait=async()=>{try{const s=await api('status');if(s.started_at!==s0){toast('synoarr a redémarré');setTimeout(()=>location.reload(),800);return}}catch(e){}
    if(Date.now()-t0<600000)setTimeout(wait,2000);else $('#conn').textContent='⚠ synoarr ne répond plus : vérifiez le journal sur le NAS'};setTimeout(wait,2500)}
let upS0=null;
async function startUpdate(cmd){const q=cmd==='update'?'Mettre à jour synoarr depuis GitHub ? config.ini et data/ sont conservés ; synoarr redémarre ensuite.':'Revenir à la version précédente de synoarr ? synoarr redémarre ensuite.';
  if(!confirm(q))return;try{upS0=(await api('status')).started_at}catch(e){}startJob({command:cmd})}
async function restart(){if(!confirm('Redémarrer synoarr ? Les tâches en cours seront terminées avant.'))return;let s0=null;
  try{s0=(await api('status')).started_at}catch(e){}
  try{const r=await api('restart','POST');toast(r.message)}catch(e){toast(e.message,true);return}
  $('#conn').textContent='Redémarrage en cours…';const t0=Date.now();
  const wait=async()=>{try{const s=await api('status');if(s.started_at!==s0){toast('synoarr a redémarré');setTimeout(()=>location.reload(),800);return}}catch(e){}
    if(Date.now()-t0<600000)setTimeout(wait,2000);else $('#conn').textContent='⚠ synoarr ne répond plus : vérifiez le journal sur le NAS'};setTimeout(wait,2500)}
/* ------------------------------------------------------------------ Journal */
let logT=null,lqT=null;const LG={lines:'300',level:'DEBUG',q:'',auto:true};
try{Object.assign(LG,JSON.parse(localStorage.getItem('synoarr-log')||'{}'))}catch(e){}
const saveLG=()=>{try{localStorage.setItem('synoarr-log',JSON.stringify(LG))}catch(e){}};
S.logs=async()=>{app.innerHTML=`<div class="card"><div class="row tools lt">
  <select id="ln" aria-label="Nombre de lignes">${['100','300','1000'].map(n=>`<option value="${n}"${LG.lines===n?' selected':''}>${n} lignes</option>`).join('')}</select>
  <select id="lv" aria-label="Niveau">${[['DEBUG','Tous les niveaux'],['INFO','Info et plus'],['WARNING','Avertissements'],['ERROR','Erreurs']].map(l=>`<option value="${l[0]}"${LG.level===l[0]?' selected':''}>${l[1]}</option>`).join('')}</select>
  <input id="lq" type="search" placeholder="Filtrer le texte…" value="${esc(LG.q)}">
  <label class="chk"><input type="checkbox" id="la"${LG.auto?' checked':''}> Actualisation auto</label>
  <a class="btn sec" href="api/logs/download" download="synoarr.log">Télécharger le journal</a></div>
  <div id="lst" class="mut small"></div><div id="logv" class="logv" tabindex="0"></div></div>`;loadLogs()};
async function loadLogs(reset){clearTimeout(logT);const v=$('#logv');if(tab!=='logs'||!v)return;if(reset)delete v.dataset.init;
  try{const d=await api(`logs?lines=${LG.lines}&level=${LG.level}&q=${encodeURIComponent(LG.q)}`);
    const bottom=!v.dataset.init||v.scrollHeight-v.scrollTop-v.clientHeight<40;
    v.innerHTML=d.lines.length?d.lines.map(l=>`<div class="ll lv-${esc(l.level)}">${esc(l.text)}</div>`).join(''):`<p class="mut">${d.exists?'Aucune ligne ne correspond aux filtres.':'Journal introuvable : '+esc(d.path)}</p>`;
    v.dataset.init='1';if(bottom)v.scrollTop=v.scrollHeight;
    $('#lst').textContent=`${d.lines.length} entrée(s) affichée(s)${d.matched>d.lines.length?' sur '+d.matched:''} · ${esc(d.path)} (${size(d.size)||'0 o'})${d.partial?' · fin du fichier seulement':''}`;$('#conn').textContent=''}
  catch(e){$('#conn').textContent='⚠ '+e.message}
  if(LG.auto)logT=setTimeout(loadLogs,3000)}
/* ------------------------------------------------------------ Configuration */
let cfgD=null;
const dirty=()=>cfgD&&cfgD.text!==cfgD.orig;
/* formulaire (GET/POST api/config/form) + éditeur brut replié (« Mode avancé ») */
let CFF=null,CFINIT={},CFQ='',CFADV=false;const CFOPEN=new Set(['general']);
const cfId=(s,k)=>'cf_'+s+'__'+k;
const cfVal=f=>f.type==='bool'?!!f.value:f.type==='list'?(Array.isArray(f.value)?f.value.join(', '):String(f.value||'')):(f.value==null?'':String(f.value));
function cfInput(sec,f){const id=cfId(sec.name,f.key),a=`id="${id}" data-cs="${esc(sec.name)}" data-ck="${esc(f.key)}"`;
  if(f.secret)return `<input type="password" ${a} autocomplete="new-password" placeholder="${f.has_value?'•••• défini (laisser vide = inchangé)':'non défini'}">${f.has_value?`<label class="chk"><input type="checkbox" id="${id}__clr" data-cclr="${id}"> effacer</label>`:''}`;
  if(f.type==='bool')return `<label class="chk cfb"><input type="checkbox" ${a}${f.value?' checked':''}> ${f.value?'oui':'non'}</label>`;
  const v=cfVal(f);
  if(f.choices){const o=f.choices.some(c=>c[0]===v)?f.choices:f.choices.concat([[v,v]]);return `<select ${a}>${o.map(c=>`<option value="${esc(c[0])}"${c[0]===v?' selected':''}>${esc(c[1])}</option>`).join('')}</select>`}
  if(f.type==='int'||f.type==='float')return `<input type="number" ${a} step="${f.type==='int'?1:'any'}" value="${esc(v)}">`;
  return `<input type="text" ${a} spellcheck="false" value="${esc(v)}"${f.type==='list'?' placeholder="valeurs séparées par des virgules"':''}>`}
const cfDef=f=>f.secret||f.default==null?'':` <span class="mut small">· défaut : ${esc(cfVal({type:f.type,value:f.default})||'vide')}</span>`;
function cfForm(d){CFINIT={};
  return d.sections.map(sec=>`<details class="card cfs" data-sec="${esc(sec.name)}"${CFOPEN.has(sec.name)||CFQ?' open':''}><summary><b>${esc(sec.title)}</b> <span class="mut small">[${esc(sec.name)}]</span> <span class="b b-grabbed" data-cfmod="${esc(sec.name)}" hidden></span></summary>
  ${sec.help?`<p class="mut small cfsh">${esc(sec.help)}</p>`:''}${sec.fields.map(f=>{const id=cfId(sec.name,f.key);CFINIT[id]=f.secret?'':cfVal(f);
    return `<div class="cff" data-q="${esc((f.key+' '+f.label+' '+f.help+' '+sec.title+' '+sec.name).toLowerCase())}"><label class="cfl" for="${id}"><b>${esc(f.label)}</b> <code>${esc(f.key)}</code>${cfDef(f)}</label>
    <div class="cfi">${cfInput(sec,f)}<div class="small o-err" id="${id}__err"></div></div>${f.help?`<div class="mut small cfh">${esc(f.help)}</div>`:''}</div>`}).join('')}</details>`).join('')}
function cfChanges(){const out={};let n=0;const per={};
  document.querySelectorAll('#cff [data-cs]').forEach(el=>{const s=el.dataset.cs,k=el.dataset.ck,id=el.id;let v=null;
    if(el.type==='password'){const c=$('#'+id+'__clr');if(el.value!=='')v=el.value;else if(c&&c.checked)v=''}
    else{const cur=el.type==='checkbox'?el.checked:el.value;if(cur!==CFINIT[id])v=cur}
    if(v!==null){(out[s]=out[s]||{})[k]=v;n++;per[s]=(per[s]||0)+1}});
  return {out,n,per}}
function cfUpdate(t){if(t&&t.type==='checkbox'&&t.dataset.cs&&t.parentNode.classList.contains('cfb'))t.parentNode.lastChild.textContent=t.checked?' oui':' non';
  const c=cfChanges();document.querySelectorAll('[data-cfmod]').forEach(b=>{const k=c.per[b.dataset.cfmod]||0;b.hidden=!k;b.textContent=k+' modifié'+(k>1?'s':'')});
  const cn=$('#cfn');if(cn)cn.textContent=c.n?c.n+' modification'+(c.n>1?'s':'')+' non enregistrée'+(c.n>1?'s':''):'';
  document.querySelectorAll('[data-cffsave]').forEach(b=>b.disabled=!c.n);return c}
function cfFilter(){const q=CFQ.trim().toLowerCase();let shown=0;
  document.querySelectorAll('#cff .cfs').forEach(sec=>{let vis=0;sec.querySelectorAll('.cff').forEach(r=>{const ok=!q||r.dataset.q.includes(q);r.hidden=!ok;if(ok)vis++});
    sec.hidden=!vis;if(q&&vis)sec.open=true;shown+=vis});const e=$('#cfqn');if(e)e.textContent=q?shown+' option(s)':''}
async function cfgFormSave(){const c=cfChanges(),box=$('#cffr');if(!c.n){toast('Aucune modification');return}
  document.querySelectorAll('#cff .o-err').forEach(e=>e.textContent='');
  try{const r=await api('config/form','POST',{changes:c.out});toast(r.message);await S.cfg();$('#cffr').innerHTML=cfgResult(r)}
  catch(e){const p=(e.data&&e.data.problems)||[];
    p.forEach(x=>{const m=/^\[(\w+)\]\s+(?:\w+\.)?(\w+)/.exec(x);const el=m&&$('#'+cfId(m[1],m[2])+'__err');if(el){el.textContent=x;const d=el.closest('details');if(d)d.open=true}});
    box.innerHTML=`<div class="msg ko"><b>${esc(e.message)}</b>${p.length?`<ul>${p.map(x=>`<li>${esc(x)}</li>`).join('')}</ul>`:''}</div>`;box.scrollIntoView({block:'nearest'});toast(e.message,true)}}
S.cfg=async()=>{const [d,f]=await Promise.all([api('config'),api('config/form').catch(e=>({error:e.message}))]);if(!dirty())cfgD={text:d.text,orig:d.text};else cfgD.orig=d.text;CFF=f;
  app.innerHTML=`${d.auth_enabled?'':`<div class="card alert"><h3>⚠ Interface web non protégée</h3><p>Aucun identifiant n'est défini dans la section <code>[web]</code> : toute personne connectée à votre réseau local peut ouvrir cette page, <b>lire la configuration avec tous les mots de passe</b> (Download Station, jeton Plex, clés d'API) et la modifier.</p>
  <p>Renseignez <b>Identifiant</b> et <b>Mot de passe</b> dans « Interface web » ci-dessous, enregistrez, puis redémarrez synoarr.</p></div>`}
  <div id="rsb">${rsBanner(d.restart_reasons)}</div>
  <div class="card cfbar"><div class="row"><input id="cfq" type="search" placeholder="Rechercher une option (ex. langues, port, telegram)…" value="${esc(CFQ)}" aria-label="Rechercher une option"><span id="cfqn" class="mut small"></span></div>
  <div class="row cfsv"><button data-cffsave="1" disabled>Enregistrer</button><button class="sec" data-cffreset="1">Annuler les modifications</button><span id="cfn" class="mut small"></span></div>
  <div class="mut small brk">${esc(d.path)} · seules les options modifiées sont réécrites (commentaires conservés) ; le fichier est vérifié avant d'être remplacé.</div><div id="cffr"></div></div>
  <div id="cff">${f.error?`<div class="msg ko">${esc(f.error)}</div>`:cfForm(f)}</div>
  <div class="row cfsv"><button data-cffsave="1" disabled>Enregistrer</button></div>
  <details class="card" id="cfadv"${CFADV?' open':''}><summary><b>Mode avancé (fichier config.ini)</b> <span id="cfdirty" class="b b-grabbed" hidden>modifié</span></summary>
  <div class="cfgh"><div class="mut small brk">${esc(d.path)}${d.encoding!=='utf-8'?` · encodage actuel ${esc(d.encoding)} : sera enregistré en UTF-8`:''}</div>
  <div class="acts"><button data-cfgsave="1">Enregistrer le fichier</button><button class="sec" data-cfgreset="1">Annuler les modifications</button><button class="sec" data-cfgrestore="1"${d.backups.length?'':' disabled'}>Restaurer la sauvegarde précédente</button></div></div>
  <div id="cfr"></div><textarea id="cft" spellcheck="false" autocapitalize="off" autocomplete="off" autocorrect="off" wrap="off" aria-label="Contenu de config.ini"></textarea>
  <p class="mut small">Le fichier est vérifié avant d'être remplacé ; en cas d'erreur rien n'est modifié. Les 5 versions précédentes sont conservées (config.ini.bak1 à .bak5)${d.backups.length?' — dernière sauvegarde : '+esc(d.backups[0].modified):''}. Ctrl+S pour enregistrer.</p></details>`;
  $('#cft').value=cfgD.text;markDirty();cfFilter();cfUpdate()};
function markDirty(){const b=$('#cfdirty');if(b)b.hidden=!dirty()}
function cfgResult(r){let h=`<div class="msg ok">✔ ${esc(r.message)}</div>`;
  if(r.warnings&&r.warnings.length)h+=`<div class="msg wa"><b>Avertissements :</b><ul>${r.warnings.map(w=>`<li>${esc(w)}</li>`).join('')}</ul></div>`;
  if(r.auth_configured&&!r.auth_enabled)h+='<div class="msg wa">Les identifiants de l\'interface web seront demandés après le redémarrage.</div>';
  return h}
async function cfgSave(){if(!cfgD)return;const text=$('#cft').value;try{const r=await api('config','POST',{text});cfgD={text,orig:text};
    await S.cfg();$('#cfr').innerHTML=cfgResult(r);toast(r.message)}
  catch(e){const p=(e.data&&e.data.problems)||[];$('#cfr').innerHTML=`<div class="msg ko"><b>${esc(e.message)}</b>${p.length?`<ul>${p.map(x=>`<li>${esc(x)}</li>`).join('')}</ul>`:''}</div>`;toast(e.message,true)}}
async function cfgRestore(){if(!confirm('Restaurer la sauvegarde précédente de config.ini ? La version actuelle sera conservée comme sauvegarde.'))return;
  try{const r=await api('config/restore','POST');cfgD=null;await S.cfg();$('#cfr').innerHTML=cfgResult(r);toast(r.message)}
  catch(e){const p=(e.data&&e.data.problems)||[];$('#cfr').innerHTML=`<div class="msg ko"><b>${esc(e.message)}</b>${p.length?`<ul>${p.map(x=>`<li>${esc(x)}</li>`).join('')}</ul>`:''}</div>`;toast(e.message,true)}}
/* -------------------------------------------------------------------- moteur */
async function refresh(){clearTimeout(timer);document.querySelectorAll('nav a').forEach(a=>a.classList.toggle('on',a.hash==='#'+tab));
  if(tab!=='logs')clearTimeout(logT);
  if(S[tab]){if(mounted!==tab){mounted=tab;try{await S[tab]();$('#conn').textContent=''}catch(e){mounted=null;app.innerHTML=`<p class="card msg ko">${esc(e.message)}</p>`}}return}
  mounted=null;
  const fa=document.activeElement;if(fa&&app.contains(fa)&&((fa.dataset&&fa.dataset.prio)||fa.type==='date')){timer=setTimeout(refresh,15000);return}
  dlActive=false;
  try{const html=await (V[tab]||V.dash)();const ae=document.activeElement,fid=ae&&ae.id,pos=fid&&ae.selectionStart;
    app.innerHTML=html;if(fid&&$('#'+fid)){const el=$('#'+fid);el.focus();if(pos!=null&&el.setSelectionRange)el.setSelectionRange(pos,pos)}
    $('#conn').textContent='';syncBulk()}
  catch(e){$('#conn').textContent='⚠ '+e.message}
  timer=setTimeout(refresh,dlActive&&(tab==='watch'||tab==='dash')?5000:15000)}
addEventListener('hashchange',()=>{tab=(location.hash||'#dash').slice(1);mounted=null;app.innerHTML='<p class="mut">Chargement…</p>';syncBulk();refresh()});
addEventListener('beforeunload',e=>{if(dirty()||(tab==='cfg'&&$('#cff')&&cfChanges().n)){e.preventDefault();e.returnValue=''}});
document.addEventListener('keydown',e=>{if(tab==='cfg'&&(e.ctrlKey||e.metaKey)&&e.key==='s'){e.preventDefault();if(document.activeElement&&document.activeElement.id==='cft')cfgSave();else cfgFormSave()}});
app.addEventListener('input',e=>{const id=e.target.id;if(id==='wq'||id==='wf')refresh();
  else if(id==='prwu')PRW.url=e.target.value;else if(id==='prwk')PRW.key=e.target.value;
  else if(id==='lq'){LG.q=e.target.value;saveLG();clearTimeout(lqT);lqT=setTimeout(()=>loadLogs(true),300)}
  else if(id==='cft'&&cfgD){cfgD.text=e.target.value;markDirty()}
  else if(id==='cfq'){CFQ=e.target.value;cfFilter()}
  else if(e.target.dataset.cs||e.target.dataset.cclr)cfUpdate(e.target)});
app.addEventListener('change',e=>{const t=e.target,id=t.id;
  if(t.dataset.lang)act(()=>api('media/'+t.dataset.lang+'/language','POST',{languages:t.value}));
  else if(t.dataset.sel)selToggle(+t.dataset.sel,t.checked);
  else if(t.dataset.cs||t.dataset.cclr)cfUpdate(t);
  else if(id==='hp'){HP.p=t.value;if(HP.p==='custom'&&!HP.from){HP.from=isoD(new Date(Date.now()-29*864e5));HP.to=HP.to||isoD(new Date())}saveHP();refresh()}
  else if(id==='hpf'||id==='hpt'){HP[id==='hpf'?'from':'to']=t.value;saveHP();t.blur();refresh()}
  else if(id==='cald'){CALD=t.value;try{localStorage.setItem('synoarr.cald',CALD)}catch(e){}refresh()}
  else if(t.dataset.prio)setPrio(t.dataset.prio,t.value);
  else if(id==='ln'||id==='lv'){LG[id==='ln'?'lines':'level']=t.value;saveLG();loadLogs(true)}
  else if(id==='la'){LG.auto=t.checked;saveLG();loadLogs()}});
app.addEventListener('submit',e=>{if(e.target.id==='prwf'){e.preventDefault();prowlarr();return}
  if(e.target.id==='jktf'){e.preventDefault();jackett();return}
  if(e.target.id==='sf'){e.preventDefault();const q=$('#sq').value.trim();if(q)startJob({command:'search',query:q})}});
app.addEventListener('click',e=>{const b=e.target.closest('button');if(!b)return;const d=b.dataset;
  if(d.run)act(()=>api('run/'+d.run,'POST'));
  else if(d.src)openSrc(d);
  else if(d.wadd)openAdd();
  else if(d.selmode){SEL=SEL?null:new Set();refresh()}
  else if(d.wdel)openDel(d);
  else if(d.ltf&&d.lts==='srch'){LTF.srch=d.ltf;if(srch&&jobs[srch])drawSearch(jobs[srch])}
  else if(d.tfr){const v=prompt('Titre français utilisé pour chercher les releases françaises (vide = recherche automatique sur TMDb) :',d.v||'');
    if(v!==null)act(()=>api('media/'+d.tfr+'/title_fr','POST',{title_fr:v}))}
  else if(d.wst){WST=d.wst;try{localStorage.setItem('synoarr.wst',WST)}catch(e){}refresh()}
  else if(d.iedit)openIdxEdit(d.iedit);
  else if(d.pd)setPrio(d.pd,d.v);
  else if(d.prws)prwSync();
  else if(d.prwx)prwForget();
  else if(d.jkts)jktSync();
  else if(d.jktx)jktForget();
  else if(d.cmd==='update'||d.cmd==='update_rollback')startUpdate(d.cmd);
  else if(d.cmd)startJob({command:d.cmd,force:d.cmd==='search_all'&&!!($('#force')&&$('#force').checked)});
  else if(d.plex)plexLogin();
  else if(d.restart)restart();
  else if(d.grab)grab(+d.grab,b);
  else if(d.job)openJob(+d.job);
  else if(d.cfgsave)cfgSave();
  else if(d.cffsave)cfgFormSave();
  else if(d.cffreset){if(!cfChanges().n||confirm('Abandonner les modifications non enregistrées du formulaire ?')){mounted=null;refresh()}}
  else if(d.cfgreset){if(!dirty()||confirm('Abandonner les modifications non enregistrées ?')){cfgD=null;mounted=null;refresh()}}
  else if(d.cfgrestore)cfgRestore();
  else if(d.search)act(()=>api('media/'+d.search+'/search','POST'));
  else if(d.st)act(()=>api('media/'+d.st+'/status','POST',{status:d.v}),'Statut mis à jour');
  else if(d.eps){const id=+d.eps;exp.has(id)?exp.delete(id):exp.add(id);refresh()}
  else if(d.test){const t=$('#tr'+d.test);t.textContent='Test en cours…';t.className='tres';
    api('indexers/'+d.test+'/test','POST').then(r=>{t.textContent=(r.ok?'✔ ':'✖ ')+r.message;t.className='tres '+(r.ok?'ok':'ko')},er=>{t.textContent='✖ '+er.message;t.className='tres ko'})}
  else if(d.en)act(()=>api('indexers/'+d.en+'/enabled','POST',{enabled:d.v==='1'}),'Indexer mis à jour');
  else if(d.del){if(confirm('Supprimer l\'indexer « '+d.n+' » ?'))act(()=>api('indexers/'+d.del,'DELETE'),'Indexer supprimé')}
  else if(d.cat)act(()=>api('indexers','POST',{catalog_id:d.cat}),'Indexer ajouté')});
$('#addf').addEventListener('submit',e=>{e.preventDefault();const f=e.target,v={};new FormData(f).forEach((x,k)=>v[k]=x);
  act(async()=>{const r=await api('indexers','POST',v);f.reset();return r},'Indexer ajouté')});
function setPrio(id,v){const n=Number(v);if(!Number.isInteger(n)||n<1||n>50){toast('Priorité invalide : nombre entier de 1 (le plus prioritaire) à 50',true);refresh();return}
  act(()=>api('indexers/'+id+'/priority','POST',{priority:n}),'Priorité mise à jour')}
/* ---------------------------------------------------- Changer de source */
let SRC=null;const srcm=$('#srcm');
function openSrc(d){const num=v=>v===''||v==null?null:+v;
  SRC={mid:+d.src,s:num(d.s),e:num(d.e),g:d.g?+d.g:null,label:d.t||'',rt:d.rt||'',job:null,res:[],fo:null,run:0};srcJob=null;
  $('#srch').textContent=SRC.g?'Changer de source':'Choisir une source';
  $('#srcb').innerHTML=`<p class="small"><b>${esc(SRC.label)}</b>${SRC.rt?`<br><span class="mut brk">Source actuelle : ${esc(SRC.rt)}</span>`:''}</p>
  ${SRC.g?`<div class="row opts"><label class="chk"><input type="checkbox" id="sdel" checked> Supprimer l'ancien téléchargement</label><label class="chk"><input type="checkbox" id="sblk"> Bloquer l'ancienne release</label></div>`:''}
  <div id="smsg"></div>
  <h4>Résultats des indexers</h4><div id="sfo"></div><div id="sres2"><p class="mut">Recherche en cours sur les indexers…</p></div>
  <h4>Lien magnet ou URL d'un .torrent</h4><form id="slf" class="row" autocomplete="off"><input id="slink" type="text" spellcheck="false" placeholder="magnet:?xt=urn:btih:… ou https://…/fichier.torrent" aria-label="Lien magnet ou URL d'un fichier .torrent"><button type="submit">Envoyer le lien</button></form>
  <h4>Fichier .torrent</h4><form id="sff" class="row"><input id="sfile" type="file" accept=".torrent,application/x-bittorrent" aria-label="Fichier .torrent"><button type="submit">Envoyer le fichier</button></form>`;
  srcm.hidden=false;document.body.classList.add('noscroll');$('#srcx').focus();srcSearch()}
function closeSrc(){SRC=null;srcm.hidden=true;if(!GM)document.body.classList.remove('noscroll')}
async function srcSearch(){const S0=SRC,run=++S0.run,live=()=>SRC===S0&&S0.run===run;const body={season:S0.s,episode:S0.e};if(S0.fo!==null)body.french_only=S0.fo;
  const k=$('#sfoc');if(k)k.disabled=true;
  try{const r=await api('media/'+S0.mid+'/sources','POST',body);S0.job=r.job.id;let j=r.job,fails=0;
    while(live()&&j.status==='running'){await sleep(800);try{j=await api('jobs/'+j.id);fails=0}catch(e){if(e.status===404||++fails>20)throw e}}
    if(live())drawSrcRes(j)}
  catch(e){if(live()){$('#sres2').innerHTML=`<div class="msg ko">${esc(e.message)}</div>`;const c=$('#sfoc');if(c)c.disabled=false}}}
/* « Titre français seulement » : relance la recherche des sources (filtres et tri conservés) */
const fbNote=r=>r&&r.french_only_default==null&&r.french_only==null&&(r.fallback||r.french_title_fallback)?' <span class="b b-grabbed">recherche élargie au titre original</span>':'';
function drawFo(r){const el=$('#sfo');if(!el)return;const dflt=r?(r.french_only_default!=null?r.french_only_default:null):null;
  if(!r||(dflt==null&&r.french_only==null)){el.innerHTML='';return}
  const on=r.french_only!=null?!!r.french_only:!!dflt;
  el.innerHTML=`<div class="row opts"><label class="chk"><input type="checkbox" id="sfoc"${on?' checked':''}> Titre français seulement</label>${on&&(r.fallback||r.french_title_fallback)?'<span class="mut small">(recherche élargie au titre original)</span>':''}${dflt!=null&&on!==!!dflt?`<span class="mut small">(par défaut : ${dflt?'oui':'non'})</span>`:''}</div>`}
function srcFoToggle(on){if(!SRC)return;SRC.fo=on;srcJob=null;$('#sres2').innerHTML='<p class="mut">Nouvelle recherche sur les indexers…</p>';srcSearch()}
let srcJob=null;
/* tri + filtres de la liste des sources */
const SF={key:'score',dir:-1,q:'',smin:'',smax:'',seed:'',idx:'',ok:false};
const SCOLS=[['score','Score'],['title','Release'],['size','Taille'],['seeders','Seeders'],['indexer','Indexer']];
const sval=(x,k)=>k==='title'||k==='indexer'?String(x[k]||'').toLowerCase():(x[k]==null?-1:+x[k]);
function srcRows(list){const GB=1073741824;const q=SF.q.trim().toLowerCase();
  const smin=SF.smin===''?null:parseFloat(SF.smin)*GB,smax=SF.smax===''?null:parseFloat(SF.smax)*GB,seed=SF.seed===''?null:parseInt(SF.seed,10);
  const out=list.filter(x=>ltPass(x,LTF.src)&&(!q||String(x.title).toLowerCase().includes(q))&&(smin==null||isNaN(smin)||(x.size||0)>=smin)
    &&(smax==null||isNaN(smax)||((x.size||0)>0&&x.size<=smax))&&(seed==null||isNaN(seed)||(x.seeders!=null&&x.seeders>=seed))
    &&(!SF.idx||x.indexer===SF.idx)&&(!SF.ok||x.score!=null));
  out.sort((a,b)=>{const va=sval(a,SF.key),vb=sval(b,SF.key);return (va<vb?-1:va>vb?1:0)*SF.dir||(a.index-b.index)});return out}
function drawSrcTable(){const r=srcJob&&srcJob.result,el=$('#srctbl');if(!r||!el)return;const vis=srcRows(r.results);
  $('#srcn').textContent=`${vis.length} / ${r.results.length} affiché(s)`;
  $('#srcchips').innerHTML=ltChips(r.results,'src');
  const th=c=>`<th${c[0]==='size'||c[0]==='seeders'||c[0]==='indexer'?' class="hm"':''}><button type="button" class="sortb${SF.key===c[0]?' on':''}" data-sort="${c[0]}" title="Trier par ${c[1].toLowerCase()}">${c[1]}${SF.key===c[0]?(SF.dir<0?' ▼':' ▲'):''}</button></th>`;
  el.innerHTML=vis.length?`<div class="tw srt"><table><tr>${SCOLS.map(th).join('')}<th></th></tr>
  ${vis.map(x=>`<tr${x.score==null?' class="rej"':''}><td class="nw">${x.score==null?'rejeté':esc(x.score)}</td><td class="brk"><b>${esc(x.title)}</b>${lt(x)}<div class="mut small">${esc(x.reason)}</div><div class="mut small sm">${size(x.size)} · ${nv(x.seeders)} seeders · ${esc(x.indexer)}${pr(x)}</div></td>
  <td class="hm nw">${size(x.size)}</td><td class="hm">${nv(x.seeders)}</td><td class="hm">${esc(x.indexer)}${pr(x)}</td><td><button class="xs${x.score==null?' sec':''}" data-pick="${x.index}">${x.score==null?'Choisir<span class="hm"> quand même</span>':'Choisir'}</button></td></tr>`).join('')}</table></div>`
  :'<p class="mut">Aucun résultat ne correspond aux filtres.</p>'}
function drawSrcRes(j){const first=srcJob!==j||!$('#srctbl');srcJob=j;const r=j.result,el=$('#sres2');drawFo(j.status==='error'?null:r);if(j.status==='error'||!r){el.innerHTML=`<div class="msg ko">${esc(j.error||'Recherche impossible')}</div>`;return}
  SRC.res=r.results;const langs=esc((r.languages||[]).join(', ')||'—');
  if(!r.results.length){const ts=(r.titles||[]).length?(r.titles||[]).map(esc).join(' · '):esc(r.request);
    el.innerHTML=`<p class="mut">Aucun résultat. Titres cherchés : <b>${ts}</b>${r.fallback?' (rien avec le titre français : recherche élargie au titre original)':r.french_title_only?' (titre français seulement)':''}${fbNote(r)} · langues : ${langs}. Collez un lien ou envoyez un fichier .torrent ci-dessous.</p>`;return}
  if(first){const ok=r.results.filter(x=>x.score!=null).length,idxs=[...new Set(r.results.map(x=>x.indexer))].sort();
    if(SF.idx&&!idxs.includes(SF.idx))SF.idx='';
    const opt=(v,l)=>`<option value="${esc(v)}"${SF.key===v?' selected':''}>${l}</option>`;
    el.innerHTML=`<p class="mut small">Requête : ${esc(r.request)}${(r.titles||[]).length?` · titres cherchés : <b>${(r.titles||[]).map(esc).join(' · ')}</b>${r.fallback?' (rien avec le titre français : recherche élargie au titre original)':r.french_title_only?' (titre français seulement)':''}`:''}${fbNote(r)} · langues : ${langs} · ${r.total} résultat(s), ${ok} acceptable(s). Les résultats rejetés peuvent aussi être choisis. <span class="b b-done">.torrent</span> = fichier téléchargé directement ; <span class="b b-grabbed">magnet</span> = Download Station doit d'abord trouver des sources.</p>
    <div id="srcchips"></div>
    <div class="row sfil" role="group" aria-label="Filtrer les sources">
      <input id="sfq" type="search" placeholder="Filtrer le titre (ex. 1080p, MULTi…)" value="${esc(SF.q)}" aria-label="Filtrer le titre">
      <label class="mini">Taille (Go) <input id="sfsmin" type="number" min="0" step="0.1" placeholder="min" value="${esc(SF.smin)}" aria-label="Taille minimale en Go"> – <input id="sfsmax" type="number" min="0" step="0.1" placeholder="max" value="${esc(SF.smax)}" aria-label="Taille maximale en Go"></label>
      <label class="mini">Seeders ≥ <input id="sfseed" type="number" min="0" step="1" placeholder="0" value="${esc(SF.seed)}" aria-label="Seeders minimum"></label>
      <select id="sfidx" aria-label="Indexer"><option value="">Tous les indexers</option>${idxs.map(v=>`<option value="${esc(v)}"${SF.idx===v?' selected':''}>${esc(v)}</option>`).join('')}</select>
      <select id="sfsort" aria-label="Trier par">${opt('score','Tri : score')}${opt('seeders','Tri : seeders')}${opt('size','Tri : taille')}${opt('title','Tri : titre')}${opt('indexer','Tri : indexer')}</select>
      <label class="chk"><input type="checkbox" id="sfok"${SF.ok?' checked':''}> Acceptables seulement</label>
      <button type="button" class="xs sec" data-sfreset="1">Réinitialiser</button><span id="srcn" class="mut small"></span>
    </div><div id="srctbl"></div>`}
  drawSrcTable()}
function srcFilterInput(){SF.q=$('#sfq').value;SF.smin=$('#sfsmin').value;SF.smax=$('#sfsmax').value;SF.seed=$('#sfseed').value;
  SF.idx=$('#sfidx').value;SF.ok=$('#sfok').checked;const k=$('#sfsort').value;if(k!==SF.key){SF.key=k;SF.dir=(k==='title'||k==='indexer')?1:-1}drawSrcTable()}
async function srcSend(body){const S0=SRC;if(!S0)return;const msg=$('#smsg'),btns=[...document.querySelectorAll('#srcb button')];
  Object.assign(body,{season:S0.s,episode:S0.e,grab_id:S0.g,delete_old:!!($('#sdel')&&$('#sdel').checked),blocklist_old:!!($('#sblk')&&$('#sblk').checked)});
  btns.forEach(b=>b.disabled=true);msg.innerHTML='<div class="msg wa">Envoi à Download Station…</div>';msg.scrollIntoView({block:'nearest'});
  try{const r=await api('media/'+S0.mid+'/replace','POST',body);
    if(r.ok){toast(r.message);closeSrc();refresh();return}
    if(SRC===S0)msg.innerHTML=`<div class="msg ko">✖ ${esc(r.message)}</div>`;toast(r.message,true)}
  catch(e){if(SRC===S0)msg.innerHTML=`<div class="msg ko">✖ ${esc(e.message)}</div>`;toast(e.message,true)}
  btns.forEach(b=>b.disabled=false);if(SRC===S0)msg.scrollIntoView({block:'nearest'})}
function pick(i){const x=(SRC&&SRC.res||[]).find(r=>r.index===i);if(!x)return;
  if(x.score==null&&!confirm('Cette release a été rejetée :\n'+x.reason+'\n\nL\'envoyer quand même à Download Station ?'))return;
  srcSend({job_id:SRC.job,index:i,accept_rejected:x.score==null})}
async function srcFile(){const f=$('#sfile').files[0];if(!f){toast('Choisissez un fichier .torrent',true);return}
  if(f.size>700*1024){toast('Fichier trop volumineux pour un .torrent (700 Ko maximum)',true);return}
  try{const data=await new Promise((ok,ko)=>{const rd=new FileReader();rd.onload=()=>ok(String(rd.result));rd.onerror=()=>ko(new Error('Lecture du fichier impossible'));rd.readAsDataURL(f)});
    srcSend({torrent_base64:data.slice(data.indexOf(',')+1)})}catch(e){toast(e.message,true)}}
srcm.addEventListener('click',e=>{if(e.target===srcm||e.target.closest('[data-mclose]')){closeSrc();return}
  const f=e.target.closest('button[data-ltf]');if(f){LTF.src=f.dataset.ltf;drawSrcTable();return}
  const so=e.target.closest('button[data-sort]');if(so){const k=so.dataset.sort;if(SF.key===k)SF.dir=-SF.dir;else{SF.key=k;SF.dir=(k==='title'||k==='indexer')?1:-1}
    if($('#sfsort'))$('#sfsort').value=SF.key;drawSrcTable();return}
  if(e.target.closest('[data-sfreset]')){Object.assign(SF,{key:'score',dir:-1,q:'',smin:'',smax:'',seed:'',idx:'',ok:false});LTF.src='all';
    if(srcJob){const j0=srcJob;srcJob=null;drawSrcRes(j0)}return}
  const b=e.target.closest('button[data-pick]');if(b)pick(+b.dataset.pick)});
srcm.addEventListener('input',e=>{if(e.target.closest('.sfil'))srcFilterInput()});
srcm.addEventListener('change',e=>{if(e.target.id==='sfoc'){srcFoToggle(e.target.checked);return}if(e.target.closest('.sfil'))srcFilterInput()});
srcm.addEventListener('submit',e=>{e.preventDefault();if(e.target.id==='slf'){const v=$('#slink').value.trim();if(!v){toast('Collez un lien magnet ou une URL .torrent',true);return}srcSend({link:v})}
  else if(e.target.id==='sff')srcFile()});
document.addEventListener('keydown',e=>{if(e.key!=='Escape')return;if(SRC)closeSrc();else if(GM)closeGM()});
/* ------------------------------------------- Watchlist : ajouter / supprimer */
let GM=null;const gm=$('#gm');
function openGM(kind,title,html){GM={kind,res:[]};$('#gmh').textContent=title;$('#gmb').innerHTML=html;gm.hidden=false;document.body.classList.add('noscroll')}
function closeGM(){GM=null;gm.hidden=true;if(!SRC)document.body.classList.remove('noscroll')}
function openAdd(){openGM('add','Ajouter à la Watchlist',`<form id="wsf" class="row" autocomplete="off"><input id="wsq" type="search" placeholder="Titre du film ou de la série" required maxlength="200" aria-label="Titre">
  <select id="wsk" aria-label="Type"><option value="">Tous</option><option value="movie">Film</option><option value="show">Série</option></select><input id="wsy" class="yr" type="number" min="1870" max="2100" placeholder="Année" aria-label="Année (facultatif)"><button type="submit">Rechercher</button></form>
  <div class="row opts"><label class="chk"><input type="checkbox" id="wsp" checked> Ajouter aussi à la Watchlist Plex</label><label class="chk"><input type="checkbox" id="wsn" checked> Rechercher un téléchargement tout de suite</label>
  <label class="chk">Langue <select id="wsl" aria-label="Langue du téléchargement"><option value="">Auto</option><option value="en">Anglais (en)</option><option value="fr">Français (fr)</option><option value="en,fr">Anglais + Français</option></select></label></div>
  <div id="wsm"></div><div id="wsr"><p class="mut small">Recherche dans Plex (Découvrir) et TMDb si une clé est configurée.</p></div>
  <details><summary>Ajout manuel (sans recherche)</summary><form id="wmf" class="add" autocomplete="off"><select id="wmt" aria-label="Type"><option value="movie">Film</option><option value="show">Série</option></select>
  <input id="wmn" placeholder="Titre" required maxlength="200" aria-label="Titre"><input id="wmy" type="number" min="1870" max="2100" placeholder="Année (facultatif)" aria-label="Année"><input id="wmi" placeholder="IMDb, ex. tt0133093 (facultatif)" aria-label="Identifiant IMDb">
  <div class="full"><button type="submit">Ajouter</button></div></form></details>`);$('#wsq').focus()}
async function wSearch(){const q=$('#wsq').value.trim();if(!q)return;const r0=$('#wsr');r0.innerHTML='<p class="mut">Recherche…</p>';
  try{const r=await api(`watchlist/search?q=${encodeURIComponent(q)}&kind=${$('#wsk').value}&year=${encodeURIComponent($('#wsy').value)}`);if(!GM)return;GM.res=r.results;
    const w=(r.warnings||[]).map(x=>`<div class="msg wa">${esc(x)}</div>`).join('');
    r0.innerHTML=w+(r.results.length?`<div class="cands">${r.results.map((c,i)=>`<div class="cand">${c.poster_url&&/^https:\/\//.test(c.poster_url)?`<img src="${esc(c.poster_url)}" alt="" loading="lazy" referrerpolicy="no-referrer">`:'<div class="noimg" aria-hidden="true">?</div>'}
      <div class="cb"><div><b>${esc(c.title)}</b> ${c.year?`<span class="mut">(${esc(c.year)})</span>`:''}</div><div class="meta">${badge(c.type)} <span class="b">${c.source==='plex'?'Plex':'TMDb'}</span>${c.in_watchlist?' <span class="b b-done">déjà dans la liste</span>':''}</div>
      ${c.original_title&&c.original_title!==c.title?`<div class="mut small">${esc(c.original_title)}</div>`:''}${c.overview?`<p class="small ov">${esc(c.overview)}</p>`:''}
      <div><button class="xs" data-wpick="${i}"${c.in_watchlist?' disabled':''}>${c.in_watchlist?'Déjà suivi':'Ajouter'}</button></div></div></div>`).join('')}</div>`:`<p class="mut">Aucun résultat pour « ${esc(q)} » : essayez l'ajout manuel ci-dessous.</p>`)}
  catch(e){if(GM)r0.innerHTML=`<div class="msg ko">${esc(e.message)}</div>`}}
function wDone(r){const m=$('#wsm');if(m)m.innerHTML=`<div class="msg ok">✔ ${esc(r.message)}</div>${r.warning?`<div class="msg wa">${esc(r.warning)}</div>`:''}`;
  toast(r.message+(r.warning?' — '+r.warning:''));refresh()}
async function wAdd(i,b){const c=GM&&GM.res[i];if(!c)return;b.disabled=true;b.textContent='Ajout…';
  try{const r=await api('watchlist/add','POST',{candidate:c,add_to_plex:$('#wsp').checked,search_now:$('#wsn').checked,languages:$('#wsl').value});b.textContent='Ajouté ✔';wDone(r)}
  catch(e){b.disabled=false;b.textContent='Ajouter';toast(e.message,true);const m=$('#wsm');if(m)m.innerHTML=`<div class="msg ko">✖ ${esc(e.message)}</div>`}}
async function wManual(){const b=$('#wmf button');b.disabled=true;
  try{const r=await api('watchlist/manual','POST',{type:$('#wmt').value,title:$('#wmn').value.trim(),year:$('#wmy').value||null,imdb:$('#wmi').value.trim(),search_now:$('#wsn').checked,languages:$('#wsl').value});$('#wmf').reset();wDone(r)}
  catch(e){toast(e.message,true);const m=$('#wsm');if(m)m.innerHTML=`<div class="msg ko">✖ ${esc(e.message)}</div>`}b.disabled=false}
async function openIdxEdit(id){let i;try{i=await api('indexers/'+id)}catch(e){toast(e.message,true);return}
  const opt=(v,l)=>`<option value="${v}"${i.type===v?' selected':''}>${l}</option>`;
  openGM('iedit','Modifier l\'indexer',`<form id="ief" data-id="${i.id}" autocomplete="off">
  ${i.synced_by?`<div class="msg wa small">Indexer synchronisé avec ${esc(i.synced_by)} : l'URL et la clé${i.synced_by==='Prowlarr'?' et la priorité':''} sont réécrites à chaque synchronisation. Modifiez-les plutôt dans ${esc(i.synced_by)}.</div>`:''}
  <label class="fl">Nom<input id="ien" type="text" value="${esc(i.name)}" required></label>
  <label class="fl">Type<select id="iet">${opt('torznab','Torznab')}${opt('rss','RSS')}${opt('internet_archive','Internet Archive')}</select></label>
  <label class="fl">URL<input id="ieu" type="text" spellcheck="false" value="${esc(i.url)}" required></label>
  <label class="fl">Clé API<input id="iek" type="password" autocomplete="new-password" placeholder="${i.has_api_key?'inchangée (laisser vide pour la garder)':'aucune'}"></label>
  ${i.has_api_key?'<label class="chk"><input type="checkbox" id="iekc"> Effacer la clé API</label>':''}
  <label class="fl">Catégories Torznab<input id="iec" type="text" value="${esc(i.categories||'')}" placeholder="ex. 2000,5000 (vide = automatique)"></label>
  <label class="fl">Priorité (1 = le plus prioritaire, 50 = le moins)<input id="iep" type="number" min="1" max="50" value="${esc(i.priority)}"></label>
  <label class="chk"><input type="checkbox" id="iea"${i.enabled?' checked':''}> Actif</label>
  <div id="iem"></div>
  <div class="row"><button type="submit">Enregistrer et tester</button><button type="button" class="sec" data-gmclose="1">Annuler</button></div></form>`);
  $('#ien').focus()}
async function saveIdx(f){const id=f.dataset.id,m=$('#iem'),b=f.querySelector('button[type=submit]');b.disabled=true;
  const body={name:$('#ien').value,type:$('#iet').value,url:$('#ieu').value,api_key:$('#iek').value,
    clear_api_key:!!($('#iekc')&&$('#iekc').checked),categories:$('#iec').value,priority:$('#iep').value,enabled:$('#iea').checked};
  try{const r=await api('indexers/'+id+'/edit','POST',body);m.innerHTML='<div class="msg wa small">Enregistré, test en cours…</div>';
    let t;try{t=await api('indexers/'+id+'/test','POST',{})}catch(e){t={ok:false,message:e.message}}
    closeGM();toast(r.message+' — test : '+(t.ok?'OK':'échec')+(t.message?' ('+t.message+')':''),!t.ok);await refresh()}
  catch(e){b.disabled=false;m.innerHTML=`<div class="msg ko small">${esc(e.message)}</div>`}}
function openDel(d){openGM('del','Supprimer de la Watchlist',`<p>Retirer « <b>${esc(d.n)}</b> » de synoarr ?</p>
  ${d.local==='1'?'<p class="mut small">Entrée locale (hors Watchlist Plex).</p>':'<label class="chk"><input type="checkbox" id="wdp" checked> Retirer aussi de la Watchlist Plex</label>'}
  <label class="chk"><input type="checkbox" id="wdd"> Supprimer aussi les téléchargements en cours${+d.dl?` (${esc(d.dl)})`:''}</label>
  <p class="mut small">Les fichiers déjà rangés dans la bibliothèque ne sont jamais supprimés. Un téléchargement interrompu peut perdre ses données partielles.</p>
  <div class="row"><button class="danger" data-wdelok="${esc(d.wdel)}">Supprimer</button><button class="sec" data-gmclose="1">Annuler</button></div>`);gm.querySelector('[data-wdelok]').focus()}
async function wDel(id,b){b.disabled=true;
  try{const r=await api('media/'+id,'DELETE',{remove_from_plex:!!($('#wdp')&&$('#wdp').checked),delete_downloads:!!($('#wdd')&&$('#wdd').checked)});
    closeGM();toast(r.message+(r.warning?' — '+r.warning:''),false);await refresh()}
  catch(e){b.disabled=false;toast(e.message,true)}}
gm.addEventListener('click',e=>{if(e.target===gm||e.target.closest('[data-gmclose]')){closeGM();return}const b=e.target.closest('button');if(!b)return;
  if(b.dataset.wpick)wAdd(+b.dataset.wpick,b);else if(b.dataset.wdelok)wDel(b.dataset.wdelok,b);else if(b.dataset.bdelok)bulkDel(b)});
gm.addEventListener('submit',e=>{e.preventDefault();if(e.target.id==='wsf')wSearch();else if(e.target.id==='wmf')wManual();else if(e.target.id==='ief')saveIdx(e.target)});
/* ------------------------------------------------- Watchlist : actions groupées */
const bulkBar=$('#bulk');
function syncBulk(){const on=tab==='watch'&&!!SEL,n=SEL?SEL.size:0;bulkBar.hidden=!on;document.body.classList.toggle('selmode',on);
  if(!on)return;$('#bkn').textContent=n?n+' sélectionné'+(n>1?'s':''):'Aucun élément sélectionné';
  bulkBar.querySelectorAll('[data-bk]').forEach(b=>{if(!['all','none','exit'].includes(b.dataset.bk))b.disabled=!n});$('#bkl').disabled=!n;
  document.body.style.setProperty('--bkh',bulkBar.offsetHeight+'px')}
function selToggle(id,on){if(!SEL)return;if(on===undefined)on=!SEL.has(id);on?SEL.add(id):SEL.delete(id);
  const c=app.querySelector(`.item[data-id="${id}"]`);if(c){c.classList.toggle('sel',on);const k=c.querySelector('[data-sel]');if(k)k.checked=on}syncBulk()}
async function bulk(action,extra){if(!SEL||!SEL.size){toast('Aucun élément sélectionné',true);return null}
  try{const r=await api('media/bulk','POST',Object.assign({ids:[...SEL],action},extra||{}));
    toast(r.message+(r.warning?' — '+r.warning:''),!r.ok);if(action==='delete')(r.done||[]).forEach(id=>SEL&&SEL.delete(id));await refresh();return r}
  catch(e){toast(e.message,true);return null}}
function openBulkDel(){const ms=SEL?[...SEL].map(id=>WMAP[id]).filter(Boolean):[];if(!ms.length){toast('Aucun élément sélectionné',true);return}
  const plex=ms.filter(m=>!String(m.plex_key||'').startsWith('local:')).length,dl=ms.reduce((n,m)=>n+(m.downloads||[]).filter(d=>d.grab_status==='sent').length,0);
  openGM('bdel','Supprimer de la Watchlist',`<p>Retirer <b>${ms.length} élément${ms.length>1?'s':''}</b> de synoarr ?</p>
  <ul class="bdl small">${ms.slice(0,8).map(m=>`<li>${esc(m.title)}${m.year?` <span class="mut">(${esc(m.year)})</span>`:''}</li>`).join('')}${ms.length>8?`<li class="mut">… et ${ms.length-8} autre(s)</li>`:''}</ul>
  ${plex?`<label class="chk"><input type="checkbox" id="wdp" checked> Retirer aussi de la Watchlist Plex${plex<ms.length?` (${plex} sur ${ms.length})`:''}</label>`:'<p class="mut small">Entrées locales (hors Watchlist Plex).</p>'}
  <label class="chk"><input type="checkbox" id="wdd"> Supprimer aussi les téléchargements en cours${dl?` (${dl})`:''}</label>
  <p class="mut small">Les fichiers déjà rangés dans la bibliothèque ne sont jamais supprimés. Un téléchargement interrompu peut perdre ses données partielles.</p>
  <div class="row"><button class="danger" data-bdelok="1">Supprimer ${ms.length} élément${ms.length>1?'s':''}</button><button class="sec" data-gmclose="1">Annuler</button></div>`);gm.querySelector('[data-bdelok]').focus()}
async function bulkDel(b){b.disabled=true;const r=await bulk('delete',{remove_from_plex:!!($('#wdp')&&$('#wdp').checked),delete_downloads:!!($('#wdd')&&$('#wdd').checked)});
  if(r)closeGM();else b.disabled=false}
bulkBar.addEventListener('click',e=>{const b=e.target.closest('button[data-bk]');if(!b||!SEL)return;const k=b.dataset.bk;
  if(k==='all'){WVIS.forEach(id=>SEL.add(id));refresh()}else if(k==='none'){SEL.clear();refresh()}else if(k==='exit'){SEL=null;refresh()}
  else if(k==='delete')openBulkDel();else bulk(k)});
bulkBar.addEventListener('change',e=>{if(e.target.id!=='bkl'||e.target.value==='-')return;const v=e.target.value;e.target.value='-';bulk('language',{languages:v})});
app.addEventListener('click',e=>{if(!SEL||tab!=='watch')return;const c=e.target.closest('.item[data-id]');
  if(!c||e.target.closest('button,a,input,select,label,summary,details,textarea,.dls'))return;selToggle(+c.dataset.id)});
app.addEventListener('toggle',e=>{const t=e.target;if(t.id==='cfadv')CFADV=t.open;else if(t.dataset&&t.dataset.sec&&!CFQ){t.open?CFOPEN.add(t.dataset.sec):CFOPEN.delete(t.dataset.sec)}},true);
app.addEventListener('error',e=>{const t=e.target;if(t&&t.tagName==='IMG'){BADIMG.add(t.getAttribute('src'));t.remove()}},true);
const showForm=()=>{$('#addc').hidden=tab!=='idx';msBox.hidden=!(tab==='dash'||!V[tab]&&!S[tab])};addEventListener('hashchange',showForm);showForm();
refresh();
"""

INDEX_HTML = """<!doctype html>
<html lang="fr"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>synoarr</title><meta name="color-scheme" content="light dark">
<link rel="icon" href="icon.svg" type="image/svg+xml"><link rel="manifest" href="manifest.webmanifest">
<link rel="apple-touch-icon" href="apple-touch-icon.png"><meta name="apple-mobile-web-app-capable" content="yes">
<meta name="mobile-web-app-capable" content="yes"><meta name="apple-mobile-web-app-title" content="synoarr">
<meta name="apple-mobile-web-app-status-bar-style" content="default">
<meta name="theme-color" media="(prefers-color-scheme: light)" content="#ffffff">
<meta name="theme-color" media="(prefers-color-scheme: dark)" content="#1b1f26">
<style>
:root{--bg:#f4f5f7;--card:#fff;--fg:#1d2330;--mut:#6b7280;--bd:#e3e6eb;--ac:#2563eb;--acf:#fff;--ok:#15803d;--okb:#dcfce7;
--ko:#b91c1c;--kob:#fee2e2;--wa:#a16207;--wab:#fef3c7;--inb:#e0e7ff;--inf:#3730a3;--neb:#eceff3;--pub:#f3e8ff;--puf:#7e22ce}
@media (prefers-color-scheme:dark){:root{--bg:#111418;--card:#1b1f26;--fg:#e6e8eb;--mut:#9aa3af;--bd:#2c323c;--ac:#3b82f6;
--ok:#4ade80;--okb:#14321f;--ko:#f87171;--kob:#3b1717;--wa:#facc15;--wab:#3a2f0b;--inb:#1e2550;--inf:#a5b4fc;--neb:#262b33;--pub:#2e1a47;--puf:#d8b4fe}}
*{box-sizing:border-box}[hidden]{display:none!important}body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.45 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
header{background:var(--card);border-bottom:1px solid var(--bd);position:sticky;top:0;z-index:2}
.hd{max-width:1000px;margin:auto;padding:10px 16px 0;display:flex;align-items:center;gap:12px;flex-wrap:wrap}
h1{font-size:18px;margin:0 8px 8px 0}h1 span{color:var(--ac)}#conn{color:var(--ko);font-size:13px;margin-bottom:8px}
nav{display:flex;gap:2px;overflow-x:auto;flex:1 1 auto;scrollbar-width:none;align-self:flex-end}nav::-webkit-scrollbar{display:none}
nav a{padding:8px 11px;color:var(--mut);text-decoration:none;border-bottom:2px solid transparent;white-space:nowrap}
nav a.on{color:var(--fg);border-color:var(--ac);font-weight:600}
main{max-width:1000px;margin:auto;padding:16px}
.card{background:var(--card);border:1px solid var(--bd);border-radius:10px;padding:14px 16px;margin-bottom:14px}
h3{margin:0 0 10px;font-size:15px}.mut{color:var(--mut)}.small{font-size:12px}code{font-size:13px}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:14px;margin-bottom:14px}.grid .card{margin:0}
.stat b{display:block;font-size:26px}.stat span{color:var(--mut);font-size:13px}
.row{display:flex;gap:8px;flex-wrap:wrap;align-items:center}.tools{margin-bottom:14px}
dl{display:grid;grid-template-columns:max-content 1fr;gap:6px 16px;margin:14px 0 0}dt{color:var(--mut)}dd{margin:0}
button,a.btn{font:inherit;font-size:14px;border:1px solid var(--ac);background:var(--ac);color:var(--acf);border-radius:7px;padding:6px 12px;cursor:pointer;text-decoration:none;display:inline-block}
button.sec,a.btn.sec{background:transparent;color:var(--ac)}button.danger{background:transparent;color:var(--ko);border-color:var(--ko)}
button.lnk{background:none;border:0;color:var(--ac);padding:0;text-align:left}
button:disabled{opacity:.45;cursor:default}button:hover:not(:disabled){filter:brightness(1.1)}
input,select,textarea{font:inherit;font-size:14px;padding:6px 10px;border:1px solid var(--bd);border-radius:7px;background:var(--card);color:var(--fg);min-width:0}
#wq,#sq,#lq{flex:1;min-width:160px}
.b{display:inline-block;font-size:12px;padding:1px 8px;border-radius:99px;background:var(--neb);color:var(--mut);white-space:nowrap;font-weight:400}
.b-done,.b-imported,.b-monitored,.b-planned{background:var(--okb);color:var(--ok)}.b-failed,.b-error{background:var(--kob);color:var(--ko)}
.b-grabbed,.b-sent,.b-running{background:var(--wab);color:var(--wa)}.b-wanted{background:var(--inb);color:var(--inf)}.b-anim{background:var(--pub);color:var(--puf)}
.item{padding:10px 0;border-top:1px solid var(--bd)}.card.item{padding:12px 16px;border-top:1px solid var(--bd)}
.card>.item:first-of-type{border-top:0}.off{opacity:.6}
.it{display:flex;justify-content:space-between;gap:10px;align-items:flex-start;flex-wrap:wrap}.it>div:first-child{flex:1;min-width:200px}
.meta{display:flex;gap:6px;flex-wrap:wrap;align-items:center;margin-top:4px;font-size:13px}
.url{word-break:break-all}.acts{display:flex;gap:6px;flex-wrap:wrap;align-items:center}.lsel{font-size:13px;padding:5px 6px}
.bar{height:5px;background:var(--neb);border-radius:9px;margin-top:8px;overflow:hidden;max-width:320px}.bar i{display:block;height:100%;background:var(--ok)}
.tres{font-size:13px;margin-top:4px}.tres.ok{color:var(--ok)}.tres.ko{color:var(--ko)}
details{margin-top:10px}summary{cursor:pointer;color:var(--mut)}
table{width:100%;border-collapse:collapse;font-size:13px}td,th{text-align:left;padding:6px;border-top:1px solid var(--bd);vertical-align:top}
th{color:var(--mut);font-weight:600;border-top:0}table.eps{table-layout:fixed}.eps td:first-child{width:44px}.eps td:nth-child(3){width:96px}.eps td:last-child{width:150px}.tw{overflow-x:auto}.nw{white-space:nowrap}.brk{word-break:break-word}
.ev{list-style:none;margin:0;padding:0;font-size:13px}.ev li{padding:5px 0;border-top:1px solid var(--bd)}.ev li:first-child{border:0}
.ev time{color:var(--mut)}.k{font-weight:600}.lv-ERROR,.lv-CRITICAL{color:var(--ko)}.lv-WARNING{color:var(--wa)}.lv-DEBUG{color:var(--mut)}
.err{border-color:var(--ko)}.err ul{margin:0;padding-left:18px;color:var(--ko);font-size:13px}
.warn{border-color:var(--wa);background:var(--wab)}.warn button{margin-left:6px}
.alert{border:2px solid var(--ko);background:var(--kob)}.alert h3{color:var(--ko)}.alert p{margin:6px 0}
form.add{display:grid;grid-template-columns:1fr 1fr;gap:8px}form.add .full{grid-column:1/-1}
.cmds{display:grid;grid-template-columns:repeat(auto-fill,minmax(270px,1fr));gap:10px}
.cfsh,.cfh{white-space:pre-line}.cfs>summary{font-size:15px;color:var(--fg);padding:2px 0}.cfs[open]>summary{margin-bottom:6px}.cfsh{margin:0 0 6px}
.cff{display:grid;grid-template-columns:minmax(220px,2fr) 3fr;gap:2px 16px;padding:9px 0;border-top:1px solid var(--bd);align-items:start}.cff .cfh{grid-column:1/-1}
.cfl{font-size:14px}.cfl code{font-size:12px;color:var(--mut)}.cfi{display:flex;flex-wrap:wrap;gap:4px 10px;align-items:center}.cfi>input[type=text],.cfi>input[type=number],.cfi>input[type=password],.cfi>select{flex:1;min-width:0;width:100%}
.cfi .o-err:empty{display:none}.cfi .o-err{flex-basis:100%}.cfbar #cfq{flex:1;min-width:200px}.cfsv{margin:10px 0}.cfb{color:var(--fg)}
.hper{gap:8px 12px}.hper input[type=date]{margin-left:4px}
.cmdcats{display:grid;grid-template-columns:repeat(auto-fit,minmax(300px,1fr));gap:14px;margin-bottom:10px}.cmdcats>.card{margin:0;min-width:0}.cmdl{display:flex;flex-direction:column;gap:8px}
.cmd{display:flex;flex-direction:column;align-items:flex-start;gap:5px;padding:10px 12px;border:1px solid var(--bd);border-radius:8px}.cmd>span{font-size:13px}
.chk{display:flex;gap:6px;align-items:center;font-size:13px;color:var(--mut)}
.fl{display:block;margin:8px 0;font-size:13px;color:var(--mut)}.fl input,.fl select{display:block;width:100%;box-sizing:border-box;margin-top:4px}
.con,.logv{font:12px/1.5 ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;background:var(--bg);border:1px solid var(--bd);border-radius:8px;padding:8px 10px;overflow:auto;margin:6px 0 0;white-space:pre-wrap;word-break:break-word}
.con{max-height:440px}.logv{height:calc(100vh - 250px);min-height:280px}.ll{white-space:pre-wrap;padding:1px 0}
.o-ok{color:var(--ok)}.o-err{color:var(--ko)}.o-warn{color:var(--wa)}.rej{opacity:.65}.dst{color:var(--ok)}.sm,.mo{display:none}
.plex{margin-top:12px;padding:12px 14px;border:1px dashed var(--ac);border-radius:8px;font-size:14px}.plex a{color:var(--ac)}
.code{font:700 34px/1 ui-monospace,Menlo,Consolas,monospace;letter-spacing:8px;display:block;margin:10px 0}
.jrow{display:flex;gap:8px;align-items:center;flex-wrap:wrap;padding:5px 0;border-top:1px solid var(--bd);font-size:14px}.jrow:first-child{border:0}
.cfgh{display:flex;justify-content:space-between;gap:10px;flex-wrap:wrap;align-items:flex-start;margin-bottom:10px}.cfgh h3{margin-bottom:2px}
#cft{display:block;width:100%;height:62vh;min-height:320px;font:13px/1.5 ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;white-space:pre;overflow:auto;tab-size:4;resize:vertical}
.msg{padding:8px 12px;border-radius:8px;margin:0 0 10px;font-size:14px}.msg ul{margin:4px 0 0;padding-left:20px}
.msg.ok{background:var(--okb);color:var(--ok)}.msg.ko{background:var(--kob);color:var(--ko)}.msg.wa{background:var(--wab);color:var(--wa)}
#toast{position:fixed;left:50%;bottom:20px;transform:translateX(-50%);background:var(--fg);color:var(--bg);padding:9px 16px;
border-radius:8px;opacity:0;pointer-events:none;transition:opacity .2s;max-width:90vw;font-size:14px;z-index:5}#toast.show{opacity:1}#toast.bad{background:var(--ko);color:#fff}
button.xs{font-size:12px;padding:3px 8px}h4{margin:14px 0 6px;font-size:14px}
.dls{margin-top:10px;display:flex;flex-direction:column;gap:8px}.dl{border:1px solid var(--bd);border-radius:8px;padding:8px 10px}.card>.dl+.dl,.card>h3+.dl,.card>h4+.dl{margin-top:8px}
.dlh{display:flex;gap:6px 8px;align-items:baseline;flex-wrap:wrap;font-size:14px}.dlh .brk{flex:1 1 220px;min-width:0}.pct{font-variant-numeric:tabular-nums}
.dlm{display:flex;gap:8px;align-items:center;justify-content:space-between;flex-wrap:wrap;margin-top:4px}.dlm>span{flex:1 1 240px;min-width:0}
.pbar{height:8px;background:var(--neb);border-radius:9px;overflow:hidden;margin-top:6px}.pbar i{display:block;height:100%;background:var(--ac);transition:width .4s}.pbar.ok i{background:var(--ok)}.pbar.ko i{background:var(--ko)}
.ds-downloading,.ds-finishing,.ds-extracting,.ds-hash_checking{background:var(--inb);color:var(--inf)}.ds-seeding,.ds-finished{background:var(--okb);color:var(--ok)}
.ds-error,.ds-failed,.ds-introuvable{background:var(--kob);color:var(--ko)}.ds-waiting,.ds-paused,.ds-filehosting_waiting{background:var(--wab);color:var(--wa)}
.spk{margin:8px 0 2px}.eps td:last-child .xs{margin-top:2px}table.eps td:last-child{width:210px}
.prio{display:flex;align-items:center;gap:4px;font-size:13px;color:var(--mut);margin-right:4px}.prio label{margin-right:2px}.pb{padding:3px 9px;line-height:1.2}.pin{width:58px;text-align:center;padding:4px 4px}
.modal{position:fixed;inset:0;background:rgba(0,0,0,.5);z-index:10;display:flex;align-items:flex-start;justify-content:center;padding:28px 16px;overflow-y:auto}
.mbox{background:var(--card);color:var(--fg);border:1px solid var(--bd);border-radius:12px;width:100%;max-width:920px;padding:14px 16px 18px;box-shadow:0 10px 40px rgba(0,0,0,.3)}
.mhd{display:flex;justify-content:space-between;align-items:center;gap:10px}.mhd h3{margin:0}.opts{margin:4px 0 6px;gap:6px 16px}
#slink{flex:1;min-width:200px}#sfile{flex:1;min-width:0;max-width:100%}.srt{max-height:46vh;overflow:auto;border:1px solid var(--bd);border-radius:8px}.wtabs{gap:6px;margin:8px 0;align-items:center}.sfil{gap:6px;align-items:center;margin:6px 0}.sfil input[type=search]{flex:1;min-width:160px}.sfil input[type=number]{width:64px}.mini{font-size:12px;color:var(--mut);white-space:nowrap}.sortb{white-space:nowrap;background:none;border:0;padding:0;color:inherit;font:inherit;font-weight:600;cursor:pointer}.sortb.on{color:var(--ac)}.srt table td,.srt table th{padding:5px 6px}
body.noscroll{overflow:hidden}#gm .mbox{max-width:760px}.b-local{background:var(--pub);color:var(--puf)}
.cands{display:flex;flex-direction:column;gap:8px;margin-top:10px}.cand{display:flex;gap:12px;border:1px solid var(--bd);border-radius:8px;padding:8px}
.cand img,.noimg{width:62px;height:93px;object-fit:cover;border-radius:6px;flex:0 0 62px;background:var(--neb)}.noimg{display:flex;align-items:center;justify-content:center;color:var(--mut)}
.cb{flex:1;min-width:0;display:flex;flex-direction:column;gap:4px}.ov{margin:0;color:var(--mut);display:-webkit-box;-webkit-line-clamp:3;-webkit-box-orient:vertical;overflow:hidden}
.yr{width:96px}#wsq,#prwu,#prwk{flex:1;min-width:180px}#gm details{margin-top:14px}#wmf{margin-top:8px}
.wr{display:flex;gap:12px;align-items:flex-start}.wr>.it{flex:1;min-width:0}
.pst{position:relative;flex:0 0 46px;width:46px;height:69px;border-radius:6px;overflow:hidden;background:var(--neb);display:flex;align-items:center;justify-content:center}
.pst::before{content:attr(data-l);color:var(--mut);font-weight:700;font-size:18px}.pst img{position:absolute;inset:0;width:100%;height:100%;object-fit:cover;background:var(--neb)}
.pst.sm{flex-basis:34px;width:34px;height:51px}.pst.sm::before{font-size:14px}
.wck{flex:0 0 auto;width:20px;height:20px;margin:24px 0 0;accent-color:var(--ac);cursor:pointer}
body.selmode #app .item{cursor:pointer}body.selmode #app .item .acts{display:none}.card.item.sel{border-color:var(--ac);box-shadow:0 0 0 1px var(--ac) inset}
#bulk{position:fixed;left:0;right:0;bottom:0;z-index:4;background:var(--card);border-top:1px solid var(--bd);box-shadow:0 -4px 18px rgba(0,0,0,.12);padding:10px 16px calc(10px + env(safe-area-inset-bottom,0px))}
.bkin{max-width:1000px;margin:auto;display:flex;flex-wrap:wrap;gap:8px 12px;align-items:center}.bkn{font-weight:600;font-size:14px;min-width:120px}
.bkg,.bka{display:flex;gap:6px;flex-wrap:wrap;align-items:center}.bka{flex:1}.bkx{margin-left:auto}#bkl{font-size:13px;padding:5px 6px}
body.selmode main{padding-bottom:calc(var(--bkh,64px) + 16px)}body.selmode #toast{bottom:calc(var(--bkh,64px) + 12px)}
.bdl{margin:4px 0 10px;padding-left:20px;max-height:180px;overflow:auto}
.grid2{display:grid;grid-template-columns:repeat(auto-fit,minmax(300px,1fr));gap:14px;margin-bottom:14px}.grid2>.card{margin:0;min-width:0}
.sto+.sto{margin-top:12px;padding-top:12px;border-top:1px solid var(--bd)}.stoh{display:flex;gap:4px 10px;flex-wrap:wrap;align-items:baseline}
.pbar.sb{height:12px;margin:6px 0 5px}.pbar.wa i{background:var(--wa)}.imp li{display:block}
.cday h4{margin:12px 0 4px;padding-bottom:4px;border-bottom:1px solid var(--bd)}.cday:first-of-type h4{margin-top:4px}.cday h4.tdy{color:var(--ac)}
.crow{display:flex;gap:10px;align-items:center;padding:6px 0}.crow+.crow{border-top:1px dashed var(--bd)}.crow .cb{flex:1;min-width:0;gap:2px}.crow .meta{margin-top:2px}.cact{flex:0 0 auto}
@media (max-width:600px){.cff{grid-template-columns:1fr}.pst{flex-basis:40px;width:40px;height:60px}.wck{margin-top:20px}.wr{gap:10px}.bkn{min-width:0;flex:1}.bka{order:3;flex-basis:100%}.bkx{margin-left:0}.crow{flex-wrap:wrap}.cact{margin-left:44px}table.eps td:last-child{width:auto}.modal{padding:8px}.mbox{padding:12px}.srt{max-height:52vh}.cand img,.noimg{width:48px;height:72px;flex-basis:48px}.prio label{display:none}.hm{display:none}.sm{display:block}.mo{display:inline}nav{width:100%;flex-wrap:wrap;row-gap:0}nav a{padding:6px 8px;font-size:14px}.hd{padding-top:8px}h1{margin-bottom:2px}.eps td:nth-child(3){display:none}.eps td:last-child{width:auto;text-align:right}form.add{grid-template-columns:1fr}main{padding:12px}.it>div:first-child{min-width:100%}.card{padding:12px}.logv{height:calc(100vh - 330px)}.code{font-size:28px}}
</style></head><body>
<header><div class="hd"><h1><span>syno</span>arr</h1>
<nav><a href="#dash"><span class="hm">Tableau de bord</span><span class="mo">Accueil</span></a><a href="#watch">Watchlist</a><a href="#cal">À venir</a><a href="#idx">Indexers</a><a href="#hist">Historique</a><a href="#cmd">Commandes</a><a href="#logs">Journal</a><a href="#cfg"><span class="hm">Configuration</span><span class="mo">Config</span></a></nav><div id="conn"></div></div></header>
<main><div class="card" id="msearch" hidden><h3>Recherche manuelle</h3><form id="sf" class="row" autocomplete="off"><input id="sq" type="search" placeholder="ex. Night of the Living Dead 1968 · Série S01E02" required maxlength="200" aria-label="Recherche manuelle sur les indexers"><button type="submit">Rechercher</button></form>
<p class="mut small">Rien n'est envoyé automatiquement : choisissez une release acceptable puis « Envoyer à Download Station ».</p><div id="sres"></div></div>
<div id="app"><p class="mut">Chargement…</p></div>
<div class="card" id="addc" hidden><h3>Ajouter un indexer par URL</h3>
<form class="add" id="addf" autocomplete="off"><input name="name" placeholder="Nom" required><input name="url" placeholder="URL (Torznab, RSS…)" required>
<input name="api_key" type="password" placeholder="Clé d'API (facultatif)" autocomplete="new-password"><input name="categories" placeholder="Catégories (ex. 2000,5000)">
<input name="priority" type="number" min="1" max="50" step="1" placeholder="Priorité 1-50 (défaut 25)" aria-label="Priorité (1 = le plus prioritaire)">
<div class="full"><button type="submit">Ajouter</button></div></form></div></main>
<div id="srcm" class="modal" hidden role="dialog" aria-modal="true" aria-labelledby="srch"><div class="mbox">
<div class="mhd"><h3 id="srch">Changer de source</h3><button type="button" class="sec xs" id="srcx" data-mclose="1">Fermer</button></div><div id="srcb"></div></div></div>
<div id="gm" class="modal" hidden role="dialog" aria-modal="true" aria-labelledby="gmh"><div class="mbox">
<div class="mhd"><h3 id="gmh">Watchlist</h3><button type="button" class="sec xs" data-gmclose="1">Fermer</button></div><div id="gmb"></div></div></div>
<div id="bulk" hidden role="region" aria-label="Actions sur la sélection"><div class="bkin">
<span id="bkn" class="bkn" aria-live="polite"></span><span class="bkg"><button type="button" class="xs sec" data-bk="all">Tout</button><button type="button" class="xs sec" data-bk="none">Aucun</button></span>
<span class="bka"><button type="button" data-bk="search">Rechercher</button>
<select id="bkl" aria-label="Langues des éléments sélectionnés"><option value="-">Langue…</option><option value="">Auto</option><option value="en">en</option><option value="fr">fr</option><option value="en,fr">en+fr</option></select>
<button type="button" class="sec" data-bk="ignore">Ignorer</button><button type="button" class="sec" data-bk="unignore">Reprendre</button>
<button type="button" class="danger" data-bk="delete">Supprimer</button></span><button type="button" class="xs sec bkx" data-bk="exit" aria-label="Quitter le mode Sélection">Fermer</button></div></div>
<div id="toast" role="status"></div>
<script>""" + INDEX_JS + """</script></body></html>
"""

CSP = ("default-src 'none'; script-src 'sha256-%s'; style-src 'unsafe-inline'; connect-src 'self'; img-src 'self' data: https:; manifest-src 'self'; "
       "base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
       % base64.b64encode(hashlib.sha256(INDEX_JS.encode("utf-8")).digest()).decode("ascii"))
