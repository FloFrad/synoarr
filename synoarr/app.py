"""Application wiring + daemon loop."""
from __future__ import annotations

# ---- watchlist add/delete : API App pour l'interface web (ValueError = saisie invalide, message français → 400)
#
# app.search_titles(query: str, kind: None|"movie"|"show" = None, year: int|str|None = None) -> dict
#     {"query": str, "warnings": [str], "results": [candidat]}   (ne lève que ValueError : requête vide,
#     type ou année invalides ; Plex/TMDb en panne → avertissement dans "warnings")
#     candidat = {"source": "plex"|"tmdb", "type": "movie"|"show", "title", "original_title", "year",
#                 "rating_key" (Plex, sinon None), "tmdb", "imdb", "tvdb", "overview" (≤ 300 car.),
#                 "poster_url" (URL publique ou None), "in_watchlist": bool, "media_id": int|None}
# app.add_to_watchlist(candidate: dict, add_to_plex: bool = True) -> dict
#     candidate = un candidat ci-dessus tel quel, ou au minimum {"type", "title"} (+ year, imdb, tmdb, tvdb,
#     rating_key, original_title). Avec rating_key et add_to_plex : ajout à la Watchlist Plex puis import ;
#     sinon (ou si Plex échoue) entrée locale (plex_key "local:...", jamais retirée par la synchro).
#     Déjà présent (même clé Plex, même imdb/tmdb/tvdb, ou à défaut même type + même titre (titre original
#     compris) + année ±1, lignes masquées comprises) → ligne existante réactivée (démasquée, "ignored" →
#     wanted/monitored). Retour : ligne de la table media + "plex_added": bool|None,
#     "already_present": bool, "reactivated": bool (ligne masquée/ignorée réactivée), "warning": str|None. Appels réseau synchrones (Plex, TMDb : quelques s).
#     Ensuite, pour chercher tout de suite : app.search_now(row["id"]).
# app.add_manual(type_: str, title: str, year=None, imdb=None, tmdb=None) -> dict   (idem, entrée locale)
# app.remove_from_watchlist(media_id: int, remove_from_plex: bool = True, delete_downloads: bool = False)
#     -> {"removed": True, "plex_removed": bool|None, "warning": str|None, "downloads_deleted": int}
#     Entrée locale ou retirée de Plex → ligne supprimée (épisodes supprimés, historique des envois gardé).
#     Retrait Plex refusé/impossible ou remove_from_plex=False → ligne masquée (hidden=1) + "ignored", avec un
#     avertissement (elle reste dans la Watchlist Plex). delete_downloads : supprime les tâches Download
#     Station des envois en cours (jamais de fichier de la bibliothèque). ValueError si id inconnu.
# db.list_media(..., include_hidden=False) : les entrées masquées n'apparaissent plus dans les listes.
# Une ligne media est locale si plex_key commence par "local:".

import contextlib
import datetime as _dt
import logging
import logging.handlers
import os
import re
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


PRIORITY_DEFAULT = 25


