from __future__ import annotations

import errno
import os
import shutil
import tempfile
import time
import unittest
from typing import Any, Dict, List, Optional
from unittest import mock

from synoarr import db as db_mod
from synoarr import organizer as organizer_mod
from synoarr.config import Config
from synoarr.db import Database
from synoarr.models import DSTask
from synoarr.organizer import Organizer


class FakeDS:
    def __init__(self, tasks: Optional[List[DSTask]] = None, fail: bool = False):
        self.tasks = tasks or []
        self.fail = fail
        self.deleted = []  # type: List[str]

    def list_tasks(self) -> List[DSTask]:
        if self.fail:
            raise OSError("connexion refusée")
        return list(self.tasks)

    def delete_task(self, task_id: str) -> None:
        self.deleted.append(task_id)


class FakeTmdb:
    def __init__(self, movies: Optional[Dict[str, Dict]] = None, shows: Optional[Dict[str, Dict]] = None,
                 episodes: Optional[Dict[Any, str]] = None):
        self.enabled = True
        self.movies = movies or {}
        self.shows = shows or {}
        self.episodes = episodes or {}
        self.calls = []  # type: List[Any]

    def search_movie(self, title: str, year: Optional[int]) -> Optional[Dict]:
        self.calls.append(("movie", title, year))
        return self.movies.get(title)

    def search_show(self, title: str) -> Optional[Dict]:
        self.calls.append(("show", title))
        return self.shows.get(title)

    def episode_title(self, tmdb_id: str, season: int, episode: int) -> Optional[str]:
        self.calls.append(("episode", tmdb_id, season, episode))
        return self.episodes.get((tmdb_id, season, episode))


class OrganizerTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="synoarr-org-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.dl = os.path.join(self.tmp, "Download")
        self.movies = os.path.join(self.tmp, "Vidéos", "Films")
        self.series = os.path.join(self.tmp, "Vidéos", "Séries")
        os.makedirs(self.dl)
        self.cfg = Config()
        self.cfg.general.data_dir = os.path.join(self.tmp, "data")
        self.cfg.paths.download_dir = self.dl
        self.cfg.paths.movies_dir = self.movies
        self.cfg.paths.series_dir = self.series
        self.cfg.paths.min_age_minutes = 0
        # these tests exercise the mechanics with the 1.0 naming layout (new layout: NewNamingTests)
        self.cfg.naming.movie_format = "{title} ({year})/{title} ({year})"
        self.cfg.naming.series_format = "{show}/Season {season:02d}/{show} - S{season:02d}E{episode:02d}"
        self.db = Database(":memory:")
        self.addCleanup(self.db.close)
        self.ds = FakeDS()
        self.refreshed = 0

    def org(self, ds: Any = "default", tmdb: Any = None) -> Organizer:
        def _refresh() -> None:
            self.refreshed += 1
        o = Organizer(self.cfg, self.db, self.ds if ds == "default" else ds, tmdb, on_imported=_refresh)
        o.min_video_size = 0
        return o

    def grab(self, name: str, media_id: Optional[int] = None) -> int:
        """Record *name* as a download sent by synoarr (only those leftover folders may be deleted)."""
        return self.db.add_grab(media_id, None, None, name, name, "idx", None, 1000)

    def make(self, rel: str, size: int = 1000, fill: bytes = b"v", base: Optional[str] = None) -> str:
        path = os.path.join(base or self.dl, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as fh:
            fh.write((fill * size)[:size])
        return path

    def age(self, path: str, minutes: float) -> None:
        ts = time.time() - minutes * 60
        for dirpath, dirs, files in os.walk(path, topdown=False):
            for n in dirs + files:
                os.utime(os.path.join(dirpath, n), (ts, ts))
        os.utime(path, (ts, ts))

    def movie_path(self, *parts: str) -> str:
        return os.path.join(self.movies, *parts)

    def series_path(self, *parts: str) -> str:
        return os.path.join(self.series, *parts)

    @staticmethod
    def read(path: str) -> bytes:
        with open(path, "rb") as fh:
            return fh.read()

    def by_status(self, actions: List[Dict], status: str) -> List[Dict]:
        return [a for a in actions if a["status"] == status]


class MovieTests(OrganizerTestBase):
    def test_movie_folder_with_sample_nfo_and_subtitles(self) -> None:
        entry = "Inception.2010.1080p.BluRay.x264-GRP"
        self.make(entry + "/Inception.2010.1080p.BluRay.x264-GRP.mkv", 3000)
        self.make(entry + "/Sample/inception.sample.mkv", 100)
        self.make(entry + "/Inception.2010.1080p.BluRay.x264-GRP.nfo", 10)
        self.make(entry + "/Subs/2_English.srt", 10)
        self.make(entry + "/Subs/3_French.srt", 11)
        self.make(entry + "/Subs/4_French.forced.srt", 12)
        self.grab(entry)
        o = self.org()
        o.min_video_size = 1000
        actions = o.run_once()
        dst = self.movie_path("Inception (2010)", "Inception (2010).mkv")
        self.assertTrue(os.path.isfile(dst), actions)
        self.assertEqual(len(self.read(dst)), 3000)
        self.assertTrue(os.path.isfile(self.movie_path("Inception (2010)", "Inception (2010).en.srt")))
        self.assertTrue(os.path.isfile(self.movie_path("Inception (2010)", "Inception (2010).fr.srt")))
        self.assertTrue(os.path.isfile(self.movie_path("Inception (2010)", "Inception (2010).fr.forced.srt")))
        self.assertFalse(os.path.exists(os.path.join(self.movies, "Inception (2010)", "inception.sample.mkv")))
        # the sample is junk: the leftover folder is removed after the move
        self.assertFalse(os.path.exists(os.path.join(self.dl, entry)))
        self.assertEqual(len(self.by_status(actions, "imported")), 4)
        self.assertEqual(self.refreshed, 1)
        self.assertEqual(self.db.recent_events()[0]["kind"], "import")

    def test_single_file_movie_with_sibling_subtitle(self) -> None:
        self.make("The.Matrix.1999.1080p.BluRay.mkv")
        self.make("The.Matrix.1999.1080p.BluRay.fr.srt", 5)
        self.make("unrelated.srt", 5)
        self.org().run_once()
        self.assertTrue(os.path.isfile(self.movie_path("The Matrix (1999)", "The Matrix (1999).mkv")))
        self.assertTrue(os.path.isfile(self.movie_path("The Matrix (1999)", "The Matrix (1999).fr.srt")))
        self.assertTrue(os.path.isfile(os.path.join(self.dl, "unrelated.srt")))

    def test_movie_without_year_has_no_empty_parentheses(self) -> None:
        self.make("Some Movie 1080p.mkv")
        self.org().run_once()
        self.assertTrue(os.path.isfile(self.movie_path("Some Movie", "Some Movie.mkv")),
                        os.listdir(self.movies))

    def test_lowercase_title_is_title_cased_but_not_mixed_case(self) -> None:
        self.make("inception.2010.1080p.bluray.x264-grp.mkv")
        self.make("RoboCop.1987.720p.mkv")
        self.org().run_once()
        self.assertTrue(os.path.isfile(self.movie_path("Inception (2010)", "Inception (2010).mkv")))
        self.assertTrue(os.path.isfile(self.movie_path("RoboCop (1987)", "RoboCop (1987).mkv")))

    def test_unicode_paths(self) -> None:
        self.dl = os.path.join(self.tmp, "Vidéos", "Téléchargements")
        os.makedirs(self.dl)
        self.cfg.paths.download_dir = self.dl
        self.make("Le.Fabuleux.Destin.d.Amélie.Poulain.2001.FRENCH.1080p/Amélie.2001.mkv")
        self.make("Le.Fabuleux.Destin.d.Amélie.Poulain.2001.FRENCH.1080p/Amélie.2001.français.srt", 5)
        actions = self.org().run_once()
        folder = "Le Fabuleux Destin d Amélie Poulain (2001)"
        self.assertTrue(os.path.isfile(self.movie_path(folder, folder + ".mkv")), actions)
        self.assertTrue(os.path.isfile(self.movie_path(folder, folder + ".fr.srt")))

    def test_small_extra_videos_ignored_but_only_video_kept(self) -> None:
        self.make("Big.Movie.2015.1080p/Big.Movie.2015.1080p.mkv", 3000)
        self.make("Big.Movie.2015.1080p/Featurette.mkv", 100)
        self.make("Tiny.Movie.2016.mkv", 100)
        o = self.org()
        o.min_video_size = 1000
        actions = o.run_once()
        self.assertTrue(os.path.isfile(self.movie_path("Big Movie (2015)", "Big Movie (2015).mkv")))
        self.assertTrue(os.path.isfile(self.movie_path("Tiny Movie (2016)", "Tiny Movie (2016).mkv")))
        # the featurette is a real (non-sample) video: the folder is kept
        self.assertTrue(os.path.isfile(os.path.join(self.dl, "Big.Movie.2015.1080p", "Featurette.mkv")))
        self.assertTrue(any("Featurette" in a["src"] and a["status"] == "skipped" for a in actions))

    def test_default_threshold_is_50_mb(self) -> None:
        self.assertEqual(organizer_mod.MIN_VIDEO_SIZE, 50 * 1024 * 1024)
        self.assertEqual(Organizer.min_video_size, 50 * 1024 * 1024)

    def test_two_big_videos_extra_left_in_place(self) -> None:
        self.make("Movie.2012.1080p/Movie.2012.1080p.mkv", 3000)
        self.make("Movie.2012.1080p/Behind.The.Scenes.mkv", 2000)
        self.org().run_once()
        self.assertTrue(os.path.isfile(self.movie_path("Movie (2012)", "Movie (2012).mkv")))
        self.assertEqual(len(self.read(self.movie_path("Movie (2012)", "Movie (2012).mkv"))), 3000)
        self.assertTrue(os.path.isfile(os.path.join(self.dl, "Movie.2012.1080p", "Behind.The.Scenes.mkv")))

    def test_multi_part_movie(self) -> None:
        self.make("Old.Movie.1980/Old.Movie.1980.CD1.avi", 2000)
        self.make("Old.Movie.1980/Old.Movie.1980.CD2.avi", 1900)
        self.grab("Old.Movie.1980")
        self.org().run_once()
        self.assertTrue(os.path.isfile(self.movie_path("Old Movie (1980)", "Old Movie (1980) - pt1.avi")))
        self.assertTrue(os.path.isfile(self.movie_path("Old Movie (1980)", "Old Movie (1980) - pt2.avi")))
        self.assertFalse(os.path.exists(os.path.join(self.dl, "Old.Movie.1980")))

    def test_tmdb_used_when_download_unknown(self) -> None:
        tmdb = FakeTmdb(movies={"Le Fabuleux Destin": {"title": "Amélie: Le Film", "year": 2001, "tmdb": "194"}})
        self.make("Le.Fabuleux.Destin.2001.1080p.mkv")
        self.org(tmdb=tmdb).run_once()
        self.assertTrue(os.path.isfile(self.movie_path("Amélie - Le Film (2001)", "Amélie - Le Film (2001).mkv")),
                        os.listdir(self.movies))
        self.assertIn(("movie", "Le Fabuleux Destin", 2001), tmdb.calls)

    def test_tmdb_failure_falls_back_to_parse(self) -> None:
        tmdb = FakeTmdb()
        tmdb.search_movie = mock.Mock(side_effect=RuntimeError("boom"))  # type: ignore[assignment]
        self.make("Heat.1995.1080p.mkv")
        self.org(tmdb=tmdb).run_once()
        self.assertTrue(os.path.isfile(self.movie_path("Heat (1995)", "Heat (1995).mkv")))


class GrabMetadataTests(OrganizerTestBase):
    def test_grab_metadata_overrides_release_name(self) -> None:
        mid = self.db.upsert_media("plex://movie/1", "movie", "Le Fabuleux Destin d'Amélie Poulain: Édition",
                                   2001, "tt0211915", "194", None)
        entry = "Amelie.2001.1080p.BluRay.x264-GRP"
        gid = self.db.add_grab(mid, None, None, entry, entry, "idx", None, 1000)
        self.make(entry + "/amelie.mkv")
        self.org().run_once()
        folder = "Le Fabuleux Destin d'Amélie Poulain - Édition (2001)"
        self.assertTrue(os.path.isfile(self.movie_path(folder, folder + ".mkv")), os.listdir(self.movies))
        self.assertEqual(self.db.get_media(mid)["status"], "done")
        self.assertEqual(self.db.query_one("SELECT status FROM grabs WHERE id = ?", (gid,))["status"], "imported")

    def test_series_grab_uses_db_show_title_and_file_episodes(self) -> None:
        mid = self.db.upsert_media("plex://show/1", "show", "The Office (US)", 2005, None, None, "73244")
        for ep in (1, 2, 3):
            self.db.upsert_episode(mid, 2, ep, "Ep %d" % ep, None, status="grabbed")
        entry = "The.Office.US.S02.720p.WEB-DL"
        gid = self.db.add_grab(mid, 2, None, entry, entry, "idx", None, 1000)
        self.make(entry + "/The.Office.US.S02E01.720p.mkv")
        self.make(entry + "/The.Office.US.S02E02.720p.mkv")
        self.cfg.naming.series_format = "{show}/Season {season:02d}/{show} - S{season:02d}E{episode:02d} - {episode_title}"
        self.org().run_once()
        self.assertTrue(os.path.isfile(self.series_path("The Office (US)", "Season 02",
                                                        "The Office (US) - S02E01 - Ep 1.mkv")),
                        os.listdir(self.series_path("The Office (US)", "Season 02")))
        self.assertTrue(os.path.isfile(self.series_path("The Office (US)", "Season 02",
                                                        "The Office (US) - S02E02 - Ep 2.mkv")))
        statuses = {e["episode"]: e["status"] for e in self.db.list_episodes(mid)}
        # E03 was not in the season pack: back to "wanted" so that the searcher looks for it again
        self.assertEqual(statuses, {1: "done", 2: "done", 3: "wanted"})
        self.assertEqual(self.db.query_one("SELECT status FROM grabs WHERE id = ?", (gid,))["status"], "imported")

    def test_single_episode_grab_with_obfuscated_file_name(self) -> None:
        mid = self.db.upsert_media("plex://show/2", "show", "Dark", 2017, None, None, None)
        entry = "Dark.S01E05.1080p.NF.WEB-DL"
        self.db.add_grab(mid, 1, 5, entry, entry, "idx", None, 1000)
        self.make(entry + "/abc123xyz.mkv")
        self.org().run_once()
        self.assertTrue(os.path.isfile(self.series_path("Dark", "Season 01", "Dark - S01E05.mkv")))

    def test_only_known_downloads(self) -> None:
        self.cfg.paths.only_known_downloads = True
        mid = self.db.upsert_media("plex://movie/9", "movie", "Known", 2020, None, None, None)
        self.db.add_grab(mid, None, None, "Known.2020.1080p", "Known.2020.1080p", "idx", None, 1)
        self.make("Known.2020.1080p/Known.2020.1080p.mkv")
        self.make("Unknown.2019.1080p/Unknown.2019.1080p.mkv")
        actions = self.org().run_once()
        self.assertTrue(os.path.isfile(self.movie_path("Known (2020)", "Known (2020).mkv")))
        self.assertTrue(os.path.isfile(os.path.join(self.dl, "Unknown.2019.1080p", "Unknown.2019.1080p.mkv")))
        self.assertTrue(any(a["status"] == "skipped" and "Unknown" in a["src"] for a in actions))


class SeriesTests(OrganizerTestBase):
    def test_season_pack_with_nested_season_folder_and_multi_episode(self) -> None:
        entry = "Breaking.Bad.S01.1080p.BluRay.x264-GRP"
        self.make(entry + "/Season 1/Breaking.Bad.S01E01.1080p.BluRay.mkv")
        self.make(entry + "/Season 1/Breaking.Bad.S01E02.1080p.BluRay.mkv")
        self.make(entry + "/Season 1/Breaking.Bad.S01E03E04.1080p.BluRay.mkv")
        self.make(entry + "/Season 1/Breaking.Bad.S01E02.1080p.BluRay.en.srt", 5)
        self.make(entry + "/Breaking.Bad.S01.nfo", 5)
        self.grab(entry)
        actions = self.org().run_once()
        season = self.series_path("Breaking Bad", "Season 01")
        self.assertEqual(sorted(os.listdir(season)), [
            "Breaking Bad - S01E01.mkv", "Breaking Bad - S01E02.en.srt", "Breaking Bad - S01E02.mkv",
            "Breaking Bad - S01E03-E04.mkv"], actions)
        self.assertFalse(os.path.exists(os.path.join(self.dl, entry)))

    def test_single_episode_file_lowercase(self) -> None:
        self.make("show.name.s02e05.720p.hdtv.mkv")
        self.org().run_once()
        self.assertTrue(os.path.isfile(self.series_path("Show Name", "Season 02", "Show Name - S02E05.mkv")))

    def test_pack_with_unparseable_file_keeps_folder(self) -> None:
        entry = "Some.Show.S03.720p"
        self.make(entry + "/Some.Show.S03E01.720p.mkv")
        self.make(entry + "/Bonus Interview.mkv")
        actions = self.org().run_once()
        self.assertTrue(os.path.isfile(self.series_path("Some Show", "Season 03", "Some Show - S03E01.mkv")))
        self.assertTrue(os.path.isfile(os.path.join(self.dl, entry, "Bonus Interview.mkv")))
        self.assertTrue(any(a["status"] == "skipped" and "Bonus" in a["src"] for a in actions))

    def test_tmdb_show_and_episode_title(self) -> None:
        tmdb = FakeTmdb(shows={"Lost": {"title": "Lost: Les Disparus", "year": 2004, "tmdb": "4607"}},
                        episodes={("4607", 1, 2): "Pilot (2)"})
        self.cfg.naming.series_format = "{show}/Season {season:02d}/{show} - S{season:02d}E{episode:02d} - {episode_title}"
        self.make("Lost.S01E02.720p.mkv")
        self.org(tmdb=tmdb).run_once()
        self.assertTrue(os.path.isfile(self.series_path("Lost - Les Disparus", "Season 01",
                                                        "Lost - Les Disparus - S01E02 - Pilot (2).mkv")),
                        os.listdir(self.series))

    def test_empty_episode_title_leaves_no_trailing_dash(self) -> None:
        self.cfg.naming.series_format = "{show}/Season {season:02d}/{show} - S{season:02d}E{episode:02d} - {episode_title}"
        self.make("Lost.S01E03.720p.mkv")
        self.org().run_once()
        self.assertTrue(os.path.isfile(self.series_path("Lost", "Season 01", "Lost - S01E03.mkv")),
                        os.listdir(self.series_path("Lost", "Season 01")))


class SafetyTests(OrganizerTestBase):
    def test_active_ds_task_is_skipped(self) -> None:
        entry = "Movie.2010.1080p-GRP"
        self.make(entry + "/Movie.2010.1080p-GRP.mkv")
        self.ds.tasks = [DSTask(id="dbid_1", title=entry.lower(), status="downloading")]
        actions = self.org().run_once()
        self.assertTrue(os.path.isfile(os.path.join(self.dl, entry, entry + ".mkv")))
        self.assertFalse(os.path.exists(self.movies))
        self.assertEqual(actions[0]["status"], "skipped")
        self.assertIn("Download Station", actions[0]["message"])

    def test_error_task_is_skipped(self) -> None:
        self.make("Broken.2011.mkv")
        self.ds.tasks = [DSTask(id="dbid_2", title="Broken.2011.mkv", status="error")]
        self.org().run_once()
        self.assertTrue(os.path.isfile(os.path.join(self.dl, "Broken.2011.mkv")))

    def test_active_task_found_through_grab_torrent_name(self) -> None:
        entry = "Movie.2010.1080p-GRP"
        self.db.add_grab(None, None, None, entry, "www.site.com - Movie 2010", "idx", None, 1)
        self.make(entry + "/" + entry + ".mkv")
        self.ds.tasks = [DSTask(id="dbid_3", title="www.site.com - Movie 2010", status="paused")]
        self.org().run_once()
        self.assertTrue(os.path.isfile(os.path.join(self.dl, entry, entry + ".mkv")))

    def test_completed_task_deleted_after_import(self) -> None:
        entry = "Movie.2010.1080p-GRP"
        self.make(entry + "/" + entry + ".mkv")
        self.ds.tasks = [DSTask(id="dbid_4", title=entry, status="seeding"),
                         DSTask(id="dbid_5", title="Other", status="finished")]
        self.org().run_once()
        self.assertTrue(os.path.isfile(self.movie_path("Movie (2010)", "Movie (2010).mkv")))
        self.assertEqual(self.ds.deleted, ["dbid_4"])

    def test_task_kept_when_option_disabled(self) -> None:
        self.cfg.paths.remove_task_after_import = False
        self.make("Movie.2010.mkv")
        self.ds.tasks = [DSTask(id="dbid_6", title="Movie.2010.mkv", status="finished")]
        self.org().run_once()
        self.assertEqual(self.ds.deleted, [])

    def test_ds_unreachable_doubles_min_age(self) -> None:
        self.cfg.paths.min_age_minutes = 5
        self.ds.fail = True
        recent = self.make("Recent.2001.mkv")
        old = self.make("Old.2002.mkv")
        self.age(recent, 7)
        self.age(old, 11)
        with self.assertLogs("synoarr.organizer", "WARNING") as logs:
            actions = self.org().run_once()
        self.assertTrue(any("injoignable" in m for m in logs.output))
        self.assertTrue(os.path.isfile(recent))
        self.assertTrue(os.path.isfile(self.movie_path("Old (2002)", "Old (2002).mkv")))
        self.assertEqual([a["status"] for a in actions if "Recent" in a["src"]], ["skipped"])

    def test_no_ds_configured(self) -> None:
        self.make("Movie.2010.mkv")
        self.org(ds=None).run_once()
        self.assertTrue(os.path.isfile(self.movie_path("Movie (2010)", "Movie (2010).mkv")))

    def test_age_guard(self) -> None:
        self.cfg.paths.min_age_minutes = 5
        entry = self.make("Fresh.2020.1080p/Fresh.2020.1080p.mkv")
        folder = os.path.dirname(entry)
        actions = self.org().run_once()
        self.assertEqual(actions[0]["status"], "skipped")
        self.assertTrue(os.path.isfile(entry))
        self.age(folder, 6)
        self.org().run_once()
        self.assertTrue(os.path.isfile(self.movie_path("Fresh (2020)", "Fresh (2020).mkv")))

    def test_age_guard_uses_newest_nested_file(self) -> None:
        self.cfg.paths.min_age_minutes = 5
        folder = os.path.dirname(self.make("Nested.2020/Nested.2020.mkv"))
        self.age(folder, 30)
        self.make("Nested.2020/sub/new.nfo", 3)  # just written
        self.org().run_once()
        self.assertTrue(os.path.isfile(os.path.join(folder, "Nested.2020.mkv")))

    def test_ignored_entries_and_partial_files(self) -> None:
        self.make(".hidden.2010.mkv")
        self.make("@eaDir/thumb.mkv")
        self.make("#recycle/Movie.2010.mkv")
        self.make("Download.2010.mkv.part")
        self.make("Partial.2010/Partial.2010.mkv")
        self.make("Partial.2010/Partial.2010.part2.mkv.!ut")
        self.make("readme.txt")
        actions = self.org().run_once()
        self.assertFalse(os.path.exists(self.movies))
        self.assertEqual([(os.path.basename(a["src"]), a["status"]) for a in actions], [("Partial.2010", "skipped")])

    def test_dry_run_touches_nothing(self) -> None:
        entry = "Inception.2010.1080p"
        self.make(entry + "/Inception.2010.1080p.mkv")
        self.make(entry + "/Inception.2010.1080p.en.srt", 5)
        self.ds.tasks = [DSTask(id="dbid_7", title=entry, status="finished")]
        before = sorted(os.listdir(os.path.join(self.dl, entry)))
        actions = self.org().run_once(dry_run=True)
        self.assertEqual(sorted(os.listdir(os.path.join(self.dl, entry))), before)
        self.assertFalse(os.path.exists(self.movies))
        self.assertEqual(self.ds.deleted, [])
        self.assertEqual(self.refreshed, 0)
        self.assertEqual({a["status"] for a in actions}, {"planned"})
        self.assertIn(self.movie_path("Inception (2010)", "Inception (2010).mkv"), [a["dst"] for a in actions])
        self.assertEqual(self.db.recent_events(), [])

    def test_config_dry_run_forces_dry_run(self) -> None:
        self.cfg.general.dry_run = True
        self.make("Movie.2010.mkv")
        actions = self.org().run_once(dry_run=False)
        self.assertEqual(actions[0]["status"], "planned")
        self.assertTrue(os.path.isfile(os.path.join(self.dl, "Movie.2010.mkv")))

    def test_plan_does_not_touch_anything(self) -> None:
        src = self.make("Movie.2010.mkv")
        plan = self.org().plan(src)
        self.assertEqual(plan[0][0], src)
        self.assertEqual(plan[0][1], self.movie_path("Movie (2010)", "Movie (2010).mkv"))
        self.assertEqual(plan[0][2]["kind"], "movie")
        self.assertTrue(os.path.isfile(src))
        self.assertFalse(os.path.exists(self.movies))

    def test_run_once_never_raises(self) -> None:
        self.make("Aaa.2010.mkv")
        self.make("Bbb.2011.mkv")
        o = self.org()
        real = o._plan_entry

        def flaky(entry_path: str, name: str, grab: Any) -> Any:
            if name.startswith("Aaa"):
                raise RuntimeError("fichier corrompu")
            return real(entry_path, name, grab)
        o._plan_entry = flaky  # type: ignore[assignment]
        with self.assertLogs("synoarr.organizer", "ERROR"):
            actions = o.run_once()
        self.assertEqual(self.by_status(actions, "error")[0]["src"], os.path.join(self.dl, "Aaa.2010.mkv"))
        self.assertTrue(os.path.isfile(self.movie_path("Bbb (2011)", "Bbb (2011).mkv")))

    def test_missing_download_dir(self) -> None:
        self.cfg.paths.download_dir = os.path.join(self.tmp, "nope")
        self.assertEqual(self.org().run_once(), [])

    def test_library_inside_download_dir_is_protected(self) -> None:
        self.cfg.paths.movies_dir = os.path.join(self.dl, "Films")
        self.make("Films/Existing (2000)/Existing (2000).mkv")
        actions = self.org().run_once()
        self.assertEqual(actions, [])
        self.assertTrue(os.path.isfile(os.path.join(self.dl, "Films", "Existing (2000)", "Existing (2000).mkv")))


class DestinationTests(OrganizerTestBase):
    def setUp(self) -> None:
        super().setUp()
        self.src = self.make("Movie.2010.1080p.mkv", 2000, b"n")
        self.dst = self.movie_path("Movie (2010)", "Movie (2010).mkv")

    def test_destination_same_size_same_content(self) -> None:
        self.make(self.dst, 2000, b"n", base="/")
        actions = self.org().run_once()
        self.assertEqual(actions[0]["status"], "imported")
        self.assertFalse(os.path.exists(self.src))
        self.assertEqual(self.read(self.dst), b"n" * 2000)
        self.assertEqual(self.refreshed, 0)  # nothing new in the library

    def test_destination_same_size_different_content_kept(self) -> None:
        self.make(self.dst, 2000, b"o", base="/")
        actions = self.org().run_once()
        self.assertEqual(actions[0]["status"], "skipped")
        self.assertTrue(os.path.isfile(self.src))
        self.assertEqual(self.read(self.dst), b"o" * 2000)

    def test_destination_bigger_is_kept(self) -> None:
        self.make(self.dst, 5000, b"o", base="/")
        actions = self.org().run_once()
        self.assertEqual(actions[0]["status"], "skipped")
        self.assertTrue(os.path.isfile(self.src))
        self.assertEqual(self.read(self.dst), b"o" * 5000)

    def test_destination_smaller_is_replaced(self) -> None:
        self.make(self.dst, 500, b"o", base="/")
        actions = self.org().run_once()
        self.assertEqual(actions[0]["status"], "imported")
        self.assertFalse(os.path.exists(self.src))
        self.assertEqual(self.read(self.dst), b"n" * 2000)
        self.assertEqual(os.listdir(os.path.dirname(self.dst)), ["Movie (2010).mkv"])

    def test_copy_mode(self) -> None:
        self.cfg.paths.action = "copy"
        actions = self.org().run_once()
        self.assertEqual(actions[0]["status"], "imported")
        self.assertTrue(os.path.isfile(self.src))
        self.assertEqual(self.read(self.dst), b"n" * 2000)
        self.assertNotEqual(os.stat(self.src).st_ino, os.stat(self.dst).st_ino)

    def test_copy_mode_second_cycle_is_quiet(self) -> None:
        self.cfg.paths.action = "copy"
        o = self.org()
        o.run_once()
        events = len(self.db.recent_events())
        self.assertEqual(o.run_once(), [])
        self.assertEqual(len(self.db.recent_events()), events)
        self.assertEqual(self.refreshed, 1)
        # a fresh organizer (restart) sees it already imported, without new event nor refresh
        actions = self.org().run_once()
        self.assertEqual(actions[0]["message"], "déjà présent")
        self.assertEqual(len(self.db.recent_events()), events)
        self.assertEqual(self.refreshed, 1)
        self.assertTrue(os.path.isfile(self.src))

    def test_hardlink_mode(self) -> None:
        self.cfg.paths.action = "hardlink"
        self.org().run_once()
        self.assertTrue(os.path.isfile(self.src))
        self.assertEqual(os.stat(self.src).st_ino, os.stat(self.dst).st_ino)
        # second run with a new organizer: same inode => already present
        actions = self.org().run_once()
        self.assertEqual(actions[0]["status"], "imported")
        self.assertTrue(os.path.isfile(self.src))

    def test_hardlink_falls_back_to_copy(self) -> None:
        self.cfg.paths.action = "hardlink"
        with mock.patch("synoarr.organizer.os.link", side_effect=OSError(errno.EXDEV, "cross-device")):
            actions = self.org().run_once()
        self.assertEqual(actions[0]["status"], "imported")
        self.assertIn("copie", actions[0]["message"])
        self.assertTrue(os.path.isfile(self.src))
        self.assertNotEqual(os.stat(self.src).st_ino, os.stat(self.dst).st_ino)
        self.assertEqual(self.read(self.dst), b"n" * 2000)

    def test_move_across_filesystems(self) -> None:
        with mock.patch("synoarr.organizer.os.rename", side_effect=OSError(errno.EXDEV, "cross-device")):
            actions = self.org().run_once()
        self.assertEqual(actions[0]["status"], "imported")
        self.assertFalse(os.path.exists(self.src))
        self.assertEqual(self.read(self.dst), b"n" * 2000)
        self.assertEqual(os.listdir(os.path.dirname(self.dst)), ["Movie (2010).mkv"])

    def test_failed_copy_leaves_source_and_no_partial_file(self) -> None:
        with mock.patch("synoarr.organizer.os.rename", side_effect=OSError(errno.EXDEV, "cross-device")), \
                mock.patch("synoarr.organizer.shutil.copyfile", side_effect=OSError(errno.ENOSPC, "disque plein")):
            actions = self.org().run_once()
        self.assertEqual(actions[0]["status"], "error")
        self.assertTrue(os.path.isfile(self.src))
        self.assertEqual(os.listdir(os.path.dirname(self.dst)), [])


class LeftoverTests(OrganizerTestBase):
    def test_leftovers_kept_when_disabled(self) -> None:
        self.cfg.paths.delete_leftovers = False
        self.make("Movie.2010/Movie.2010.mkv")
        self.make("Movie.2010/Movie.2010.nfo", 5)
        self.org().run_once()
        self.assertTrue(os.path.isfile(os.path.join(self.dl, "Movie.2010", "Movie.2010.nfo")))

    def test_leftovers_kept_in_copy_mode(self) -> None:
        self.cfg.paths.action = "copy"
        self.make("Movie.2010/Movie.2010.mkv")
        self.org().run_once()
        self.assertTrue(os.path.isfile(os.path.join(self.dl, "Movie.2010", "Movie.2010.mkv")))

    def test_leftovers_kept_when_import_failed(self) -> None:
        self.make("Movie.2010/Movie.2010.mkv", 100)
        self.make("Movie.2010/Movie.2010.nfo", 5)
        self.make(self.movie_path("Movie (2010)", "Movie (2010).mkv"), 500, base="/")  # bigger, kept
        self.org().run_once()
        self.assertTrue(os.path.isfile(os.path.join(self.dl, "Movie.2010", "Movie.2010.mkv")))

    def test_leftovers_deleted_when_no_video_remains(self) -> None:
        self.make("Movie.2010/Movie.2010.mkv")
        self.make("Movie.2010/Extras/poster.jpg", 5)
        self.make("Movie.2010/Movie.2010.nfo", 5)
        self.grab("Movie.2010")
        self.org().run_once()
        self.assertFalse(os.path.exists(os.path.join(self.dl, "Movie.2010")))

    def test_unknown_folder_is_never_deleted(self) -> None:
        # QA case: a personal folder dropped in Download must keep its other files
        self.make("My Holiday 2019/clip.mp4", 3000)
        self.make("My Holiday 2019/taxes.pdf", 10)
        self.make("My Holiday 2019/notes.nfo", 10)
        self.org().run_once()
        self.assertTrue(os.path.isfile(os.path.join(self.dl, "My Holiday 2019", "taxes.pdf")))
        self.assertTrue(os.path.isfile(os.path.join(self.dl, "My Holiday 2019", "notes.nfo")))

    def test_grabbed_folder_with_non_junk_file_is_kept(self) -> None:
        self.make("Movie.2010/Movie.2010.mkv")
        self.make("Movie.2010/Movie.2010.nfo", 5)
        self.make("Movie.2010/bonus.pdf", 5)
        self.grab("Movie.2010")
        self.org().run_once()
        self.assertTrue(os.path.isfile(self.movie_path("Movie (2010)", "Movie (2010).mkv")))
        self.assertTrue(os.path.isfile(os.path.join(self.dl, "Movie.2010", "bonus.pdf")))

    def test_trailers_and_extras_are_not_the_movie(self) -> None:
        self.make("Trailer.Pack.2020/Big.Movie.2020.Trailer.1080p.mkv", 3000)
        actions = self.org().run_once()
        self.assertFalse(os.path.exists(self.movies), actions)
        self.make("Big.Movie.2020.1080p/Big.Movie.2020.1080p.mkv", 2000)
        self.make("Big.Movie.2020.1080p/Featurettes/Making.Of.mkv", 3000)
        self.org().run_once()
        self.assertEqual(os.listdir(self.movie_path("Big Movie (2020)")), ["Big Movie (2020).mkv"])
        self.make("Trailer.Park.Boys.S01E01.mkv")
        self.org().run_once()
        self.assertTrue(os.path.isfile(self.series_path("Trailer Park Boys", "Season 01",
                                                        "Trailer Park Boys - S01E01.mkv")))

    def test_missing_share_is_never_created(self) -> None:
        from unittest import mock
        self.make("Movie.2010/Movie.2010.mkv")
        with mock.patch.object(organizer_mod, "missing_share_root", return_value="/volume1/Videos"):
            actions = self.org().run_once()
        self.assertEqual([a["status"] for a in actions], ["error"])
        self.assertIn("/volume1/Videos", actions[0]["message"])
        self.assertFalse(os.path.exists(self.movies))
        self.assertTrue(os.path.isfile(os.path.join(self.dl, "Movie.2010", "Movie.2010.mkv")))

    def test_movie_named_sample_extra_not_treated_as_junk(self) -> None:
        self.make("The.Sample.2020.1080p/The.Sample.2020.1080p.mkv", 3000)
        self.make("The.Sample.2020.1080p/The.Sample.2020.Extras.mkv", 2000)
        self.make("The.Sample.2020.1080p/Sample/the.sample.2020.sample.mkv", 10)
        self.org().run_once()
        self.assertTrue(os.path.isfile(self.movie_path("The Sample (2020)", "The Sample (2020).mkv")))
        self.assertTrue(os.path.isfile(os.path.join(self.dl, "The.Sample.2020.1080p", "The.Sample.2020.Extras.mkv")))

    def test_sample_word_in_download_dir_path_is_harmless(self) -> None:
        self.dl = os.path.join(self.tmp, "sample")
        os.makedirs(self.dl)
        self.cfg.paths.download_dir = self.dl
        self.make("Movie.2010/Movie.2010.mkv", 3000)
        self.make("Movie.2010/Bonus.mkv", 2000)
        self.org().run_once()
        self.assertTrue(os.path.isfile(os.path.join(self.dl, "Movie.2010", "Bonus.mkv")))

    def test_leftovers_kept_when_video_in_subfolder(self) -> None:
        self.make("Movie.2010/Movie.2010.mkv", 3000)
        self.make("Movie.2010/Extras/Deleted Scenes.mkv", 100)
        o = self.org()
        o.min_video_size = 1000
        o.run_once()
        self.assertTrue(os.path.isfile(self.movie_path("Movie (2010)", "Movie (2010).mkv")))
        self.assertTrue(os.path.isfile(os.path.join(self.dl, "Movie.2010", "Extras", "Deleted Scenes.mkv")))


class NamingTests(unittest.TestCase):
    def test_clean_component(self) -> None:
        c = organizer_mod._clean_component
        self.assertEqual(c("Mission: Impossible (1996)"), "Mission - Impossible (1996)")
        self.assertEqual(c("Title ()"), "Title")
        self.assertEqual(c('What? <Is> "This"|*'), "What Is This")
        self.assertEqual(c("Name..."), "Name")
        self.assertEqual(c(".."), "")
        self.assertEqual(c("Show - S01E02 - "), "Show - S01E02")

    def test_subtitle_suffix(self) -> None:
        s = organizer_mod._subtitle_suffix
        self.assertEqual(s(".fr", False), ".fr")
        self.assertEqual(s(".eng.forced", False), ".en.forced")
        self.assertEqual(s("2_English", True), ".en")
        self.assertEqual(s("Movie.2010.1080p.SDH.en", True), ".en.sdh")
        self.assertEqual(s("It.2017", True), "")
        self.assertEqual(s("", False), "")

    def test_smart_case(self) -> None:
        self.assertEqual(organizer_mod._smart_case("the office"), "The Office")
        self.assertEqual(organizer_mod._smart_case("amélie"), "Amélie")
        self.assertEqual(organizer_mod._smart_case("RoboCop"), "RoboCop")


if __name__ == "__main__":
    unittest.main()


class ReviewFixesTests(OrganizerTestBase):
    """Interplay with the searcher: grab association, failed downloads, DS down, concurrency."""

    def test_failed_grab_is_never_imported(self) -> None:
        mid = self.db.upsert_media("plex://movie/9", "movie", "Dune", 2021, None, None, None)
        name = "Dune.2021.1080p.WEB-DL.x264-GRP"
        gid = self.db.add_grab(mid, None, None, name, name, "idx", None, 1000)
        self.db.set_grab_status(gid, "failed", "broken")
        self.make(name + "/Dune.2021.1080p.mkv")
        actions = self.org().run_once()
        self.assertEqual(self.by_status(actions, "imported"), [])
        self.assertTrue(os.path.isfile(os.path.join(self.dl, name, "Dune.2021.1080p.mkv")))
        self.assertEqual(self.db.get_media(mid)["status"], "wanted")

    def test_ds_down_keeps_our_downloads_but_imports_manual_ones(self) -> None:
        name = "Dune.2021.1080p.WEB-DL.x264-GRP"
        self.db.add_grab(None, None, None, name, name, "idx", None, 1000)
        self.make(name + "/Dune.2021.1080p.mkv")
        self.make("Heat.1995.1080p.BluRay.mkv")
        actions = self.org(ds=FakeDS(fail=True)).run_once()
        self.assertTrue(os.path.isfile(os.path.join(self.dl, name, "Dune.2021.1080p.mkv")))
        self.assertTrue(os.path.isfile(self.movie_path("Heat (1995)", "Heat (1995).mkv")), actions)

    def test_magnet_grab_without_dn_is_associated_by_parsing(self) -> None:
        mid = self.db.upsert_media("plex://show/7", "show", "Le Bureau des Légendes", 2015, None, None, None)
        self.db.upsert_episode(mid, 5, 3, "Titre", None, status="grabbed")
        # indexer title != torrent name (magnet without dn: torrent_name falls back to the release title)
        gid = self.db.add_grab(mid, 5, 3, "Le Bureau des Legendes S05E03 FRENCH 1080p",
                               "Le Bureau des Legendes S05E03 FRENCH 1080p", "idx", "ab" * 20, 1000)
        self.make("Le.Bureau.des.Legendes.S05E03.FRENCH.1080p.WEB.H264-FRATERNiTY.mkv")
        self.org().run_once()
        self.assertTrue(os.path.isfile(self.series_path("Le Bureau des Légendes", "Season 05",
                                                        "Le Bureau des Légendes - S05E03.mkv")))
        self.assertEqual(self.db.get_episode(mid, 5, 3)["status"], "done")
        self.assertEqual(self.db.query_one("SELECT status FROM grabs WHERE id = ?", (gid,))["status"], "imported")

    def test_guess_does_not_take_a_pack_grab_for_an_episode(self) -> None:
        mid = self.db.upsert_media("plex://show/7", "show", "Lupin", 2021, None, None, None)
        gid = self.db.add_grab(mid, 1, None, "Lupin S01 pack", "Lupin S01 pack", "idx", None, 1000)
        org = self.org()
        self.assertIsNone(org._guess_grab("Lupin.S01E02.1080p.mkv", False))
        self.assertEqual(org._guess_grab("Lupin.S01.FRENCH.1080p.WEB", True)["id"], gid)

    def test_manual_download_uses_watchlist_title_and_marks_done(self) -> None:
        mid = self.db.upsert_media("plex://movie/3", "movie", "L'Arme fatale", 1987, None, None, None)
        self.make("L.Arme.Fatale.1987.FRENCH.1080p.BluRay.mkv")
        self.org().run_once()
        self.assertTrue(os.path.isfile(self.movie_path("L'Arme fatale (1987)", "L'Arme fatale (1987).mkv")),
                        os.listdir(self.movies))
        self.assertEqual(self.db.get_media(mid)["status"], "done")

    def test_manual_download_ambiguous_title_is_not_associated(self) -> None:
        self.db.upsert_media("plex://movie/1", "movie", "Dune", 1984, None, None, None)
        self.db.upsert_media("plex://movie/2", "movie", "Dune", 2021, None, None, None)
        self.make("Dune.1080p.mkv")
        self.org().run_once()
        self.assertTrue(os.path.isfile(self.movie_path("Dune", "Dune.mkv")), os.listdir(self.movies))
        self.assertEqual({m["status"] for m in self.db.list_media()}, {"wanted"})

    def test_nfd_entry_name_matches_nfc_grab(self) -> None:
        import unicodedata
        mid = self.db.upsert_media("plex://movie/4", "movie", "Amélie", 2001, None, None, None)
        nfc = "Amélie.2001.1080p.BluRay"
        self.db.add_grab(mid, None, None, nfc, nfc, "idx", None, 1000)
        self.make(unicodedata.normalize("NFD", nfc) + "/movie.mkv")
        self.org().run_once()
        self.assertEqual(self.db.get_media(mid)["status"], "done")

    def test_second_organizer_is_locked_out(self) -> None:
        if db_mod._fcntl is None:  # pragma: no cover
            self.skipTest("fcntl indisponible")
        os.makedirs(self.cfg.data_dir)
        self.make("Heat.1995.1080p.BluRay.mkv")
        first = self.org()
        fd = first._lock()
        self.assertNotIn(fd, (None, False))
        try:
            self.assertEqual(self.org().run_once(), [])
            self.assertTrue(os.path.isfile(os.path.join(self.dl, "Heat.1995.1080p.BluRay.mkv")))
        finally:
            os.close(fd)
        self.org().run_once()
        self.assertTrue(os.path.isfile(self.movie_path("Heat (1995)", "Heat (1995).mkv")))


class NewNamingTests(OrganizerTestBase):
    """Default layout requested by the user:
    video/Films/Titre (Année).mkv, video/Films/<Collection>/Titre (Année).mkv,
    video/TvShows/Série/Saison 01/Série - S01E02 - Titre épisode.mkv"""

    def setUp(self) -> None:
        super().setUp()
        defaults = Config().naming
        self.cfg.naming.movie_format = defaults.movie_format
        self.cfg.naming.movie_collection_format = defaults.movie_collection_format
        self.cfg.naming.series_format = defaults.series_format

    def test_movie_without_collection_is_flat(self) -> None:
        entry = "Heat.1995.1080p.BluRay.x264-GRP"
        self.make(entry + "/Heat.1995.1080p.BluRay.x264-GRP.mkv")
        self.make(entry + "/Heat.1995.1080p.BluRay.x264-GRP.en.srt", 10)
        self.grab(entry)
        self.org().run_once()
        self.assertEqual(sorted(os.listdir(self.movies)), ["Heat (1995).en.srt", "Heat (1995).mkv"])

    def test_collection_from_watchlist_item(self) -> None:
        mid = self.db.upsert_media("plex://movie/m", "movie", "The Matrix Reloaded", 2003, None, "604", None)
        self.db.set_media_details(mid, collection="The Matrix Collection")
        entry = "The.Matrix.Reloaded.2003.1080p.BluRay.x264-GRP"
        self.db.add_grab(mid, None, None, entry, entry, "idx", None, 1000)
        self.make(entry + "/movie.mkv")
        self.org().run_once()
        self.assertTrue(os.path.isfile(self.movie_path("The Matrix Collection", "The Matrix Reloaded (2003).mkv")),
                        os.listdir(self.movies))

    def test_collection_from_tmdb_for_manual_download(self) -> None:
        class Tmdb(FakeTmdb):
            def movie_details(self, tmdb_id: str) -> Optional[Dict]:
                return {"collection": "Alien Collection", "genres": ["Horror"], "is_animation": False}
        tmdb = Tmdb(movies={"Aliens": {"title": "Aliens", "year": 1986, "tmdb": "679"}})
        self.make("Aliens.1986.1080p.BluRay.x264-GRP.mkv")
        self.org(tmdb=tmdb).run_once()
        self.assertTrue(os.path.isfile(self.movie_path("Alien Collection", "Aliens (1986).mkv")),
                        os.listdir(self.movies))

    def test_series_saison_folder_with_episode_title(self) -> None:
        mid = self.db.upsert_media("plex://show/s", "show", "Severance", 2022, None, None, None)
        self.db.upsert_episode(mid, 1, 2, "Half Loop", "2022-02-18", status="grabbed")
        entry = "Severance.S01E02.1080p.WEB-DL.x264-GRP.mkv"
        self.db.add_grab(mid, 1, 2, entry[:-4], entry, "idx", None, 1000)
        self.make(entry)
        self.org().run_once()
        self.assertTrue(os.path.isfile(self.series_path("Severance", "Saison 01",
                                                        "Severance - S01E02 - Half Loop.mkv")),
                        os.listdir(self.series))

    def test_series_without_episode_title_has_no_trailing_dash(self) -> None:
        self.make("Some.Show.S03E04.720p.HDTV.x264-GRP.mkv")
        self.org().run_once()
        self.assertTrue(os.path.isfile(self.series_path("Some Show", "Saison 03", "Some Show - S03E04.mkv")),
                        os.listdir(self.series_path("Some Show", "Saison 03")))
