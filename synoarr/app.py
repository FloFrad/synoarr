"""Application wiring + daemon loop."""
from __future__ import annotations

import contextlib
import datetime as _dt
import logging
import logging.handlers
import os
import shutil
import signal
import stat
import sys
import tempfile
import threading
import time
import uuid
from collections import OrderedDict
from typing import Any, Dict, Iterator, List, Optional, Tuple

from . import __version__
from .config import Config, ConfigError, load_config
from .db import Database, now_iso
from .httpclient import HttpClient, HttpError
from .indexers import CATALOG, create_indexer, detect_type

log = logging.getLogger(__name__)


def setup_logging(cfg: Config, console: bool = True) -> None:
    level = getattr(logging, (cfg.general.log_level or "INFO").upper(), logging.INFO)
    root = logging.getLogger()
    root.setLevel(level)
    for h in list(root.handlers):
        root.removeHandler(h)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s", "%Y-%m-%d %H:%M:%S")
    if console:
        sh = logging.StreamHandler()
        sh.setFormatter(fmt)
        root.addHandler(sh)
    try:
        os.makedirs(cfg.data_dir, exist_ok=True)
        fh = logging.handlers.RotatingFileHandler(os.path.join(cfg.data_dir, "synoarr.log"),
                                                  maxBytes=2 * 1024 * 1024, backupCount=3, encoding="utf-8")
        fh.setFormatter(fmt)
        root.addHandler(fh)
    except OSError as exc:  # pragma: no cover - read-only FS
        root.warning("Impossible d'écrire le journal dans %s : %s", cfg.data_dir, exc)


def is_local_url(url: str) -> bool:
    """Loopback / LAN address (DSM seen from the NAS): such requests must bypass any HTTP proxy."""
    import ipaddress
    import urllib.parse
    try:
        host = (urllib.parse.urlsplit(url if "://" in url else "http://" + url).hostname or "").lower()
    except ValueError:
        return False
    if not host or host == "localhost" or host.endswith((".local", ".lan", ".home", ".localdomain")) \
            or "." not in host:
        return True
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    return ip.is_private or ip.is_loopback or ip.is_link_local


class IndexerManager:
    """CRUD on indexers (used by the CLI and the web UI)."""

    def __init__(self, db: Database, http: HttpClient):
        self.db = db
        self.http = http

    def list(self) -> List[Dict[str, Any]]:
        return self.db.list_indexers()

    def catalog(self) -> List[Dict[str, Any]]:
        installed = {(r["url"].rstrip("/").lower()) for r in self.db.list_indexers()}
        out = []
        for entry in CATALOG:
            e = dict(entry)
            e["installed"] = entry["url"].rstrip("/").lower() in installed
            out.append(e)
        return out

    def add_from_catalog(self, catalog_id: str) -> Dict[str, Any]:
        entry = next((e for e in CATALOG if e["id"] == catalog_id), None)
        if entry is None:
            raise ValueError("Indexer inconnu dans le catalogue : %s (voir « indexer catalog »)" % catalog_id)
        if self.db.get_indexer(entry["name"]):
            raise ValueError("L'indexer « %s » est déjà installé" % entry["name"])
        cats = entry.get("categories") or ""
        if isinstance(cats, (list, tuple)):
            cats = ",".join(str(c) for c in cats)
        new_id = self.db.add_indexer(entry["name"], entry["type"], entry["url"], "", cats)
        self.db.add_event("indexer", "Indexer ajouté : %s" % entry["name"])
        return self.db.get_indexer(new_id) or {}

    def add_url(self, name: str, url: str, api_key: str = "", categories: str = "",
                type_: Optional[str] = None) -> Dict[str, Any]:
        name = (name or "").strip()
        url = (url or "").strip()
        if not name:
            raise ValueError("Le nom de l'indexer est obligatoire")
        if not url.lower().startswith(("http://", "https://")):
            raise ValueError("L'URL doit commencer par http:// ou https://")
        if self.db.get_indexer(name):
            raise ValueError("Un indexer nommé « %s » existe déjà" % name)
        if not type_:
            type_ = detect_type(url, api_key, self.http)
        new_id = self.db.add_indexer(name, type_, url, api_key or "", categories or "")
        self.db.add_event("indexer", "Indexer ajouté : %s (%s)" % (name, type_))
        return self.db.get_indexer(new_id) or {}

    def _row(self, indexer_id: Any) -> Dict[str, Any]:
        row = self.db.get_indexer(indexer_id)
        if row is None:
            raise ValueError("Indexer introuvable : %s" % indexer_id)
        return row

    def test(self, indexer_id: Any) -> Tuple[bool, str]:
        row = self._row(indexer_id)
        try:
            return create_indexer(row, self.http).test()
        except Exception as exc:
            return False, str(exc)

    def set_enabled(self, indexer_id: Any, enabled: bool) -> None:
        row = self._row(indexer_id)
        self.db.update_indexer(row["id"], enabled=1 if enabled else 0)

    def delete(self, indexer_id: Any) -> None:
        row = self._row(indexer_id)
        self.db.delete_indexer(row["id"])
        self.db.add_event("indexer", "Indexer supprimé : %s" % row["name"])

    def active(self) -> List[Any]:
        out = []
        for row in self.db.list_indexers(enabled_only=True):
            try:
                out.append(create_indexer(row, self.http))
            except Exception as exc:
                log.warning("Indexer %s ignoré : %s", row.get("name"), exc)
        return out