def check_priority(value: Any) -> int:
    """Priorité d'indexer saisie (1 = le plus prioritaire … 50) -> int. Lève ValueError en français."""
    if isinstance(value, bool):
        value = None
    try:
        prio = int(str(value).strip()) if not isinstance(value, int) else value
    except (TypeError, ValueError):
        prio = None
    if prio is None or not 1 <= prio <= 50:
        raise ValueError("Priorité invalide : nombre entier de 1 (le plus prioritaire) à 50 attendu")
    return prio


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
                type_: Optional[str] = None, priority: Any = None) -> Dict[str, Any]:
        name = (name or "").strip()
        url = (url or "").strip()
        if not name:
            raise ValueError("Le nom de l'indexer est obligatoire")
        if not url.lower().startswith(("http://", "https://")):
            raise ValueError("L'URL doit commencer par http:// ou https://")
        prio = PRIORITY_DEFAULT if priority in (None, "") else check_priority(priority)
        if self.db.get_indexer(name):
            raise ValueError("Un indexer nommé « %s » existe déjà" % name)
        if not type_:
            type_ = detect_type(url, api_key, self.http)
        new_id = self.db.add_indexer(name, type_, url, api_key or "", categories or "", priority=prio)
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

    def set_priority(self, indexer_id: Any, priority: Any) -> Dict[str, Any]:
        """Priorité 1 (le plus prioritaire) à 50. Lève ValueError (message français)."""
        prio = check_priority(priority)
        row = self._row(indexer_id)
        self.db.update_indexer(row["id"], priority=prio)
        if int(row.get("priority") or 0) != prio:
            self.db.add_event("indexer", "Priorité de l'indexer %s : %d" % (row["name"], prio))
        return self.db.get_indexer(row["id"]) or {}

    def priorities(self) -> Dict[str, int]:
        """{nom: priorité 1..50} de tous les indexers."""
        from .quality import clamp_priority
        return {r["name"]: clamp_priority(r.get("priority")) for r in self.db.list_indexers()}

    # ---- import Prowlarr
    PROWLARR_DEFAULT_URL = "http://127.0.0.1:9696"

    @staticmethod
    def normalize_prowlarr_url(base_url: str) -> Tuple[str, str]:
        """(URL de base sans / final ni requête, clé trouvée dans « ?apikey= » ou ""). Lève ValueError."""
        import urllib.parse
        raw = (base_url or "").strip()
        if not raw:
            raise ValueError("Saisissez l'adresse de Prowlarr (ex. %s)" % IndexerManager.PROWLARR_DEFAULT_URL)
        if "://" not in raw:
            raw = "http://" + raw
        try:
            parts = urllib.parse.urlsplit(raw)
        except ValueError:
            parts = None
        if parts is None or parts.scheme.lower() not in ("http", "https") or not parts.hostname:
            raise ValueError("Adresse de Prowlarr invalide (ex. %s)" % IndexerManager.PROWLARR_DEFAULT_URL)
        key = ""
        for k, v in urllib.parse.parse_qsl(parts.query):
            if k.lower() == "apikey" and v.strip():
                key = v.strip()
        path = parts.path.rstrip("/")
        for suffix in ("/api/v1/indexer", "/api/v1"):
            if path.lower().endswith(suffix):
                path = path[:-len(suffix)]
        return urllib.parse.urlunsplit((parts.scheme.lower(), parts.netloc, path, "", "")).rstrip("/"), key

    def import_prowlarr(self, base_url: str, api_key: str = "", remember: bool = True,
                        prune: bool = True) -> Dict[str, Any]:
        """Ajoute (ou met à jour) un indexer Torznab « <nom> (Prowlarr) » pour chaque indexer torrent actif de
        Prowlarr (API v1). Lève ValueError (message français : clé refusée, Prowlarr injoignable...).

        remember : mémorise l'adresse et la clé pour la synchronisation automatique.
        prune    : désactive les indexers importés de Prowlarr qui n'y existent plus ou y sont désactivés
                   (et réactive ceux que la synchro avait désactivés quand ils reviennent)."""
        from .quality import clamp_priority
        base, key_in_url = self.normalize_prowlarr_url(base_url)
        key = (api_key or "").strip() or key_in_url
        if not key:
            raise ValueError("Saisissez la clé API de Prowlarr (Prowlarr > Settings > General > API Key)")
        http = self.http if not is_local_url(base) else HttpClient(timeout=30, use_proxy=False)
        try:
            resp = http.get(base + "/api/v1/indexer", headers={"X-Api-Key": key, "Accept": "application/json"},
                            raise_for_status=False, timeout=30)
        except HttpError as exc:
            raise ValueError("Prowlarr injoignable à %s (%s). Vérifiez l'adresse, par exemple %s si Prowlarr tourne "
                             "sur le NAS." % (base, exc, self.PROWLARR_DEFAULT_URL))
        if resp.status in (401, 403):
            raise ValueError("Clé API Prowlarr refusée (Prowlarr > Settings > General > API Key)")
        if resp.status == 404:
            raise ValueError("Pas d'API Prowlarr à %s (HTTP 404) : vérifiez l'adresse, par exemple %s"
                             % (base, self.PROWLARR_DEFAULT_URL))
        if not resp.ok:
            raise ValueError("Prowlarr a répondu HTTP %s à %s" % (resp.status, base))
        try:
            rows = resp.json()
        except ValueError:
            rows = None
        if not isinstance(rows, list):
            raise ValueError("Réponse inattendue de %s : ce n'est pas l'API de Prowlarr (vérifiez l'adresse, par "
                             "exemple %s)" % (base, self.PROWLARR_DEFAULT_URL))
        existing = self.db.list_indexers()
        by_url = {(r["url"] or "").rstrip("/").lower(): r for r in existing}
        added, updated, skipped = [], [], []  # type: List[str], List[str], List[str]
        for item in rows:
            if not isinstance(item, dict) or item.get("id") in (None, ""):
                continue
            pname = str(item.get("name") or "Indexer %s" % item.get("id")).strip()
            if str(item.get("protocol") or "torrent").lower() != "torrent":
                skipped.append("%s : indexer Usenet non pris en charge" % pname)
                continue
            if item.get("enable") is False:
                skipped.append("%s : désactivé dans Prowlarr" % pname)
                continue
            name = "%s (Prowlarr)" % pname
            url = "%s/%s/api" % (base, item["id"])
            prio = clamp_priority(item.get("priority"))
            row = by_url.get(url.lower()) or self.db.get_indexer(name)
            if row is not None and row.get("type") == "torznab":
                self.db.update_indexer(row["id"], url=url, api_key=key, priority=prio)
                updated.append(row["name"])
                continue
            if row is not None:
                skipped.append("%s : un indexer nommé « %s » existe déjà" % (pname, row["name"]))
                continue
            new_id = self.db.add_indexer(name, "torznab", url, key, "", priority=prio)
            by_url[url.lower()] = self.db.get_indexer(new_id) or {}
            added.append(name)
        disabled, reenabled = [], []  # type: List[str], List[str]
        if prune:
            disabled, reenabled = self._prune_prowlarr(base, rows)
        if remember:
            self.db.set_kv("prowlarr_url", base)
            self.db.set_kv("prowlarr_api_key", key)
        self.db.set_kv("prowlarr_last_sync", now_iso())
        self.db.set_kv("prowlarr_last_error", "")
        parts = ["%d ajouté(s)" % len(added), "%d mis à jour" % len(updated)]
        if disabled:
            parts.append("%d désactivé(s) (absents ou désactivés dans Prowlarr)" % len(disabled))
        if reenabled:
            parts.append("%d réactivé(s)" % len(reenabled))
        if skipped:
            parts.append("%d ignoré(s)" % len(skipped))
        message = "Import Prowlarr : " + ", ".join(parts)
        if not rows:
            message = "Aucun indexer configuré dans Prowlarr"
        if self.db.get_kv("jackett_url") and remember:
            message += " — attention : Jackett est aussi synchronisé ; utilisez l'un OU l'autre (doublons)"
        if added or disabled or reenabled or not remember:
            self.db.add_event("indexer", message)
        return {"ok": True, "added": added, "updated": updated, "skipped": skipped, "disabled": disabled,
                "reenabled": reenabled, "message": message}

    def _prune_prowlarr(self, base: str, rows: List[Any]) -> Tuple[List[str], List[str]]:
        """Disable our "(Prowlarr)" indexers that Prowlarr no longer lists as enabled torrent indexers;
        re-enable the ones this sync had disabled when they come back. Indexers disabled by the user stay so."""
        active_urls = set()
        for item in rows:
            if isinstance(item, dict) and item.get("id") not in (None, "") and item.get("enable") is not False \
                    and str(item.get("protocol") or "torrent").lower() == "torrent":
                active_urls.add(("%s/%s/api" % (base, item["id"])).lower())
        return self._prune_source("prowlarr", base, active_urls, lambda u: u.endswith("/api"))

    def _prune_source(self, source: str, base: str, active_urls: Any,
                      is_ours: Any) -> Tuple[List[str], List[str]]:
        """Shared by Prowlarr and Jackett syncs: disable our indexers of *source* (URL under *base*, shape
        accepted by *is_ours*) missing from *active_urls*; re-enable those this sync had disabled."""
        import json as _json
        kv = "%s_disabled_by_sync" % source
        try:
            by_sync = set(_json.loads(self.db.get_kv(kv) or "[]"))
        except ValueError:
            by_sync = set()
        prefix = (base + "/").lower()
        active_urls = {u.rstrip("/").lower() for u in active_urls}
        disabled, reenabled = [], []  # type: List[str], List[str]
        for r in self.db.list_indexers():
            url = (r.get("url") or "").rstrip("/").lower()
            if r.get("type") != "torznab" or not url.startswith(prefix) or not is_ours(url):
                continue
            if url in active_urls:
                if not r["enabled"] and url in by_sync:
                    self.db.update_indexer(r["id"], enabled=1)
                    by_sync.discard(url)
                    reenabled.append(r["name"])
            elif r["enabled"]:
                self.db.update_indexer(r["id"], enabled=0)
                by_sync.add(url)
                disabled.append(r["name"])
        self.db.set_kv(kv, _json.dumps(sorted(by_sync)))
        return disabled, reenabled

    def prowlarr_settings(self, cfg: Optional[Config] = None) -> Dict[str, Any]:
        """Adresse/clé utilisées pour la synchro automatique : [prowlarr] de config.ini, sinon celles mémorisées
        lors du dernier import. La clé n'est jamais renvoyée en clair."""
        url = (getattr(cfg.prowlarr, "url", "") if cfg is not None else "") or self.db.get_kv("prowlarr_url") or ""
        key = (getattr(cfg.prowlarr, "api_key", "") if cfg is not None else "") or \
            self.db.get_kv("prowlarr_api_key") or ""
        hours = float(getattr(cfg.prowlarr, "sync_interval_hours", 6.0)) if cfg is not None else 6.0
        return {"url": url, "configured": bool(url and key), "has_key": bool(key), "interval_hours": hours,
                "last_sync": self.db.get_kv("prowlarr_last_sync") or None,
                "last_error": self.db.get_kv("prowlarr_last_error") or None, "_key": key}

    def sync_prowlarr(self, cfg: Optional[Config] = None) -> Optional[Dict[str, Any]]:
        """Synchronisation automatique (démon). None si Prowlarr n'est pas configuré. Ne lève jamais."""
        st = self.prowlarr_settings(cfg)
        if not st["configured"]:
            return None
        try:
            res = self.import_prowlarr(st["url"], st["_key"], remember=False, prune=True)
            log.info(res["message"])
            return res
        except Exception as exc:  # Prowlarr arrêté, clé changée... : réessayé au prochain cycle
            self.db.set_kv("prowlarr_last_error", "%s %s" % (now_iso(), exc))
            log.warning("Synchronisation Prowlarr impossible : %s", exc)
            return {"ok": False, "message": str(exc)}

    # ---- Jackett (même principe que Prowlarr : soit l'un, soit l'autre) -------------------------
    JACKETT_DEFAULT_URL = "http://127.0.0.1:9117"
    _JACKETT_PATH = "/api/v2.0/indexers/%s/results/torznab"

    @staticmethod
    def normalize_jackett_url(base_url: str) -> Tuple[str, str]:
        """(URL de base de Jackett, clé trouvée dans « ?apikey= » ou ""). Accepte aussi une URL Torznab copiée
        depuis Jackett (…/api/v2.0/indexers/xxx/results/torznab/). Lève ValueError."""
        import urllib.parse
        raw = (base_url or "").strip()
        if not raw:
            raise ValueError("Saisissez l'adresse de Jackett (ex. %s)" % IndexerManager.JACKETT_DEFAULT_URL)
        if "://" not in raw:
            raw = "http://" + raw
        try:
            parts = urllib.parse.urlsplit(raw)
        except ValueError:
            parts = None
        if parts is None or parts.scheme.lower() not in ("http", "https") or not parts.hostname:
            raise ValueError("Adresse de Jackett invalide (ex. %s)" % IndexerManager.JACKETT_DEFAULT_URL)
        key = ""
        for k, v in urllib.parse.parse_qsl(parts.query):
            if k.lower() == "apikey" and v.strip():
                key = v.strip()
        path = parts.path.rstrip("/")
        low = path.lower()
        idx = low.find("/api/v2.0")
        if idx >= 0:
            path = path[:idx]
        elif low.endswith("/ui/dashboard"):
            path = path[:-len("/ui/dashboard")]
        return urllib.parse.urlunsplit((parts.scheme.lower(), parts.netloc, path, "", "")).rstrip("/"), key

    def _jackett_list(self, base: str, key: str) -> List[Dict[str, str]]:
        """Configured Jackett indexers [{id, name}]: Torznab « t=indexers » on the « all » endpoint (API key),
        else the UI JSON API (works when Jackett has no admin password). Raises ValueError (French)."""
        from xml.etree import ElementTree as ET
        http = self.http if not is_local_url(base) else HttpClient(timeout=30, use_proxy=False)
        url = base + self._JACKETT_PATH % "all" + "/api"
        try:
            resp = http.get(url, params={"apikey": key, "t": "indexers", "configured": "true"},
                            raise_for_status=False, timeout=30)
        except HttpError as exc:
            raise ValueError("Jackett injoignable à %s (%s). Vérifiez l'adresse, par exemple %s si Jackett tourne "
                             "sur le NAS." % (base, exc, self.JACKETT_DEFAULT_URL))
        body = resp.body or b""
        if resp.status in (401, 403) or b"Invalid API Key" in body or b'code="100"' in body:
            raise ValueError("Clé API Jackett refusée (en haut à droite de la page Jackett : « API Key »)")
        out = []  # type: List[Dict[str, str]]
        if resp.ok:
            try:
                root = ET.fromstring(body)
                for el in root.iter():
                    if el.tag.rsplit("}", 1)[-1] != "indexer" or not el.get("id"):
                        continue
                    if (el.get("configured") or "true").lower() != "true":
                        continue
                    title = el.findtext("title") or el.get("id")
                    if el.get("id") != "all":
                        out.append({"id": el.get("id"), "name": title.strip()})
                if out or root.tag.rsplit("}", 1)[-1] == "indexers":
                    return out
            except ET.ParseError:
                pass
        # fallback: JSON UI API (no admin password set)
        try:
            resp = http.get(base + "/api/v2.0/indexers", params={"configured": "true", "apikey": key},
                            headers={"Accept": "application/json"}, raise_for_status=False, timeout=30)
            rows = resp.json() if resp.ok else None
        except (HttpError, ValueError):
            rows = None
        if isinstance(rows, list):
            for r in rows:
                if isinstance(r, dict) and r.get("id") and r.get("configured", True):
                    out.append({"id": str(r["id"]), "name": str(r.get("name") or r["id"])})
            return out
        if resp.status == 404:
            raise ValueError("Pas de Jackett à %s (HTTP 404) : vérifiez l'adresse, par exemple %s"
                             % (base, self.JACKETT_DEFAULT_URL))
        raise ValueError("Impossible de lister les indexers de Jackett à %s (HTTP %s)" % (base, resp.status))

    def import_jackett(self, base_url: str, api_key: str = "", remember: bool = True,
                       prune: bool = True) -> Dict[str, Any]:
        """Ajoute (ou met à jour) un indexer Torznab « <nom> (Jackett) » par indexer configuré dans Jackett.
        Même logique que import_prowlarr (Jackett n'a pas de priorité : 25 par défaut, modifiable ici)."""
        base, key_in_url = self.normalize_jackett_url(base_url)
        key = (api_key or "").strip() or key_in_url
        if not key:
            raise ValueError("Saisissez la clé API de Jackett (en haut à droite de la page Jackett : « API Key »)")
        items = self._jackett_list(base, key)
        by_url = {(r["url"] or "").rstrip("/").lower(): r for r in self.db.list_indexers()}
        added, updated, skipped = [], [], []  # type: List[str], List[str], List[str]
        active = []
        for it in items:
            name = "%s (Jackett)" % it["name"]
            url = base + self._JACKETT_PATH % it["id"] + "/"
            active.append(url)
            row = by_url.get(url.rstrip("/").lower()) or self.db.get_indexer(name)
            if row is not None and row.get("type") == "torznab":
                self.db.update_indexer(row["id"], url=url, api_key=key)  # priority: user's choice, kept
                updated.append(row["name"])
                continue
            if row is not None:
                skipped.append("%s : un indexer nommé « %s » existe déjà" % (it["name"], row["name"]))
                continue
            new_id = self.db.add_indexer(name, "torznab", url, key, "", priority=25)
            by_url[url.rstrip("/").lower()] = self.db.get_indexer(new_id) or {}
            added.append(name)
        disabled, reenabled = [], []  # type: List[str], List[str]
        if prune:
            disabled, reenabled = self._prune_source("jackett", base, active,
                                                     lambda u: "/api/v2.0/indexers/" in u)
        if remember:
            self.db.set_kv("jackett_url", base)
            self.db.set_kv("jackett_api_key", key)
        self.db.set_kv("jackett_last_sync", now_iso())
        self.db.set_kv("jackett_last_error", "")
        parts = ["%d ajouté(s)" % len(added), "%d mis à jour" % len(updated)]
        if disabled:
            parts.append("%d désactivé(s) (absents de Jackett)" % len(disabled))
        if reenabled:
            parts.append("%d réactivé(s)" % len(reenabled))
        if skipped:
            parts.append("%d ignoré(s)" % len(skipped))
        message = "Import Jackett : " + ", ".join(parts) if items else "Aucun indexer configuré dans Jackett"
        if self.db.get_kv("prowlarr_url") and remember:
            message += " — attention : Prowlarr est aussi synchronisé ; utilisez l'un OU l'autre (doublons)"
        if added or disabled or reenabled or not remember:
            self.db.add_event("indexer", message)
        return {"ok": True, "added": added, "updated": updated, "skipped": skipped, "disabled": disabled,
                "reenabled": reenabled, "message": message}

    def jackett_settings(self, cfg: Optional[Config] = None) -> Dict[str, Any]:
        url = (getattr(getattr(cfg, "jackett", None), "url", "") if cfg is not None else "") or \
            self.db.get_kv("jackett_url") or ""
        key = (getattr(getattr(cfg, "jackett", None), "api_key", "") if cfg is not None else "") or \
            self.db.get_kv("jackett_api_key") or ""
        hours = float(getattr(getattr(cfg, "prowlarr", None), "sync_interval_hours", 6.0)) if cfg is not None else 6.0
        return {"url": url, "configured": bool(url and key), "has_key": bool(key), "interval_hours": hours,
                "last_sync": self.db.get_kv("jackett_last_sync") or None,
                "last_error": self.db.get_kv("jackett_last_error") or None, "_key": key}

    def sync_jackett(self, cfg: Optional[Config] = None) -> Optional[Dict[str, Any]]:
        st = self.jackett_settings(cfg)
        if not st["configured"]:
            return None
        try:
            res = self.import_jackett(st["url"], st["_key"], remember=False, prune=True)
            log.info(res["message"])
            return res
        except Exception as exc:
            self.db.set_kv("jackett_last_error", "%s %s" % (now_iso(), exc))
            log.warning("Synchronisation Jackett impossible : %s", exc)
            return {"ok": False, "message": str(exc)}

    def forget_jackett(self) -> None:
        for k in ("jackett_url", "jackett_api_key", "jackett_last_sync", "jackett_last_error"):
            self.db.set_kv(k, None)

    def forget_prowlarr(self) -> None:
        for k in ("prowlarr_url", "prowlarr_api_key", "prowlarr_last_sync", "prowlarr_last_error"):
            self.db.set_kv(k, None)

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
    "update_check": "Vérifier les mises à jour",
    "update": "Mettre à jour synoarr",
    "update_rollback": "Revenir à la version précédente",
}
MAX_JOBS = 20
# statuts Download Station d'un téléchargement qui avance (rafraîchissement rapide de l'interface)
ACTIVE_DS_STATUSES = ("waiting", "downloading", "finishing", "hash_checking", "filehosting_waiting", "extracting")
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


