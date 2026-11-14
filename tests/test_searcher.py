"""Unit tests of the Sonarr/Radarr-like logic of searcher.py, with fake Plex, Download Station and indexers."""
from __future__ import annotations

import datetime as _dt
import os
import shutil
import tempfile
import threading
import unittest
from typing import Any, Dict, List, Optional

from synoarr.bencode import bencode, torrent_info
from synoarr.config import Config
from synoarr.db import Database
from synoarr.downloadstation import DownloadStationError
from synoarr.httpclient import HttpError, Response
from synoarr.models import DSTask, EpisodeInfo, Release, WatchItem
from synoarr.searcher import Searcher

GiB = 1024 ** 3
TODAY = _dt.date.today()


def day(offset: int) -> str:
    return (TODAY + _dt.timedelta(days=offset)).isoformat()


def torrent(name: str) -> bytes:
    info = {b"name": name.encode("utf-8"), b"piece length": 16384, b"pieces": b"\0" * 20, b"length": 1000}
    return bencode({b"announce": b"http://tracker.invalid/announce", b"info": info})


class FakePlex:
    def __init__(self) -> None:
        self.items = []  # type: List[WatchItem]
        self.episodes = {}  # type: Dict[str, List[EpisodeInfo]]
        self.fail = False

    def watchlist(self) -> List[WatchItem]:
        if self.fail:
            raise HttpError("plex down", None)
        return list(self.items)

    def get_guids(self, key: str) -> Dict[str, str]:
        return {}

    def show_episodes(self, key: str) -> List[EpisodeInfo]:
        return list(self.episodes.get(key, []))


class FakeDS:
    def __init__(self) -> None:
        self.added = []  # type: List[str]
        self.tasks = []  # type: List[DSTask]
        self.deleted = []  # type: List[str]
        self.fail_add = None  # type: Optional[Exception]

    def add_torrent_file(self, data: bytes, filename: str) -> None:
        if self.fail_add:
            raise self.fail_add
        name, _ = torrent_info(data)
        self.added.append(name)
        self.tasks.append(DSTask(id="t%d" % len(self.tasks), title=name, status="downloading"))

    def add_url(self, url: str) -> None:
        if self.fail_add:
            raise self.fail_add
        self.added.append(url)

    def list_tasks(self) -> List[DSTask]:
        return list(self.tasks)

    def delete_task(self, task_id: str) -> None:
        self.deleted.append(task_id)
        self.tasks = [t for t in self.tasks if t.id != task_id]


class FakeHttp:
    """Serves the .torrent of every release; status overrides per URL."""

    def __init__(self) -> None:
        self.status = {}  # type: Dict[str, int]
        self.names = {}  # type: Dict[str, str]

    def get(self, url: str, **kw: Any) -> Response:
        status = self.status.get(url, 200)
        if status != 200:
            return Response(status, {}, b"error", url)
        name = self.names.get(url) or url.rsplit("/", 1)[-1][:-len(".torrent")]
        return Response(200, {"content-type": "application/x-bittorrent"}, torrent(name), url)


class FakeIndexer:
    name = "fake"

    def __init__(self) -> None:
        self.releases = []  # type: List[Release]
        self.requests = []  # type: List[Any]

    def add(self, title: str, size: float = 2.0, seeders: int = 20, info_hash: Optional[str] = None) -> Release:
        r = Release(title=title, indexer=self.name, download_url="http://idx/dl/%s.torrent" % title,
                    size=int(size * GiB), seeders=seeders, info_hash=info_hash)
        self.releases.append(r)
        return r

    def search(self, req: Any) -> List[Release]:
        self.requests.append(req)
        return list(self.releases)


class SearcherTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="synoarr-search-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.cfg = Config()
        self.cfg.quality.languages = ["en", "fr"]
        self.cfg.quality.prefer_words = []
        self.cfg.downloadstation.send_mode = "file"  # FakeDS records uploaded files (url mode: SendModeTests)
        self.cfg.paths.movies_dir = os.path.join(self.tmp, "Films")
        self.cfg.paths.series_dir = os.path.join(self.tmp, "Séries")
        os.makedirs(self.cfg.paths.movies_dir)
        os.makedirs(self.cfg.paths.series_dir)
        self.db = Database(":memory:")
        self.addCleanup(self.db.close)
        self.plex = FakePlex()
        self.ds = FakeDS()
        self.http = FakeHttp()
        self.idx = FakeIndexer()
        self.searcher = Searcher(self.cfg, self.db, self.http, self.plex, self.ds, None,  # type: ignore[arg-type]
                                 lambda: [self.idx])

    def movie(self, key: str = "m1", title: str = "Heat", year: int = 1995) -> WatchItem:
        item = WatchItem(rating_key=key, type="movie", title=title, year=year)
        self.plex.items.append(item)
        return item

    def show(self, key: str = "s1", title: str = "Lupin", year: int = 2021,
             episodes: Optional[List[EpisodeInfo]] = None) -> WatchItem:
        item = WatchItem(rating_key=key, type="show", title=title, year=year)
        self.plex.items.append(item)
        self.plex.episodes[key] = episodes or []
        return item

    def media(self, key: str) -> Dict[str, Any]:
        m = self.db.get_media_by_key(key)
        assert m is not None
        return m

    def statuses(self, key: str) -> Dict[Any, str]:
        return {(e["season"], e["episode"]): e["status"] for e in self.db.list_episodes(self.media(key)["id"])}

    def touch(self, rel: str) -> None:
        path = os.path.join(self.tmp, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as fh:
            fh.write(b"x")


class WatchlistSyncTests(SearcherTestBase):
    def test_new_removed_and_readded_items(self) -> None:
        self.movie()
        self.assertEqual(self.searcher.sync_watchlist(), 1)
        self.assertEqual(self.media("m1")["status"], "wanted")
        self.plex.items = []
        self.searcher.sync_watchlist()
        self.assertEqual(self.media("m1")["on_watchlist"], 0)
        self.idx.add("Heat.1995.1080p.BluRay.x264-GRP")
        self.assertEqual(self.searcher.run_once()["searched"], 0)  # removed from the watchlist: not searched
        self.movie()
        self.searcher.sync_watchlist()
        self.assertEqual(self.media("m1")["on_watchlist"], 1)
        self.assertEqual(self.searcher.run_once()["grabbed"], 1)

    def test_plex_down_keeps_the_watchlist(self) -> None:
        self.movie()
        self.searcher.sync_watchlist()
        self.plex.fail = True
        with self.assertRaises(HttpError):
            self.searcher.sync_watchlist()
        self.assertEqual(self.media("m1")["on_watchlist"], 1)

    def test_movie_already_in_library(self) -> None:
        self.touch("Films/Heat.nfo")  # a stray file listed first must not hide the folder
        self.touch("Films/Heat (1995)/Heat (1995).mkv")
        self.movie()
        self.searcher.sync_watchlist()
        self.assertEqual(self.media("m1")["status"], "done")

    def test_movie_with_same_title_other_year_is_not_in_library(self) -> None:
        self.touch("Films/Dune (1984)/Dune (1984).mkv")
        self.movie(title="Dune", year=2021)
        self.searcher.sync_watchlist()
        self.assertEqual(self.media("m1")["status"], "wanted")

    def test_monitor_all_and_library_detection(self) -> None:
        self.touch("Séries/Lupin/Season 01/Lupin - S01E01.mkv")
        self.show(episodes=[EpisodeInfo(1, 1, "a", day(-400)), EpisodeInfo(1, 2, "b", day(-393)),
                            EpisodeInfo(2, 1, "c", day(-10)), EpisodeInfo(0, 1, "special", day(-5))])
        self.searcher.sync_watchlist()
        self.assertEqual(self.statuses("s1"), {(1, 1): "done", (1, 2): "wanted", (2, 1): "wanted"})

    def test_monitor_future(self) -> None:
        self.cfg.series.monitor = "future"
        self.show(episodes=[EpisodeInfo(1, 1, "a", day(-400)), EpisodeInfo(1, 2, "b", day(5))])
        self.searcher.sync_watchlist()
        self.assertEqual(self.statuses("s1"), {(1, 1): "skipped", (1, 2): "wanted"})
        # later, new episodes appear in the metadata: they are wanted
        self.plex.episodes["s1"].append(EpisodeInfo(2, 1, "c", day(30)))
        self.searcher.sync_watchlist()
        self.assertEqual(self.statuses("s1")[(2, 1)], "wanted")
        self.assertEqual(self.statuses("s1")[(1, 1)], "skipped")

    def test_monitor_latest_season(self) -> None:
        self.cfg.series.monitor = "latest_season"
        self.show(episodes=[EpisodeInfo(1, 1, "a", day(-400)), EpisodeInfo(2, 1, "b", day(-3)),
                            EpisodeInfo(3, 1, "c", day(60))])
        self.searcher.sync_watchlist()
        self.assertEqual(self.statuses("s1"), {(1, 1): "skipped", (2, 1): "wanted", (3, 1): "wanted"})

    def test_new_episode_covered_by_an_active_pack_grab(self) -> None:
        self.show(episodes=[])
        self.searcher.sync_watchlist()
        mid = self.media("s1")["id"]
        self.db.add_grab(mid, 1, None, "Lupin.S01.1080p", "Lupin.S01.1080p", "fake", None, 1)
        self.plex.episodes["s1"] = [EpisodeInfo(1, 1, "a", day(-400)), EpisodeInfo(2, 1, "b", day(-3))]
        self.searcher.sync_watchlist()
        self.assertEqual(self.statuses("s1"), {(1, 1): "grabbed", (2, 1): "wanted"})


class SearchTests(SearcherTestBase):
    def test_movie_grab_then_research_interval(self) -> None:
        self.movie()
        self.searcher.sync_watchlist()
        self.idx.add("Heat.1995.720p.BluRay.x264-LOW")
        self.idx.add("Heat.1995.1080p.BluRay.x264-GRP")
        self.assertEqual(self.searcher.run_once(), {"searched": 1, "grabbed": 1})
        self.assertEqual(self.ds.added, ["Heat.1995.1080p.BluRay.x264-GRP"])
        self.assertEqual(self.media("m1")["status"], "grabbed")
        self.assertEqual(self.searcher.run_once()["searched"], 0)

    def test_nothing_found_waits_for_research_interval(self) -> None:
        self.movie()
        self.searcher.sync_watchlist()
        self.assertEqual(self.searcher.run_once(), {"searched": 1, "grabbed": 0})
        self.assertEqual(self.searcher.run_once()["searched"], 0)
        self.assertEqual(self.searcher.run_once(force=True)["searched"], 1)

    def test_season_pack_when_season_complete_else_episodes(self) -> None:
        self.show(episodes=[EpisodeInfo(1, 1, "a", day(-30)), EpisodeInfo(1, 2, "b", day(-23)),
                            EpisodeInfo(2, 1, "c", day(-2)), EpisodeInfo(2, 2, "d", day(5))])
        self.searcher.sync_watchlist()
        self.idx.add("Lupin.S01.FRENCH.1080p.WEB.x264-GRP", size=8)
        self.idx.add("Lupin.S02E01.FRENCH.1080p.WEB.x264-GRP", size=1)
        self.idx.add("Lupin.S02E02.FRENCH.1080p.WEB.x264-GRP", size=1)  # not aired yet: never grabbed
        self.searcher.run_once()
        self.assertEqual(sorted(self.ds.added), ["Lupin.S01.FRENCH.1080p.WEB.x264-GRP",
                                                 "Lupin.S02E01.FRENCH.1080p.WEB.x264-GRP"])
        self.assertEqual(self.statuses("s1"), {(1, 1): "grabbed", (1, 2): "grabbed", (2, 1): "grabbed",
                                               (2, 2): "wanted"})
        grabs = {(g["season"], g["episode"]) for g in self.db.list_grabs()}
        self.assertEqual(grabs, {(1, None), (2, 1)})

    def test_multi_episode_release_covers_both_episodes(self) -> None:
        self.cfg.series.season_packs = False
        self.show(episodes=[EpisodeInfo(1, 1, "a", day(-30)), EpisodeInfo(1, 2, "b", day(-30))])
        self.searcher.sync_watchlist()
        self.idx.add("Lupin.S01E01E02.1080p.WEB.x264-GRP", size=2)
        self.searcher.run_once()
        self.assertEqual(self.ds.added, ["Lupin.S01E01E02.1080p.WEB.x264-GRP"])
        self.assertEqual(self.statuses("s1"), {(1, 1): "grabbed", (1, 2): "grabbed"})

    def test_already_grabbed_release_is_not_grabbed_again(self) -> None:
        self.movie()
        self.searcher.sync_watchlist()
        best = self.idx.add("Heat.1995.1080p.BluRay.x264-GRP")
        self.idx.add("Heat.1995.720p.BluRay.x264-LOW")
        mid = self.media("m1")["id"]
        self.db.add_grab(mid, None, None, best.title, best.title, "fake", None, 1)  # imported earlier
        self.searcher.run_once()
        self.assertEqual(self.ds.added, ["Heat.1995.720p.BluRay.x264-LOW"])

    def test_same_info_hash_under_another_title_is_not_sent(self) -> None:
        self.movie()
        self.searcher.sync_watchlist()
        mid = self.media("m1")["id"]
        _, info_hash = torrent_info(torrent("Heat.1995.1080p.BluRay.x264-GRP"))
        self.db.add_grab(mid, None, None, "Heat 1995 1080p (autre indexer)", "x", "other", info_hash, 1)
        self.idx.add("Heat.1995.1080p.BluRay.x264-GRP")  # no info hash in the indexer answer
        self.searcher.run_once()
        self.assertEqual(self.ds.added, [])
        self.assertFalse(self.db.is_blocked("Heat.1995.1080p.BluRay.x264-GRP"))

    def test_failed_download_is_blocklisted_and_replaced(self) -> None:
        self.cfg.series.season_packs = False
        self.show(episodes=[EpisodeInfo(1, 1, "a", day(-30))])
        self.searcher.sync_watchlist()
        self.idx.add("Lupin.S01E01.1080p.WEB.x264-GRP", seeders=50)
        self.idx.add("Lupin.S01E01.720p.WEB.x264-OTHER", seeders=5)
        self.searcher.run_once()
        self.assertEqual(self.ds.added, ["Lupin.S01E01.1080p.WEB.x264-GRP"])
        self.ds.tasks[0].status = "error"
        self.ds.tasks[0].error = "broken_link"
        self.searcher.run_once()
        self.assertTrue(self.db.is_blocked("Lupin.S01E01.1080p.WEB.x264-GRP"))
        self.assertEqual(self.ds.deleted, ["t0"])
        self.assertEqual(self.ds.added[-1], "Lupin.S01E01.720p.WEB.x264-OTHER")
        self.assertEqual(self.statuses("s1"), {(1, 1): "grabbed"})
        statuses = sorted(g["status"] for g in self.db.list_grabs())
        self.assertEqual(statuses, ["failed", "sent"])

    def test_failed_pack_keeps_episodes_of_other_active_grabs(self) -> None:
        self.show(episodes=[EpisodeInfo(1, 1, "a", day(-30)), EpisodeInfo(1, 2, "b", day(-30))])
        self.searcher.sync_watchlist()
        mid = self.media("s1")["id"]
        self.db.set_episode_status(mid, 1, None, "grabbed")
        self.db.add_grab(mid, 1, 1, "Lupin.S01E01.1080p", "Lupin.S01E01.1080p", "fake", None, 1)
        self.db.add_grab(mid, 1, None, "Lupin.S01.1080p", "Lupin.S01.1080p", "fake", None, 1)
        self.ds.tasks = [DSTask("a", "Lupin.S01E01.1080p", "downloading"), DSTask("b", "Lupin.S01.1080p", "error")]
        self.searcher.check_failed_downloads()
        self.assertEqual(self.statuses("s1"), {(1, 1): "grabbed", (1, 2): "wanted"})

    def test_download_station_errors_do_not_blocklist(self) -> None:
        self.movie()
        self.searcher.sync_watchlist()
        rel = self.idx.add("Heat.1995.1080p.BluRay.x264-GRP")
        self.ds.fail_add = DownloadStationError("Dossier de destination introuvable", 403)
        self.searcher.run_once()
        self.assertFalse(self.db.is_blocked(rel.title))
        self.assertEqual(self.media("m1")["status"], "wanted")
        self.ds.fail_add = None
        self.http.status[rel.download_url] = 429  # indexer rate limit: temporary
        self.searcher.run_once(force=True)
        self.assertFalse(self.db.is_blocked(rel.title))
        self.http.status[rel.download_url] = 404  # dead link: blocklisted
        self.searcher.run_once(force=True)
        self.assertTrue(self.db.is_blocked(rel.title))

    def test_discovery_mode_without_episode_list(self) -> None:
        self.show(episodes=[])
        self.searcher.sync_watchlist()
        self.idx.add("Lupin.S01-S03.MULTi.1080p.WEB.x264-GRP", size=20)
        self.idx.add("Lupin.S02.MULTi.1080p.WEB.x264-GRP", size=8)
        self.idx.add("Lupin.S04.MULTi.1080p.WEB.x264-GRP", size=8)
        self.searcher.run_once()
        self.assertEqual(sorted(self.ds.added), ["Lupin.S01-S03.MULTi.1080p.WEB.x264-GRP",
                                                 "Lupin.S04.MULTi.1080p.WEB.x264-GRP"])
        self.searcher.run_once(force=True)  # nothing new
        self.assertEqual(len(self.ds.added), 2)

    def test_dry_run_sends_nothing(self) -> None:
        self.cfg.general.dry_run = True
        self.movie()
        self.searcher.sync_watchlist()
        self.idx.add("Heat.1995.1080p.BluRay.x264-GRP")
        self.searcher.run_once()
        self.assertEqual(self.ds.added, [])
        self.assertEqual(self.media("m1")["status"], "wanted")

    def test_dry_run_does_not_delay_real_searches(self) -> None:
        self.cfg.general.dry_run = True
        self.show(episodes=[EpisodeInfo(1, 1, "a", day(-30))])
        self.movie()
        self.searcher.sync_watchlist()
        self.searcher.run_once()
        self.assertIsNone(self.media("m1")["last_search_at"])
        self.assertEqual({e["last_search_at"] for e in self.db.list_episodes(self.media("s1")["id"])}, {None})
        self.cfg.general.dry_run = False
        self.assertEqual(self.searcher.run_once()["searched"], 2)

    def test_concurrent_search_process_is_locked_out(self) -> None:
        from synoarr import db as db_mod
        if db_mod._fcntl is None:  # pragma: no cover
            self.skipTest("fcntl indisponible")
        self.cfg.general.data_dir = self.tmp
        self.movie()
        self.searcher.sync_watchlist()
        fd = db_mod.try_file_lock(self.tmp, "search.lock")
        try:
            self.assertEqual(self.searcher.run_once()["searched"], 0)
        finally:
            db_mod.release_file_lock(fd)
        self.assertEqual(self.searcher.run_once()["searched"], 1)

    def test_stop_event_interrupts_the_cycle(self) -> None:
        self.movie()
        self.searcher.sync_watchlist()
        self.idx.add("Heat.1995.1080p.BluRay.x264-GRP")
        self.searcher.stop_event = threading.Event()
        self.searcher.stop_event.set()
        self.assertEqual(self.searcher.run_once()["searched"], 0)

    def test_grabbed_status_does_not_overwrite_done(self) -> None:
        self.movie()
        self.searcher.sync_watchlist()
        mid = self.media("m1")["id"]
        self.idx.add("Heat.1995.1080p.BluRay.x264-GRP")
        original = self.searcher.grab

        def grab_and_import(*a: Any, **kw: Any) -> bool:
            ok = original(*a, **kw)
            self.db.set_media_status(mid, "done")  # the organizer imports meanwhile
            return ok
        self.searcher.grab = grab_and_import  # type: ignore[assignment]
        self.searcher.run_once()
        self.assertEqual(self.media("m1")["status"], "done")


if __name__ == "__main__":
    unittest.main()


class LanguageAndCollectionTests(SearcherTestBase):
    """English by default, French for animation (genre from Plex or TMDb), collections from TMDb."""

    def setUp(self) -> None:
        super().setUp()
        self.cfg.quality.languages = ["en"]
        self.cfg.quality.languages_animation = ["fr"]

    def test_live_action_movie_gets_english_release(self) -> None:
        self.movie(title="Heat", year=1995)
        self.idx.add("Heat.1995.TRUEFRENCH.1080p.BluRay.x264-FR", seeders=500)
        self.idx.add("Heat.1995.1080p.BluRay.x264-GRP", seeders=5)
        self.searcher.sync_watchlist()
        self.assertEqual(self.media("m1")["is_animation"], 0)
        self.searcher.run_once()
        self.assertEqual([g["release_title"] for g in self.db.list_grabs()], ["Heat.1995.1080p.BluRay.x264-GRP"])

    def test_animated_movie_gets_french_release(self) -> None:
        item = self.movie(title="Coco", year=2017)
        item.genres = ["Animation", "Family"]
        self.idx.add("Coco.2017.1080p.BluRay.x264-GRP", seeders=500)
        self.idx.add("Coco.2017.TRUEFRENCH.1080p.BluRay.x264-FR", seeders=5)
        self.searcher.sync_watchlist()
        self.assertEqual(self.media("m1")["is_animation"], 1)
        self.searcher.run_once()
        self.assertEqual([g["release_title"] for g in self.db.list_grabs()],
                         ["Coco.2017.TRUEFRENCH.1080p.BluRay.x264-FR"])

    def test_genres_and_collection_from_plex_details_and_tmdb(self) -> None:
        class Plex(FakePlex):
            def get_details(self, key: str) -> Dict[str, Any]:
                return {"guids": {}, "genres": ["Animation"]}

        class Tmdb:
            enabled = True

            def movie_details(self, tmdb_id: str) -> Dict[str, Any]:
                return {"collection": "Toy Story Collection", "genres": ["Animation"], "is_animation": True}

            def find_by_imdb(self, imdb: str) -> Dict[str, str]:
                return {"movie": "863"}

        self.searcher.plex = Plex()
        self.searcher.tmdb = Tmdb()
        item = WatchItem(rating_key="m9", type="movie", title="Toy Story 2", year=1999, guids={"imdb": "tt0120363"})
        self.searcher.plex.items.append(item)
        self.searcher.sync_watchlist()
        m = self.media("m9")
        self.assertEqual(m["collection"], "Toy Story Collection")
        self.assertEqual(m["is_animation"], 1)
        self.assertEqual(self.searcher.languages_for(m), ["fr"])

    def test_language_override(self) -> None:
        self.movie(title="Heat", year=1995)
        self.searcher.sync_watchlist()
        self.db.set_language_override(self.media("m1")["id"], "fr, en")
        self.assertEqual(self.searcher.languages_for(self.media("m1")), ["fr", "en"])

    def test_movie_in_collection_folder_or_flat_is_detected(self) -> None:
        self.touch("Films/Alien Collection/Aliens (1986).mkv")
        self.touch("Films/Heat (1995).mkv")
        self.touch("Films/Heat.nfo")
        self.assertTrue(self.searcher._movie_in_library("Aliens", 1986))
        self.assertTrue(self.searcher._movie_in_library("Heat", 1995))
        self.assertFalse(self.searcher._movie_in_library("Alien", 1979))


class ManualSourceTests(SearcherTestBase):
    MAGNET = "magnet:?xt=urn:btih:ABCDEF0123456789ABCDEF0123456789ABCDEF01&dn=Heat.1995.1080p.BluRay-MAN"

    def setUp(self) -> None:
        super().setUp()
        self.movie()
        self.searcher.sync_watchlist()
        self.m = self.media("m1")

    def test_release_from_link_validation(self) -> None:
        r = Searcher.release_from_link(self.MAGNET, "Heat")
        self.assertEqual((r.title, r.indexer, r.info_hash), ("Heat.1995.1080p.BluRay-MAN", "manuel",
                                                             "abcdef0123456789abcdef0123456789abcdef01"))
        r = Searcher.release_from_link("magnet:?xt=urn:btih:" + "a" * 40, "Heat")
        self.assertEqual(r.title, "Heat (lien manuel)")
        r = Searcher.release_from_link("https://site.example/dl/Heat%201995.torrent?passkey=SECRET", "Heat")
        self.assertEqual((r.title, r.download_url is not None), ("Heat 1995", True))
        self.assertNotIn("SECRET", r.title)
        r = Searcher.release_from_link("http://site.example/get?id=4", "Heat")
        self.assertEqual(r.title, "Heat (lien manuel)")
        for bad in ("", "magnet:?dn=x&xt=urn:btih:" + "a" * 40, "magnet:?xt=urn:btih:zz", "ftp://x/y.torrent",
                    "https://", "javascript:alert(1)", "http://a b/c"):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    Searcher.release_from_link(bad)

    def test_grab_manual_magnet_and_url(self) -> None:
        rel = Searcher.release_from_link(self.MAGNET, "Heat")
        self.assertTrue(self.searcher.grab(rel, self.m, None, None))
        self.assertEqual(len(self.ds.added), 1)
        self.assertEqual(self.ds.added[0], self.MAGNET)  # sent exactly as provided
        g = self.db.list_grabs()[0]
        self.assertEqual((g["indexer"], g["info_hash"], g["media_id"]),
                         ("manuel", "abcdef0123456789abcdef0123456789abcdef01", self.m["id"]))
        errors = []  # type: List[str]
        self.assertFalse(self.searcher.grab(rel, self.m, None, None, error_out=errors))
        self.assertIn("déjà été envoyé", errors[0])
        url = Searcher.release_from_link("http://idx/dl/Heat.1995.720p.WEB-DL.torrent", "Heat")
        self.assertTrue(self.searcher.grab(url, self.m, None, None))
        self.assertEqual(self.ds.added[-1], "Heat.1995.720p.WEB-DL")

    def test_broken_manual_link_is_not_blocklisted(self) -> None:
        url = Searcher.release_from_link("http://idx/dl/x.torrent", "Heat")
        self.http.status["http://idx/dl/x.torrent"] = 404
        errors = []  # type: List[str]
        self.assertFalse(self.searcher.grab(url, self.m, None, None, error_out=errors))
        self.assertIn("404", errors[0])
        self.assertFalse(self.db.is_blocked(url.title))

    def test_grab_torrent_bytes(self) -> None:
        data = torrent("Heat.1995.1080p.BluRay.x264-UP")
        errors = []  # type: List[str]
        self.assertTrue(self.searcher.grab_torrent_bytes(data, self.m, None, None, error_out=errors), errors)
        self.assertEqual(self.ds.added, ["Heat.1995.1080p.BluRay.x264-UP"])
        g = self.db.list_grabs()[0]
        self.assertEqual((g["release_title"], g["torrent_name"], g["indexer"], g["size"]),
                         ("Heat.1995.1080p.BluRay.x264-UP", "Heat.1995.1080p.BluRay.x264-UP", "manuel", 1000))
        self.assertFalse(self.searcher.grab_torrent_bytes(data, self.m, None, None, error_out=errors))
        self.assertIn("déjà", errors[-1])
        with self.assertRaises(ValueError):
            self.searcher.grab_torrent_bytes(b"<html>nope</html>", self.m, None, None)
        self.ds.fail_add = DownloadStationError("Dossier de destination introuvable")
        self.assertFalse(self.searcher.grab_torrent_bytes(torrent("Other"), self.m, None, None, error_out=errors))
        self.assertIn("destination", errors[-1])

    def test_grab_torrent_bytes_dry_run(self) -> None:
        self.cfg.general.dry_run = True
        errors = []  # type: List[str]
        self.assertFalse(self.searcher.grab_torrent_bytes(torrent("Heat.1995"), self.m, None, None, errors))
        self.assertIn("simulation", errors[0])
        self.assertEqual(self.ds.added, [])

    def test_request_for(self) -> None:
        req = self.searcher.request_for(self.m)
        self.assertEqual((req.kind, req.title, req.year, req.languages), ("movie", "Heat", 1995, ["en", "fr"]))
        self.show(episodes=[EpisodeInfo(1, 1, "a", day(-10))])
        self.searcher.sync_watchlist()
        show = self.media("s1")
        self.assertEqual(self.searcher.request_for(show, 1, None).kind, "season")
        req = self.searcher.request_for(show, 1, 2)
        self.assertEqual((req.kind, req.season, req.episode), ("episode", 1, 2))
        with self.assertRaises(ValueError):
            self.searcher.request_for(show)

    def test_indexer_priority_used_by_the_search(self) -> None:
        other = FakeIndexer()
        other.name = "favori"
        other.priority = 1  # type: ignore[attr-defined]
        self.idx.priority = 50  # type: ignore[attr-defined]
        self.idx.add("Heat.1995.1080p.BluRay.x264-MANY", seeders=5000)
        other.add("Heat.1995.1080p.BluRay.x264-PRIO", seeders=2)
        self.searcher.indexers_provider = lambda: [self.idx, other]
        self.searcher.run_once(force=True)
        self.assertEqual(self.db.list_grabs()[0]["release_title"], "Heat.1995.1080p.BluRay.x264-PRIO")
        report = self.searcher.search_report(self.searcher.request_for(self.m))
        self.assertEqual(report[0][0].indexer, "favori")


class TaskIndexTests(unittest.TestCase):
    def test_matching_order(self) -> None:
        from synoarr.searcher import TaskIndex
        tasks = [DSTask(id="1", title="Amélie.2001.1080p", status="downloading"),
                 DSTask(id="2", title="abcdef0123456789abcdef0123456789abcdef01", status="downloading",
                        info_hash="abcdef0123456789abcdef0123456789abcdef01"),
                 DSTask(id="3", title="The.Show.S01E01.720p.torrent", status="error")]
        idx = TaskIndex(tasks)
        nfd = "Amélie.2001.1080p"
        self.assertEqual(idx.find({"torrent_name": nfd, "release_title": "x"}).id, "1")
        self.assertEqual(idx.find({"torrent_name": "Lien manuel", "release_title": "Lien manuel",
                                   "info_hash": "ABCDEF0123456789ABCDEF0123456789ABCDEF01"}).id, "2")
        self.assertEqual(idx.find({"torrent_name": None, "release_title": "The Show S01E01 720p"}).id, "3")
        self.assertIsNone(idx.find({"torrent_name": "", "release_title": "Nothing"}))
        self.assertIsNone(TaskIndex([DSTask(id="9", title="", status="x")]).find({"release_title": "..."}))


class StalledMagnetTests(SearcherTestBase):
    def test_magnet_gets_trackers_and_stalled_download_is_replaced(self) -> None:
        import time as _time
        from synoarr.models import Release
        self.movie(title="Heat", year=1995)
        self.searcher.sync_watchlist()
        media = self.media("m1")
        rel = Release(title="Heat.1995.1080p.BluRay.x264-GRP", indexer="idx",
                      magnet="magnet:?xt=urn:btih:%s&dn=Heat" % ("a" * 40))
        self.cfg.downloadstation.extra_trackers = ["udp://tracker.opentrackr.org:1337/announce"]
        self.assertTrue(self.searcher.grab(rel, media, None, None))
        sent = self.ds.added[-1]
        self.assertIn("tr=udp%3A%2F%2Ftracker.opentrackr.org", sent)
        # Download Station: still waiting, no peer, created 7 h ago -> abandoned after stalled_hours (6)
        self.ds.tasks = [DSTask(id="m1", title="Heat", status="waiting", info_hash="a" * 40,
                                created=int(_time.time() - 7 * 3600), peers=0, seeders=0)]
        self.db.execute("UPDATE grabs SET created_at = datetime('now', 'localtime', '-7 hours')")
        self.db.set_media_status(media["id"], "grabbed")
        self.searcher.check_failed_downloads()
        grab = self.db.list_grabs()[0]
        self.assertEqual(grab["status"], "failed")
        self.assertIn("aucune source", grab["message"])
        self.assertTrue(self.db.is_blocked("Heat.1995.1080p.BluRay.x264-GRP"))
        self.assertEqual(self.ds.deleted, ["m1"])
        self.assertEqual(self.media("m1")["status"], "wanted")

    def test_recent_or_active_download_is_not_stalled(self) -> None:
        import time as _time
        task = DSTask(id="x", title="X", status="waiting", created=int(_time.time() - 3600), peers=0)
        self.assertFalse(self.searcher._is_stalled(task, {"created_at": None}, 6))
        task = DSTask(id="x", title="X", status="waiting", created=int(_time.time() - 9 * 3600), peers=3)
        self.assertFalse(self.searcher._is_stalled(task, {"created_at": None}, 6))
        task = DSTask(id="x", title="X", status="downloading", created=int(_time.time() - 9 * 3600))
        self.assertFalse(self.searcher._is_stalled(task, {"created_at": None}, 6))



class SendModeTests(SearcherTestBase):
    """Links from the indexer are handed over unmodified."""

    def test_url_mode_gives_the_link_to_download_station(self) -> None:
        self.cfg.downloadstation.send_mode = "url"
        self.movie(title="Heat", year=1995)
        self.searcher.sync_watchlist()
        rel = self.idx.add("Heat.1995.1080p.BluRay.x264-GRP")
        self.assertTrue(self.searcher.grab(rel, self.media("m1"), None, None))
        self.assertEqual(self.ds.added, [rel.download_url])  # the indexer link itself, not a re-upload
        g = self.db.list_grabs()[0]
        self.assertEqual(g["torrent_name"], "Heat.1995.1080p.BluRay.x264-GRP")  # still known for matching

    def test_url_mode_falls_back_to_original_file(self) -> None:
        self.cfg.downloadstation.send_mode = "url"
        self.movie(title="Heat", year=1995)
        self.searcher.sync_watchlist()
        rel = self.idx.add("Heat.1995.1080p.BluRay.x264-GRP")
        uploads = []

        def refuse(url: str) -> None:
            raise RuntimeError("lien injoignable")
        self.ds.add_url = refuse  # type: ignore[assignment]
        orig = self.ds.add_torrent_file
        self.ds.add_torrent_file = lambda data, fn: (uploads.append(data), orig(data, fn))  # type: ignore
        self.assertTrue(self.searcher.grab(rel, self.media("m1"), None, None))
        self.assertEqual(uploads, [self.http.get(rel.download_url).body])  # byte-for-byte original

    def test_magnet_sent_as_is_by_default(self) -> None:
        from synoarr.models import Release
        self.movie(title="Heat", year=1995)
        self.searcher.sync_watchlist()
        magnet = "magnet:?xt=urn:btih:%s&dn=Heat%%201995&tr=udp%%3A%%2F%%2Fx" % ("b" * 40)
        self.assertTrue(self.searcher.grab(Release(title="Heat.1995.1080p", indexer="i", magnet=magnet),
                                           self.media("m1"), None, None))
        self.assertEqual(self.ds.added, [magnet])

    def test_unparseable_but_torrent_looking_file_is_still_sent(self) -> None:
        from synoarr.searcher import _looks_like_torrent
        self.assertTrue(_looks_like_torrent(b"d8:announce3:abc4:infod4:name1:xee"))
        self.assertFalse(_looks_like_torrent(b"<html>error</html>"))



class LinkTypeTests(unittest.TestCase):
    def test_probe_classifies_links(self) -> None:
        from synoarr.app import App
        from synoarr.models import Release
        from tests.fakes import FakeServer

        def handler(req: Any) -> Any:
            if req.path == "/t.torrent":
                return 200, {}, torrent("Movie.2020.1080p")
            if req.path == "/m":
                return 302, {"Location": "magnet:?xt=urn:btih:" + "c" * 40}, b""
            return 404, {}, b"nope"

        class Job:
            def emit(self, level: str, msg: str) -> None:
                pass
        with FakeServer(handler) as srv:
            rels = [Release(title="A", indexer="i", download_url=srv.url + "/t.torrent"),
                    Release(title="B", indexer="i", download_url=srv.url + "/m"),
                    Release(title="C", indexer="i", magnet="magnet:?xt=urn:btih:" + "d" * 40),
                    Release(title="D", indexer="i", download_url=srv.url + "/missing")]
            report = [(r, None, "") for r in rels]
            results = [{"index": i} for i in range(len(rels))]
            App._detect_link_types(object.__new__(App), Job(), report, results)
        self.assertEqual([r["link_type"] for r in results], ["torrent", "magnet", "magnet", "inconnu"])



class PreferMagnetTests(SearcherTestBase):
    def test_prefer_magnet_like_prowlarr(self) -> None:
        from synoarr.models import Release
        self.movie(title="Heat", year=1995)
        self.searcher.sync_watchlist()
        magnet = "magnet:?xt=urn:btih:%s&dn=Heat" % ("e" * 40)
        rel = self.idx.add("Heat.1995.1080p.BluRay.x264-GRP")
        rel = Release(title=rel.title, indexer="i", download_url=rel.download_url, magnet=magnet)
        self.cfg.downloadstation.prefer = "magnet"
        self.assertTrue(self.searcher.grab(rel, self.media("m1"), None, None))
        self.assertEqual(self.ds.added, [magnet])


class FrenchTitleTests(SearcherTestBase):
    """French chosen -> the French title is searched too (French releases use it)."""

    def setUp(self) -> None:
        super().setUp()

        class Tmdb:
            enabled = True
            calls = []

            def localized_title(self, tmdb_id: str, kind: str, language: str = "fr-FR") -> Optional[str]:
                self.calls.append((tmdb_id, kind, language))
                return "Les Évadés" if tmdb_id == "278" else None

            def find_by_imdb(self, imdb: str) -> Dict[str, str]:
                return {"movie": "278"}

            def movie_details(self, tmdb_id: str) -> Dict[str, Any]:
                return {"collection": None, "genres": ["Drama"], "is_animation": False}
        self.tmdb = Tmdb()
        self.searcher.tmdb = self.tmdb
        self.queries = []
        orig = self.idx.search

        def search(req: Any) -> List[Release]:
            self.queries.append(req.title)
            return orig(req)
        self.idx.search = search  # type: ignore[assignment]

    def test_french_release_found_with_french_title(self) -> None:
        item = WatchItem(rating_key="m7", type="movie", title="The Shawshank Redemption", year=1994,
                         guids={"tmdb": "278"})
        self.plex.items.append(item)
        self.searcher.sync_watchlist()
        m = self.media("m7")
        self.db.set_language_override(m["id"], "fr")
        self.idx.add("Les.Evades.1994.MULTi.1080p.BluRay.x264-FR")
        self.idx.add("The.Shawshank.Redemption.1994.1080p.BluRay.x264-EN")  # English only: rejected for fr
        stats = self.searcher.run_once(media_id=m["id"], force=True)
        self.assertEqual(stats["grabbed"], 1)
        self.assertEqual([g["release_title"] for g in self.db.list_grabs()],
                         ["Les.Evades.1994.MULTi.1080p.BluRay.x264-FR"])
        self.assertIn("Les Évadés", self.queries)
        self.assertEqual(self.media("m7")["title_fr"], "Les Évadés")  # cached
        n = len(self.tmdb.calls)
        self.searcher.request_for(self.media("m7"))
        self.assertEqual(len(self.tmdb.calls), n)  # no new TMDb call

    def test_english_items_do_not_search_the_french_title(self) -> None:
        self.cfg.quality.languages = ["en"]
        item = WatchItem(rating_key="m8", type="movie", title="The Shawshank Redemption", year=1994,
                         guids={"tmdb": "278"})
        self.plex.items.append(item)
        self.searcher.sync_watchlist()
        self.searcher.run_once(media_id=self.media("m8")["id"], force=True)
        self.assertNotIn("Les Évadés", self.queries)


class VanishedTaskTests(SearcherTestBase):
    def test_task_deleted_by_hand_closes_the_grab_after_grace(self) -> None:
        self.movie(title="Heat", year=1995)
        self.searcher.sync_watchlist()
        m = self.media("m1")
        rel = self.idx.add("Heat.1995.1080p.BluRay.x264-GRP")
        self.assertTrue(self.searcher.grab(rel, m, None, None))
        self.db.set_media_status(m["id"], "grabbed")
        self.ds.tasks = []  # removed from Download Station
        self.searcher.check_failed_downloads()
        self.assertEqual(self.db.list_grabs()[0]["status"], "sent")  # grace period
        self.db.execute("UPDATE grabs SET created_at = datetime('now', 'localtime', '-2 hours')")
        self.searcher.check_failed_downloads()
        g = self.db.list_grabs()[0]
        self.assertEqual((g["status"], g["message"]), ("failed", "tâche absente de Download Station"))
        self.assertFalse(self.db.is_blocked(rel.title))  # not the release's fault
        self.assertEqual(self.media("m1")["status"], "wanted")


class FrenchSearchEndToEndTests(SearcherTestBase):
    """Reproduces the user's case: an indexer answering id searches with English-named releases only."""

    def setUp(self) -> None:
        super().setUp()
        self.cfg.quality.languages = ["en"]

        class Tmdb:
            enabled = True

            def localized_title(self, tmdb_id: str, kind: str, language: str = "fr-FR") -> Optional[str]:
                return "Les Évadés"

            def find_by_imdb(self, imdb: str) -> Dict[str, str]:
                return {"movie": "278"}

            def movie_details(self, tmdb_id: str) -> Dict[str, Any]:
                return {"collection": None, "genres": ["Drama"], "is_animation": False}
        self.searcher.tmdb = Tmdb()
        self.calls = []

        class IdFirstIndexer:
            name = "Torznab"
            priority = 25

            def __init__(self, outer: Any) -> None:
                self.outer = outer

            def rel(self, title: str, seeders: int) -> Release:
                url = "http://idx/%s.torrent" % title
                return Release(title=title, indexer=self.name, download_url=url, size=int(2e9), seeders=seeders)

            def search(self, req: Any) -> List[Release]:
                self.outer.calls.append((req.title, req.imdb))
                if req.imdb:  # id search: English-named results only
                    return [self.rel("The.Shawshank.Redemption.1994.1080p.BluRay.x264-EN", 900)]
                if "vad" in req.title.lower():  # text search with the French title
                    return [self.rel("Les.Evades.1994.1080p.BluRay.x264-NOTAG", 300),
                            self.rel("Les.Evades.1994.TRUEFRENCH.1080p.WEB-DL.x264-VF", 100)]
                return []
        self.searcher.indexers_provider = lambda: [IdFirstIndexer(self)]

    def test_french_selected_gets_a_french_release(self) -> None:
        item = WatchItem(rating_key="m9", type="movie", title="The Shawshank Redemption", year=1994,
                         guids={"imdb": "tt0111161", "tmdb": "278"})
        self.plex.items.append(item)
        self.searcher.sync_watchlist()
        mid = self.media("m9")["id"]
        self.db.set_language_override(mid, "fr")
        self.searcher.run_once(media_id=mid, force=True)
        self.assertIn(("Les Évadés", None), self.calls)  # French title searched as text, without the id
        grabs = [g["release_title"] for g in self.db.list_grabs()]
        self.assertEqual(grabs, ["Les.Evades.1994.TRUEFRENCH.1080p.WEB-DL.x264-VF"])  # French title + VF preferred

    def test_english_default_keeps_the_english_release(self) -> None:
        item = WatchItem(rating_key="m10", type="movie", title="The Shawshank Redemption", year=1994,
                         guids={"imdb": "tt0111161", "tmdb": "278"})
        self.plex.items.append(item)
        self.searcher.sync_watchlist()
        self.searcher.run_once(media_id=self.media("m10")["id"], force=True)
        self.assertEqual([g["release_title"] for g in self.db.list_grabs()],
                         ["The.Shawshank.Redemption.1994.1080p.BluRay.x264-EN"])
