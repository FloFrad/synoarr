"""Orchestration "Radarr/Sonarr": Plex Watchlist -> wanted items -> indexers -> best release -> Download Station."""
from __future__ import annotations

import datetime as _dt
import logging
import os
import re
import time
import threading
import unicodedata
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Optional, Tuple

from . import bencode, quality
from .db import Database, now_iso, parse_iso, release_file_lock, try_file_lock
from .httpclient import HttpClient, HttpError, redact
from .models import EpisodeInfo, Release, SearchRequest, WatchItem
from .parser import normalize_title, parse, titles_match

if TYPE_CHECKING:  # pragma: no cover
    from .config import Config
    from .downloadstation import DownloadStation
    from .indexers.base import Indexer
    from .plex import PlexClient
    from .tmdb import TmdbClient

log = logging.getLogger(__name__)

ANIMATION_GENRES = {"animation", "anime", "animé", "dessin animé", "animación", "animazione"}
VIDEO_EXT = {"mkv", "mp4", "avi", "m4v", "mov", "wmv", "ts", "m2ts", "mpg", "mpeg", "webm"}


class _AlreadyGrabbed(Exception):
    """The torrent (same info hash) was already sent to Download Station."""


def _is_ds_error(exc: BaseException) -> bool:
    try:
        from .downloadstation import DownloadStationError
    except Exception:  # pragma: no cover
        return False
    return isinstance(exc, DownloadStationError)


def _data_dir(cfg: Any) -> str:
    try:
        return cfg.data_dir
    except Exception:  # pragma: no cover
        return ""


def _ext(name: str) -> str:
    return name.rsplit(".", 1)[-1].lower() if "." in name else ""


def _fold(text: str) -> str:
    return unicodedata.normalize("NFC", text).strip().casefold()


def _strip_torrent_ext(text: str) -> str:
    return text[:-8] if text.lower().endswith(".torrent") else text


def _today() -> str:
    return _dt.date.today().isoformat()


def _is_aired(air_date: Optional[str]) -> bool:
    """Unknown air date counts as aired (better to search than to miss it)."""
    return not air_date or air_date[:10] <= _today()


def _is_local_key(plex_key: Optional[str]) -> bool:
    """Entry added by hand in synoarr ("local:..."), unknown to the Plex Watchlist."""
    return str(plex_key or "").startswith("local:")


def _looks_like_torrent(body: bytes) -> bool:
    head = body.lstrip()[:1]
    return head == b"d" and b"4:info" in body