# ---- watchlist add/delete: input checks and candidate merge
_IMDB_RE = re.compile(r"^tt\d+$", re.I)


def _check_kind(kind: Any, allow_none: bool = False) -> Optional[str]:
    k = str(kind or "").strip().lower()
    k = {"film": "movie", "movies": "movie", "serie": "show", "série": "show", "tv": "show",
         "series": "show", "séries": "show"}.get(k, k)
    if not k and allow_none:
        return None
    if k not in ("movie", "show"):
        raise ValueError("Type invalide : %s (film « movie » ou série « show »)" % (kind or "vide"))
    return k


def _check_year(year: Any) -> Optional[int]:
    if year in (None, ""):
        return None
    try:
        y = int(str(year).strip())
    except (TypeError, ValueError):
        raise ValueError("Année invalide : %s" % year)
    if not 1870 <= y <= 2200:
        raise ValueError("Année invalide : %s" % year)
    return y


def _to_year(year: Any) -> Optional[int]:
    """Année tolérante (None si absente ou invalide), pour comparer des lignes existantes."""
    try:
        return _check_year(year)
    except ValueError:
        return None


def _clean_fields(data: Any) -> Dict[str, Any]:
    """Validated copy of a candidate / manual entry. Raises ValueError (French message)."""
    if not isinstance(data, dict):
        raise ValueError("Entrée invalide : un objet {type, title, ...} est attendu")
    title = " ".join(str(data.get("title") or "").split())
    if not title:
        raise ValueError("Le titre est obligatoire")
    out = {"type": _check_kind(data.get("type")), "title": title[:300],
           "year": _check_year(data.get("year"))}  # type: Dict[str, Any]
    imdb = str(data.get("imdb") or "").strip().lower()
    if imdb:
        if imdb.isdigit():
            imdb = "tt" + imdb
        if not _IMDB_RE.match(imdb):
            raise ValueError("Identifiant IMDb invalide : %s (ex. tt0133093)" % data.get("imdb"))
    out["imdb"] = imdb or None
    for key, name in (("tmdb", "TMDb"), ("tvdb", "TVDB")):
        val = str(data.get(key) or "").strip()
        if val and not val.isdigit():
            raise ValueError("Identifiant %s invalide : %s" % (name, val))
        out[key] = val or None
    rk = str(data.get("rating_key") or "").strip()
    if rk and (rk.startswith("local:") or not re.match(r"^[A-Za-z0-9_.-]{1,100}$", rk)):
        raise ValueError("Identifiant Plex invalide : %s" % rk)
    out["rating_key"] = rk or None
    orig = " ".join(str(data.get("original_title") or "").split())
    out["original_title"] = orig if orig and orig != title else None
    return out


