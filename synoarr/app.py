"""Application wiring + daemon loop."""
from __future__ import annotations

import datetime as _dt
import logging
import logging.handlers
import os
import signal
import threading
import time
import uuid
from typing import Any, Dict, List, Optional, Tuple

from . import __version__
from .config import Config
from .db import Database, now_iso
from .httpclient import HttpClient
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


class App:
    def __init__(self, cfg: Config):
        from .downloadstation import DownloadStation
        from .organizer import Organizer
        from .plex import PlexClient, refresh_library
        from .searcher import Searcher
        from .tmdb import TmdbClient

        self.cfg = cfg
        self.started_at = now_iso()
        os.makedirs(cfg.data_dir, exist_ok=True)
        self.db = Database(os.path.join(cfg.data_dir, "synoarr.db"))
        self.http = HttpClient(timeout=30)
        self.ds_http = HttpClient(timeout=60, verify_ssl=cfg.downloadstation.verify_ssl)
        self.indexer_manager = IndexerManager(self.db, self.http)

        client_id = self.db.get_kv("plex_client_id")
        if not client_id:
            client_id = "synoarr-" + uuid.uuid4().hex
            self.db.set_kv("plex_client_id", client_id)
        self.plex_client_id = client_id
        token = self.plex_token
        self.plex = PlexClient(token, client_id, self.http) if token else None

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
        self._job_lock = threading.Lock()
        self._stop = threading.Event()
        self._last: Dict[str, Optional[str]] = {"watchlist": None, "process": None}
        self._errors: List[str] = []
        self.web: Any = None

    # ------------------------------------------------------------- settings
    @property
    def plex_token(self) -> str:
        return self.cfg.plex.token or self.db.get_kv("plex_token") or ""

    def save_plex_token(self, token: str) -> None:
        from .plex import PlexClient
        self.db.set_kv("plex_token", token)
        self.plex = PlexClient(token, self.plex_client_id, self.http)
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
        with self._job_lock:
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
        with self._job_lock:
            try:
                return self.organizer.run_once(dry_run=self.cfg.general.dry_run if dry_run is None else dry_run)
            except Exception as exc:
                log.exception("Traitement des téléchargements en erreur : %s", exc)
                self._record_error("process", exc)
                return []
            finally:
                self._last["process"] = now_iso()

    def _background(self, target: Any, *args: Any) -> None:
        threading.Thread(target=target, args=args, daemon=True).start()

    # web UI hooks
    def run_watchlist_now(self) -> None:
        self._background(self.job_watchlist)

    def run_process_now(self) -> None:
        self._background(self.job_process)

    def search_now(self, media_id: int) -> None:
        self._background(self.job_watchlist, int(media_id), True)

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
        }

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
        watch_every = max(1.0, self.cfg.general.watchlist_interval_minutes) * 60
        process_every = max(0.5, self.cfg.general.process_interval_minutes) * 60
        next_watch = time.time() + 5
        next_process = time.time() + 2
        while not self._stop.is_set():
            now = time.time()
            if now >= next_process:
                self.job_process()
                next_process = time.time() + process_every
            if now >= next_watch:
                self.job_watchlist()
                next_watch = time.time() + watch_every
            self._stop.wait(2)
        if self.web is not None:
            self.web.stop()
        self.db.close()
        log.info("synoarr arrêté")

    def run_once(self) -> None:
        """One full cycle (for a scheduled-task setup instead of the daemon)."""
        self.job_process()
        self.job_watchlist()
        self.job_process()


def today() -> str:  # pragma: no cover - helper for templates
    return _dt.date.today().isoformat()