class Searcher:
    def __init__(self, cfg: "Config", db: Database, http: HttpClient,
                 plex: Optional["PlexClient"], ds: Optional["DownloadStation"], tmdb: Optional["TmdbClient"],
                 indexers_provider: Callable[[], List["Indexer"]]):
        self.cfg = cfg
        self.db = db
        self.http = http
        self.plex = plex
        self.ds = ds
        self.tmdb = tmdb
        self.indexers_provider = indexers_provider
        self._episode_errors = set()  # media ids whose episode list failed to load (retry, never guess)
        # set by the daemon on shutdown: a long search cycle stops between two items
        self.stop_event = None  # type: Optional[threading.Event]

    def _touch_media(self, media_id: int) -> None:
        if not self.cfg.general.dry_run:  # a simulation must not delay the real searches
            self.db.touch_media_search(media_id)

    def _touch_episode(self, episode_id: int) -> None:
        if not self.cfg.general.dry_run:
            self.db.touch_episode_search(episode_id)

    def _stopping(self) -> bool:
        return self.stop_event is not None and self.stop_event.is_set()

    def _video_exts(self) -> set:
        exts = getattr(getattr(self.cfg, "naming", None), "video_extensions", None) or VIDEO_EXT
        return set(e.lower().lstrip(".") for e in exts)

    # ================================================================ watchlist sync
    def sync_watchlist(self) -> int:
        """Import the Plex Watchlist into the database. Returns the number of items."""
        if self.plex is None:
            log.warning("Plex non configuré : lancez « python3 synoarr.py plex-login » ou renseignez [plex] token")
            self.sync_local_items()  # entries added by hand in synoarr do not need Plex
            return 0
        try:
            items = self.plex.watchlist()
            keys = []
            for item in items:
                try:
                    self._sync_item(item)
                    keys.append(item.rating_key)
                except Exception as exc:  # one bad item must not stop the sync
                    log.exception("Watchlist : erreur sur « %s » : %s", item.title, exc)
                    keys.append(item.rating_key)
            self.db.mark_off_watchlist(keys)
            # entries removed in synoarr but kept hidden because Plex still had them: gone from Plex now
            self.db.purge_hidden_off_watchlist()
            log.info("Watchlist Plex : %d élément(s)", len(items))
            return len(items)
        finally:
            self.sync_local_items()  # also when Plex is unreachable

    def _sync_item(self, item: WatchItem) -> None:
        guids = dict(item.guids or {})
        if not guids and self.plex is not None:
            try:
                guids = self.plex.get_guids(item.rating_key)
            except Exception as exc:
                log.debug("guids indisponibles pour %s: %s", item.title, exc)
        existing = self.db.get_media_by_key(item.rating_key)
        if existing is None:
            existing = self._adopt_local_entry(item, guids)
        media_id = self.db.upsert_media(item.rating_key, item.type, item.title, item.year, guids.get("imdb"),
                                        guids.get("tmdb"), guids.get("tvdb"), item.original_title)
        if existing is None:
            self.db.add_event("watchlist", "Ajouté depuis la Watchlist : %s%s" % (
                item.title, " (%s)" % item.year if item.year else ""))
        media = self.db.get_media(media_id)
        assert media is not None
        if media.get("hidden"):
            return  # removed by the user in synoarr, still in the Plex Watchlist: stays hidden and ignored
        if media.get("genres") is None:  # details never fetched yet (new item or upgraded database)
            self._fetch_details(media, item)
            media = self.db.get_media(media_id) or media
        if item.type == "movie":
            if media["status"] == "wanted" and self._movie_in_library(item.title, item.year,
                                                                       self._alt_titles(media)):
                self.db.set_media_status(media_id, "done")
                log.info("%s est déjà présent dans %s", item.title, self.cfg.paths.movies_dir)
        else:
            self._sync_episodes(media)

    def _fetch_details(self, media: Dict[str, Any], item: Optional[WatchItem] = None) -> None:
        """Genres (animation => French) and movie collection, from Plex then TMDb. Never raises."""
        genres = list(item.genres) if item is not None else []
        if _is_local_key(media.get("plex_key")):
            media = self._find_tmdb_id(media)
        elif not genres and self.plex is not None and hasattr(self.plex, "get_details"):
            try:
                genres = list(self.plex.get_details(media["plex_key"]).get("genres") or [])
            except Exception as exc:
                log.debug("Genres Plex indisponibles pour %s : %s", media["title"], exc)
        collection = None
        tmdb_anim = None
        if self.tmdb is not None and getattr(self.tmdb, "enabled", False):
            try:
                tmdb_id = media.get("tmdb")
                if not tmdb_id and media.get("imdb"):
                    found = self.tmdb.find_by_imdb(media["imdb"]) or {}
                    tmdb_id = found.get("movie" if media["type"] == "movie" else "tv")
                if tmdb_id:
                    det = (self.tmdb.movie_details(str(tmdb_id)) if media["type"] == "movie"
                           else self.tmdb.show_details(str(tmdb_id))) or {}
                    collection = det.get("collection")
                    tmdb_anim = det.get("is_animation")
                    if not genres:
                        genres = list(det.get("genres") or [])
            except Exception as exc:
                log.debug("Détails TMDb indisponibles pour %s : %s", media["title"], exc)
        is_anim = any(g.lower() in ANIMATION_GENRES for g in genres) or bool(tmdb_anim)
        self.db.set_media_details(media["id"], collection=collection, genres=genres, is_animation=is_anim)
        if is_anim:
            log.info("%s : animation → langues %s", media["title"],
                     ", ".join(self.cfg.quality.languages_animation))

    # ---- watchlist add/delete
    def sync_plex_item(self, item: WatchItem) -> Optional[Dict[str, Any]]:
        """Import one Plex Watchlist item now (just added from synoarr). Returns its media row."""
        self._sync_item(item)
        return self.db.get_media_by_key(item.rating_key)

    def sync_local_items(self) -> int:
        """Refresh entries added by hand (details, episodes via TMDb, library check). Never raises."""
        rows = self.db.list_local_media()
        for media in rows:
            try:
                self.sync_local_item(media)
            except Exception as exc:  # one bad item must not stop the sync
                log.exception("Watchlist : erreur sur l'entrée locale « %s » : %s", media.get("title"), exc)
        if rows:
            log.info("Entrées locales : %d élément(s)", len(rows))
        return len(rows)

    def sync_local_item(self, media: Dict[str, Any]) -> None:
        if media.get("hidden"):
            return
        if media.get("genres") is None:
            self._fetch_details(media)
            media = self.db.get_media(media["id"]) or media
            if media.get("hidden"):
                return  # masked as a duplicate of another entry (same TMDb id)
        if media["type"] == "movie":
            if media["status"] == "wanted" and self._movie_in_library(media["title"], media.get("year"),
                                                                       self._alt_titles(media)):
                self.db.set_media_status(media["id"], "done")
                log.info("%s est déjà présent dans %s", media["title"], self.cfg.paths.movies_dir)
        else:
            self._sync_episodes(media)

    def _adopt_local_entry(self, item: WatchItem, guids: Dict[str, str]) -> Optional[Dict[str, Any]]:
        """A local entry for the same movie/show (same imdb/tmdb/tvdb) becomes this Plex item: no duplicate
        when the user also adds it to the Plex Watchlist."""
        if not guids:
            return None
        row = self.db.find_media_by_ids(item.type, guids.get("imdb"), guids.get("tmdb"), guids.get("tvdb"))
        if row is None or not _is_local_key(row.get("plex_key")):
            return None
        self.db.set_media_key(row["id"], item.rating_key)
        log.info("« %s » est maintenant suivi depuis la Watchlist Plex", row["title"])
        return self.db.get_media(row["id"])

    def _find_tmdb_id(self, media: Dict[str, Any]) -> Dict[str, Any]:
        """Local entry without any id: look its TMDb id up by title (exact title match only)."""
        if media.get("tmdb") or media.get("imdb") or self.tmdb is None or not getattr(self.tmdb, "enabled", False):
            return media
        try:
            found = (self.tmdb.search_movie(media["title"], media.get("year")) if media["type"] == "movie"
                     else self.tmdb.search_show(media["title"]))
        except Exception as exc:
            log.debug("Recherche TMDb impossible pour %s : %s", media["title"], exc)
            return media
        if found and found.get("tmdb"):
            self.db.execute("UPDATE media SET tmdb = ? WHERE id = ? AND tmdb IS NULL", (found["tmdb"], media["id"]))
            media = self.db.get_media(media["id"]) or media
            media = self._hide_if_duplicate(media)
        return media

    def _hide_if_duplicate(self, media: Dict[str, Any]) -> Dict[str, Any]:
        """Local entry whose freshly found TMDb id is already used by another visible row of the same type:
        one of them is masked + ignored (no second download). A Plex row always wins over the local entry;
        between two local entries the older one is kept."""
        if not media.get("tmdb") or not _is_local_key(media.get("plex_key")):
            return media
        other = self.db.query_one("SELECT * FROM media WHERE type = ? AND tmdb = ? AND id != ? "
                                  "AND COALESCE(hidden, 0) = 0 ORDER BY id LIMIT 1",
                                  (media["type"], str(media["tmdb"]), media["id"]))
        if other is None:
            return media
        loser = media
        if _is_local_key(other.get("plex_key")) and other["id"] > media["id"]:
            loser = other
        self.db.set_media_hidden(loser["id"], True)
        self.db.set_media_status(loser["id"], "ignored")
        kept = other if loser is media else media
        log.warning("« %s » (n° %d) est un doublon de « %s » (n° %d, même identifiant TMDb %s) : masqué et ignoré",
                    loser["title"], loser["id"], kept["title"], kept["id"], media["tmdb"])
        self.db.add_event("watchlist", "Doublon masqué : %s (même œuvre que « %s »)" % (loser["title"], kept["title"]),
                          "WARNING")
        return self.db.get_media(media["id"]) or media
    # ---- end watchlist add/delete

    def languages_for(self, media: Optional[Dict[str, Any]]) -> List[str]:
        """Accepted audio languages for *media*: manual override > animation > [quality] languages."""
        if not media:
            return list(self.cfg.quality.languages)
        override = (media.get("language_override") or "").strip()
        if override:
            return [x.strip().lower() for x in override.replace(";", ",").split(",") if x.strip()]
        if media.get("is_animation"):
            return list(self.cfg.quality.languages_animation)
        return list(self.cfg.quality.languages)

    def _sync_episodes(self, media: Dict[str, Any]) -> None:
        episodes: List[EpisodeInfo] = []
        if self.plex is not None and not _is_local_key(media.get("plex_key")):  # Plex knows no local entry
            try:
                episodes = self.plex.show_episodes(media["plex_key"])
            except Exception as exc:
                log.warning("Épisodes Plex indisponibles pour %s : %s", media["title"], exc)
                self._episode_errors.add(media["id"])
        if not episodes and self.tmdb is not None and getattr(self.tmdb, "enabled", False):
            tmdb_id = media.get("tmdb")
            if not tmdb_id:
                found = self.tmdb.search_show(media["title"])
                tmdb_id = found.get("tmdb") if found else None
            if tmdb_id:
                episodes = self.tmdb.show_episodes(str(tmdb_id))
        if not episodes:
            if media["id"] not in self._episode_errors:
                log.info("Aucune liste d'épisodes pour %s : recherche par saisons complètes", media["title"])
            return
        self._episode_errors.discard(media["id"])
        first_sync = not self.db.list_episodes(media["id"])
        policy = self.cfg.series.monitor
        added_on = (media.get("added_at") or _today())[:10]
        aired_seasons = sorted({e.season for e in episodes if e.season > 0 and _is_aired(e.air_date)})
        latest = aired_seasons[-1] if aired_seasons else max([e.season for e in episodes] or [1])
        in_library = self._episodes_in_library(media["title"], self._alt_titles(media))
        # downloads already sent (e.g. season packs grabbed while no episode list was known)
        active = self.db.query("SELECT season, episode FROM grabs WHERE media_id = ? AND status = 'sent' "
                               "AND season IS NOT NULL", (media["id"],))
        for ep in episodes:
            if ep.season <= 0:
                continue  # specials
            status = "wanted"
            if (ep.season, ep.episode) in in_library:
                status = "done"
            elif any(g["season"] == ep.season and g["episode"] in (None, ep.episode) for g in active):
                status = "grabbed"
            elif policy == "future" and first_sync and ep.air_date and ep.air_date[:10] < added_on:
                status = "skipped"
            elif policy == "latest_season" and ep.season < latest:
                status = "skipped"
            self.db.upsert_episode(media["id"], ep.season, ep.episode, ep.title, ep.air_date, status)
            if status == "done":
                self.db.set_episode_status(media["id"], ep.season, ep.episode, "done", only_if=("wanted",))

    # ------------------------------------------------------------ library checks
    def _movie_in_library(self, title: str, year: Optional[int], alt_titles: Optional[List[str]] = None) -> bool:
        """True if movies_dir holds this movie: "Title (Year).mkv", "Title (Year)/..." or
        "<Collection>/Title (Year).mkv" (one level of collection folders)."""
        return self._find_movie_file(title, year, alt_titles) is not None

    def _find_movie_file(self, title: str, year: Optional[int],
                         alt_titles: Optional[List[str]] = None) -> Optional[str]:
        """Path of the video of this movie in movies_dir (biggest video of its folder), or None."""
        root = self.cfg.paths.movies_dir
        if not os.path.isdir(root):
            return None
        vexts = self._video_exts()

        def matches(name: str) -> bool:
            p = parse(name)
            if not titles_match(p.title, title, alt_titles):
                return False
            return not (year and p.year and abs(p.year - year) > 1)

        def biggest_video(folder: str) -> Optional[str]:
            best, best_size = None, -1  # type: Optional[str], int
            for dirpath, _dirs, files in os.walk(folder):
                for f in files:
                    if _ext(f) not in vexts:
                        continue
                    full = os.path.join(dirpath, f)
                    try:
                        size = os.path.getsize(full)
                    except OSError:
                        size = 0
                    if size > best_size:
                        best, best_size = full, size
            return best

        try:
            names = os.listdir(root)
        except OSError:
            return None
        for name in names:
            if name.startswith((".", "@", "#")):
                continue
            path = os.path.join(root, name)
            if os.path.isfile(path):
                # a stray "Title.nfo" must not count
                if _ext(name) in vexts and matches(name):
                    return path
                continue
            if not os.path.isdir(path):
                continue
            if matches(name):
                found = biggest_video(path)
                if found:
                    return found
            # collection folder: look one level down
            try:
                children = os.listdir(path)
            except OSError:
                continue
            for child in children:
                cpath = os.path.join(path, child)
                if child.startswith((".", "@", "#")) or not matches(child):
                    continue
                if os.path.isfile(cpath) and _ext(child) in vexts:
                    return cpath
                if os.path.isdir(cpath):
                    found = biggest_video(cpath)
                    if found:
                        return found
        return None

    def _episodes_in_library(self, show_title: str, alt_titles: Optional[List[str]] = None) -> set:
        return set(self._library_episode_files(show_title, alt_titles))

    def _library_episode_files(self, show_title: str,
                               alt_titles: Optional[List[str]] = None) -> Dict[Tuple[int, int], str]:
        """{(season, episode): video path} of this show in series_dir."""
        found = {}  # type: Dict[Tuple[int, int], str]
        root = self.cfg.paths.series_dir
        if not os.path.isdir(root):
            return found
        try:
            names = os.listdir(root)
        except OSError:
            return found
        vexts = self._video_exts()
        for name in names:
            if name.startswith((".", "@", "#")):
                continue
            if not titles_match(parse(name).title or name, show_title, alt_titles) and \
                    not titles_match(name, show_title, alt_titles):
                continue
            for dirpath, _dirs, files in os.walk(os.path.join(root, name)):
                for f in sorted(files):
                    if _ext(f) not in vexts:
                        continue
                    p = parse(f)
                    for s in p.seasons[:1]:
                        for e in p.episodes:
                            found.setdefault((s, e), os.path.join(dirpath, f))
        return found

    # ================================================================ search cycle
    def run_once(self, media_id: Optional[int] = None, force: bool = False) -> Dict[str, int]:
        """Search every wanted item (or only *media_id*). Returns counters."""
        lock = try_file_lock(_data_dir(self.cfg), "search.lock")
        if lock is False:
            log.info("Une autre recherche est en cours (démon ou commande) : recherche ignorée")
            return {"searched": 0, "grabbed": 0}
        try:
            return self._run_once(media_id, force)
        finally:
            release_file_lock(lock)

    def _run_once(self, media_id: Optional[int], force: bool) -> Dict[str, int]:
        stats = {"searched": 0, "grabbed": 0}
        indexers = self.indexers_provider()
        if not indexers:
            log.warning("Aucun indexer actif : ajoutez-en avec « python3 synoarr.py indexer catalog »")
            return stats
        self.check_failed_downloads()
        medias = [self.db.get_media(media_id)] if media_id else self.db.list_media(on_watchlist_only=True)
        for media in medias:
            if not media:
                continue
            if self._stopping():
                log.info("Arrêt demandé : recherche interrompue")
                break
            try:
                if media["type"] == "movie" and media["status"] == "wanted":
                    self._search_movie(media, indexers, force or bool(media_id), stats)
                elif media["type"] == "movie" and media["status"] == "done":
                    self._search_movie_upgrade(media, indexers, force or bool(media_id), stats)  # F3
                elif media["type"] == "show" and media["status"] == "monitored":
                    self._search_show(media, indexers, force or bool(media_id), stats)
                    self._search_show_upgrades(media, indexers, force or bool(media_id), stats)  # F3
            except Exception as exc:
                log.exception("Recherche : erreur sur %s : %s", media.get("title"), exc)
                self.db.add_event("search", "Erreur de recherche pour %s : %s" % (media.get("title"), exc), "ERROR")
        if stats["searched"]:
            log.info("Recherche terminée : %d recherche(s), %d envoi(s) vers Download Station",
                     stats["searched"], stats["grabbed"])
        return stats

    def _due(self, last_search_at: Optional[str], force: bool) -> bool:
        if force:
            return True
        last = parse_iso(last_search_at)
        if last is None:
            return True
        return (_dt.datetime.now() - last).total_seconds() >= self.cfg.general.research_interval_hours * 3600

    def _alt_titles(self, media: Dict[str, Any]) -> List[str]:
        """Other titles of the work, also searched on the indexers: original title, and the French title when
        French is one of the item's languages (French releases are named after the French title)."""
        alts = []
        if media.get("original_title") and media["original_title"] != media["title"]:
            alts.append(media["original_title"])
        if "fr" in self.languages_for(media):
            fr = self.french_title(media)
            if fr and fr.strip().lower() != (media.get("title") or "").strip().lower() and fr not in alts:
                alts.insert(0, fr)
        return alts

    def _title_fr_if_wanted(self, media: Dict[str, Any]) -> Optional[str]:
        """French title when French is wanted (may equal the main title: a Plex account in French already
        shows French titles). Used to search by text and to recognise untagged French releases."""
        if "fr" not in self.languages_for(media):
            return None
        return self.french_title(media) or None

    def french_title(self, media: Dict[str, Any]) -> Optional[str]:
        """French title: typed by the user or fetched once from TMDb (cached in the database)."""
        cached = media.get("title_fr")
        if cached is not None:
            return cached or None
        if self.tmdb is None or not getattr(self.tmdb, "enabled", False) or not media.get("id"):
            return None
        kind = "movie" if media.get("type") == "movie" else "show"
        title = None
        try:
            tmdb_id = media.get("tmdb")
            if not tmdb_id and media.get("imdb") and hasattr(self.tmdb, "find_by_imdb"):
                found = self.tmdb.find_by_imdb(media["imdb"]) or {}
                tmdb_id = found.get("movie" if kind == "movie" else "tv")
            if tmdb_id and hasattr(self.tmdb, "localized_title"):
                title = self.tmdb.localized_title(str(tmdb_id), kind, "fr-FR")
        except Exception as exc:
            log.debug("Titre français indisponible pour %s : %s", media.get("title"), exc)
            return None
        self.db.set_title_fr(int(media["id"]), title or "")
        media["title_fr"] = title or ""
        if title and title != media.get("title"):
            log.info("Titre français de %s : %s", media.get("title"), title)
        return title or None

    def _search_movie(self, media: Dict[str, Any], indexers: List["Indexer"], force: bool,
                      stats: Dict[str, int]) -> None:
        if not self._due(media.get("last_search_at"), force):
            return
        req = SearchRequest(languages=self.languages_for(media), kind="movie", title=media["title"], year=media.get("year"), imdb=media.get("imdb"),
                            tmdb=media.get("tmdb"), alt_titles=self._alt_titles(media), title_fr=self._title_fr_if_wanted(media))
        self._touch_media(media["id"])
        stats["searched"] += 1
        best = self._find_best(req, indexers)
        if best and self.grab(best, media, None, None):
            # conditional: the organizer may have imported it meanwhile (status done)
            self.db.execute("UPDATE media SET status = 'grabbed', updated_at = ? WHERE id = ? AND status = 'wanted'",
                            (now_iso(), media["id"]))
            stats["grabbed"] += 1

    def _search_show(self, media: Dict[str, Any], indexers: List["Indexer"], force: bool,
                     stats: Dict[str, int]) -> None:
        all_eps = self.db.list_episodes(media["id"])
        if not all_eps:
            if media["id"] in self._episode_errors:
                log.info("Liste d'épisodes de %s momentanément indisponible : recherche reportée", media["title"])
            elif self.cfg.series.monitor != "all":
                log.info("Aucune liste d'épisodes pour %s : impossible d'appliquer monitor = %s sans elle "
                         "(ajoutez une clé TMDb)", media["title"], self.cfg.series.monitor)
            else:
                self._search_show_without_episodes(media, indexers, force, stats)
            return
        wanted = [e for e in all_eps if e["status"] == "wanted" and _is_aired(e["air_date"])]
        if not wanted:
            return
        by_season: Dict[int, List[Dict[str, Any]]] = {}
        for e in wanted:
            by_season.setdefault(e["season"], []).append(e)
        for season, eps in sorted(by_season.items()):
            season_eps = [e for e in all_eps if e["season"] == season]
            season_complete = all(_is_aired(e["air_date"]) for e in season_eps)
            due_eps = [e for e in eps if self._due(e.get("last_search_at"), force)]
            if not due_eps:
                continue
            # 1) season pack when the whole season has aired and several episodes are missing
            if self.cfg.series.season_packs and season_complete and len(eps) >= 2:
                req = SearchRequest(languages=self.languages_for(media), kind="season", title=media["title"], year=media.get("year"), season=season,
                                    imdb=media.get("imdb"), tvdb=media.get("tvdb"), tmdb=media.get("tmdb"),
                                    alt_titles=self._alt_titles(media), title_fr=self._title_fr_if_wanted(media))
                stats["searched"] += 1
                best = self._find_best(req, indexers)
                if best and self.grab(best, media, season, None):
                    self.db.set_episode_status(media["id"], season, None, "grabbed", only_if=("wanted",))
                    stats["grabbed"] += 1
                    for e in due_eps:
                        self._touch_episode(e["id"])
                    continue
            # 2) episode by episode
            for e in due_eps:
                if self._stopping():
                    return
                current = self.db.get_episode(media["id"], season, e["episode"])
                if current is not None and current["status"] != "wanted":
                    continue  # covered by a multi-episode release grabbed just before
                req = SearchRequest(languages=self.languages_for(media), kind="episode", title=media["title"], year=media.get("year"), season=season,
                                    episode=e["episode"], imdb=media.get("imdb"), tvdb=media.get("tvdb"),
                                    tmdb=media.get("tmdb"), alt_titles=self._alt_titles(media), title_fr=self._title_fr_if_wanted(media))
                self._touch_episode(e["id"])
                stats["searched"] += 1
                best = self._find_best(req, indexers)
                if best and self.grab(best, media, season, e["episode"]):
                    grabbed_eps = set(parse(best.title).episodes) | {e["episode"]}
                    for ep_num in grabbed_eps:  # "S01E01E02": both episodes are coming
                        self.db.set_episode_status(media["id"], season, ep_num, "grabbed", only_if=("wanted",))
                    stats["grabbed"] += 1

    def _search_show_without_episodes(self, media: Dict[str, Any], indexers: List["Indexer"], force: bool,
                                      stats: Dict[str, int]) -> None:
        """No episode list (no Plex/TMDb data): grab the best season pack of every season found."""
        if not self._due(media.get("last_search_at"), force):
            return
        self._touch_media(media["id"])
        grabbed_seasons = set()
        for g in self.db.query("SELECT season, release_title FROM grabs WHERE media_id = ? AND status != 'failed'",
                               (media["id"],)):
            grabbed_seasons.add(g["season"])
            pr = parse(g["release_title"] or "")
            if pr.is_season_pack:
                grabbed_seasons.update(pr.seasons)  # "S01-S03" pack covers 3 seasons
        base = SearchRequest(languages=self.languages_for(media), kind="season", title=media["title"], year=media.get("year"),
                             alt_titles=self._alt_titles(media), title_fr=self._title_fr_if_wanted(media))
        stats["searched"] += 1
        discovery = SearchRequest(languages=self.languages_for(media), kind="season", title=media["title"], season=None,
                                  alt_titles=base.alt_titles, title_fr=base.title_fr)
        results = self._collect(discovery, indexers)
        extra = self.fallback_queries(discovery)
        if extra and not quality.rank_releases(results, discovery, self.cfg.quality, self._is_excluded,
                                               priorities=indexer_priorities(indexers)):
            log.info("%s (%s)", FRENCH_FALLBACK_MESSAGE, discovery.describe())
            results = results + self._run_queries(extra, indexers)
        seasons = set()
        for r in results:
            pr = parse(r.title)
            if pr.is_season_pack:
                seasons.update(pr.seasons)
        for season in sorted(seasons):
            if season in grabbed_seasons:
                continue
            req = SearchRequest(languages=self.languages_for(media), kind="season", title=media["title"], year=media.get("year"), season=season,
                                alt_titles=base.alt_titles, title_fr=base.title_fr)
            best = quality.pick_best(results, req, self.cfg.quality, self._is_excluded,
                                     priorities=indexer_priorities(indexers))
            if best and self.grab(best, media, season, None):
                stats["grabbed"] += 1
                grabbed_seasons.update(parse(best.title).seasons)  # "S01-S03" pack covers 3 seasons

    # ------------------------------------------------------------ quality upgrades (F3)
    MAX_UPGRADE_SEARCHES = 25  # upgrade searches per cycle (oldest first), so a big library never floods indexers

    def _upgrades_enabled(self) -> bool:
        return bool(getattr(self.cfg.quality, "upgrade", False))

    def _upgrade_budget(self, stats: Dict[str, int]) -> bool:
        return stats.get("upgrade_searched", 0) < self.MAX_UPGRADE_SEARCHES

    @staticmethod
    def _quality_of_file(path: str) -> Tuple[Optional[str], Optional[str]]:
        """(resolution, source) read from a library file name, then from its folder name."""
        p = parse(os.path.basename(path))
        res, src = p.resolution, p.source
        if not res:
            q = parse(os.path.basename(os.path.dirname(path)))
            res, src = q.resolution, src or q.source
        return res, src

    def current_resolution(self, media: Dict[str, Any]) -> Optional[str]:
        """Resolution of the movie file in the library: stored at import, else read once from the library file
        name (cached, "" = unknown). None = unknown (never upgraded)."""
        stored = media.get("quality_resolution")
        if stored is not None:
            return stored or None
        path = self._find_movie_file(media["title"], media.get("year"), self._alt_titles(media))
        res, src = self._quality_of_file(path) if path else (None, None)
        if not self.cfg.general.dry_run:
            self.db.set_media_quality(int(media["id"]), res or "", src, None, path)
        return res

    def episode_resolutions(self, media: Dict[str, Any], episodes: List[Dict[str, Any]]) -> Dict[int, Optional[str]]:
        """{episode id: resolution of its library file} (stored at import, else read once from the library)."""
        out = {}  # type: Dict[int, Optional[str]]
        unknown = [e for e in episodes if e.get("quality_resolution") is None]
        files = self._library_episode_files(media["title"], self._alt_titles(media)) if unknown else {}
        for e in episodes:
            stored = e.get("quality_resolution")
            if stored is None:
                path = files.get((e["season"], e["episode"]))
                res, src = self._quality_of_file(path) if path else (None, None)
                if not self.cfg.general.dry_run:
                    self.db.set_episode_quality(int(media["id"]), e["season"], e["episode"], res or "", src, None,
                                                path)
                stored = res or ""
            out[e["id"]] = stored or None
        return out

    def _search_movie_upgrade(self, media: Dict[str, Any], indexers: List["Indexer"], force: bool,
                              stats: Dict[str, int]) -> None:
        """Movie in the library below [quality] upgrade_until: search a strictly better resolution."""
        if not self._upgrades_enabled() or not self._due(media.get("last_search_at"), force):
            return
        current = self.current_resolution(media)
        if not quality.needs_upgrade(current, self.cfg.quality) or self.db.has_active_grab(media["id"]):
            return
        if not self._upgrade_budget(stats):
            return
        req = self.request_for(media)
        self._touch_media(media["id"])
        stats["searched"] += 1
        stats["upgrade_searched"] = stats.get("upgrade_searched", 0) + 1
        log.info("Amélioration possible de %s (actuellement %s, jusqu'à %s)", media["title"], current,
                 self.cfg.quality.upgrade_until)
        best = self._find_best(req, indexers, current_resolution=current)
        if best and self.grab(best, media, None, None, upgrade=True):
            stats["grabbed"] += 1
            stats["upgrades"] = stats.get("upgrades", 0) + 1

    def _search_show_upgrades(self, media: Dict[str, Any], indexers: List["Indexer"], force: bool,
                              stats: Dict[str, int]) -> None:
        """Episodes in the library below [quality] upgrade_until: search a better resolution, one by one."""
        if not self._upgrades_enabled():
            return
        done = [e for e in self.db.list_episodes(media["id"], status="done")
                if self._due(e.get("last_search_at"), force)]
        if not done:
            return
        resolutions = self.episode_resolutions(media, done)
        for e in done:
            if self._stopping() or not self._upgrade_budget(stats):
                return
            current = resolutions.get(e["id"])
            if not quality.needs_upgrade(current, self.cfg.quality):
                continue
            if self.db.has_active_grab(media["id"], e["season"], e["episode"]):
                continue
            req = self.request_for(media, e["season"], e["episode"])
            self._touch_episode(e["id"])
            stats["searched"] += 1
            stats["upgrade_searched"] = stats.get("upgrade_searched", 0) + 1
            best = self._find_best(req, indexers, current_resolution=current)
            if best and self.grab(best, media, e["season"], e["episode"], upgrade=True):
                stats["grabbed"] += 1
                stats["upgrades"] = stats.get("upgrades", 0) + 1

    # ------------------------------------------------------------ indexer queries
    MAX_EXTRA_TITLES = 2  # other titles searched besides the main one (French title, original title)

    def _collect(self, req: SearchRequest, indexers: List["Indexer"]) -> List[Release]:
        """Results of every indexer for the main title, then for the other titles (French / original title):
        a French release is named after the French title and is not found with the English one."""
        return self._run_queries(self.queries_for(req), indexers)

    def french_title_only_applies(self, req: SearchRequest) -> bool:
        langs = {x.lower() for x in (req.languages or [])}
        return bool(req.title_fr) and langs == {"fr"} and bool(getattr(self.cfg.quality, "french_title_only", False))

    def fallback_queries(self, req: SearchRequest) -> List[SearchRequest]:
        """[quality] french_title_fallback: searches run when the French-title-only search found nothing
        acceptable (original/main title, id search...), without the one already sent. [] if it does not apply."""
        if not self.french_title_only_applies(req) or not getattr(self.cfg.quality, "french_title_fallback", True):
            return []

        def key(q: SearchRequest) -> Tuple[str, Any, Any, Any]:
            return ((q.title or "").strip().lower(), q.imdb, q.tvdb, q.tmdb)
        done = {key(q) for q in self.queries_for(req)}
        return [q for q in self.queries_for(req, widen=True) if key(q) not in done]

    def queries_for(self, req: SearchRequest, widen: bool = False) -> List[SearchRequest]:
        """The searches actually sent to the indexers for *req* (also shown in « Changer de source »).
        *widen*: the normal searches even when french_title_only applies (fallback)."""
        import dataclasses
        if not widen and self.french_title_only_applies(req):
            # [quality] french_title_only: one text search with the French title only
            return [dataclasses.replace(req, title=req.title_fr, imdb=None, tmdb=None, tvdb=None,
                                        alt_titles=[t for t in [req.title] + list(req.alt_titles or [])
                                                    if t and t != req.title_fr])]
        queries = [req]
        seen_titles = {(req.title or "").strip().lower()}
        if "fr" in [x.lower() for x in (req.languages or [])] and (req.imdb or req.tvdb or req.tmdb):
            # French wanted: also a plain text search of the main title (an id search returns the
            # English-named releases only, and a Plex account in French already gives the French title)
            queries.append(dataclasses.replace(req, imdb=None, tmdb=None, tvdb=None))
        if req.title_fr and req.title_fr.strip().lower() not in seen_titles:
            seen_titles.add(req.title_fr.strip().lower())
            queries.append(dataclasses.replace(req, title=req.title_fr, imdb=None, tmdb=None, tvdb=None,
                                               alt_titles=[req.title] + list(req.alt_titles or [])))
        for alt in req.alt_titles or []:
            key = (alt or "").strip().lower()
            if key and key not in seen_titles and len(queries) <= self.MAX_EXTRA_TITLES + 1:
                seen_titles.add(key)
                # text search only: an id search (imdbid/tvdbid) would return the same English-named results
                queries.append(dataclasses.replace(req, title=alt, imdb=None, tmdb=None, tvdb=None,
                                                   alt_titles=[req.title] + [a for a in req.alt_titles if a != alt]))
        return queries

    def _run_queries(self, queries: List[SearchRequest], indexers: List["Indexer"]) -> List[Release]:
        results: List[Release] = []
        seen = set()
        for q in queries:
            for idx in indexers:
                try:
                    found = idx.search(q)
                except Exception as exc:  # indexers should not raise, be defensive anyway
                    log.warning("Indexer %s : erreur %s", getattr(idx, "name", "?"), exc)
                    continue
                log.debug("Indexer %s : %d résultat(s) pour %s", idx.name, len(found), q.describe())
                for r in found:
                    key = (r.indexer, r.title, r.download_url or r.magnet or r.info_hash)
                    if key in seen:
                        continue
                    seen.add(key)
                    results.append(r)
        return results

    def _pick(self, results: List[Release], req: SearchRequest, indexers: List["Indexer"],
              current_resolution: Optional[str] = None) -> Optional[Release]:
        prios = indexer_priorities(indexers)
        if current_resolution:  # upgrade: strictly better resolution than the library file
            return quality.pick_upgrade(results, req, self.cfg.quality, current_resolution, self._is_excluded,
                                        priorities=prios)
        return quality.pick_best(results, req, self.cfg.quality, self._is_excluded, priorities=prios)

    def _find_best(self, req: SearchRequest, indexers: List["Indexer"],
                   current_resolution: Optional[str] = None) -> Optional[Release]:
        results = self._collect(req, indexers)
        best = self._pick(results, req, indexers, current_resolution)
        if best is None:
            extra = self.fallback_queries(req)
            if extra:  # [quality] french_title_fallback
                log.info("%s (%s)", FRENCH_FALLBACK_MESSAGE, req.describe())
                results = results + self._run_queries(extra, indexers)
                best = self._pick(results, req, indexers, current_resolution)
        what = ("amélioration (mieux que %s) " % current_resolution) if current_resolution else ""
        if best is None:
            log.info("Rien de satisfaisant %spour %s (%d résultat(s) examiné(s))", what, req.describe(), len(results))
        else:
            log.info("Meilleur résultat %spour %s : %s [%s]", what, req.describe(), best.title, best.indexer)
        return best

    def _is_excluded(self, release_title: str) -> bool:
        """Blocklisted, or already sent / imported (releases without info hash are recognised by title)."""
        if self.db.is_blocked(release_title):
            return True
        return self.db.query_one("SELECT 1 AS x FROM grabs WHERE lower(release_title) = lower(?) "
                                 "AND status != 'failed'", (release_title,)) is not None

    _resend = False  # set during a manual (re)send chosen by the user: duplicates allowed

    def _hash_already_grabbed(self, info_hash: Optional[str]) -> bool:
        if not info_hash or self._resend:
            return False
        return self.db.query_one("SELECT 1 AS x FROM grabs WHERE lower(info_hash) = lower(?) AND status != 'failed'",
                                 (info_hash,)) is not None

    def search_report(self, req: SearchRequest,
                      info: Optional[Dict[str, Any]] = None) -> List[Tuple[Release, Optional[int], str]]:
        """Used by the CLI / « Changer de source »: every result with its score / rejection reason.
        *info* (optional dict) receives "fallback" (True when french_title_only found nothing acceptable and the
        search was widened, see fallback_queries) and "queries" (the extra searches sent)."""
        indexers = self.indexers_provider()
        results = self._collect(req, indexers)
        priorities = indexer_priorities(indexers)

        def report(found: List[Release]) -> List[Tuple[Release, Optional[int], str]]:
            out = []
            for r in found:
                score, reason = quality.evaluate(r, req, self.cfg.quality, priorities)
                if score is not None and self.db.is_blocked(r.title):
                    score, reason = None, "release bloquée (échec précédent)"
                out.append((r, score, reason))
            out.sort(key=lambda t: (t[1] is None, -(t[1] or 0)))
            return out
        out = report(results)
        if info is not None:
            info.setdefault("fallback", False)
        if not any(score is not None for _r, score, _x in out):
            extra = self.fallback_queries(req)
            if extra:
                if info is not None:
                    info["fallback"] = True
                    info["queries"] = extra
                seen = {(r.indexer, r.title, r.download_url or r.magnet or r.info_hash) for r in results}
                more = [r for r in self._run_queries(extra, indexers)
                        if (r.indexer, r.title, r.download_url or r.magnet or r.info_hash) not in seen]
                out = report(results + more)
        return out

    # ================================================================ grab
    def grab(self, release: Release, media: Optional[Dict[str, Any]], season: Optional[int],
             episode: Optional[int], error_out: Optional[List[str]] = None, resend: bool = False,
             upgrade: bool = False) -> bool:
        """Send *release* to Download Station and record it. Returns True on success.
        *error_out* (optional list) receives the French reason of a failure.
        *resend*: the user chose this release by hand -> sent again even if the same torrent was sent before
        (its earlier, unfinished downloads are closed).
        *upgrade*: better version of an item already in the library (F3): the grab is marked as an upgrade and
        the item status is left unchanged."""
        if resend:
            self._resend = True
            try:
                ok = self.grab(release, media, season, episode, error_out)
            finally:
                self._resend = False
            if ok:
                self._close_duplicates()
            return ok
        def fail(msg: str) -> bool:
            if error_out is not None:
                error_out.append(msg)
            return False
        label = media["title"] if media else release.title
        if self._hash_already_grabbed(release.info_hash):
            log.info("Déjà envoyé : %s", release.title)
            return fail("ce torrent a déjà été envoyé à Download Station")
        if self.cfg.general.dry_run:
            log.info("[dry-run] Enverrait à Download Station : %s", release.title)
            self.db.add_event("grab", "[dry-run] %s → %s" % (label, release.title))
            return fail("mode simulation (dry_run) actif : rien n'a été envoyé")
        if self.ds is None:
            log.error("Download Station non configuré : impossible d'envoyer %s", release.title)
            return fail("Download Station n'est pas configuré")
        try:
            torrent_name, info_hash = self._send(release)
        except _AlreadyGrabbed:
            log.info("Déjà envoyé (même info hash) : %s", release.title)
            return fail("ce torrent (même info hash) a déjà été envoyé à Download Station")
        except Exception as exc:
            log.warning("Échec de l'envoi de %s : %s", release.title, exc)
            self.db.add_event("grab", "Échec de l'envoi de %s : %s" % (release.title, exc), "ERROR")
            # Only a release that is itself broken is blocklisted (dead link, not a torrent). A Download
            # Station problem (session, permissions, destination...), a network error, an indexer that
            # refuses the API key or rate-limits (401/403/429) must not blocklist every release.
            broken = isinstance(exc, ValueError) or (isinstance(exc, HttpError) and exc.status in (404, 410))
            if broken and not _is_ds_error(exc) and release.indexer != MANUAL_INDEXER:
                self.db.block_release(release.title, str(exc)[:200])
            return fail(str(exc) or type(exc).__name__)
        grab_id = self.db.add_grab(media["id"] if media else None, season, episode, release.title,
                                   torrent_name or release.title, release.indexer, info_hash or release.info_hash,
                                   release.size or None)
        what = label
        if season is not None and episode is not None:
            what += " S%02dE%02d" % (season, episode)
        elif season is not None:
            what += " saison %d" % season
        if upgrade:
            self.db.mark_grab_upgrade(grab_id)
            self.db.add_event("upgrade", "Amélioration de qualité envoyée à Download Station : %s ← %s [%s]"
                              % (what, release.title, release.indexer))
            log.info("Amélioration de qualité envoyée à Download Station : %s (%s)", release.title, what)
            return True
        self.db.add_event("grab", "Envoyé à Download Station : %s ← %s [%s]" % (what, release.title, release.indexer))
        log.info("Envoyé à Download Station : %s (%s)", release.title, what)
        return True

    def _close_duplicates(self) -> None:
        """After a manual resend: older « sent » grabs of the same torrent are closed (the newest one stays)."""
        last = self.db.query_one("SELECT * FROM grabs ORDER BY id DESC LIMIT 1")
        if not last or not last.get("info_hash"):
            return
        for g in self.db.query("SELECT id FROM grabs WHERE lower(info_hash) = lower(?) AND status = 'sent' AND id != ?",
                               (last["info_hash"], last["id"])):
            self.db.set_grab_status(g["id"], "failed", "renvoyé manuellement")

    def _send(self, release: Release) -> Tuple[Optional[str], Optional[str]]:
        """Send *release* to Download Station WITHOUT modifying what the indexer (Prowlarr...) provides:
        - a .torrent link: synoarr downloads it and uploads the original bytes untouched (like Prowlarr/Sonarr),
          or, with send_mode = url, Download Station downloads the link itself;
        - a magnet (direct or behind a redirect): sent as is."""
        assert self.ds is not None
        if release.magnet and (getattr(self.cfg.downloadstation, "prefer", "torrent") or "") == "magnet":
            return self._send_magnet(release.magnet)
        if release.download_url:
            resp = self.http.get(release.download_url, raise_for_status=False, timeout=60)
            if resp.redirect_location and resp.redirect_location.lower().startswith("magnet:"):
                return self._send_magnet(resp.redirect_location)
            if resp.ok and resp.body:
                body = resp.body
                name, info_hash = None, None  # type: Optional[str], Optional[str]
                try:
                    name, info_hash = bencode.torrent_info(body)
                except ValueError:
                    if not _looks_like_torrent(body):  # HTML error page, JSON...
                        if release.magnet:
                            return self._send_magnet(release.magnet)
                        raise ValueError("le lien ne renvoie pas un fichier .torrent valide (%s)"
                                         % redact(release.download_url))
                    log.debug("Fichier .torrent non analysable par synoarr, envoyé tel quel : %s", release.title)
                if self._hash_already_grabbed(info_hash):
                    raise _AlreadyGrabbed()
                mode = (getattr(self.cfg.downloadstation, "send_mode", "file") or "file").lower()
                if mode == "url":
                    try:
                        self.ds.add_url(release.download_url)
                        log.info("Lien .torrent transmis à Download Station (téléchargé par Download Station)")
                        return name or release.title, info_hash
                    except Exception as exc:  # DS cannot reach the link: upload the same bytes ourselves
                        log.info("Download Station ne peut pas télécharger le lien (%s) : envoi du fichier", exc)
                filename = re.sub(r"[^\w.\- ()\[\]]+", "_", name or release.title or "release")[:150] + ".torrent"
                self.ds.add_torrent_file(body, filename)  # original bytes, never modified
                return name or release.title, info_hash
            if release.magnet:
                return self._send_magnet(release.magnet)
            raise HttpError("HTTP %s en téléchargeant le .torrent" % resp.status, resp.status, resp.body,
                            release.download_url)
        if release.magnet:
            return self._send_magnet(release.magnet)
        raise ValueError("aucun lien de téléchargement")

    def _send_magnet(self, magnet: str) -> Tuple[Optional[str], Optional[str]]:
        assert self.ds is not None
        name, info_hash = bencode.magnet_info(magnet)
        if self._hash_already_grabbed(info_hash):
            raise _AlreadyGrabbed()
        fixed, reason = bencode.normalize_magnet(magnet)
        if reason:
            log.warning("Lien magnet mal encodé (%s) : réparé avant l'envoi (même hash, même nom, mêmes trackers)",
                        reason)
            magnet = fixed
            name = bencode.magnet_info(magnet)[0] or name
        trackers = getattr(self.cfg.downloadstation, "extra_trackers", None) or []  # empty by default: as is
        self.ds.add_url(bencode.add_trackers(magnet, trackers) if trackers else magnet)
        return name, info_hash

    # ================================================================ failed downloads
    def check_failed_downloads(self) -> None:
        """Detect Download Station tasks in error for our grabs → blocklist + search again."""
        if self.ds is None:
            return
        sent = self.db.list_grabs(status="sent")
        if not sent:
            return
        try:
            tasks = self.ds.list_tasks()
        except Exception as exc:
            log.debug("Download Station indisponible pour vérifier les erreurs : %s", exc)
            return
        index = TaskIndex(tasks)
        stalled_h = float(getattr(self.cfg.downloadstation, "stalled_hours", 0) or 0)
        for g in sent:
            task = index.find(g)
            if task is None:
                self._forget_vanished(g)
                continue
            if task.status == "error":
                reason = task.error or "erreur Download Station"
            elif stalled_h > 0 and self._is_stalled(task, g, stalled_h):
                reason = "aucune source après %g h (lien magnet sans métadonnées ou sans pair)" % stalled_h
            else:
                continue
            log.warning("Téléchargement en échec : %s (%s) → nouvelle recherche", g["release_title"], reason)
            self.db.set_grab_status(g["id"], "failed", reason)
            self.db.block_release(g["release_title"], reason)
            self.db.add_event("download", "Échec du téléchargement %s : %s" % (g["release_title"], reason), "ERROR")
            try:
                self.ds.delete_task(task.id)
            except Exception as exc:
                log.debug("Suppression de la tâche %s impossible : %s", task.id, exc)
            if g.get("media_id"):
                media = self.db.get_media(g["media_id"])
                if media and media["type"] == "movie":
                    if not g.get("upgrade"):  # a failed upgrade leaves the movie in the library ("done")
                        self.db.set_media_status(media["id"], "wanted")
                    self.db.execute("UPDATE media SET last_search_at = NULL WHERE id = ?", (media["id"],))
                elif media and g.get("season") is not None:
                    # back to wanted, except episodes another active download still covers
                    self.db.reset_grab_episodes(g)
                    self.db.execute("UPDATE episodes SET last_search_at = NULL WHERE media_id = ? AND season = ?",
                                    (media["id"], g["season"]))

    VANISHED_GRACE_HOURS = 1.0

    def _forget_vanished(self, g: Dict[str, Any]) -> None:
        """The Download Station task of a grab was deleted (by hand, or never really created) and nothing was
        imported: after a grace period the grab is closed (not blocklisted) and the item is searched again."""
        sent_at = parse_iso(g.get("created_at"))
        if sent_at is None or (_dt.datetime.now() - sent_at).total_seconds() < self.VANISHED_GRACE_HOURS * 3600:
            return
        name = g.get("torrent_name") or g.get("release_title") or ""
        if name and os.path.exists(os.path.join(self.cfg.paths.download_dir, name)):
            return  # files are there: the organizer will import them
        log.info("Tâche Download Station disparue pour %s : téléchargement clos", g.get("release_title"))
        self.db.set_grab_status(g["id"], "failed", "tâche absente de Download Station")
        media = self.db.get_media(g["media_id"]) if g.get("media_id") else None
        if media and media["type"] == "movie" and media.get("status") == "grabbed":
            self.db.set_media_status(media["id"], "wanted")
        elif media and g.get("season") is not None:
            self.db.reset_grab_episodes(g)

    @staticmethod
    def _is_stalled(task: Any, grab: Dict[str, Any], hours: float) -> bool:
        """Still "waiting" with nothing downloaded and no peer, long after it was sent."""
        if task.status not in ("waiting", "unknown"):
            return False
        if (getattr(task, "downloaded", 0) or 0) > 0:
            return False
        if (getattr(task, "peers", None) or 0) > 0 or (getattr(task, "seeders", None) or 0) > 0:
            return False
        started = getattr(task, "created", None)
        sent_at = parse_iso(grab.get("created_at"))
        ages = []
        if started:
            ages.append(time.time() - float(started))
        if sent_at is not None:
            ages.append((_dt.datetime.now() - sent_at).total_seconds())
        return bool(ages) and min(ages) >= hours * 3600

    # ================================================================ manual sources (« Changer de source »)
    def request_for(self, media: Dict[str, Any], season: Optional[int] = None,
                    episode: Optional[int] = None) -> SearchRequest:
        """SearchRequest for exactly this movie / season pack / episode (languages of the item)."""
        common = dict(title=media["title"], year=media.get("year"), imdb=media.get("imdb"), tmdb=media.get("tmdb"),
                      alt_titles=self._alt_titles(media), languages=self.languages_for(media),
                      title_fr=self._title_fr_if_wanted(media))  # type: Dict[str, Any]
        if media.get("type") == "movie":
            return SearchRequest(kind="movie", **common)
        common["tvdb"] = media.get("tvdb")
        if season is None:
            raise ValueError("Précisez la saison de la série")
        if episode is None:
            return SearchRequest(kind="season", season=int(season), **common)
        return SearchRequest(kind="episode", season=int(season), episode=int(episode), **common)

    @staticmethod
    def release_from_link(link: str, label: str = "") -> Release:
        """Release for a pasted magnet link or http(s) .torrent URL. Raises ValueError (French)."""
        import urllib.parse
        link = (link or "").strip()
        fallback = ("%s (lien manuel)" % label) if label else "Lien manuel"
        if not link:
            raise ValueError("Collez un lien magnet ou l'adresse http(s) d'un fichier .torrent")
        if len(link) > 8192 or any(c in link for c in "\r\n\t "):
            raise ValueError("Lien invalide (trop long ou contient des espaces)")
        low = link.lower()
        if low.startswith("magnet:"):
            if not low.startswith("magnet:?xt=urn:btih:"):
                raise ValueError("Lien magnet invalide : il doit commencer par magnet:?xt=urn:btih:")
            name, info_hash = bencode.magnet_info(link)
            if not info_hash:
                raise ValueError("Lien magnet invalide : l'empreinte du torrent (btih) est illisible")
            return Release(title=name or fallback, indexer=MANUAL_INDEXER, magnet=link, info_hash=info_hash)
        if low.startswith(("http://", "https://")):
            try:
                parts = urllib.parse.urlsplit(link)
            except ValueError:
                parts = None
            if parts is None or not parts.hostname:
                raise ValueError("Adresse invalide : http(s)://serveur/fichier.torrent attendu")
            leaf = urllib.parse.unquote(parts.path.rsplit("/", 1)[-1])
            title = _strip_torrent_ext(leaf).strip() if leaf.lower().endswith(".torrent") else ""
            return Release(title=title or fallback, indexer=MANUAL_INDEXER, download_url=link)
        raise ValueError("Lien non reconnu : collez un lien magnet:?xt=urn:btih:… ou une adresse http(s) de "
                         "fichier .torrent")

    def grab_torrent_bytes(self, data: bytes, media: Optional[Dict[str, Any]], season: Optional[int],
                           episode: Optional[int], error_out: Optional[List[str]] = None,
                           resend: bool = False) -> bool:
        if resend:
            self._resend = True
            try:
                ok = self.grab_torrent_bytes(data, media, season, episode, error_out)
            finally:
                self._resend = False
            if ok:
                self._close_duplicates()
            return ok
        return self._grab_torrent_bytes(data, media, season, episode, error_out)

    def _grab_torrent_bytes(self, data: bytes, media: Optional[Dict[str, Any]], season: Optional[int],
                            episode: Optional[int], error_out: Optional[List[str]] = None) -> bool:
        """Send an uploaded .torrent file to Download Station and record it like grab().
        Raises ValueError when *data* is not a valid torrent; returns False (reason in *error_out*) when
        it cannot be sent (dry_run, already sent, Download Station error)."""
        def fail(msg: str) -> bool:
            if error_out is not None:
                error_out.append(msg)
            return False
        try:
            name, info_hash = bencode.torrent_info(bytes(data or b""))
        except (ValueError, TypeError):
            raise ValueError("Fichier .torrent invalide (illisible ou ce n'est pas un torrent)")
        title = name or "Fichier .torrent"
        label = media["title"] if media else title
        if self._hash_already_grabbed(info_hash):
            log.info("Déjà envoyé : %s", title)
            return fail("ce torrent (même info hash) a déjà été envoyé à Download Station")
        if self.cfg.general.dry_run:
            log.info("[dry-run] Enverrait à Download Station : %s", title)
            self.db.add_event("grab", "[dry-run] %s → %s" % (label, title))
            return fail("mode simulation (dry_run) actif : rien n'a été envoyé")
        if self.ds is None:
            return fail("Download Station n'est pas configuré")
        filename = re.sub(r"[^\w.\- ()\[\]]+", "_", name or "release")[:150] + ".torrent"
        try:
            self.ds.add_torrent_file(bytes(data), filename)
        except Exception as exc:
            log.warning("Échec de l'envoi de %s : %s", title, exc)
            self.db.add_event("grab", "Échec de l'envoi de %s : %s" % (title, exc), "ERROR")
            return fail(str(exc) or type(exc).__name__)
        self.db.add_grab(media["id"] if media else None, season, episode, title, name or title, MANUAL_INDEXER,
                         info_hash, torrent_total_size(data) or None)
        what = label
        if season is not None and episode is not None:
            what += " S%02dE%02d" % (season, episode)
        elif season is not None:
            what += " saison %d" % season
        self.db.add_event("grab", "Envoyé à Download Station : %s ← %s [fichier .torrent]" % (what, title))
        log.info("Envoyé à Download Station : %s (%s)", title, what)
        return True


    # ================================================================ RSS sync (F4)
    def rss_sync(self) -> Dict[str, int]:
        """Sonarr-like RSS sync: the latest releases of every indexer are matched against the wanted items
        (aired episodes, movies, quality upgrades) with the same filters as a search; the best one per item is
        sent. Returns counters. Never raises for an indexer error."""
        stats = {"indexers": 0, "releases": 0, "wanted": 0, "grabbed": 0}
        lock = try_file_lock(_data_dir(self.cfg), "search.lock")
        if lock is False:
            log.info("Une recherche est en cours : synchronisation RSS reportée")
            stats["skipped"] = 1
            return stats
        try:
            return self._rss_sync(stats)
        finally:
            release_file_lock(lock)

    def _rss_sync(self, stats: Dict[str, int]) -> Dict[str, int]:
        indexers = self.indexers_provider()
        if not indexers:
            log.info("Synchronisation RSS : aucun indexer actif")
            return stats
        releases = []  # type: List[Release]
        seen = set()
        for idx in indexers:
            if self._stopping():
                return stats
            recent = getattr(idx, "recent", None)
            if recent is None:
                continue
            try:
                found = recent() or []
            except Exception as exc:  # indexers should not raise, be defensive anyway
                log.warning("Indexer %s : flux RSS illisible (%s)", getattr(idx, "name", "?"), exc)
                continue
            if found:
                stats["indexers"] += 1
            for r in found:
                key = (r.indexer, r.title, r.download_url or r.magnet or r.info_hash)
                if key not in seen:
                    seen.add(key)
                    releases.append(r)
        stats["releases"] = len(releases)
        if releases:
            prepared = [(normalize_title(r.title), parse(r.title), r) for r in releases]
            priorities = indexer_priorities(indexers)
            for media in self.db.list_media(on_watchlist_only=True):
                if self._stopping():
                    break
                try:
                    targets = self._rss_targets(media)
                    if not targets:
                        continue
                    stats["wanted"] += len(targets)
                    candidates = self._rss_candidates(targets[0][4], prepared)
                    if candidates:
                        self._rss_grab(media, targets, candidates, priorities, stats)
                except Exception as exc:
                    log.exception("Synchronisation RSS : erreur sur %s : %s", media.get("title"), exc)
        log.info("Synchronisation RSS : %d release(s) récente(s) de %d indexer(s), %d élément(s) voulu(s), "
                 "%d envoi(s) vers Download Station", stats["releases"], stats["indexers"], stats["wanted"],
                 stats["grabbed"])
        return stats

    def _rss_targets(self, media: Dict[str, Any]) -> List[Tuple[str, Optional[int], Optional[int], Optional[str],
                                                                 SearchRequest]]:
        """What this item still needs: [(kind, season, episode, current resolution if upgrade, request)]."""
        raw = []  # type: List[Tuple[str, Optional[int], Optional[int], Optional[str]]]
        if media.get("hidden"):
            return []
        if media["type"] == "movie":
            if media["status"] == "wanted":
                raw.append(("movie", None, None, None))
            elif media["status"] == "done" and self._upgrades_enabled():
                current = self.current_resolution(media)
                if quality.needs_upgrade(current, self.cfg.quality) and not self.db.has_active_grab(media["id"]):
                    raw.append(("movie", None, None, current))
        elif media["status"] == "monitored":
            all_eps = self.db.list_episodes(media["id"])
            wanted = [e for e in all_eps if e["status"] == "wanted" and _is_aired(e["air_date"])]
            by_season = {}  # type: Dict[int, List[Dict[str, Any]]]
            for e in wanted:
                by_season.setdefault(e["season"], []).append(e)
            for season, eps in sorted(by_season.items()):
                complete = all(_is_aired(e["air_date"]) for e in all_eps if e["season"] == season)
                if self.cfg.series.season_packs and complete and len(eps) >= 2:
                    raw.append(("season", season, None, None))
                for e in eps:
                    raw.append(("episode", season, e["episode"], None))
            if self._upgrades_enabled():
                done = [e for e in all_eps if e["status"] == "done"]
                resolutions = self.episode_resolutions(media, done) if done else {}
                for e in done:
                    current = resolutions.get(e["id"])
                    if quality.needs_upgrade(current, self.cfg.quality) and \
                            not self.db.has_active_grab(media["id"], e["season"], e["episode"]):
                        raw.append(("episode", e["season"], e["episode"], current))
        out = []
        for kind, season, episode, current in raw:
            req = self.request_for(media, season, episode)
            out.append((kind, season, episode, current, req))
        return out

    @staticmethod
    def _rss_candidates(req: SearchRequest, prepared: List[Tuple[str, Any, Release]]) -> List[Release]:
        """Cheap pre-filter: releases named after one of the titles of the item (quality.evaluate decides)."""
        titles = [normalize_title(t) for t in [req.title, req.title_fr] + list(req.alt_titles or []) if t]
        titles = [t for t in titles if t]
        out = []
        for norm, parsed, rel in prepared:
            if any(norm == t or norm.startswith(t + " ") for t in titles) or \
                    titles_match(parsed.title, req.title, list(req.alt_titles or []) + ([req.title_fr] if req.title_fr else [])):
                out.append(rel)
        return out

    def _rss_grab(self, media: Dict[str, Any], targets: List[Any], candidates: List[Release],
                  priorities: Dict[str, int], stats: Dict[str, int]) -> None:
        grabbed_seasons = set()  # type: set
        covered = set()  # type: set
        for kind, season, episode, current, req in targets:
            if kind == "episode" and (season in grabbed_seasons or (season, episode) in covered):
                continue
            if current:
                best = quality.pick_upgrade(candidates, req, self.cfg.quality, current, self._is_excluded,
                                            priorities=priorities)
            else:
                best = quality.pick_best(candidates, req, self.cfg.quality, self._is_excluded, priorities=priorities)
            if best is None:
                continue
            log.info("Flux RSS : %s correspond à %s%s", best.title, req.describe(),
                     " (amélioration de %s)" % current if current else "")
            if not self.grab(best, media, season, episode, upgrade=bool(current)):
                continue
            stats["grabbed"] += 1
            if kind == "movie":
                if not current:
                    self.db.execute("UPDATE media SET status = 'grabbed', updated_at = ? WHERE id = ? "
                                    "AND status = 'wanted'", (now_iso(), media["id"]))
            elif kind == "season":
                self.db.set_episode_status(media["id"], season, None, "grabbed", only_if=("wanted",))
                grabbed_seasons.add(season)
            else:
                eps = {episode}
                if not current:
                    eps |= set(parse(best.title).episodes)
                    for ep_num in eps:  # "S01E01E02": both episodes are coming
                        self.db.set_episode_status(media["id"], season, ep_num, "grabbed", only_if=("wanted",))
                covered.update((season, e) for e in eps)