def _merge_candidates(plex: List[Dict[str, Any]], tmdb: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Plex candidates first (they can join the Plex Watchlist); a TMDb result describing the same title
    (same tmdb/imdb id, or same type + title + year when Plex gave no id) only completes it."""
    from .parser import normalize_title
    out = [dict(c) for c in plex]
    for t in tmdb:
        match = None
        for c in out:
            if c.get("source") != "plex" or c.get("type") != t.get("type"):
                continue
            if (t.get("tmdb") and c.get("tmdb") == t.get("tmdb")) or \
                    (t.get("imdb") and c.get("imdb") == t.get("imdb")):
                match = c
                break
            if not c.get("tmdb") and not c.get("imdb"):
                names = {normalize_title(c.get("title") or ""), normalize_title(c.get("original_title") or "")}
                same_title = normalize_title(t.get("title") or "") in names or \
                    (t.get("original_title") and normalize_title(t["original_title"]) in names)
                if same_title and (not c.get("year") or not t.get("year") or abs(c["year"] - t["year"]) <= 1):
                    match = c
                    break
        if match is None:
            out.append(dict(t))
            continue
        for key in ("tmdb", "imdb", "tvdb", "year", "overview", "poster_url", "original_title"):
            if not match.get(key) and t.get(key):
                match[key] = t[key]
    return out
# ---- end watchlist add/delete helpers


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
        elif command in ("update_check", "update", "update_rollback"):
            target, args, key = self._cmd_update, (command,), "update"  # one update job at a time
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

    def _cmd_update(self, job: Job, command: str) -> Dict[str, Any]:
        from .updater import UpdateError, make_updater
        upd = make_updater(self.cfg, self.http)
        try:
            if command == "update_check":
                return upd.check(job.emit)
            if command == "update_rollback":
                res = upd.rollback(job.emit)
                changed = True
            else:
                res = upd.apply(job.emit)
                changed = bool(res.get("updated"))
        except UpdateError as exc:
            job.emit("err", str(exc))
            return {"ok": False, "message": str(exc)}
        if changed:
            self.db.add_event("update", "synoarr mis à jour (%s) : redémarrage" % command)
            job.emit("info", "Redémarrage de synoarr dans quelques secondes (la page se recharge toute seule)")
            self.restart(delay=2.0)
            res["restarting"] = True
        return res

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
        prios = self.indexer_manager.priorities()
        results = []
        for i, (rel, score, reason) in enumerate(report[:200]):
            results.append({"index": i, "title": rel.title, "size": rel.size or 0, "seeders": rel.seeders,
                            "leechers": rel.leechers, "indexer": rel.indexer, "priority": prios.get(rel.indexer),
                            "score": score, "reason": reason})
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

    # ------------------------------------- téléchargements en cours / changer de source (onglet Watchlist)
    DOWNLOADS_CACHE_SECONDS = 5.0     # l'interface se rafraîchit toutes les 5 à 15 s : DSM n'est pas sollicité plus
    DOWNLOADS_ERROR_CACHE_SECONDS = 30.0
    DOWNLOADS_TIMEOUT = 6.0  # listing de la progression : un DSM muet ne bloque jamais la Watchlist longtemps
    _dl_lock = threading.Lock()  # protège le cache ci-dessous ; jamais tenu pendant un appel réseau
    _dl_cache = None  # type: Optional[Tuple[Any, float, Optional[List[Any]], Optional[str]]]
    _dl_gen = 0  # incrémenté par invalidate_downloads : un rafraîchissement plus ancien n'écrit pas le cache
    _dl_refreshing = None  # type: Optional[Tuple[Any, object]]  # (ds, jeton) du rafraîchissement en cours
    _dl_client = None  # type: Optional[Tuple[Tuple[Any, ...], Any]]  # (clé, client DS à délai court)

    def invalidate_downloads(self) -> None:
        with self._dl_lock:
            self._dl_cache = None
            self._dl_gen += 1

    def _ds_progress_client(self, ds: Any) -> Any:
        """Client Download Station dédié à la progression (mêmes identifiants, délai court, sans nouvelle
        tentative). Appelé sous _dl_lock. Les doublures de test sans quick_client() sont utilisées telles quelles."""
        key = (id(ds), getattr(ds, "url", None), getattr(ds, "username", None), getattr(ds, "password", None))
        cached = self._dl_client
        if cached is not None and cached[0] == key:
            return cached[1]
        make = getattr(ds, "quick_client", None)
        client = ds
        if callable(make):
            try:
                client = make(self.DOWNLOADS_TIMEOUT)
            except Exception as exc:  # pragma: no cover - défensif
                log.debug("Client Download Station rapide impossible (%s) : client principal utilisé", exc)
        self._dl_client = (key, client)
        return client

    def _ds_tasks(self, force: bool = False) -> Tuple[Optional[List[Any]], Optional[str]]:
        """(tâches Download Station, avertissement) avec un cache court. tâches None = DS indisponible.
        Si un rafraîchissement est déjà en cours (DSM lent), renvoie aussitôt le cache précédent, ou
        « inconnu » + un avertissement, au lieu d'attendre (sauf *force*, qui interroge DSM lui-même)."""
        ds = self.ds
        if ds is None:
            return None, "Download Station n'est pas configuré : progression des téléchargements indisponible"
        with self._dl_lock:
            cache = self._dl_cache
            usable = cache is not None and cache[0] is ds
            if usable and not force:
                ttl = self.DOWNLOADS_CACHE_SECONDS if cache[3] is None else self.DOWNLOADS_ERROR_CACHE_SECONDS
                if time.time() - cache[1] < ttl:
                    return cache[2], cache[3]
            if not force and self._dl_refreshing is not None and self._dl_refreshing[0] is ds:
                if usable:
                    return cache[2], cache[3]
                return None, "Download Station ne répond pas encore : progression des téléchargements inconnue"
            token = object()
            self._dl_refreshing = (ds, token)
            gen = self._dl_gen
            client = self._ds_progress_client(ds)
        try:
            tasks = list(client.list_tasks() or [])  # type: Optional[List[Any]]
            warning = None  # type: Optional[str]
        except Exception as exc:
            log.debug("Download Station injoignable pour la progression : %s", exc)
            tasks, warning = None, "Download Station injoignable : %s" % exc
        with self._dl_lock:
            if self._dl_refreshing is not None and self._dl_refreshing[1] is token:
                self._dl_refreshing = None
            if self._dl_gen == gen and self.ds is ds:
                self._dl_cache = (ds, time.time(), tasks, warning)
        return tasks, warning

    @staticmethod
    def _download_entry(g: Dict[str, Any], task: Any, medias: Dict[int, Dict[str, Any]],
                        ds_known: bool) -> Dict[str, Any]:
        media = medias.get(g.get("media_id") or -1) or {}
        d = {"grab_id": g["id"], "media_id": g.get("media_id"), "media_title": media.get("title"),
             "media_type": media.get("type"), "release_title": g.get("release_title"), "season": g.get("season"),
             "episode": g.get("episode"), "indexer": g.get("indexer"), "grab_status": g.get("status"),
             "created_at": g.get("created_at"), "status": "inconnu", "active": False, "progress": None,
             "size": g.get("size") or 0, "downloaded": None, "uploaded": None, "speed_down": None,
             "speed_up": None, "seeders": None, "leechers": None, "peers": None, "eta_seconds": None,
             "task_id": None, "error": g.get("message") if g.get("status") == "failed" else None}
        if g.get("status") == "failed":
            d["status"] = "failed"
            return d
        if task is None:
            if ds_known:
                d["status"] = "introuvable"
            return d
        size = int(task.size or 0) or int(g.get("size") or 0)
        downloaded = int(task.downloaded or 0)
        speed = task.speed_down
        eta = None
        if speed and speed > 0 and size > downloaded and task.status == "downloading":
            eta = int((size - downloaded) / speed)
        d.update({"status": task.status, "progress": round(task.progress, 1), "size": size,
                  "downloaded": downloaded, "uploaded": task.uploaded, "speed_down": speed,
                  "speed_up": task.speed_up, "seeders": task.seeders, "leechers": task.leechers,
                  "peers": task.peers, "eta_seconds": eta, "task_id": task.id, "error": task.error,
                  "active": task.status in ACTIVE_DS_STATUSES})
        return d

    def _relevant_failed(self, sent: List[Dict[str, Any]], medias: Dict[int, Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Derniers échecs encore utiles à afficher (élément toujours recherché, aucun autre envoi en cours)."""
        out, seen = [], set()
        wanted_eps = {}  # type: Dict[int, List[Dict[str, Any]]]
        for g in self.db.list_grabs(status="failed", limit=200):
            media = medias.get(g.get("media_id") or -1)
            key = (g.get("media_id"), g.get("season"), g.get("episode"))
            if media is None or key in seen:
                continue
            seen.add(key)
            covered = any(s.get("media_id") == media["id"] and (
                media["type"] == "movie" or (s.get("season") == g.get("season") and
                                             (s.get("episode") is None or s.get("episode") == g.get("episode"))))
                for s in sent)
            if covered or media.get("status") == "ignored":
                continue
            if media["type"] == "movie":
                if media.get("status") != "wanted":
                    continue
            else:
                if g.get("season") is None:
                    continue
                if media["id"] not in wanted_eps:
                    wanted_eps[media["id"]] = self.db.list_episodes(media["id"])
                eps = [e for e in wanted_eps[media["id"]] if e["season"] == g["season"]
                       and (g.get("episode") is None or e["episode"] == g["episode"])]
                if eps and not any(e["status"] == "wanted" for e in eps):
                    continue
            out.append(g)
        return out

    def downloads_status(self, force: bool = False) -> Dict[str, Any]:
        """Téléchargements envoyés (grabs « sent ») avec leur progression Download Station, plus les échecs
        récents encore recherchés. Ne lève pas : DS injoignable -> statut « inconnu » + un avertissement."""
        from .searcher import TaskIndex
        tasks, warning = self._ds_tasks(force)
        index = TaskIndex(tasks) if tasks is not None else None
        medias = {m["id"]: m for m in self.db.list_media()}
        sent = self.db.list_grabs(status="sent", limit=500)
        items = [self._download_entry(g, index.find(g) if index is not None else None, medias, index is not None)
                 for g in sent]
        failed = [self._download_entry(g, None, medias, False) for g in self._relevant_failed(sent, medias)]
        return {"ok": warning is None, "warning": warning, "items": items, "failed": failed,
                "active": sum(1 for d in items if d["active"]), "updated_at": now_iso()}

    @staticmethod
    def _target_label(media: Dict[str, Any], season: Optional[int], episode: Optional[int]) -> str:
        label = media["title"]
        if season is not None and episode is not None:
            label += " S%02dE%02d" % (season, episode)
        elif season is not None:
            label += " saison %d" % season
        return label

    def _source_target(self, media_id: Any, season: Any, episode: Any) -> Tuple[Dict[str, Any], Optional[int],
                                                                                Optional[int]]:
        try:
            media = self.db.get_media(int(media_id))
        except (TypeError, ValueError):
            media = None
        if media is None:
            raise ValueError("Élément de la Watchlist introuvable : %s" % media_id)
        if media["type"] == "movie":
            return media, None, None
        values = []
        for name, v in (("saison", season), ("épisode", episode)):
            if v in (None, ""):
                values.append(None)
                continue
            try:
                n = int(v) if not isinstance(v, bool) else -1
            except (TypeError, ValueError):
                n = -1
            if n < 0 or n > 10000:
                raise ValueError("Numéro de %s invalide : %s" % (name, v))
            values.append(n)
        if values[0] is None:
            raise ValueError("Précisez la saison (et l'épisode) de la série")
        return media, values[0], values[1]

    def start_source_search(self, media_id: Any, season: Any = None, episode: Any = None) -> Tuple[Job, bool]:
        """Recherche de toutes les sources (acceptées ET rejetées) pour exactement ce film / cette saison / cet
        épisode, dans une tâche (GET /api/jobs/<id>). Lève ValueError."""
        media, season, episode = self._source_target(media_id, season, episode)
        req = self.searcher.request_for(media, season, episode)
        target = (media["id"], season, episode)

        def prepare(job: Job) -> None:
            job.private["target"] = target
        return self._start_job("sources", "Sources : %s" % self._target_label(media, season, episode),
                               "sources:%s:%s:%s" % target, self._cmd_sources, (target, req), True, prepare)

    def _cmd_sources(self, job: Job, target: Tuple[int, Optional[int], Optional[int]], req: Any) -> Dict[str, Any]:
        job.emit("info", "Recherche : %s (langues : %s)" % (req.describe(), ", ".join(req.languages) or "—"))
        if not self.db.list_indexers(enabled_only=True):
            job.emit("warn", "Aucun indexer actif : ajoutez-en un depuis l'onglet Indexers")
        report = self.searcher.search_report(req)[:300]
        prios = self.indexer_manager.priorities()
        job.private["releases"] = [rel for rel, _s, _r in report]
        job.private["verdicts"] = [(score, reason) for _rel, score, reason in report]
        results = []
        for i, (rel, score, reason) in enumerate(report):
            results.append({"index": i, "title": rel.title, "size": rel.size or 0, "seeders": rel.seeders,
                            "leechers": rel.leechers, "indexer": rel.indexer, "priority": prios.get(rel.indexer),
                            "score": score, "reason": reason, "magnet": bool(rel.magnet),
                            "torrent": bool(rel.download_url)})
        accepted = sum(1 for r in results if r["score"] is not None)
        job.emit("ok" if accepted else "warn", "%d résultat(s), dont %d acceptable(s)" % (len(results), accepted))
        return {"media_id": target[0], "season": target[1], "episode": target[2], "request": req.describe(),
                "kind": req.kind, "languages": list(req.languages), "total": len(results), "results": results}

    def replace_source(self, media_id: Any, season: Any = None, episode: Any = None, grab_id: Any = None,
                       job_id: Any = None, index: Any = None, link: Optional[str] = None,
                       torrent: Optional[bytes] = None, delete_old: bool = True, blocklist_old: bool = False,
                       accept_rejected: bool = False) -> Dict[str, Any]:
        """Envoie une autre source pour un film / une saison / un épisode : résultat de start_source_search
        (*job_id* + *index*), lien magnet ou URL .torrent (*link*) ou fichier .torrent (*torrent*). La tâche
        Download Station de l'ancien téléchargement (*grab_id*) est supprimée si *delete_old* et sa release
        bloquée si *blocklist_old* ; il passe en échec « remplacé manuellement » si sa tâche est supprimée ou
        introuvable, sinon (tâche conservée) il reste suivi et sera importé normalement. Lève ValueError (saisie
        invalide) ; renvoie {"ok": False, "message"} si l'envoi échoue (l'ancien téléchargement est conservé)."""
        from . import bencode
        old = None
        if grab_id not in (None, ""):
            try:
                old = self.db.query_one("SELECT * FROM grabs WHERE id = ?", (int(grab_id),))
            except (TypeError, ValueError):
                old = None
            if old is None or str(old.get("media_id")) != str(media_id):
                raise ValueError("Téléchargement introuvable pour cet élément (n° %s)" % grab_id)
            season, episode = old.get("season"), old.get("episode")
        media, season, episode = self._source_target(media_id, season, episode)
        chosen = [x for x in (job_id not in (None, ""), bool((link or "").strip()), torrent is not None) if x]
        if len(chosen) != 1:
            raise ValueError("Choisissez une seule source : un résultat de la recherche, un lien ou un fichier .torrent")
        label = self._target_label(media, season, episode)
        release = None
        if job_id not in (None, ""):
            try:
                job = self.get_job(int(job_id))
            except (TypeError, ValueError):
                job = None
            releases = job.private.get("releases") if job is not None and job.command == "sources" else None
            if not releases:
                raise ValueError("Résultats de recherche introuvables : relancez la recherche")
            if job.private.get("target") != (media["id"], season, episode):
                raise ValueError("Ces résultats concernent un autre élément : relancez la recherche")
            if isinstance(index, bool) or not isinstance(index, int) or not 0 <= index < len(releases):
                raise ValueError("Résultat introuvable (n° %s)" % index)
            release = releases[index]
            score, reason = job.private["verdicts"][index]
            if score is None and not accept_rejected:
                raise ValueError("Cette release a été rejetée (%s) : confirmez pour l'envoyer quand même" % reason)
        elif torrent is None:
            release = self.searcher.release_from_link(link or "", label)
        else:
            try:
                bencode.torrent_info(torrent)
            except (ValueError, TypeError):
                raise ValueError("Fichier .torrent invalide (illisible ou ce n'est pas un torrent)")
        if self.cfg.general.dry_run:
            if release is not None:
                self.searcher.grab(release, media, season, episode)  # journalise « [dry-run] »
            else:
                self.searcher.grab_torrent_bytes(torrent or b"", media, season, episode)
            return {"ok": False, "dry_run": True,
                    "message": "Mode simulation (dry_run) actif : rien n'a été envoyé ni supprimé"}
        if self.ds is None:
            raise ValueError("Download Station n'est pas configuré ([downloadstation] username/password)")
        old_active = old is not None and old.get("status") == "sent"
        old_task = None
        tasks_known = False
        if old_active:
            from .searcher import TaskIndex
            tasks, _warning = self._ds_tasks(force=True)
            tasks_known = tasks is not None
            old_task = TaskIndex(tasks).find(old) if tasks is not None else None
        # L'ancien envoi ne passe en échec (info hash libéré : renvoi du même torrent possible) que si sa tâche
        # va être supprimée ou est introuvable. Une tâche conservée reste « sent » : toujours suivie (progression)
        # et importée normalement, et son info hash n'est pas renvoyé une seconde fois à Download Station.
        retire_old = old_active and (delete_old or (tasks_known and old_task is None))
        if retire_old:
            self.db.set_grab_status(old["id"], "failed", "remplacé manuellement")
        errors = []  # type: List[str]
        last_id = (self.db.query_one("SELECT MAX(id) AS m FROM grabs") or {}).get("m") or 0
        try:
            if release is not None:
                ok = self.searcher.grab(release, media, season, episode, error_out=errors)
            else:
                ok = self.searcher.grab_torrent_bytes(torrent or b"", media, season, episode, error_out=errors)
        except BaseException:
            if retire_old:
                self.db.set_grab_status(old["id"], "sent", old.get("message"))
            raise
        if not ok:
            if retire_old:
                self.db.set_grab_status(old["id"], "sent", old.get("message"))
            self.invalidate_downloads()
            return {"ok": False, "message": "Échec de l'envoi à Download Station : %s" % (
                errors[0] if errors else "voir le journal")}
        new = self.db.query_one("SELECT * FROM grabs WHERE id > ? ORDER BY id DESC LIMIT 1", (last_id,)) or {}
        notes = []
        if old_active and delete_old:
            if old_task is None:
                notes.append("ancienne tâche introuvable dans Download Station")
            else:
                try:
                    self.ds.delete_task(old_task.id)
                    notes.append("ancienne tâche supprimée de Download Station")
                except Exception as exc:
                    log.warning("Suppression de la tâche %s impossible : %s", old_task.id, exc)
                    notes.append("suppression de l'ancienne tâche impossible : %s" % exc)
                    # la tâche continue : l'ancien envoi reste suivi
                    self.db.set_grab_status(old["id"], "sent", old.get("message"))
        elif old_active and old_task is not None:
            notes.append("ancienne tâche conservée dans Download Station (toujours suivie)")
        if old is not None and blocklist_old:
            self.db.block_release(old["release_title"], "remplacé manuellement")
            notes.append("ancienne release bloquée")
        if media["type"] == "movie":
            self.db.execute("UPDATE media SET status = 'grabbed', updated_at = ? WHERE id = ? "
                            "AND status IN ('wanted', 'grabbed')", (now_iso(), media["id"]))
        else:
            self.db.set_episode_status(media["id"], season, episode, "grabbed", only_if=["wanted"])
        title = new.get("release_title") or (release.title if release is not None else "fichier .torrent")
        self.db.add_event("grab", "%s pour %s : %s" % (
            "Source remplacée manuellement" if old is not None else "Source choisie manuellement", label, title))
        self.invalidate_downloads()
        msg = "Envoyé à Download Station : %s (%s)" % (title, label)
        if notes:
            msg += " — " + ", ".join(notes)
        return {"ok": True, "message": msg, "grab_id": new.get("id"), "notes": notes}

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

    # ---- watchlist add/delete (API documented at the top of this module)
    _SEARCH_LIMIT = 30

    def search_titles(self, query: str, kind: Optional[str] = None, year: Any = None) -> Dict[str, Any]:
        """Candidats à ajouter : recherche Plex discover + TMDb, fusionnés. Ne lève que ValueError (saisie)."""
        query = (query or "").strip()
        if not query:
            raise ValueError("Saisissez un titre à rechercher")
        kind = _check_kind(kind, allow_none=True)
        year = _check_year(year)
        warnings = []  # type: List[str]
        plex_cands = []  # type: List[Dict[str, Any]]
        if self.plex is None:
            warnings.append("Plex non configuré : résultats TMDb uniquement (ajout en entrée locale)")
        else:
            try:
                plex_cands = self.plex.search(query, kind)
            except Exception as exc:
                log.warning("Recherche Plex impossible pour « %s » : %s", query, exc)
                warnings.append("Recherche Plex impossible : %s" % exc)
        if year:
            plex_cands = [c for c in plex_cands if not c.get("year") or abs(int(c["year"]) - year) <= 1]
        tmdb_cands = []  # type: List[Dict[str, Any]]
        if self.tmdb is not None and self.tmdb.enabled:
            try:
                tmdb_cands = self.tmdb.search_multi(query, kind, year)
            except Exception as exc:  # pragma: no cover - search_multi never raises
                warnings.append("Recherche TMDb impossible : %s" % exc)
        elif not plex_cands and self.plex is None:
            warnings.append("Ni Plex ni TMDb ([tmdb] api_key) ne sont configurés : utilisez l'ajout manuel")
        results = _merge_candidates(plex_cands, tmdb_cands)[:self._SEARCH_LIMIT]
        for c in results:
            existing = self._find_existing(c, by_title=False)  # ids only: fast (add_to_watchlist also matches titles)
            visible = bool(existing) and not existing.get("hidden") and bool(existing.get("on_watchlist"))
            c["in_watchlist"] = visible
            c["media_id"] = existing["id"] if visible else None
        return {"query": query, "results": results, "warnings": warnings}

    def _find_existing(self, fields: Dict[str, Any], by_title: bool = True) -> Optional[Dict[str, Any]]:
        row = None
        if fields.get("rating_key"):
            row = self.db.get_media_by_key(str(fields["rating_key"]))
        if row is None:
            row = self.db.find_media_by_ids(fields.get("type"), fields.get("imdb"), fields.get("tmdb"),
                                            fields.get("tvdb"))
        if row is None and by_title:
            row = self._find_by_title(fields)
        return row

    def _find_by_title(self, fields: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Ligne de même type et même titre (titre original compris) qu'un ajout sans correspondance d'id,
        lignes masquées comprises : évite un doublon (et donc un second téléchargement) quand « Dune (2021) »
        venu de Plex est ajouté à la main. Années ±1 si les deux sont connues ; une seule connue : un film
        dont l'ajout précise l'année n'est pas rapproché d'une ligne sans année, une série si. Deux ids
        différents du même type (imdb, tmdb, tvdb) = deux œuvres distinctes (remake homonyme)."""
        from .parser import titles_match
        type_, title = fields.get("type"), (fields.get("title") or "").strip()
        if type_ not in ("movie", "show") or not title:
            return None
        names = [title] + ([fields["original_title"]] if fields.get("original_title") else [])
        year = _to_year(fields.get("year"))
        best = None  # type: Optional[Tuple[Tuple[int, int, int], Dict[str, Any]]]
        for row in self.db.list_media(type_, include_hidden=True):
            if any(fields.get(k) and row.get(k) and str(fields[k]) != str(row[k]) for k in ("imdb", "tmdb", "tvdb")):
                continue
            alts = [row["original_title"]] if row.get("original_title") else []
            if not any(titles_match(n, row.get("title") or "", alts) for n in names):
                continue
            ryear = _to_year(row.get("year"))
            if year and ryear:
                if abs(year - ryear) > 1:
                    continue
            elif type_ == "movie" and year and not ryear:
                continue
            rank = (0 if year and ryear == year else 1, 1 if row.get("hidden") else 0, row["id"])
            if best is None or rank < best[0]:
                best = (rank, row)
        return best[1] if best is not None else None

    def add_to_watchlist(self, candidate: Dict[str, Any], add_to_plex: bool = True) -> Dict[str, Any]:
        """Ajoute un candidat (search_titles) ou des champs saisis. Retourne la ligne media + "plex_added",
        "already_present", "warning". Lève ValueError (saisie invalide)."""
        f = _clean_fields(candidate)
        warning = None  # type: Optional[str]
        plex_added = None  # type: Optional[bool]
        rk = f.get("rating_key")
        label = "%s%s" % (f["title"], " (%s)" % f["year"] if f.get("year") else "")

        def plex_add(key: str) -> Optional[str]:
            """None when added to the Plex Watchlist, else a French warning."""
            if self.plex is None:
                return "Plex non configuré : « %s » est suivi en entrée locale (hors Watchlist Plex)" % label
            try:
                self.plex.add_to_watchlist(key)
                return None
            except Exception as exc:
                log.warning("Ajout de « %s » à la Watchlist Plex impossible : %s", label, exc)
                return ("Ajout à la Watchlist Plex impossible (%s) : « %s » est suivi en entrée locale"
                        % (exc, label))

        if f.get("tmdb") and not f.get("imdb") and self.tmdb is not None and self.tmdb.enabled:
            # IMDb/TVDB ids: better indexer searches and no duplicate of an entry known by its IMDb id
            try:
                for k, v in self.tmdb.external_ids(f["tmdb"], f["type"]).items():
                    if not f.get(k):
                        f[k] = v
            except Exception as exc:  # pragma: no cover - never raises
                log.debug("Identifiants TMDb indisponibles : %s", exc)
        existing = self._find_existing(f)
        if existing is not None:
            is_local = str(existing["plex_key"]).startswith("local:")
            plex_key = rk or (None if is_local else existing["plex_key"])
            changed = bool(existing.get("hidden")) or existing["status"] == "ignored"
            if existing.get("hidden"):
                self.db.set_media_hidden(existing["id"], False)  # still in the Plex Watchlist (or local)
            elif not existing.get("on_watchlist") or (is_local and rk and add_to_plex):
                changed = True
                # off the Plex Watchlist, or a local entry that can now join it
                if plex_key and add_to_plex:
                    warning = plex_add(plex_key)
                    plex_added = warning is None
                if plex_added:
                    self.db.set_media_key(existing["id"], plex_key)
                elif not is_local:
                    self.db.set_media_key(existing["id"], self._local_key(f))
                else:
                    self.db.execute("UPDATE media SET on_watchlist = 1 WHERE id = ?", (existing["id"],))
            if existing["status"] == "ignored":
                self.db.set_media_status(existing["id"], "wanted" if existing["type"] == "movie" else "monitored")
            if changed:
                self.db.add_event("watchlist", "Réactivé : %s" % label)
            row = self.db.get_media(existing["id"]) or existing
            row.update({"plex_added": plex_added, "already_present": True, "reactivated": changed,
                        "warning": warning})
            return row

        media = None  # type: Optional[Dict[str, Any]]
        if rk and add_to_plex:
            warning = plex_add(rk)
            plex_added = warning is None
            if plex_added:
                from .models import WatchItem
                guids = {k: f[k] for k in ("imdb", "tmdb", "tvdb") if f.get(k)}
                item = WatchItem(rating_key=rk, type=f["type"], title=f["title"], year=f.get("year"), guids=guids,
                                 original_title=f.get("original_title"))
                try:
                    media = self.searcher.sync_plex_item(item)
                except Exception as exc:
                    log.warning("Import de « %s » incomplet (%s) : la prochaine synchronisation le terminera",
                                label, exc)
                if media is None:
                    mid = self.db.upsert_media(rk, f["type"], f["title"], f.get("year"), f.get("imdb"),
                                               f.get("tmdb"), f.get("tvdb"), f.get("original_title"))
                    media = self.db.get_media(mid)
                self.db.add_event("watchlist", "Ajouté à la Watchlist Plex : %s" % label)
        if media is None:
            media = self._add_local(f, label)
        assert media is not None
        row = self.db.get_media(media["id"]) or media
        if row.get("hidden") and row.get("tmdb") and str(row.get("plex_key")).startswith("local:"):
            # masked by the local sync: same TMDb id as another entry -> drop the brand-new duplicate
            other = self.db.find_media_by_ids(row["type"], tmdb=row["tmdb"], include_hidden=False)
            if other is not None and other["id"] != row["id"]:
                self.db.delete_media(row["id"])
                kept = self.db.get_media(other["id"]) or other
                kept.update({"plex_added": None, "already_present": True, "reactivated": False,
                             "warning": warning})
                return kept
        row.update({"plex_added": plex_added, "already_present": False, "reactivated": False, "warning": warning})
        return row

    def _local_key(self, f: Dict[str, Any]) -> str:
        """plex_key of a local entry: "local:tmdb:<type>:<id>", "local:imdb:<tt>" or "local:<uuid>"."""
        key = None
        if f.get("tmdb"):
            key = "local:tmdb:%s:%s" % (f["type"], f["tmdb"])
        elif f.get("imdb"):
            key = "local:imdb:%s" % f["imdb"]
        if key is None or self.db.get_media_by_key(key) is not None:
            key = "local:" + uuid.uuid4().hex
        return key

    def _add_local(self, f: Dict[str, Any], label: str) -> Optional[Dict[str, Any]]:
        key = self._local_key(f)
        mid = self.db.upsert_media(key, f["type"], f["title"], f.get("year"), f.get("imdb"), f.get("tmdb"),
                                   f.get("tvdb"), f.get("original_title"))
        self.db.add_event("watchlist", "Ajouté manuellement (entrée locale) : %s" % label)
        media = self.db.get_media(mid)
        if media is not None:
            try:
                self.searcher.sync_local_item(media)
            except Exception as exc:
                log.warning("Détails de « %s » indisponibles pour l'instant (%s)", label, exc)
        return media

    def add_manual(self, type_: str, title: str, year: Any = None, imdb: Optional[str] = None,
                   tmdb: Optional[str] = None) -> Dict[str, Any]:
        """Entrée locale saisie à la main (sans recherche). Lève ValueError."""
        return self.add_to_watchlist({"type": type_, "title": title, "year": year, "imdb": imdb, "tmdb": tmdb},
                                     add_to_plex=False)

    def remove_from_watchlist(self, media_id: Any, remove_from_plex: bool = True,
                              delete_downloads: bool = False) -> Dict[str, Any]:
        """Retire une entrée. Retourne {"removed", "plex_removed", "warning", "downloads_deleted"}.
        Lève ValueError si l'entrée n'existe pas."""
        try:
            media = self.db.get_media(int(media_id))
        except (TypeError, ValueError):
            media = None
        if media is None:
            raise ValueError("Élément introuvable : %s" % media_id)
        label = "%s%s" % (media["title"], " (%s)" % media["year"] if media.get("year") else "")
        warnings = []  # type: List[str]
        deleted = 0
        if delete_downloads:
            deleted, ds_warning = self._delete_media_downloads(media)
            if ds_warning:
                warnings.append(ds_warning)
        is_local = str(media["plex_key"]).startswith("local:")
        plex_removed = None  # type: Optional[bool]
        keep_hidden = False
        if not is_local and media.get("on_watchlist"):
            if not remove_from_plex:
                keep_hidden = True
                warnings.append("« %s » reste dans votre Watchlist Plex : il est masqué et ignoré par synoarr "
                                "(retirez-le aussi de la Watchlist Plex pour l'oublier définitivement)" % label)
            elif self.plex is None:
                keep_hidden = True
                plex_removed = False
                warnings.append("Plex non configuré : « %s » reste dans votre Watchlist Plex, il est masqué et "
                                "ignoré par synoarr" % label)
            else:
                try:
                    self.plex.remove_from_watchlist(media["plex_key"])
                    plex_removed = True
                except Exception as exc:
                    log.warning("Retrait de « %s » de la Watchlist Plex impossible : %s", label, exc)
                    plex_removed = False
                    keep_hidden = True
                    warnings.append("Retrait de la Watchlist Plex impossible (%s) : « %s » y reste, il est masqué "
                                    "et ignoré par synoarr (retirez-le de la Watchlist Plex depuis Plex)"
                                    % (exc, label))
        if keep_hidden:
            self.db.set_media_status(media["id"], "ignored")
            self.db.set_media_hidden(media["id"], True)
        else:
            self.db.delete_media(media["id"])
        self.db.add_event("watchlist", "Retiré de la Watchlist : %s%s" % (
            label, " (retiré de Plex)" if plex_removed else ""))
        return {"removed": True, "plex_removed": plex_removed, "warning": " ; ".join(warnings) or None,
                "downloads_deleted": deleted}

    def _delete_media_downloads(self, media: Dict[str, Any]) -> Tuple[int, Optional[str]]:
        """Supprime les tâches Download Station des envois en cours de *media* (jamais de fichier de la
        bibliothèque). Retourne (nombre de tâches supprimées, avertissement)."""
        from .parser import normalize_title
        from .searcher import _fold, _strip_torrent_ext
        grabs = self.db.query("SELECT * FROM grabs WHERE media_id = ? AND status = 'sent'", (media["id"],))
        if not grabs:
            return 0, None
        if self.ds is None:
            return 0, "Download Station non configuré : téléchargements en cours non supprimés"
        try:
            tasks = self.ds.list_tasks()
        except Exception as exc:
            return 0, "Download Station injoignable (%s) : téléchargements en cours non supprimés" % exc
        by_title, by_norm = {}, {}  # type: Dict[str, Any], Dict[str, Any]
        for t in tasks:
            by_title.setdefault(_fold(t.title or ""), t)
            by_norm.setdefault(normalize_title(_strip_torrent_ext(t.title or "")), t)
        deleted, failed = 0, []  # type: int, List[str]
        done_ids = set()
        for g in grabs:
            keys = [k for k in (g.get("torrent_name"), g.get("release_title")) if k]
            task = next((by_title[_fold(k)] for k in keys if _fold(k) in by_title), None)
            if task is None:
                task = next((by_norm[normalize_title(_strip_torrent_ext(k))] for k in keys
                             if normalize_title(_strip_torrent_ext(k)) in by_norm), None)
            if task is None:
                continue  # no task (finished and removed?): the organizer may still import it
            if task.id not in done_ids:
                try:
                    self.ds.delete_task(task.id)
                except Exception as exc:
                    failed.append("%s (%s)" % (g["release_title"], exc))
                    continue
                done_ids.add(task.id)
                deleted += 1
            self.db.set_grab_status(g["id"], "failed", "supprimé avec l'entrée")
        if deleted:
            self.db.add_event("download", "%d téléchargement(s) supprimé(s) de Download Station avec %s"
                              % (deleted, media["title"]))
        warning = ("Suppression impossible dans Download Station : %s" % ", ".join(failed)) if failed else None
        return deleted, warning
    # ---- end watchlist add/delete

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
        next_prowlarr = time.time() + 1  # before the first search: indexers are up to date
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
                prowlarr_hours = float(self.cfg.prowlarr.sync_interval_hours or 0)
                if prowlarr_hours > 0 and now >= next_prowlarr and not self._stop.is_set():
                    next_prowlarr = time.time() + max(0.25, prowlarr_hours) * 3600
                    self._start_prowlarr_sync()
                if now >= next_watch and not self._stop.is_set():
                    self._start_watchlist_thread()
                    next_watch = time.time() + watch_every
            except Exception as exc:  # pragma: no cover - jobs already catch everything; never die
                log.exception("Boucle principale : erreur inattendue : %s", exc)
            self._stop.wait(2)
        self.shutdown()

    def _start_prowlarr_sync(self) -> bool:
        """Synchronise the Prowlarr indexers in a background thread (no-op if not configured or running)."""
        if getattr(self, "_prowlarr_running", False):
            return False
        mgr = self.indexer_manager
        if not mgr.prowlarr_settings(self.cfg)["configured"] and not mgr.jackett_settings(self.cfg)["configured"]:
            return False
        self._prowlarr_running = True

        def run() -> None:
            try:
                self.indexer_manager.sync_prowlarr(self.cfg)
                self.indexer_manager.sync_jackett(self.cfg)
            finally:
                self._prowlarr_running = False
        threading.Thread(target=run, name="prowlarr-sync", daemon=True).start()
        return True

    def jackett_status(self) -> Dict[str, Any]:
        st = dict(self.indexer_manager.jackett_settings(self.cfg))
        st.pop("_key", None)
        st["running"] = bool(getattr(self, "_prowlarr_running", False))
        return st

    def prowlarr_status(self) -> Dict[str, Any]:
        st = dict(self.indexer_manager.prowlarr_settings(self.cfg))
        st.pop("_key", None)
        st["running"] = bool(getattr(self, "_prowlarr_running", False))
        return st

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
