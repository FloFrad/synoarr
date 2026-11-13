"""Mini interface web (http.server) + API JSON de synoarr.

Une seule page HTML embarquée (vanilla JS, CSS inline, aucun CDN) et une API JSON sous ``/api``.

Onglets : Tableau de bord, Watchlist, Indexers, Historique, Commandes (toutes les commandes de la CLI, en
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

Les clés d'API ne sortent jamais de l'API indexers/catalogue : elles sont remplacées par "••••". Seul l'onglet
Configuration montre le vrai fichier (c'est la configuration de l'utilisateur, derrière l'authentification).
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import logging
import os
import re
import socket
import threading
import urllib.parse
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
        return out

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
        job, created = self.app.start_source_search(media_id, data.get("season"), data.get("episode"))
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
        row = self._app_func("add_to_watchlist")(cand, data.get("add_to_plex", True) is not False)
        return self._added(row, data.get("search_now", True) is not False)

    def api_watchlist_manual(self, query: Dict[str, str], body: Any) -> Dict[str, Any]:
        data = self._body_dict(body)
        for k in ("type", "title", "imdb", "tmdb"):
            if data.get(k) is not None and not isinstance(data.get(k), str):
                raise HttpProblem(400, "Champ « %s » invalide" % k)
        row = self._app_func("add_manual")(data.get("type") or "", data.get("title") or "", data.get("year"),
                                           (data.get("imdb") or "").strip() or None,
                                           (data.get("tmdb") or "").strip() or None)
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

    def api_grabs(self, query: Dict[str, str], body: Any) -> List[Dict[str, Any]]:
        return self.app.db.query(
            "SELECT g.*, m.title AS media_title, m.type AS media_type FROM grabs g "
            "LEFT JOIN media m ON m.id = g.media_id ORDER BY g.id DESC LIMIT ?", (self._limit(query, 200),))

    def api_events(self, query: Dict[str, str], body: Any) -> List[Dict[str, Any]]:
        return self.app.db.recent_events(self._limit(query, 100))

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
        return {"ok": True, "languages": value, "message": "Langues de « %s » : %s" % (media["title"], label)}

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
  ${fl.length?`<h4 class="mut">Échecs (toujours recherchés)</h4>${fl.map(d=>dlRow(d,true)).join('')}`:''}</div>`}
V.dash=async()=>{const [s,ev,dl]=await Promise.all([api('status'),api('events?limit=8'),api('downloads').catch(()=>null)]);const c=s.counts||{};
  const errs=(s.last_errors||[]).map(e=>`<li>${typeof e==='object'?esc(fdate(e.ts||e.at||''))+' — '+esc(e.message||e.error||JSON.stringify(e)):esc(e)}</li>`).join('');
  return `${rsBanner(s.restart_required)}<div class="grid">
  <div class="card stat"><b>${c.movies||0}</b><span>Films suivis</span></div><div class="card stat"><b>${c.shows||0}</b><span>Séries suivies</span></div>
  <div class="card stat"><b>${(c.movies_wanted||0)+(c.episodes_wanted||0)}</b><span>Recherchés</span></div>
  <div class="card stat"><b>${c.downloading||0}</b><span>En téléchargement</span></div></div>${dlBlock(dl)}
  <div class="card"><div class="row"><button data-run="watchlist">Synchroniser la Watchlist</button><button data-run="process">Traiter les téléchargements</button><a class="btn sec" href="#cmd">Autres commandes…</a></div>
  <dl><dt>Dernière synchro Watchlist</dt><dd>${esc(fdate(s.last_watchlist_run))}</dd><dt>Dernier traitement</dt><dd>${esc(fdate(s.last_process_run))}</dd>
  <dt>Plex</dt><dd>${yn(s.plex_configured)}</dd><dt>Download Station</dt><dd>${yn(s.ds_configured)}</dd>
  <dt>Indexers actifs</dt><dd>${c.indexers_enabled||0}</dd><dt>Mode simulation</dt><dd>${s.dry_run?'<span class="b b-grabbed">Activé</span>':'Non'}</dd>
  ${s.jobs_running&&s.jobs_running.length?`<dt>En cours</dt><dd>${esc(s.jobs_running.join(', '))}</dd>`:''}
  <dt>Démarré le</dt><dd>${esc(fdate(s.started_at))}</dd><dt>Version</dt><dd>${esc(s.version)}</dd></dl></div>
  ${errs?`<div class="card err"><h3>Dernières erreurs</h3><ul>${errs}</ul></div>`:''}
  <div class="card"><h3>Activité récente</h3>${evList(ev)}</div>`};
const evList=ev=>ev.length?`<ul class="ev">${ev.map(e=>`<li class="lv-${esc(e.level)}"><time>${esc(fdate(e.ts))}</time> <span class="k">${esc(e.kind)}</span> ${esc(e.message)}</li>`).join('')}</ul>`:'<p class="mut">Aucun événement.</p>';
const LANGS=[['','Auto'],['en','Anglais (en)'],['fr','Français (fr)'],['en,fr','Anglais + Français']];
const langSel=m=>{const v=m.language_override||'',o=LANGS.some(l=>l[0]===v)?LANGS:LANGS.concat([[v,v]]);
  return `<select class="lsel" data-lang="${m.id}" title="Langues recherchées" aria-label="Langues recherchées">${o.map(l=>`<option value="${esc(l[0])}"${l[0]===v?' selected':''}>${esc(l[1])}</option>`).join('')}</select>`};
V.watch=async()=>{const [ms,dl]=await Promise.all([api('media'),api('downloads').catch(()=>null)]);const f=$('#wf')?$('#wf').value:'',q=$('#wq')?$('#wq').value.toLowerCase():'';
  const items=ms.filter(m=>(!f||m.type===f)&&(!q||(m.title||'').toLowerCase().includes(q)));
  const eps=await Promise.all(items.filter(m=>exp.has(m.id)).map(async m=>[m.id,await api('media/'+m.id+'/episodes').catch(()=>[])]));
  const em=Object.fromEntries(eps);
  return `<div class="row tools"><select id="wf"><option value="">Tous</option><option value="movie"${f==='movie'?' selected':''}>Films</option><option value="show"${f==='show'?' selected':''}>Séries</option></select>
  <input id="wq" type="search" placeholder="Filtrer…" value="${esc(q)}"><button data-wadd="1">+ Ajouter</button></div>${dl&&dl.warning?`<div class="msg wa">${esc(dl.warning)}</div>`:''}
  ${items.length?items.map(m=>{const e=m.episodes,pct=e&&e.total?Math.round(100*e.done/e.total):0,ign=m.status==='ignored',dls=m.downloads||[];
  if(dls.some(d=>d.active))dlActive=true;
  return `<div class="card item${m.on_watchlist?'':' off'}"><div class="it"><div><b>${esc(m.title)}</b> ${m.year?`<span class="mut">(${esc(m.year)})</span>`:''}
  <div class="meta">${badge(m.type)} ${badge(m.status)} ${String(m.plex_key||'').startsWith('local:')?'<span class="b b-local" title="Entrée locale, hors Watchlist Plex">local</span>':''} ${m.is_animation?'<span class="b b-anim">Animation</span>':''} ${m.on_watchlist?'':'<span class="mut">retiré de la Watchlist</span>'}
  ${m.collection?`<span class="mut">Collection : ${esc(m.collection)}</span>`:''}
  ${e?`<span class="mut">${e.done}/${e.total} épisodes · ${e.wanted} recherchés${e.grabbed?' · '+e.grabbed+' en cours':''}</span>`:''}
  ${m.last_search_at?`<span class="mut">dernière recherche ${esc(fdate(m.last_search_at))}</span>`:''}
  <span class="mut">langues : ${esc((m.languages||[]).join(', ')||'—')} (${m.language_override?'manuel':'auto'})</span></div>
  ${e&&e.total?`<div class="bar"><i style="width:${pct}%"></i></div>`:''}</div>
  <div class="acts"><button data-search="${m.id}"${ign?' disabled':''}>Rechercher</button><button class="sec" data-st="${m.id}" data-v="${ign?'wanted':'ignored'}">${ign?'Reprendre':'Ignorer'}</button>
  ${m.type==='movie'&&m.status==='wanted'&&!dls.length?srcBtn(m.id,null,null,null,m.title,'','Choisir une source'):''}
  ${m.type==='show'?`<button class="sec" data-eps="${m.id}">${exp.has(m.id)?'Masquer':'Épisodes'}</button>`:''}${langSel(m)}<button class="danger" data-wdel="${m.id}" data-n="${esc(m.title)}" data-local="${String(m.plex_key||'').startsWith('local:')?1:0}" data-dl="${dls.filter(d=>d.grab_status==='sent').length}">Supprimer</button></div></div>
  ${dls.length?`<div class="dls">${dls.map(d=>dlRow(d,false)).join('')}</div>`:''}
  ${exp.has(m.id)?epTable(em[m.id]||[],m):''}</div>`}).join(''):'<p class="mut card">Watchlist vide : ajoutez des films ou séries à votre Watchlist Plex puis synchronisez.</p>'}`};
function epTable(eps,m){if(!eps.length)return '<p class="mut">Aucun épisode connu.</p>';const by={};eps.forEach(e=>(by[e.season]=by[e.season]||[]).push(e));
  return Object.keys(by).map(s=>`<details${Object.keys(by).length<3?' open':''}><summary>Saison ${esc(s)} — ${by[s].filter(e=>e.status==='done').length}/${by[s].length}</summary>
  ${by[s].filter(e=>e.status==='wanted').length>1?`<div class="row spk">${srcBtn(m.id,+s,null,null,m.title+' saison '+s,'','Choisir un pack pour la saison '+esc(s))}</div>`:''}
  <table class="eps">${by[s].map(e=>`<tr><td>E${pad(e.episode)}</td><td>${esc(e.title||'')}</td><td class="mut">${esc(e.air_date||'')}</td><td>${badge(e.status)}${e.status==='wanted'?' '+srcBtn(m.id,e.season,e.episode,null,m.title+` S${pad(e.season)}E${pad(e.episode)}`,'','Source…'):''}</td></tr>`).join('')}</table></details>`).join('')}
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
  <div class="acts">${prioCtl(i)}<button data-test="${i.id}">Tester</button><button class="sec" data-en="${i.id}" data-v="${i.enabled?0:1}">${i.enabled?'Désactiver':'Activer'}</button><button class="danger" data-del="${i.id}" data-n="${esc(i.name)}">Supprimer</button></div></div>`).join(''):'<p class="mut">Aucun indexer : ajoutez-en un depuis le catalogue ou par URL.</p>'}</div>
  <div class="card"><h3>Importer depuis Prowlarr</h3><p class="mut small">Ajoute chaque indexer torrent actif de Prowlarr comme indexer Torznab (avec sa priorité). La clé API se trouve dans Prowlarr > Settings > General > API Key. <b>Utilisez Prowlarr OU Jackett, pas les deux.</b></p>
  <form id="prwf" class="row" autocomplete="off"><input id="prwu" name="url" type="text" spellcheck="false" value="${esc(PRW.url)}" placeholder="http://127.0.0.1:9696" aria-label="Adresse de Prowlarr">
  <input id="prwk" name="api_key" type="password" autocomplete="new-password" placeholder="Clé API Prowlarr" aria-label="Clé API Prowlarr" value="${esc(PRW.key)}"><button type="submit">Importer</button></form><div id="prwr">${PRW.res}</div>${prwState(prw)}</div>
  <div class="card"><h3>Importer depuis Jackett</h3><p class="mut small">Même principe que Prowlarr : chaque indexer configuré dans Jackett devient un indexer Torznab, puis synoarr suit Jackett automatiquement. La clé API est en haut à droite de la page Jackett. <b>Utilisez Prowlarr OU Jackett, pas les deux</b> (sinon chaque site serait interrogé deux fois).</p>
  <form id="jktf" class="row" autocomplete="off"><input id="jktu" name="url" type="text" spellcheck="false" value="${esc(JKT.url)}" placeholder="http://127.0.0.1:9117" aria-label="Adresse de Jackett">
  <input id="jktk" name="api_key" type="password" autocomplete="new-password" placeholder="Clé API Jackett" aria-label="Clé API Jackett" value="${esc(JKT.key)}"><button type="submit">Importer</button></form><div id="jktr">${JKT.res}</div>${jktState(jkt)}</div>
  <div class="card"><h3>Catalogue (sources légales)</h3>${cat.map(c=>`<div class="item it"><div><b>${esc(c.name)}</b> ${badge(c.type)}<div class="meta mut">${esc(c.description||'')}</div></div>
  <div class="acts">${c.installed?'<span class="b b-done">Installé</span>':`<button data-cat="${esc(c.id)}">Ajouter</button>`}</div></div>`).join('')||'<p class="mut">Catalogue vide.</p>'}</div>`};
V.hist=async()=>{const [g,ev]=await Promise.all([api('grabs'),api('events?limit=100')]);
  return `<div class="card"><h3>Téléchargements envoyés</h3>${g.length?`<div class="tw"><table><tr><th>Date</th><th>Release</th><th class="hm">Média</th><th class="hm">Indexer</th><th class="hm">Taille</th><th>Statut</th></tr>
  ${g.map(x=>`<tr><td class="nw">${esc(fdate(x.created_at))}</td><td class="brk">${esc(x.release_title)}${x.message?`<div class="mut">${esc(x.message)}</div>`:''}</td><td class="hm">${esc(x.media_title||'')}${x.season!=null?` S${String(x.season).padStart(2,'0')}${x.episode!=null?'E'+String(x.episode).padStart(2,'0'):''}`:''}</td>
  <td class="hm">${esc(x.indexer||'')}</td><td class="hm nw">${size(x.size)}</td><td>${badge(x.status)}</td></tr>`).join('')}</table></div>`:'<p class="mut">Aucun téléchargement pour le moment.</p>'}</div>
  <div class="card"><h3>Événements</h3>${evList(ev)}</div>`};
/* ---------------------------------------------------------------- Commandes */
const CMDS=[['sync','Synchroniser la Watchlist','Importe votre Watchlist Plex (films et séries).'],
  ['search_all','Rechercher tout','Synchronise puis cherche sur les indexers tout ce qui manque.'],
  ['process','Traiter les téléchargements','Renomme et range les téléchargements terminés.'],
  ['dry_run','Simulation du traitement','Montre ce qui serait rangé, sans rien déplacer.'],
  ['check','Vérifier la configuration','Dossiers, Download Station, Plex, TMDb et indexers.'],
  ['test_indexers','Tester tous les indexers','Vérifie que chaque indexer répond.'],
  ['update_check','Vérifier les mises à jour','Compare votre version avec GitHub, sans rien modifier.'],
  ['update','Mettre à jour synoarr','Télécharge la dernière version, la vérifie, garde config.ini et data/, puis redémarre.'],
  ['update_rollback','Revenir à la version précédente','Restaure les fichiers sauvegardés lors de la dernière mise à jour, puis redémarre.']];
const PFX={ok:'[OK]  ',err:'[ERR] ',warn:'[!]   ',info:''};
let jobs={}, cur=null, srch=null, plexId=null;
const polling=new Set();
S.cmd=async()=>{app.innerHTML=`<div class="card"><h3>Commandes</h3><div class="cmds">
  ${CMDS.map(c=>`<div class="cmd"><button data-cmd="${c[0]}">${c[1]}</button><span class="mut">${c[2]}</span>${c[0]==='search_all'?'<label class="chk"><input type="checkbox" id="force"> forcer (ignorer le délai entre deux recherches)</label>':''}</div>`).join('')}
  <div class="cmd"><button data-plex="1">Connexion Plex</button><span class="mut">Relie synoarr à votre compte Plex (code à saisir sur plex.tv/link).</span></div>
  <div class="cmd"><button class="danger" data-restart="1">Redémarrer synoarr</button><span class="mut">Relance le programme, après la fin des tâches en cours.</span></div>
  </div><div id="plexbox"></div></div>
  <div class="card"><h3>Recherche manuelle</h3><form id="sf" class="row" autocomplete="off"><input id="sq" type="search" placeholder="ex. Night of the Living Dead 1968 · Série S01E02" required maxlength="200"><button type="submit">Rechercher</button></form>
  <p class="mut small">Rien n'est envoyé automatiquement : choisissez une release acceptable puis « Envoyer à Download Station ».</p><div id="sres"></div></div>
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
function draw(j){if(tab!=='cmd')return;if(j.id===cur&&$('#out'))drawOut(j);if(j.id===srch&&$('#sres'))drawSearch(j);if(j.id===plexId&&$('#plexbox'))drawPlex(j)}
const con=o=>`<pre class="con">${o.map(x=>`<span class="o-${esc(x.level)}">${esc(PFX[x.level]||'')}${esc(x.text)}</span>`).join('\n')}</pre>`;
function drawOut(j){$('#outh').innerHTML=`${esc(j.label)} ${badge(j.status)} <span class="mut small">${esc(fdate(j.started_at))}</span>`;
  const r=j.result||{};let h='';
  if(r.actions){h+=r.dry_run?'<p class="mut small">Simulation : rien n\'a été déplacé.</p>':'';
    h+=r.actions.length?`<div class="tw"><table class="acts-t"><tr><th>Statut</th><th>Fichier → destination</th></tr>${r.actions.map(a=>`<tr><td>${badge(a.status)}</td><td class="brk">${esc(a.src)}${a.dst?`<div class="dst">→ ${esc(a.dst)}</div>`:''}${a.message?`<div class="mut small">${esc(a.message)}</div>`:''}</td></tr>`).join('')}</table></div>`:'';
    h+=j.output.length?`<details${r.actions.length?'':' open'}><summary>Détail</summary>${con(j.output)}</details>`:''}
  else h+=j.output.length?con(j.output):'';
  if(j.status==='running')h+='<p class="mut">En cours…</p>';
  $('#out').innerHTML=h||'<p class="mut">Aucune sortie.</p>'}
function drawSearch(j){const r=j.result;let h=`<p class="small">${esc(j.label)} ${badge(j.status)}</p>`;
  if(j.status==='running')h+='<p class="mut">Recherche en cours sur les indexers…</p>';
  else if(j.status==='error')h+=con(j.output);
  else if(r){const ok=r.results.filter(x=>x.score!=null),ko=r.results.filter(x=>x.score==null);
    const row=x=>`<tr${x.score==null?' class="rej"':''}><td class="nw">${x.score==null?'rejeté':esc(x.score)}</td><td class="brk"><b>${esc(x.title)}</b><div class="mut small">${esc(x.reason)}</div>
      <div class="mut small sm">${size(x.size)} · ${x.seeders==null?'?':esc(x.seeders)} seeders · ${esc(x.indexer)}${pr(x)}</div></td><td class="hm nw">${size(x.size)}</td><td class="hm">${x.seeders==null?'?':esc(x.seeders)}</td><td class="hm">${esc(x.indexer)}${pr(x)}</td>
      <td>${x.score!=null?`<button data-grab="${x.index}" title="Envoyer à Download Station">Envoyer<span class="hm"> à Download Station</span></button>`:''}</td></tr>`;
    const tbl=a=>`<div class="tw"><table><tr><th>Score</th><th>Release</th><th class="hm">Taille</th><th class="hm">Seeders</th><th class="hm">Indexer</th><th></th></tr>${a.map(row).join('')}</table></div>`;
    h+=`<p class="mut small">Requête : ${esc(r.request)} (${esc(r.kind==='movie'?'film':r.kind==='season'?'saison':'épisode')}) — ${r.total} résultat(s), ${ok.length} acceptable(s)</p>`;
    h+=ok.length?tbl(ok):'<p class="mut">Aucun résultat acceptable.</p>';
    if(ko.length)h+=`<details><summary>Résultats rejetés (${ko.length})</summary>${tbl(ko)}</details>`}
  $('#sres').innerHTML=h}
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
S.cfg=async()=>{const d=await api('config');if(!dirty())cfgD={text:d.text,orig:d.text};else cfgD.orig=d.text;
  app.innerHTML=`${d.auth_enabled?'':`<div class="card alert"><h3>⚠ Interface web non protégée</h3><p>Aucun identifiant n'est défini dans la section <code>[web]</code> : toute personne connectée à votre réseau local peut ouvrir cette page, <b>lire la configuration avec tous les mots de passe</b> (Download Station, jeton Plex, clés d'API) et la modifier.</p>
  <p>Renseignez <code>username</code> et <code>password</code> dans la section <code>[web]</code> ci-dessous, enregistrez, puis redémarrez synoarr.</p></div>`}
  <div id="rsb">${rsBanner(d.restart_reasons)}</div>
  <div class="card"><div class="cfgh"><div><h3>config.ini <span id="cfdirty" class="b b-grabbed" hidden>modifié</span></h3><div class="mut small brk">${esc(d.path)}${d.encoding!=='utf-8'?` · encodage actuel ${esc(d.encoding)} : sera enregistré en UTF-8`:''}</div></div>
  <div class="acts"><button data-cfgsave="1">Enregistrer</button><button class="sec" data-cfgreset="1">Annuler les modifications</button><button class="sec" data-cfgrestore="1"${d.backups.length?'':' disabled'}>Restaurer la sauvegarde précédente</button></div></div>
  <div id="cfr"></div><textarea id="cft" spellcheck="false" autocapitalize="off" autocomplete="off" autocorrect="off" wrap="off" aria-label="Contenu de config.ini"></textarea>
  <p class="mut small">Le fichier est vérifié avant d'être remplacé ; en cas d'erreur rien n'est modifié. Les 5 versions précédentes sont conservées (config.ini.bak1 à .bak5)${d.backups.length?' — dernière sauvegarde : '+esc(d.backups[0].modified):''}. Ctrl+S pour enregistrer.</p></div>`;
  $('#cft').value=cfgD.text;markDirty()};
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
  const fa=document.activeElement;if(fa&&fa.dataset&&fa.dataset.prio&&app.contains(fa)){timer=setTimeout(refresh,15000);return}
  dlActive=false;
  try{const html=await (V[tab]||V.dash)();const ae=document.activeElement,fid=ae&&ae.id,pos=fid&&ae.selectionStart;
    app.innerHTML=html;if(fid&&$('#'+fid)){const el=$('#'+fid);el.focus();if(pos!=null&&el.setSelectionRange)el.setSelectionRange(pos,pos)}
    $('#conn').textContent='';}
  catch(e){$('#conn').textContent='⚠ '+e.message}
  timer=setTimeout(refresh,dlActive&&(tab==='watch'||tab==='dash')?5000:15000)}
addEventListener('hashchange',()=>{tab=(location.hash||'#dash').slice(1);mounted=null;app.innerHTML='<p class="mut">Chargement…</p>';refresh()});
addEventListener('beforeunload',e=>{if(dirty()){e.preventDefault();e.returnValue=''}});
document.addEventListener('keydown',e=>{if(tab==='cfg'&&(e.ctrlKey||e.metaKey)&&e.key==='s'){e.preventDefault();cfgSave()}});
app.addEventListener('input',e=>{const id=e.target.id;if(id==='wq'||id==='wf')refresh();
  else if(id==='prwu')PRW.url=e.target.value;else if(id==='prwk')PRW.key=e.target.value;
  else if(id==='lq'){LG.q=e.target.value;saveLG();clearTimeout(lqT);lqT=setTimeout(()=>loadLogs(true),300)}
  else if(id==='cft'&&cfgD){cfgD.text=e.target.value;markDirty()}});
app.addEventListener('change',e=>{const t=e.target,id=t.id;
  if(t.dataset.lang)act(()=>api('media/'+t.dataset.lang+'/language','POST',{languages:t.value}));
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
  else if(d.wdel)openDel(d);
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
  SRC={mid:+d.src,s:num(d.s),e:num(d.e),g:d.g?+d.g:null,label:d.t||'',rt:d.rt||'',job:null,res:[]};
  $('#srch').textContent=SRC.g?'Changer de source':'Choisir une source';
  $('#srcb').innerHTML=`<p class="small"><b>${esc(SRC.label)}</b>${SRC.rt?`<br><span class="mut brk">Source actuelle : ${esc(SRC.rt)}</span>`:''}</p>
  ${SRC.g?`<div class="row opts"><label class="chk"><input type="checkbox" id="sdel" checked> Supprimer l'ancien téléchargement</label><label class="chk"><input type="checkbox" id="sblk"> Bloquer l'ancienne release</label></div>`:''}
  <div id="smsg"></div>
  <h4>Résultats des indexers</h4><div id="sres2"><p class="mut">Recherche en cours sur les indexers…</p></div>
  <h4>Lien magnet ou URL d'un .torrent</h4><form id="slf" class="row" autocomplete="off"><input id="slink" type="text" spellcheck="false" placeholder="magnet:?xt=urn:btih:… ou https://…/fichier.torrent" aria-label="Lien magnet ou URL d'un fichier .torrent"><button type="submit">Envoyer le lien</button></form>
  <h4>Fichier .torrent</h4><form id="sff" class="row"><input id="sfile" type="file" accept=".torrent,application/x-bittorrent" aria-label="Fichier .torrent"><button type="submit">Envoyer le fichier</button></form>`;
  srcm.hidden=false;document.body.classList.add('noscroll');$('#srcx').focus();srcSearch()}
function closeSrc(){SRC=null;srcm.hidden=true;if(!GM)document.body.classList.remove('noscroll')}
async function srcSearch(){const S0=SRC;try{const r=await api('media/'+S0.mid+'/sources','POST',{season:S0.s,episode:S0.e});S0.job=r.job.id;let j=r.job,fails=0;
    while(SRC===S0&&j.status==='running'){await sleep(800);try{j=await api('jobs/'+j.id);fails=0}catch(e){if(e.status===404||++fails>20)throw e}}
    if(SRC===S0)drawSrcRes(j)}
  catch(e){if(SRC===S0)$('#sres2').innerHTML=`<div class="msg ko">${esc(e.message)}</div>`}}
function drawSrcRes(j){const r=j.result,el=$('#sres2');if(j.status==='error'||!r){el.innerHTML=`<div class="msg ko">${esc(j.error||'Recherche impossible')}</div>`;return}
  SRC.res=r.results;const langs=esc((r.languages||[]).join(', ')||'—');
  if(!r.results.length){el.innerHTML=`<p class="mut">Aucun résultat pour ${esc(r.request)} (langues : ${langs}). Collez un lien ou envoyez un fichier .torrent ci-dessous.</p>`;return}
  const ok=r.results.filter(x=>x.score!=null).length;
  el.innerHTML=`<p class="mut small">Requête : ${esc(r.request)} · langues : ${langs} · ${r.total} résultat(s), ${ok} acceptable(s). Les résultats rejetés peuvent aussi être choisis.</p><div class="tw srt"><table><tr><th>Score</th><th>Release</th><th class="hm">Taille</th><th class="hm">Seeders</th><th class="hm">Indexer</th><th></th></tr>
  ${r.results.map(x=>`<tr${x.score==null?' class="rej"':''}><td class="nw">${x.score==null?'rejeté':esc(x.score)}</td><td class="brk"><b>${esc(x.title)}</b><div class="mut small">${esc(x.reason)}</div><div class="mut small sm">${size(x.size)} · ${nv(x.seeders)} seeders · ${esc(x.indexer)}${pr(x)}</div></td>
  <td class="hm nw">${size(x.size)}</td><td class="hm">${nv(x.seeders)}</td><td class="hm">${esc(x.indexer)}${pr(x)}</td><td><button class="xs${x.score==null?' sec':''}" data-pick="${x.index}">${x.score==null?'Choisir<span class="hm"> quand même</span>':'Choisir'}</button></td></tr>`).join('')}</table></div>`}
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
  const b=e.target.closest('button[data-pick]');if(b)pick(+b.dataset.pick)});
srcm.addEventListener('submit',e=>{e.preventDefault();if(e.target.id==='slf'){const v=$('#slink').value.trim();if(!v){toast('Collez un lien magnet ou une URL .torrent',true);return}srcSend({link:v})}
  else if(e.target.id==='sff')srcFile()});
document.addEventListener('keydown',e=>{if(e.key!=='Escape')return;if(SRC)closeSrc();else if(GM)closeGM()});
/* ------------------------------------------- Watchlist : ajouter / supprimer */
let GM=null;const gm=$('#gm');
function openGM(kind,title,html){GM={kind,res:[]};$('#gmh').textContent=title;$('#gmb').innerHTML=html;gm.hidden=false;document.body.classList.add('noscroll')}
function closeGM(){GM=null;gm.hidden=true;if(!SRC)document.body.classList.remove('noscroll')}
function openAdd(){openGM('add','Ajouter à la Watchlist',`<form id="wsf" class="row" autocomplete="off"><input id="wsq" type="search" placeholder="Titre du film ou de la série" required maxlength="200" aria-label="Titre">
  <select id="wsk" aria-label="Type"><option value="">Tous</option><option value="movie">Film</option><option value="show">Série</option></select><input id="wsy" class="yr" type="number" min="1870" max="2100" placeholder="Année" aria-label="Année (facultatif)"><button type="submit">Rechercher</button></form>
  <div class="row opts"><label class="chk"><input type="checkbox" id="wsp" checked> Ajouter aussi à la Watchlist Plex</label><label class="chk"><input type="checkbox" id="wsn" checked> Rechercher un téléchargement tout de suite</label></div>
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
  try{const r=await api('watchlist/add','POST',{candidate:c,add_to_plex:$('#wsp').checked,search_now:$('#wsn').checked});b.textContent='Ajouté ✔';wDone(r)}
  catch(e){b.disabled=false;b.textContent='Ajouter';toast(e.message,true);const m=$('#wsm');if(m)m.innerHTML=`<div class="msg ko">✖ ${esc(e.message)}</div>`}}
async function wManual(){const b=$('#wmf button');b.disabled=true;
  try{const r=await api('watchlist/manual','POST',{type:$('#wmt').value,title:$('#wmn').value.trim(),year:$('#wmy').value||null,imdb:$('#wmi').value.trim(),search_now:$('#wsn').checked});$('#wmf').reset();wDone(r)}
  catch(e){toast(e.message,true);const m=$('#wsm');if(m)m.innerHTML=`<div class="msg ko">✖ ${esc(e.message)}</div>`}b.disabled=false}
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
  if(b.dataset.wpick)wAdd(+b.dataset.wpick,b);else if(b.dataset.wdelok)wDel(b.dataset.wdelok,b)});
gm.addEventListener('submit',e=>{e.preventDefault();if(e.target.id==='wsf')wSearch();else if(e.target.id==='wmf')wManual()});
const showForm=()=>{$('#addc').hidden=tab!=='idx'};addEventListener('hashchange',showForm);showForm();
refresh();
"""