class BusyError(Exception):
    """Une tâche (recherche, traitement…) est en cours : action refusée pour l'instant (HTTP 409)."""


class ConfigInvalid(Exception):
    """config.ini proposé invalide : *problems* = messages français. Rien n'a été modifié."""

    def __init__(self, problems: List[str]):
        Exception.__init__(self, "; ".join(problems))
        self.problems = list(problems)


class Job(object):
    """Commande lancée depuis l'interface web, exécutée dans un thread (voir App.start_command)."""

    MAX_OUTPUT = 3000

    def __init__(self, job_id: int, command: str, label: str, key: str):
        self.id = job_id
        self.command = command
        self.label = label
        self.key = key
        self.status = "running"  # running | done | error
        self.started_at = now_iso()
        self.finished_at = None  # type: Optional[str]
        self.output = []  # type: List[Tuple[str, str]]  # (niveau ok|err|warn|info, texte)
        self.result = None  # type: Any
        self.error = None  # type: Optional[str]
        self.private = {}  # type: Dict[str, Any]  # jamais envoyé au navigateur (releases avec liens privés)
        self._lock = threading.Lock()

    def emit(self, level: str, message: str) -> None:
        with self._lock:
            if len(self.output) < self.MAX_OUTPUT:
                self.output.append((level, message))
            elif len(self.output) == self.MAX_OUTPUT:
                self.output.append(("warn", "… sortie tronquée"))

    @property
    def running(self) -> bool:
        return self.status == "running"

    def to_dict(self) -> Dict[str, Any]:
        with self._lock:
            output = [{"level": lv, "text": tx} for lv, tx in self.output]
        return {"id": self.id, "command": self.command, "label": self.label, "status": self.status,
                "started_at": self.started_at, "finished_at": self.finished_at, "output": output,
                "result": self.result, "error": self.error}


class _JobLogHandler(logging.Handler):
    """Recopie dans la sortie d'une tâche les lignes du journal émises par son thread."""

    _LEVELS = {"CRITICAL": "err", "ERROR": "err", "WARNING": "warn", "INFO": "info"}

    def __init__(self, job: Job, thread_id: int):
        logging.Handler.__init__(self, logging.INFO)
        self.job = job
        self.thread_id = thread_id

    def emit(self, record: logging.LogRecord) -> None:
        if record.thread != self.thread_id:
            return
        try:
            self.job.emit(self._LEVELS.get(record.levelname, "info"), record.getMessage())
        except Exception:  # pragma: no cover - never break logging
            pass


COMMANDS = {
    "sync": "Synchroniser la Watchlist",
    "search_all": "Rechercher tout",
    "process": "Traiter les téléchargements",
    "dry_run": "Simulation du traitement",
    "check": "Vérifier la configuration",
    "test_indexers": "Tester tous les indexers",
    "search": "Recherche manuelle",
    "plex_login": "Connexion Plex",
}
MAX_JOBS = 20
CONFIG_BACKUPS = 5
PLEX_LINK_URL = "https://plex.tv/link"


def request_from_query(query: str) -> Any:
    """Requête libre (« Night of the Living Dead 1968 », « Show S01E02 ») -> SearchRequest."""
    from .models import SearchRequest
    from .parser import parse
    p = parse(query)
    if p.episodes:
        return SearchRequest("episode", p.title, season=p.season, episode=p.episode)
    if p.seasons:
        return SearchRequest("season", p.title, season=p.season)
    return SearchRequest("movie", p.title, year=p.year)


def restart_argv() -> List[str]:
    """Ligne de commande pour relancer synoarr à l'identique (os.execv)."""
    exe = sys.executable or "python3"
    argv = list(sys.argv) or ["synoarr.py", "run"]
    if os.path.basename(argv[0]) == "__main__.py":  # lancé par « python3 -m synoarr »
        return [exe, "-m", "synoarr"] + argv[1:]
    return [exe] + argv


def decode_config_bytes(data: bytes) -> Tuple[str, str]:
    """(texte, encodage détecté) : UTF-8 (avec ou sans BOM), UTF-16 ou Windows-1252, comme load_config."""
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        text, enc = data.decode("utf-16"), "utf-16"
    else:
        try:
            text, enc = data.decode("utf-8-sig"), "utf-8"
        except UnicodeDecodeError:
            text, enc = data.decode("cp1252", errors="replace"), "cp1252"
    return text.replace("\r\n", "\n").replace("\r", "\n"), enc


def _web_hint(command: str) -> str:
    """Équivalent, dans l'interface web, d'une commande citée par la vérification."""
    return {
        "plex-login": "« Connexion Plex » (onglet Commandes)",
        "indexer add internet_archive": "onglet Indexers, section Catalogue",
        "run": "« Rechercher tout » (onglet Commandes)",
    }.get(command, command)


