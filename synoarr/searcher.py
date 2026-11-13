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
            return 0
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
        log.info("Watchlist Plex : %d élément(s)", len(items))
        return len(items)

    def _sync_item(self, item: WatchItem) -> None:
        guids = dict(item.guids or {})
        if not guids and self.plex is not None:
            try:
                guids = self.plex.get_guids(item.rating_key)
            except Exception as exc:
                log.debug("guids indisponibles pour %s: %s", item.title, exc)
        existing = self.db.get_media_by_key(item.rating_key)
        media_id = self.db.upsert_media(item.rating_key, item.type, item.title, item.year, guids.get("imdb"),
                                        guids.get("tmdb"), guids.get("tvdb"), item.original_title)
        if existing is None:
            self.db.add_event("watchlist", "Ajouté depuis la Watchlist : %s%s" % (
                item.title, " (%s)" % item.year if item.year else ""))
        media = self.db.get_media(media_id)
        assert media is not None
        if item.type == "movie":
            if media["status"] == "wanted" and self._movie_in_library(item.title, item.year,
                                                                       self._alt_titles(media)):
                self.db.set_media_status(media_id, "done")
                log.info("%s est déjà présent dans %s", item.title, self.cfg.paths.movies_dir)
        else:
            self._sync_episodes(media)

    def _sync_episodes(self, media: Dict[str, Any]) -> None:
        episodes: List[EpisodeInfo] = []
        if self.plex is not None:
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
        root = self.cfg.paths.movies_dir
        if not os.path.isdir(root):
            return False
        try:
            names = os.listdir(root)
        except OSError:
            return False
        vexts = self._video_exts()
        for name in names:
            if name.startswith((".", "@", "#")):
                continue
            p = parse(name)
            if not titles_match(p.title, title, alt_titles):
                continue
            if year and p.year and abs(p.year - year) > 1:
                continue
            path = os.path.join(root, name)
            if os.path.isfile(path):
                if _ext(name) in vexts:  # a stray "Title.nfo" must not hide the "Title (Year)/" folder
                    return True
                continue
            for _dirpath, _dirs, files in os.walk(path):
                if any(_ext(f) in vexts for f in files):
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
        req = SearchRequest(kind="movie", title=media["title"], year=media.get("year"), imdb=media.get("imdb"),
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
                req = SearchRequest(kind="season", title=media["title"], year=media.get("year"), season=season,
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
                req = SearchRequest(kind="episode", title=media["title"], year=media.get("year"), season=season,
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
        base = SearchRequest(kind="season", title=media["title"], year=media.get("year"),
                             alt_titles=self._alt_titles(media))
        stats["searched"] += 1
        results = self._collect(SearchRequest(kind="season", title=media["title"], season=None,
                                              alt_titles=base.alt_titles), indexers)
        seasons = set()
        for r in results:
            pr = parse(r.title)
            if pr.is_season_pack:
                seasons.update(pr.seasons)
        for season in sorted(seasons):
            if season in grabbed_seasons:
                continue
            req = SearchRequest(kind="season", title=media["title"], year=media.get("year"), season=season,
                                alt_titles=base.alt_titles)
            best = quality.pick_best(results, req, self.cfg.quality, self._is_excluded)
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
        best = quality.pick_best(results, req, self.cfg.quality, self._is_excluded)
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
        results = self._collect(req, self.indexers_provider())
        out = []
        for r in results:
            score, reason = quality.evaluate(r, req, self.cfg.quality)
            if score is not None and self.db.is_blocked(r.title):
                score, reason = None, "release bloquée (échec précédent)"
            out.append((r, score, reason))
        out.sort(key=lambda t: (t[1] is None, -(t[1] or 0)))
        return out

    # ================================================================ grab
    def grab(self, release: Release, media: Optional[Dict[str, Any]], season: Optional[int],
             episode: Optional[int]) -> bool:
        """Send *release* to Download Station and record it. Returns True on success."""
        label = media["title"] if media else release.title
        if self._hash_already_grabbed(release.info_hash):
            log.info("Déjà envoyé : %s", release.title)
            return False
        if self.cfg.general.dry_run:
            log.info("[dry-run] Enverrait à Download Station : %s", release.title)
            self.db.add_event("grab", "[dry-run] %s → %s" % (label, release.title))
            return False
        if self.ds is None:
            log.error("Download Station non configuré : impossible d'envoyer %s", release.title)
            return False
        try:
            torrent_name, info_hash = self._send(release)
        except _AlreadyGrabbed:
            log.info("Déjà envoyé (même info hash) : %s", release.title)
            return False
        except Exception as exc:
            log.warning("Échec de l'envoi de %s : %s", release.title, exc)
            self.db.add_event("grab", "Échec de l'envoi de %s : %s" % (release.title, exc), "ERROR")
            # Only a release that is itself broken is blocklisted (dead link, not a torrent). A Download
            # Station problem (session, permissions, destination...), a network error, an indexer that
            # refuses the API key or rate-limits (401/403/429) must not blocklist every release.
            broken = isinstance(exc, ValueError) or (isinstance(exc, HttpError) and exc.status in (404, 410))
            if broken and not _is_ds_error(exc):
                self.db.block_release(release.title, str(exc)[:200])
            return False
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
        by_title = {}  # type: Dict[str, Any]
        by_norm = {}  # type: Dict[str, Any]
        for t in tasks:
            by_title.setdefault(_fold(t.title or ""), t)
            by_norm.setdefault(normalize_title(_strip_torrent_ext(t.title or "")), t)
        for g in sent:
            task = None
            for key in (g.get("torrent_name"), g.get("release_title")):
                if key and task is None:
                    task = by_title.get(_fold(key))
            for key in (g.get("torrent_name"), g.get("release_title")):
                if key and task is None:
                    task = by_norm.get(normalize_title(_strip_torrent_ext(key))) or None
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
