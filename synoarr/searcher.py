"""Orchestration "Radarr/Sonarr": Plex Watchlist -> wanted items -> indexers -> best release -> Download Station."""
from __future__ import annotations

import datetime as _dt
import logging
import os
import re
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
        root = self.cfg.paths.movies_dir
        if not os.path.isdir(root):
            return False
        vexts = self._video_exts()

        def matches(name: str) -> bool:
            p = parse(name)
            if not titles_match(p.title, title, alt_titles):
                return False
            return not (year and p.year and abs(p.year - year) > 1)

        def has_video(folder: str) -> bool:
            for _dirpath, _dirs, files in os.walk(folder):
                if any(_ext(f) in vexts for f in files):
                    return True
            return False

        try:
            names = os.listdir(root)
        except OSError:
            return False
        for name in names:
            if name.startswith((".", "@", "#")):
                continue
            path = os.path.join(root, name)
            if os.path.isfile(path):
                # a stray "Title.nfo" must not count
                if _ext(name) in vexts and matches(name):
                    return True
                continue
            if not os.path.isdir(path):
                continue
            if matches(name) and has_video(path):
                return True
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
                    return True
                if os.path.isdir(cpath) and has_video(cpath):
                    return True
        return False

    def _episodes_in_library(self, show_title: str, alt_titles: Optional[List[str]] = None) -> set:
        found = set()
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
            for _dirpath, _dirs, files in os.walk(os.path.join(root, name)):
                for f in files:
                    if _ext(f) not in vexts:
                        continue
                    p = parse(f)
                    for s in p.seasons[:1]:
                        for e in p.episodes:
                            found.add((s, e))
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
                elif media["type"] == "show" and media["status"] == "monitored":
                    self._search_show(media, indexers, force or bool(media_id), stats)
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
        alts = []
        if media.get("original_title") and media["original_title"] != media["title"]:
            alts.append(media["original_title"])
        return alts

    def _search_movie(self, media: Dict[str, Any], indexers: List["Indexer"], force: bool,
                      stats: Dict[str, int]) -> None:
        if not self._due(media.get("last_search_at"), force):
            return
        req = SearchRequest(languages=self.languages_for(media), kind="movie", title=media["title"], year=media.get("year"), imdb=media.get("imdb"),
                            tmdb=media.get("tmdb"), alt_titles=self._alt_titles(media))
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
                                    alt_titles=self._alt_titles(media))
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
                                    tmdb=media.get("tmdb"), alt_titles=self._alt_titles(media))
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
                             alt_titles=self._alt_titles(media))
        stats["searched"] += 1
        results = self._collect(SearchRequest(languages=self.languages_for(media), kind="season", title=media["title"], season=None,
                                              alt_titles=base.alt_titles), indexers)
        seasons = set()
        for r in results:
            pr = parse(r.title)
            if pr.is_season_pack:
                seasons.update(pr.seasons)
        for season in sorted(seasons):
            if season in grabbed_seasons:
                continue
            req = SearchRequest(languages=self.languages_for(media), kind="season", title=media["title"], year=media.get("year"), season=season,
                                alt_titles=base.alt_titles)
            best = quality.pick_best(results, req, self.cfg.quality, self._is_excluded,
                                     priorities=indexer_priorities(indexers))
            if best and self.grab(best, media, season, None):
                stats["grabbed"] += 1
                grabbed_seasons.update(parse(best.title).seasons)  # "S01-S03" pack covers 3 seasons

    # ------------------------------------------------------------ indexer queries
    def _collect(self, req: SearchRequest, indexers: List["Indexer"]) -> List[Release]:
        results: List[Release] = []
        for idx in indexers:
            try:
                found = idx.search(req)
            except Exception as exc:  # indexers should not raise, be defensive anyway
                log.warning("Indexer %s : erreur %s", getattr(idx, "name", "?"), exc)
                continue
            log.debug("Indexer %s : %d résultat(s) pour %s", idx.name, len(found), req.describe())
            results.extend(found)
        return results

    def _find_best(self, req: SearchRequest, indexers: List["Indexer"]) -> Optional[Release]:
        results = self._collect(req, indexers)
        best = quality.pick_best(results, req, self.cfg.quality, self._is_excluded,
                                 priorities=indexer_priorities(indexers))
        if best is None:
            log.info("Rien de satisfaisant pour %s (%d résultat(s) examiné(s))", req.describe(), len(results))
        else:
            log.info("Meilleur résultat pour %s : %s [%s]", req.describe(), best.title, best.indexer)
        return best

    def _is_excluded(self, release_title: str) -> bool:
        """Blocklisted, or already sent / imported (releases without info hash are recognised by title)."""
        if self.db.is_blocked(release_title):
            return True
        return self.db.query_one("SELECT 1 AS x FROM grabs WHERE lower(release_title) = lower(?) "
                                 "AND status != 'failed'", (release_title,)) is not None

    def _hash_already_grabbed(self, info_hash: Optional[str]) -> bool:
        if not info_hash:
            return False
        return self.db.query_one("SELECT 1 AS x FROM grabs WHERE lower(info_hash) = lower(?) AND status != 'failed'",
                                 (info_hash,)) is not None

    def search_report(self, req: SearchRequest) -> List[Tuple[Release, Optional[int], str]]:
        """Used by the CLI: every result with its score / rejection reason."""
        indexers = self.indexers_provider()
        results = self._collect(req, indexers)
        priorities = indexer_priorities(indexers)
        out = []
        for r in results:
            score, reason = quality.evaluate(r, req, self.cfg.quality, priorities)
            if score is not None and self.db.is_blocked(r.title):
                score, reason = None, "release bloquée (échec précédent)"
            out.append((r, score, reason))
        out.sort(key=lambda t: (t[1] is None, -(t[1] or 0)))
        return out

    # ================================================================ grab
    def grab(self, release: Release, media: Optional[Dict[str, Any]], season: Optional[int],
             episode: Optional[int], error_out: Optional[List[str]] = None) -> bool:
        """Send *release* to Download Station and record it. Returns True on success.
        *error_out* (optional list) receives the French reason of a failure."""
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
        self.db.add_grab(media["id"] if media else None, season, episode, release.title,
                         torrent_name or release.title, release.indexer, info_hash or release.info_hash,
                         release.size or None)
        what = label
        if season is not None and episode is not None:
            what += " S%02dE%02d" % (season, episode)
        elif season is not None:
            what += " saison %d" % season
        self.db.add_event("grab", "Envoyé à Download Station : %s ← %s [%s]" % (what, release.title, release.indexer))
        log.info("Envoyé à Download Station : %s (%s)", release.title, what)
        return True

    def _send(self, release: Release) -> Tuple[Optional[str], Optional[str]]:
        assert self.ds is not None
        if release.download_url:
            resp = self.http.get(release.download_url, raise_for_status=False, timeout=60)
            if resp.redirect_location and resp.redirect_location.lower().startswith("magnet:"):
                return self._send_magnet(resp.redirect_location)
            if resp.ok and resp.body:
                try:
                    name, info_hash = bencode.torrent_info(resp.body)
                except ValueError:
                    if release.magnet:
                        return self._send_magnet(release.magnet)
                    raise ValueError("le lien ne renvoie pas un fichier .torrent valide (%s)"
                                     % redact(release.download_url))
                if self._hash_already_grabbed(info_hash):
                    raise _AlreadyGrabbed()
                filename = re.sub(r"[^\w.\- ()\[\]]+", "_", name or "release")[:150] + ".torrent"
                self.ds.add_torrent_file(resp.body, filename)
                return name, info_hash
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
        self.ds.add_url(magnet)
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
        for g in sent:
            task = index.find(g)
            if task is None or task.status != "error":
                continue
            reason = task.error or "erreur Download Station"
            log.warning("Téléchargement en erreur : %s (%s) → nouvelle recherche", g["release_title"], reason)
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
                    self.db.set_media_status(media["id"], "wanted")
                    self.db.execute("UPDATE media SET last_search_at = NULL WHERE id = ?", (media["id"],))
                elif media and g.get("season") is not None:
                    # back to wanted, except episodes another active download still covers
                    self.db.reset_grab_episodes(g)
                    self.db.execute("UPDATE episodes SET last_search_at = NULL WHERE media_id = ? AND season = ?",
                                    (media["id"], g["season"]))

    # ================================================================ manual sources (« Changer de source »)
    def request_for(self, media: Dict[str, Any], season: Optional[int] = None,
                    episode: Optional[int] = None) -> SearchRequest:
        """SearchRequest for exactly this movie / season pack / episode (languages of the item)."""
        common = dict(title=media["title"], year=media.get("year"), imdb=media.get("imdb"), tmdb=media.get("tmdb"),
                      alt_titles=self._alt_titles(media), languages=self.languages_for(media))  # type: Dict[str, Any]
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


# ====================================================================== shared helpers
MANUAL_INDEXER = "manuel"  # Release.indexer of a source chosen by hand (magnet, URL, .torrent file)


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