INDEX_HTML = """<!doctype html>
<html lang="fr"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>synoarr</title><link rel="icon" href="data:,"><meta name="color-scheme" content="light dark">
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
.cmd{display:flex;flex-direction:column;align-items:flex-start;gap:5px;padding:10px 12px;border:1px solid var(--bd);border-radius:8px}.cmd>span{font-size:13px}
.chk{display:flex;gap:6px;align-items:center;font-size:13px;color:var(--mut)}
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
#slink{flex:1;min-width:200px}#sfile{flex:1;min-width:0;max-width:100%}.srt{max-height:46vh;overflow:auto;border:1px solid var(--bd);border-radius:8px}.srt table td,.srt table th{padding:5px 6px}
body.noscroll{overflow:hidden}#gm .mbox{max-width:760px}.b-local{background:var(--pub);color:var(--puf)}
.cands{display:flex;flex-direction:column;gap:8px;margin-top:10px}.cand{display:flex;gap:12px;border:1px solid var(--bd);border-radius:8px;padding:8px}
.cand img,.noimg{width:62px;height:93px;object-fit:cover;border-radius:6px;flex:0 0 62px;background:var(--neb)}.noimg{display:flex;align-items:center;justify-content:center;color:var(--mut)}
.cb{flex:1;min-width:0;display:flex;flex-direction:column;gap:4px}.ov{margin:0;color:var(--mut);display:-webkit-box;-webkit-line-clamp:3;-webkit-box-orient:vertical;overflow:hidden}
.yr{width:96px}#wsq,#prwu,#prwk{flex:1;min-width:180px}#gm details{margin-top:14px}#wmf{margin-top:8px}
@media (max-width:600px){table.eps td:last-child{width:auto}.modal{padding:8px}.mbox{padding:12px}.srt{max-height:52vh}.cand img,.noimg{width:48px;height:72px;flex-basis:48px}.prio label{display:none}.hm{display:none}.sm{display:block}.mo{display:inline}nav{width:100%;flex-wrap:wrap;row-gap:0}nav a{padding:6px 8px;font-size:14px}.hd{padding-top:8px}h1{margin-bottom:2px}.eps td:nth-child(3){display:none}.eps td:last-child{width:auto;text-align:right}form.add{grid-template-columns:1fr}main{padding:12px}.it>div:first-child{min-width:100%}.card{padding:12px}.logv{height:calc(100vh - 330px)}.code{font-size:28px}}
</style></head><body>
<header><div class="hd"><h1><span>syno</span>arr</h1>
<nav><a href="#dash"><span class="hm">Tableau de bord</span><span class="mo">Accueil</span></a><a href="#watch">Watchlist</a><a href="#idx">Indexers</a><a href="#hist">Historique</a><a href="#cmd">Commandes</a><a href="#logs">Journal</a><a href="#cfg"><span class="hm">Configuration</span><span class="mo">Config</span></a></nav><div id="conn"></div></div></header>
<main><div id="app"><p class="mut">Chargement…</p></div>
<div class="card" id="addc" hidden><h3>Ajouter un indexer par URL</h3>
<form class="add" id="addf" autocomplete="off"><input name="name" placeholder="Nom" required><input name="url" placeholder="URL (Torznab, RSS…)" required>
<input name="api_key" type="password" placeholder="Clé d'API (facultatif)" autocomplete="new-password"><input name="categories" placeholder="Catégories (ex. 2000,5000)">
<input name="priority" type="number" min="1" max="50" step="1" placeholder="Priorité 1-50 (défaut 25)" aria-label="Priorité (1 = le plus prioritaire)">
<div class="full"><button type="submit">Ajouter</button></div></form></div></main>
<div id="srcm" class="modal" hidden role="dialog" aria-modal="true" aria-labelledby="srch"><div class="mbox">
<div class="mhd"><h3 id="srch">Changer de source</h3><button type="button" class="sec xs" id="srcx" data-mclose="1">Fermer</button></div><div id="srcb"></div></div></div>
<div id="gm" class="modal" hidden role="dialog" aria-modal="true" aria-labelledby="gmh"><div class="mbox">
<div class="mhd"><h3 id="gmh">Watchlist</h3><button type="button" class="sec xs" data-gmclose="1">Fermer</button></div><div id="gmb"></div></div></div>
<div id="toast" role="status"></div>
<script>""" + INDEX_JS + """</script></body></html>
"""

CSP = ("default-src 'none'; script-src 'sha256-%s'; style-src 'unsafe-inline'; connect-src 'self'; img-src 'self' data: https:; "
       "base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
       % base64.b64encode(hashlib.sha256(INDEX_JS.encode("utf-8")).digest()).decode("ascii"))