class App:
    plex_urls = {}  # type: Dict[str, str]  # tests : discover_url / metadata_url / plextv_url de PlexClient
    pin_poll_interval = 3.0
    pin_timeout = 300.0

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.started_at = now_iso()
        os.makedirs(cfg.data_dir, exist_ok=True)
        self.db = Database(os.path.join(cfg.data_dir, "synoarr.db"))
        self.http = HttpClient(timeout=30)
        self.indexer_manager = IndexerManager(self.db, self.http)

        client_id = self.db.get_kv("plex_client_id")
        if not client_id:
            client_id = "synoarr-" + uuid.uuid4().hex
            self.db.set_kv("plex_client_id", client_id)
        self.plex_client_id = client_id
        # One lock per job: a long search (many indexers, slow timeouts) must never delay imports.
        # Searcher and organizer only share the database (thread-safe) and Download Station (locked).
        self._watch_lock = threading.Lock()
        self._process_lock = threading.Lock()
        self._watch_thread = None  # type: Optional[threading.Thread]
        self._stop = threading.Event()
        self._build_components()
        self._last: Dict[str, Optional[str]] = {"watchlist": None, "process": None}
        self._errors: List[str] = []
        self.web: Any = None
        # web jobs (Commandes tab)
        self._jobs = OrderedDict()  # type: OrderedDict[int, Job]
        self._jobs_lock = threading.Lock()
        self._job_seq = 0
        # settings that only a restart applies (web server, data folder)
        self._boot = self._restart_settings(cfg)
        self._restarting = False

    def _new_plex_client(self, token: str) -> Any:
        from .plex import PlexClient
        client = PlexClient(token, self.plex_client_id, self.http)
        for attr, url in (self.plex_urls or {}).items():
            setattr(client, attr, url)
        return client

    def _build_components(self) -> None:
        """(Re)crée les clients et services à partir de self.cfg (démarrage et rechargement de config.ini).
        La base de données, le client HTTP Internet et l'interface web sont conservés."""
        from .downloadstation import DownloadStation
        from .organizer import Organizer
        from .plex import refresh_library
        from .searcher import Searcher
        from .tmdb import TmdbClient

        cfg = self.cfg
        self.ds_http = HttpClient(timeout=60, verify_ssl=cfg.downloadstation.verify_ssl,
                                  use_proxy=not is_local_url(cfg.downloadstation.url))
        token = self.plex_token
        self.plex = self._new_plex_client(token) if token else None
        self.tmdb = TmdbClient(cfg.tmdb.api_key, cfg.tmdb.language, self.http)
        dsc = cfg.downloadstation
        self.ds = (DownloadStation(dsc.url, dsc.username, dsc.password, dsc.destination, self.ds_http)
                   if dsc.username else None)

        def _refresh() -> None:
            if cfg.plex.server_url:
                tok = cfg.plex.server_token or self.plex_token
                try:
                    refresh_library(cfg.plex.server_url, tok, self.http)
                except Exception as exc:
                    log.warning("Rafraîchissement de la bibliothèque Plex impossible : %s", exc)

        self.organizer = Organizer(cfg, self.db, self.ds, self.tmdb if self.tmdb.enabled else None,
                                   on_imported=_refresh)
        self.searcher = Searcher(cfg, self.db, self.http, self.plex, self.ds,
                                 self.tmdb if self.tmdb.enabled else None, self.indexer_manager.active)
        self.searcher.stop_event = self._stop

    # ------------------------------------------------------------- settings
    @property
    def plex_token(self) -> str:
        return self.cfg.plex.token or self.db.get_kv("plex_token") or ""

    def save_plex_token(self, token: str) -> None:
        self.db.set_kv("plex_token", token)
        self.plex = self._new_plex_client(token)
        self.searcher.plex = self.plex

    # ----------------------------------------------------------------- jobs
    def _record_error(self, what: str, exc: BaseException) -> None:
        msg = "%s %s : %s" % (now_iso(), what, exc)
        self._errors = (self._errors + [msg])[-20:]
        try:
            self.db.add_event(what, "Erreur : %s" % exc, "ERROR")
        except Exception:  # pragma: no cover
            pass

    def job_watchlist(self, media_id: Optional[int] = None, force: bool = False) -> None:
        """Sync the Plex Watchlist then search everything that is wanted."""
        with self._watch_lock:
            if self._stop.is_set():
                return
            try:
                if media_id is None:
                    try:
                        self.searcher.sync_watchlist()
                    except Exception as exc:  # Plex down: still search what we already know
                        log.warning("Lecture de la Watchlist Plex impossible : %s", exc)
                        self._record_error("watchlist", exc)
                self.searcher.run_once(media_id=media_id, force=force)
            except Exception as exc:
                log.exception("Cycle Watchlist/recherche en erreur : %s", exc)
                self._record_error("watchlist", exc)
            finally:
                self._last["watchlist"] = now_iso()

    def job_process(self, dry_run: Optional[bool] = None) -> List[Dict[str, Any]]:
        with self._process_lock:
            try:
                return self.organizer.run_once(dry_run=self.cfg.general.dry_run if dry_run is None else dry_run)
            except Exception as exc:
                log.exception("Traitement des téléchargements en erreur : %s", exc)
                self._record_error("process", exc)
                return []
            finally:
                self._last["process"] = now_iso()

    def _background(self, target: Any, *args: Any) -> threading.Thread:
        t = threading.Thread(target=target, args=args, daemon=True, name="synoarr-job")
        t.start()
        return t

    # web UI hooks
    def run_watchlist_now(self) -> bool:
        """Start a sync + search in the background. False if one is already running (no queue of clicks)."""
        if self._watch_lock.locked():
            return False
        self._background(self.job_watchlist)
        return True

    def run_process_now(self) -> bool:
        if self._process_lock.locked():
            return False
        self._background(self.job_process)
        return True

    def search_now(self, media_id: int) -> None:
        self._background(self.job_watchlist, int(media_id), True)

    def _start_watchlist_thread(self) -> None:
        """Daemon loop: the sync/search cycle runs in its own thread so imports keep going meanwhile."""
        if self._watch_thread is not None and self._watch_thread.is_alive():
            return
        self._watch_thread = threading.Thread(target=self.job_watchlist, daemon=True, name="synoarr-watchlist")
        self._watch_thread.start()

    def set_media_status(self, media_id: int, status: str) -> None:
        if status not in ("wanted", "ignored", "monitored", "done"):
            raise ValueError("Statut invalide : %s" % status)
        media = self.db.get_media(int(media_id))
        if media is None:
            raise ValueError("Élément introuvable : %s" % media_id)
        if status == "wanted" and media["type"] == "show":
            status = "monitored"
        if status == "monitored" and media["type"] == "movie":
            status = "wanted"
        self.db.set_media_status(int(media_id), status)

    def status(self) -> Dict[str, Any]:
        return {
            "version": __version__,
            "started_at": self.started_at,
            "last_watchlist_run": self._last["watchlist"],
            "last_process_run": self._last["process"],
            "last_errors": list(self._errors),
            "dry_run": self.cfg.general.dry_run,
            "plex_configured": bool(self.plex_token),
            "ds_configured": self.ds is not None,
            "tmdb_configured": bool(self.tmdb.enabled),
            "indexers": len(self.db.list_indexers(enabled_only=True)),
            "restart_required": self.restart_reasons(),
            "jobs_running": [j.label for j in self._busy_jobs()],
        }

    # ------------------------------------------------------------ web jobs
    def start_command(self, command: str, force: bool = False, query: str = "") -> Tuple[Job, bool]:
        """Lance une commande de l'onglet Commandes dans un thread. Retourne (tâche, créée) : si la même
        commande tourne déjà, la tâche existante est renvoyée avec créée=False. Lève ValueError."""
        query = (query or "").strip()
        if command == "plex_login":
            return self.start_plex_login()
        if command not in COMMANDS:
            raise ValueError("Commande inconnue : %s" % command)
        label, key = COMMANDS[command], command
        capture = True
        if command == "sync":
            target, args = self._cmd_sync, ()  # type: Any, Tuple[Any, ...]
        elif command == "search_all":
            target, args = self._cmd_search_all, (bool(force),)
            label += " (forcé)" if force else ""
        elif command == "process":
            target, args = self._cmd_process, (False,)
        elif command == "dry_run":
            target, args = self._cmd_process, (True,)
        elif command == "check":
            target, args, capture = self._cmd_check, (), False
        elif command == "test_indexers":
            target, args = self._cmd_test_indexers, ()
        else:  # search
            if not query:
                raise ValueError("Saisissez un titre à rechercher (ex. « Night of the Living Dead 1968 »)")
            if len(query) > 200:
                raise ValueError("Recherche trop longue (200 caractères maximum)")
            target, args = self._cmd_search_query, (query,)
            label, key = "Recherche : %s" % query, "search:" + query.lower()
        return self._start_job(command, label, key, target, args, capture)

    def _start_job(self, command: str, label: str, key: str, target: Any, args: Tuple[Any, ...] = (),
                   capture_logs: bool = True, prepare: Any = None) -> Tuple[Job, bool]:
        with self._jobs_lock:
            for job in self._jobs.values():
                if job.key == key and job.running:
                    return job, False
            self._job_seq += 1
            job = Job(self._job_seq, command, label, key)
            if prepare is not None:
                prepare(job)
            self._jobs[job.id] = job
            finished = [j for j in self._jobs.values() if not j.running]
            while len(self._jobs) > MAX_JOBS and finished:
                self._jobs.pop(finished.pop(0).id, None)
        t = threading.Thread(target=self._run_job, args=(job, target, args, capture_logs), daemon=True,
                             name="synoarr-job-%d" % job.id)
        t.start()
        return job, True

    def _run_job(self, job: Job, target: Any, args: Tuple[Any, ...], capture_logs: bool) -> None:
        handler = None
        if capture_logs:
            handler = _JobLogHandler(job, threading.get_ident())
            logging.getLogger().addHandler(handler)
        try:
            result = target(job, *args)
            if result is not None:
                job.result = result
            if job.status == "running":
                job.status = "done"
        except Exception as exc:
            if not isinstance(exc, (ValueError, BusyError, HttpError)):
                log.exception("Tâche « %s » en erreur : %s", job.label, exc)
            job.error = str(exc) or type(exc).__name__
            job.emit("err", job.error)
            job.status = "error"
        finally:
            if handler is not None:
                logging.getLogger().removeHandler(handler)
            job.finished_at = now_iso()

    def get_job(self, job_id: int) -> Optional[Job]:
        with self._jobs_lock:
            return self._jobs.get(int(job_id))

    def list_jobs(self) -> List[Job]:
        with self._jobs_lock:
            return list(reversed(list(self._jobs.values())))

    def _busy_jobs(self) -> List[Job]:
        with self._jobs_lock:
            return [j for j in self._jobs.values() if j.running and j.command != "plex_login"]

    @contextlib.contextmanager
    def _wait_lock(self, lock: Any, job: Job) -> Iterator[None]:
        if not lock.acquire(False):
            job.emit("info", "Un cycle est déjà en cours : attente de sa fin…")
            lock.acquire()
        try:
            yield
        finally:
            lock.release()

    def _cmd_sync(self, job: Job) -> Dict[str, Any]:
        if self.plex is None:
            raise ValueError("Plex n'est pas connecté : utilisez « Connexion Plex »")
        with self._wait_lock(self._watch_lock, job):
            count = self.searcher.sync_watchlist()
        job.emit("ok", "Watchlist synchronisée : %d élément(s)" % count)
        return {"count": count}

    def _cmd_search_all(self, job: Job, force: bool) -> Dict[str, Any]:
        with self._wait_lock(self._watch_lock, job):
            try:
                self.searcher.sync_watchlist()
            except Exception as exc:  # Plex down: still search what we already know
                job.emit("warn", "Watchlist Plex illisible (%s) : recherche sur les données déjà connues" % exc)
            try:
                stats = self.searcher.run_once(force=force)
            finally:
                self._last["watchlist"] = now_iso()
        job.emit("ok", "%d recherche(s), %d envoi(s) vers Download Station" % (stats["searched"], stats["grabbed"]))
        return stats

    def _cmd_process(self, job: Job, dry_run: bool) -> Dict[str, Any]:
        dry = bool(dry_run or self.cfg.general.dry_run)
        with self._wait_lock(self._process_lock, job):
            actions = self.organizer.run_once(dry_run=dry) or []
            if not dry:
                self._last["process"] = now_iso()
        if not actions:
            job.emit("info", "Rien à traiter dans %s" % self.cfg.paths.download_dir)
        for a in actions:
            st = a.get("status") or ""
            level = "ok" if st in ("imported", "planned") else ("err" if st in ("error", "failed") else "info")
            job.emit(level, "%s : %s%s%s" % (st, a.get("src"), (" → " + a["dst"]) if a.get("dst") else "",
                                            (" (" + a["message"] + ")") if a.get("message") else ""))
        clean = [{"src": a.get("src") or "", "dst": a.get("dst") or "", "status": a.get("status") or "",
                  "message": a.get("message") or ""} for a in actions]
        return {"dry_run": dry, "actions": clean}

    def _cmd_check(self, job: Job) -> Dict[str, Any]:
        from .cli import check_report
        for w in getattr(self.cfg, "warnings", None) or []:
            job.emit("warn", w)
        code = check_report(self.cfg, job.emit, app=self, hint=_web_hint)
        return {"ok": code == 0}

    def _cmd_test_indexers(self, job: Job) -> Dict[str, Any]:
        rows = self.indexer_manager.list()
        if not rows:
            job.emit("err", "Aucun indexer : ajoutez-en un depuis l'onglet Indexers")
        results = []
        for row in rows:
            ok, msg = self.indexer_manager.test(row["id"])
            state = "" if row["enabled"] else " (désactivé)"
            job.emit("ok" if ok else "err", "%s%s : %s" % (row["name"], state, msg))
            results.append({"id": row["id"], "name": row["name"], "ok": bool(ok), "message": msg})
        return {"results": results}

    def _cmd_search_query(self, job: Job, query: str) -> Dict[str, Any]:
        req = request_from_query(query)
        job.emit("info", "Recherche : %s (%s)" % (req.describe(), req.kind))
        if not self.db.list_indexers(enabled_only=True):
            job.emit("warn", "Aucun indexer actif : ajoutez-en un depuis l'onglet Indexers")
        report = self.searcher.search_report(req)
        job.private["releases"] = [rel for rel, _score, _reason in report]
        results = []
        for i, (rel, score, reason) in enumerate(report[:200]):
            results.append({"index": i, "title": rel.title, "size": rel.size or 0, "seeders": rel.seeders,
                            "leechers": rel.leechers, "indexer": rel.indexer, "score": score, "reason": reason})
        accepted = sum(1 for _r, score, _x in report if score is not None)
        job.emit("ok" if accepted else "warn", "%d résultat(s), dont %d acceptable(s)" % (len(report), accepted))
        return {"query": query, "request": req.describe(), "kind": req.kind, "total": len(report),
                "results": results}

    def _match_media(self, title: str) -> Tuple[Optional[Dict[str, Any]], Optional[int], List[int]]:
        """Élément de la Watchlist désigné sans ambiguïté par le nom d'une release (sinon None)."""
        from .parser import parse, titles_match
        p = parse(title)
        is_show = bool(p.seasons or p.episodes)
        found = []
        for m in self.db.list_media():
            if m["type"] != ("show" if is_show else "movie"):
                continue
            alts = [m["original_title"]] if m.get("original_title") else []
            if not titles_match(p.title, m["title"], alts):
                continue
            if not is_show and p.year and m.get("year") and abs(int(p.year) - int(m["year"])) > 1:
                continue
            found.append(m)
        if len(found) != 1 or (is_show and p.season is None):
            return None, None, []
        return found[0], (p.season if is_show else None), (list(p.episodes) if is_show else [])

    def grab_search_result(self, job_id: int, index: int) -> Dict[str, Any]:
        """Envoie à Download Station le résultat n° *index* d'une recherche manuelle (tâche *job_id*)."""
        job = self.get_job(job_id)
        releases = job.private.get("releases") if job is not None and job.command == "search" else None
        if not releases:
            raise ValueError("Résultats de recherche introuvables : relancez la recherche")
        if not 0 <= int(index) < len(releases):
            raise ValueError("Résultat introuvable (n° %s)" % index)
        rel = releases[int(index)]
        if self.cfg.general.dry_run:
            self.searcher.grab(rel, None, None, None)  # journalise « [dry-run] »
            return {"ok": False, "message": "Mode simulation (dry_run) actif : rien n'a été envoyé"}
        if self.ds is None:
            raise ValueError("Download Station n'est pas configuré ([downloadstation] username/password)")
        media, season, episodes = self._match_media(rel.title)
        episode = episodes[0] if episodes else None
        if not self.searcher.grab(rel, media, season, episode):
            return {"ok": False, "message": "Envoi impossible : release déjà envoyée ou erreur (voir le journal)"}
        if media is not None:
            if media["type"] == "movie":
                self.db.execute("UPDATE media SET status = 'grabbed', updated_at = ? WHERE id = ? "
                                "AND status = 'wanted'", (now_iso(), media["id"]))
            elif episodes:
                for ep in episodes:
                    self.db.set_episode_status(media["id"], season, ep, "grabbed", only_if=["wanted"])
            else:
                self.db.set_episode_status(media["id"], season, None, "grabbed", only_if=["wanted"])
        msg = "Envoyé à Download Station : %s" % rel.title
        if media is not None:
            msg += " (associé à « %s »)" % media["title"]
        return {"ok": True, "message": msg, "media_id": media["id"] if media else None}

    # ----------------------------------------------------------- Plex login
    def start_plex_login(self) -> Tuple[Job, bool]:
        """Crée un code PIN plex.tv (lève HttpError si plex.tv est injoignable) puis attend en arrière-plan
        que l'utilisateur le saisisse sur https://plex.tv/link ; le jeton est alors enregistré."""
        with self._jobs_lock:
            for job in self._jobs.values():
                if job.command == "plex_login" and job.running:
                    return job, False
        client = self._new_plex_client("")
        pin_id, code = client.start_pin_login()

        def prepare(job: Job) -> None:
            job.result = {"code": code, "url": PLEX_LINK_URL, "linked": False}
            job.emit("info", "Ouvrez %s (connecté à votre compte Plex) et saisissez le code %s" % (PLEX_LINK_URL, code))
            if self.cfg.plex.token:
                job.emit("warn", "[plex] token est renseigné dans config.ini : il reste prioritaire sur le jeton "
                         "obtenu ici. Videz cette ligne pour utiliser le nouveau jeton.")
        return self._start_job("plex_login", COMMANDS["plex_login"], "plex_login", self._cmd_plex_wait,
                               (client, pin_id, code), False, prepare)

    def _cmd_plex_wait(self, job: Job, client: Any, pin_id: int, code: str) -> Dict[str, Any]:
        deadline = time.time() + self.pin_timeout
        result = {"code": code, "url": PLEX_LINK_URL, "linked": False}  # type: Dict[str, Any]
        while time.time() < deadline and not self._stop.is_set():
            try:
                token = client.check_pin(pin_id)
            except HttpError as exc:
                if exc.status == 404:  # expired
                    raise
                log.debug("Vérification du code Plex : %s", exc)
                token = None
            if token:
                self.save_plex_token(token)
                ok, msg = self.plex.test() if self.plex is not None else (False, "")
                job.emit("ok" if ok else "err", "Jeton Plex enregistré. " + msg)
                result["linked"] = True
                result["ok"] = bool(ok)
                return result
            self._stop.wait(self.pin_poll_interval)
        raise ValueError("Code non validé à temps : relancez « Connexion Plex »")

    # ---------------------------------------------------------------- restart
    def restart(self, delay: float = 1.0) -> None:
        """Relance le processus (os.execv) après *delay* s, une fois la recherche / le traitement en cours
        terminés (un déplacement de fichier n'est jamais interrompu). Même PID : start.sh et Docker suivent."""
        if self._restarting:
            return
        self._restarting = True
        threading.Thread(target=self._do_restart, args=(delay,), daemon=True, name="synoarr-restart").start()

    def _do_restart(self, delay: float) -> None:
        time.sleep(delay)
        log.info("Redémarrage de synoarr demandé depuis l'interface web…")
        stop = threading.Event()
        stop.set()
        self.searcher.stop_event = stop  # a long search cycle stops between two items
        got_w = self._watch_lock.acquire(timeout=300)
        got_p = self._process_lock.acquire(timeout=300)
        argv = restart_argv()
        try:
            if self.web is not None:
                try:
                    self.web.stop()
                except Exception as exc:  # pragma: no cover
                    log.debug("Arrêt de l'interface web : %s", exc)
            if got_w and got_p:
                try:
                    self.db.close()
                except Exception:  # pragma: no cover
                    pass
            for h in logging.getLogger().handlers:
                try:
                    h.flush()
                except Exception:  # pragma: no cover
                    pass
            os.execv(argv[0], argv)
        except Exception as exc:
            log.error("Redémarrage impossible : %s", exc)
            for h in logging.getLogger().handlers:
                try:
                    h.flush()
                except Exception:  # pragma: no cover
                    pass
            os._exit(3)  # base fermée, interface arrêtée : mieux vaut s'arrêter que rester à moitié vivant

    # ------------------------------------------------------------ config.ini
    @staticmethod
    def _restart_settings(cfg: Config) -> Dict[str, Any]:
        w = cfg.web
        return {"[web] enabled": w.enabled, "[web] host": w.host, "[web] port": w.port,
                "[web] username": w.username, "[web] password": w.password, "[general] data_dir": cfg.data_dir}

    def restart_reasons(self) -> List[str]:
        """Réglages modifiés dans config.ini qui ne s'appliqueront qu'au redémarrage."""
        now = self._restart_settings(self.cfg)
        return [k for k, v in self._boot.items() if now.get(k) != v]

    def config_path(self) -> str:
        if not self.cfg.path:
            raise ValueError("synoarr n'a pas été lancé avec un fichier config.ini : édition impossible")
        return self.cfg.path

    def config_backups(self) -> List[Dict[str, Any]]:
        path = self.config_path()
        out = []
        for i in range(1, CONFIG_BACKUPS + 1):
            p = "%s.bak%d" % (path, i)
            try:
                mtime = os.path.getmtime(p)
            except OSError:
                continue
            out.append({"name": os.path.basename(p),
                        "modified": _dt.datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M:%S")})
        return out

    def read_config_text(self) -> Dict[str, Any]:
        path = self.config_path()
        try:
            with open(path, "rb") as fh:
                text, enc = decode_config_bytes(fh.read())
        except OSError as exc:
            raise ValueError("Lecture de %s impossible : %s" % (path, exc.strerror or exc))
        return {"path": path, "text": text, "encoding": enc, "backups": self.config_backups(),
                "restart_reasons": self.restart_reasons()}

    def _prepare_config(self, text: str, path: str) -> Tuple[str, Config]:
        """Écrit *text* (UTF-8) dans un fichier temporaire à côté de config.ini, avec les droits de l'original,
        et le valide (load_config + validate). Retourne (chemin temporaire, Config). Lève ConfigInvalid."""
        if not isinstance(text, str) or not text.strip():
            raise ConfigInvalid(["Le fichier de configuration ne peut pas être vide"])
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        if not text.endswith("\n"):
            text += "\n"
        folder = os.path.dirname(os.path.abspath(path))
        try:
            fd, tmp = tempfile.mkstemp(prefix=".config-", suffix=".ini.tmp", dir=folder)
        except OSError as exc:
            raise ValueError("Écriture impossible dans %s : %s" % (folder, exc.strerror or exc))
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(text.encode("utf-8"))
                fh.flush()
                os.fsync(fh.fileno())
            try:
                st = os.stat(path)
                os.chmod(tmp, stat.S_IMODE(st.st_mode))
                if hasattr(os, "chown"):
                    try:
                        os.chown(tmp, st.st_uid, st.st_gid)
                    except OSError:  # not root: the file stays ours
                        pass
            except OSError:
                pass
            try:
                cfg = load_config(tmp)
            except ConfigError as exc:
                raise ConfigInvalid([str(exc).replace(tmp, path)])
            problems = cfg.validate()
            if problems:
                raise ConfigInvalid(problems)
            cfg.path = os.path.abspath(path)
            cfg.warnings = [w.replace(os.path.basename(tmp), os.path.basename(path)) for w in cfg.warnings]
            return tmp, cfg
        except BaseException:
            try:
                os.remove(tmp)
            except OSError:
                pass
            raise

    def _backup_config(self, path: str) -> None:
        if not os.path.isfile(path):
            return
        for i in range(CONFIG_BACKUPS - 1, 0, -1):
            src = "%s.bak%d" % (path, i)
            if os.path.exists(src):
                os.replace(src, "%s.bak%d" % (path, i + 1))
        bak = path + ".bak1"
        shutil.copy2(path, bak)
        try:
            st = os.stat(path)
            os.chown(bak, st.st_uid, st.st_gid)
        except (OSError, AttributeError):
            pass

    @contextlib.contextmanager
    def _exclusive(self, timeout: float = 5.0) -> Iterator[None]:
        """Aucune tâche ne tourne pendant le bloc (sinon BusyError)."""
        busy = self._busy_jobs()
        if busy:
            raise BusyError("Une tâche est en cours (%s) : réessayez quand elle sera terminée" % busy[0].label)
        if not self._watch_lock.acquire(timeout=timeout):
            raise BusyError("Une tâche est en cours (recherche) : réessayez dans un instant")
        try:
            if not self._process_lock.acquire(timeout=timeout):
                raise BusyError("Une tâche est en cours (traitement des téléchargements) : réessayez dans un instant")
            try:
                yield
            finally:
                self._process_lock.release()
        finally:
            self._watch_lock.release()

    def save_config_text(self, text: str) -> Dict[str, Any]:
        """Valide puis enregistre config.ini (sauvegarde .bak1…bak5, remplacement atomique, droits conservés)
        et applique la nouvelle configuration. Lève ConfigInvalid (rien n'est modifié), BusyError, ValueError."""
        path = self.config_path()
        tmp, cfg = self._prepare_config(text, path)
        try:
            with self._exclusive():
                self._backup_config(path)
                try:
                    os.replace(tmp, path)
                except OSError:  # config.ini monté seul dans Docker (EBUSY) : réécriture en place
                    with open(tmp, "rb") as src, open(path, "wb") as dst:
                        dst.write(src.read())
                self._apply_config(cfg)
        finally:
            if os.path.exists(tmp):
                try:
                    os.remove(tmp)
                except OSError:
                    pass
        log.info("config.ini enregistré depuis l'interface web et rechargé")
        try:
            self.db.add_event("config", "Configuration modifiée depuis l'interface web")
        except Exception:  # pragma: no cover
            pass
        reasons = self.restart_reasons()
        msg = "Configuration enregistrée et appliquée"
        if reasons:
            msg += " — redémarrage nécessaire pour : " + ", ".join(reasons)
        return {"ok": True, "message": msg, "restart_required": bool(reasons), "restart_reasons": reasons,
                "warnings": list(cfg.warnings), "backups": self.config_backups()}

    def restore_config_backup(self) -> Dict[str, Any]:
        """Restaure config.ini.bak1 (la version remplacée devient à son tour la sauvegarde .bak1)."""
        bak = self.config_path() + ".bak1"
        try:
            with open(bak, "rb") as fh:
                text, _enc = decode_config_bytes(fh.read())
        except OSError:
            raise ValueError("Aucune sauvegarde de config.ini à restaurer")
        out = self.save_config_text(text)
        out["message"] = out["message"].replace("Configuration enregistrée", "Sauvegarde précédente restaurée", 1)
        return out

    def reload_config(self) -> Dict[str, Any]:
        """Relit config.ini et reconstruit les composants (sans redémarrer)."""
        path = self.config_path()
        try:
            cfg = load_config(path)
        except ConfigError as exc:
            raise ConfigInvalid([str(exc)])
        problems = cfg.validate()
        if problems:
            raise ConfigInvalid(problems)
        with self._exclusive():
            self._apply_config(cfg)
        reasons = self.restart_reasons()
        return {"ok": True, "restart_required": bool(reasons), "restart_reasons": reasons}

    def _apply_config(self, cfg: Config) -> None:
        """À appeler sous _exclusive() : remplace la configuration et reconstruit les composants."""
        self.cfg = cfg
        self._build_components()
        level = getattr(logging, (cfg.general.log_level or "INFO").upper(), None)
        if isinstance(level, int):
            logging.getLogger().setLevel(level)

    def log_file(self) -> str:
        """Fichier journal réellement utilisé (celui du démarrage, même si data_dir a changé depuis)."""
        for h in logging.getLogger().handlers:
            if isinstance(h, logging.handlers.RotatingFileHandler):
                return h.baseFilename
        return os.path.join(self.cfg.data_dir, "synoarr.log")

    # --------------------------------------------------------------- daemon
    def stop(self, *_args: Any) -> None:
        log.info("Arrêt demandé")
        self._stop.set()

    def run_forever(self) -> None:
        log.info("synoarr %s démarré (données : %s)", __version__, self.cfg.data_dir)
        if self.cfg.general.dry_run:
            log.warning("Mode dry_run actif : rien ne sera envoyé ni déplacé")
        try:
            signal.signal(signal.SIGTERM, self.stop)
            signal.signal(signal.SIGINT, self.stop)
        except ValueError:  # not in main thread (tests)
            pass
        if self.cfg.web.enabled:
            from .web import WebServer
            try:
                self.web = WebServer(self, self.cfg.web.host, self.cfg.web.port,
                                     self.cfg.web.username, self.cfg.web.password)
                self.web.start()
                log.info("Interface web : http://<ip-du-nas>:%s", self.cfg.web.port)
            except OSError as exc:
                log.error("Interface web impossible sur le port %s : %s", self.cfg.web.port, exc)
        next_watch = time.time() + 5
        next_process = time.time() + 2
        while not self._stop.is_set():
            try:
                now = time.time()
                # re-read at each turn: a config change made from the web UI applies without restart
                watch_every = max(1.0, self.cfg.general.watchlist_interval_minutes) * 60
                process_every = max(0.5, self.cfg.general.process_interval_minutes) * 60
                next_watch = min(next_watch, now + watch_every)
                next_process = min(next_process, now + process_every)
                if now >= next_process:
                    self.job_process()
                    next_process = time.time() + process_every
                if now >= next_watch and not self._stop.is_set():
                    self._start_watchlist_thread()
                    next_watch = time.time() + watch_every
            except Exception as exc:  # pragma: no cover - jobs already catch everything; never die
                log.exception("Boucle principale : erreur inattendue : %s", exc)
            self._stop.wait(2)
        self.shutdown()

    def shutdown(self, timeout: float = 60.0) -> None:
        """Stop the web server, let running jobs finish (a file move is never cut), close the database."""
        self._stop.set()
        if self.web is not None:
            try:
                self.web.stop()
            except Exception as exc:  # pragma: no cover
                log.debug("Arrêt de l'interface web : %s", exc)
        deadline = time.time() + timeout
        if self._watch_thread is not None and self._watch_thread.is_alive():
            log.info("Attente de la fin de la recherche en cours…")
            self._watch_thread.join(max(0.0, deadline - time.time()))
        got_watch = self._watch_lock.acquire(timeout=max(0.1, deadline - time.time()))
        got_process = self._process_lock.acquire(timeout=max(0.1, deadline - time.time()))
        try:
            if got_watch and got_process:
                self.db.close()
            else:  # a job is still running: closing the database under it would only raise errors
                log.warning("Arrêt sans attendre la fin du traitement en cours")
        finally:
            if got_process:
                self._process_lock.release()
            if got_watch:
                self._watch_lock.release()
        log.info("synoarr arrêté")

    def run_once(self) -> None:
        """One full cycle (for a scheduled-task setup instead of the daemon)."""
        self.job_process()
        self.job_watchlist()
        self.job_process()


def today() -> str:  # pragma: no cover - helper for templates
    return _dt.date.today().isoformat()