# ====================================================================== shared helpers
MANUAL_INDEXER = "manuel"
FRENCH_FALLBACK_MESSAGE = "Aucun résultat avec le titre français : recherche élargie au titre original"  # Release.indexer of a source chosen by hand (magnet, URL, .torrent file)


def indexer_priorities(indexers: Optional[List[Any]]) -> Dict[str, int]:
    """{indexer name: priority 1..50} for quality.pick_best(priorities=...)."""
    out = {}  # type: Dict[str, int]
    for idx in indexers or []:
        name = getattr(idx, "name", None)
        if name:
            out.setdefault(name, quality.clamp_priority(getattr(idx, "priority", quality.PRIORITY_DEFAULT)))
    return out


def torrent_total_size(data: bytes) -> int:
    """Total size in bytes announced by a .torrent (0 if unknown)."""
    try:
        info = bencode.bdecode(bytes(data)).get(b"info") or {}
        if b"length" in info:
            return int(info[b"length"])
        return sum(int(f.get(b"length") or 0) for f in info.get(b"files") or [] if isinstance(f, dict))
    except Exception:
        return 0


class TaskIndex(object):
    """Download Station tasks indexed to find the task of a grab (exact name with NFC/casefold, then info
    hash from the magnet URI, then normalized title). Shared by the failed-download check and the
    download progress shown in the web UI."""

    def __init__(self, tasks: Optional[List[Any]]):
        self.by_title = {}  # type: Dict[str, Any]
        self.by_hash = {}  # type: Dict[str, Any]
        self.by_norm = {}  # type: Dict[str, Any]
        for t in tasks or []:
            title = getattr(t, "title", "") or ""
            if title:
                self.by_title.setdefault(_fold(title), t)
            norm = normalize_title(_strip_torrent_ext(title))
            if norm:
                self.by_norm.setdefault(norm, t)
            h = (getattr(t, "info_hash", None) or "").lower()
            if h:
                self.by_hash.setdefault(h, t)

    def find(self, grab: Dict[str, Any]) -> Optional[Any]:
        keys = [k for k in (grab.get("torrent_name"), grab.get("release_title")) if k]
        for k in keys:
            task = self.by_title.get(_fold(k))
            if task is not None:
                return task
        h = (grab.get("info_hash") or "").lower()
        if h and h in self.by_hash:
            return self.by_hash[h]
        for k in keys:
            norm = normalize_title(_strip_torrent_ext(k))
            if norm and norm in self.by_norm:
                return self.by_norm[norm]
        return None
