"""« Déjà présent » (film / série / saison / épisode, annulable) et changement automatique de source d'un
téléchargement bloqué depuis plusieurs jours."""
from __future__ import annotations

import datetime as _dt
from typing import Any, Dict

from synoarr.db import now_iso
from synoarr.models import DSTask, EpisodeInfo

from test_searcher import GiB, SearcherTestBase
from test_web import RealAppWebBase


def days_ago(n: float) -> str:
    return (_dt.datetime.now() - _dt.timedelta(days=n)).replace(microsecond=0).isoformat(sep=" ")


class StuckDownloadTests(SearcherTestBase):
    OLD = "Heat.1995.1080p.BluRay.x264-OLD"
    NEW = "Heat.1995.1080p.WEB-DL.x264-NEW"

    def grab_movie(self, downloaded: int = 1024 * 1024, status: str = "downloading",
                   sent_days: float = 4) -> Dict[str, Any]:
        self.movie(title="Heat", year=1995)
        self.searcher.sync_watchlist()
        self.idx.add(self.OLD, seeders=50)
        self.assertEqual(self.searcher.run_once()["grabbed"], 1)
        self.db.execute("UPDATE grabs SET created_at = ?", (days_ago(sent_days),))
        task = self.ds.tasks[-1]
        task.status, task.size, task.downloaded, task.peers, task.seeders = status, 2 * GiB, downloaded, 4, 0
        return self.db.list_grabs()[0]

    def events(self) -> str:
        return "\n".join(e["message"] for e in self.db.query("SELECT message FROM events"))

    def test_stuck_download_replaced_by_another_source(self) -> None:
        self.grab_movie()
        self.idx.add(self.NEW, seeders=10)
        self.searcher.check_failed_downloads()
        grabs = {g["release_title"]: g for g in self.db.list_grabs()}
        self.assertEqual(grabs[self.OLD]["status"], "failed")
        self.assertIn("sans progrès depuis 4", grabs[self.OLD]["message"])
        self.assertTrue(self.db.is_blocked(self.OLD))
        self.assertEqual(self.ds.deleted, ["t0"])
        self.assertEqual(grabs[self.NEW]["status"], "sent")
        self.assertEqual(self.media("m1")["status"], "grabbed")
        self.assertIn("Source changée automatiquement", self.events())

    def test_no_other_source_keeps_the_download_and_retries_daily(self) -> None:
        self.grab_movie(downloaded=0)
        self.searcher.check_failed_downloads()
        g = self.db.list_grabs()[0]
        self.assertEqual(g["status"], "sent")
        self.assertIsNotNone(g["stuck_checked_at"])
        self.assertEqual(self.ds.deleted, [])
        self.assertIn("aucune autre source", self.events())
        searches = len(self.idx.requests)
        self.searcher.check_failed_downloads()  # within 24 h: no new search, no new event
        self.assertEqual(len(self.idx.requests), searches)
        self.assertEqual(self.events().count("aucune autre source"), 1)
        # a day later a new source appeared: replaced
        self.db.execute("UPDATE grabs SET stuck_checked_at = ?", (days_ago(1.1),))
        self.idx.add(self.NEW, seeders=10)
        self.searcher.check_failed_downloads()
        self.assertEqual({g["release_title"]: g["status"] for g in self.db.list_grabs()},
                         {self.OLD: "failed", self.NEW: "sent"})

    def test_progressing_download_is_kept(self) -> None:
        self.grab_movie(downloaded=int(0.5 * GiB))  # first look: already well started -> clock starts now
        self.idx.add(self.NEW, seeders=10)
        self.searcher.check_failed_downloads()
        g = self.db.list_grabs()[0]
        self.assertEqual(g["status"], "sent")
        self.assertEqual(g["progress_bytes"], int(0.5 * GiB))
        # 4 days later with a real gain (more than 1 %): still fine, clock restarts
        self.db.execute("UPDATE grabs SET progress_at = ?", (days_ago(4),))
        self.ds.tasks[-1].downloaded = int(0.6 * GiB)
        self.searcher.check_failed_downloads()
        g = self.db.list_grabs()[0]
        self.assertEqual(g["status"], "sent")
        self.assertEqual(g["progress_bytes"], int(0.6 * GiB))
        # 4 more days with a tiny gain only: stuck -> replaced
        self.db.execute("UPDATE grabs SET progress_at = ?", (days_ago(4),))
        self.ds.tasks[-1].downloaded = int(0.6 * GiB) + 1024
        self.searcher.check_failed_downloads()
        self.assertEqual({g["release_title"]: g["status"] for g in self.db.list_grabs()},
                         {self.OLD: "failed", self.NEW: "sent"})

    def test_recent_paused_or_disabled_is_never_replaced(self) -> None:
        self.grab_movie(sent_days=1)  # 1 day < 3 days
        self.idx.add(self.NEW, seeders=10)
        self.searcher.check_failed_downloads()
        self.assertEqual(len(self.db.list_grabs()), 1)
        self.db.execute("UPDATE grabs SET created_at = ?, progress_at = ?", (days_ago(9), days_ago(9)))
        self.ds.tasks[-1].status = "paused"  # paused by the user
        self.searcher.check_failed_downloads()
        self.assertEqual(len(self.db.list_grabs()), 1)
        self.ds.tasks[-1].status = "downloading"
        self.cfg.downloadstation.stuck_days = 0  # disabled
        self.searcher.check_failed_downloads()
        self.assertEqual(len(self.db.list_grabs()), 1)
        self.cfg.downloadstation.stuck_days = 3
        self.searcher.check_failed_downloads()
        self.assertEqual(len(self.db.list_grabs()), 2)

    def test_stuck_episode_replaced_by_the_same_episode(self) -> None:
        self.show(episodes=[EpisodeInfo(season=1, episode=1, title="Pilot", air_date="2021-01-08")])
        self.searcher.sync_watchlist()
        self.idx.add("Lupin.S01E01.1080p.WEB.x264-OLD", seeders=50)
        self.assertEqual(self.searcher.run_once()["grabbed"], 1)
        self.db.execute("UPDATE grabs SET created_at = ?", (days_ago(5),))
        self.ds.tasks[-1].size, self.ds.tasks[-1].downloaded = GiB, 0
        self.idx.add("Lupin.S01E01.1080p.WEB.x264-NEW", seeders=5)
        self.searcher.check_failed_downloads()
        grabs = {g["release_title"]: g for g in self.db.list_grabs()}
        self.assertEqual(grabs["Lupin.S01E01.1080p.WEB.x264-OLD"]["status"], "failed")
        new = grabs["Lupin.S01E01.1080p.WEB.x264-NEW"]
        self.assertEqual((new["status"], new["season"], new["episode"]), ("sent", 1, 1))
        self.assertEqual(self.statuses("s1")[(1, 1)], "grabbed")

    def test_stuck_events_are_notified(self) -> None:
        from synoarr.notify import classify
        self.assertEqual(classify("download", "Source changée automatiquement : a remplacé par b", "WARNING"),
                         ["error"])
        self.assertEqual(classify("download", "2 téléchargement(s) supprimé(s)", "INFO"), [])

    def test_progress_step(self) -> None:
        self.assertEqual(self.searcher.progress_step(0), 20 * 1024 * 1024)
        self.assertEqual(self.searcher.progress_step(10 * GiB), 10 * GiB // 100)


class PresentWebTest(RealAppWebBase):
    def setUp(self) -> None:
        super().setUp()
        db = self.app.db
        self.movie_id = db.upsert_media("pm1", "movie", "Film Présent", 2001, None, None, None)
        db.execute("UPDATE media SET on_watchlist = 1, status = 'wanted' WHERE id = ?", (self.movie_id,))
        self.show_id = db.upsert_media("ps1", "show", "Série Présente", 2020, None, None, None)
        db.execute("UPDATE media SET on_watchlist = 1, status = 'monitored' WHERE id = ?", (self.show_id,))
        future = (_dt.date.today() + _dt.timedelta(days=30)).isoformat()
        for s, e, air, st in ((1, 1, "2020-01-01", "done"), (1, 2, "2020-01-08", "wanted"),
                              (2, 1, "2021-01-01", "wanted"), (2, 2, "2021-01-08", "grabbed"),
                              (2, 3, future, "wanted")):
            db.upsert_episode(self.show_id, s, e, "E%d" % e, air, st)

    def eps(self) -> Dict[Any, str]:
        return {(e["season"], e["episode"]): e["status"] for e in self.app.db.list_episodes(self.show_id)}

    def post(self, mid: int, body: Dict[str, Any]):
        return self.req("/api/media/%d/present" % mid, "POST", body)

    def test_movie_present_then_undo(self) -> None:
        status, data, _ = self.post(self.movie_id, {})
        self.assertEqual(status, 200, data)
        self.assertIn("déjà présent", data["message"])
        m = self.app.db.get_media(self.movie_id)
        self.assertEqual(m["status"], "done")
        self.assertTrue(m["marked_present"])
        self.assertEqual(self.post(self.movie_id, {})[0], 400)  # already done
        status, data, _ = self.post(self.movie_id, {"present": False})
        self.assertEqual(status, 200, data)
        m = self.app.db.get_media(self.movie_id)
        self.assertEqual((m["status"], m["marked_present"]), ("wanted", None))
        self.assertEqual(self.post(self.movie_id, {"present": False})[0], 400)  # nothing to undo

    def test_episode_season_and_show(self) -> None:
        status, data, _ = self.post(self.show_id, {"season": 1, "episode": 2})
        self.assertEqual(status, 200, data)
        self.assertEqual(self.eps()[(1, 2)], "done")
        status, data, _ = self.post(self.show_id, {})  # whole show: aired episodes only, never in progress
        self.assertEqual(status, 200, data)
        self.assertEqual(data["count"], 1)
        self.assertEqual(self.eps(), {(1, 1): "done", (1, 2): "done", (2, 1): "done", (2, 2): "grabbed",
                                      (2, 3): "wanted"})
        self.assertEqual(self.post(self.show_id, {"season": 2})[0], 400)  # nothing aired left in season 2
        # undo season 1: only the episode marked by hand goes back to search (1x01 was really imported)
        status, data, _ = self.post(self.show_id, {"season": 1, "present": False})
        self.assertEqual(status, 200, data)
        self.assertEqual(self.eps()[(1, 1)], "done")
        self.assertEqual(self.eps()[(1, 2)], "wanted")

    def test_validation_and_bulk(self) -> None:
        self.assertEqual(self.post(self.show_id, {"season": "1"})[0], 400)
        self.assertEqual(self.post(self.show_id, {"episode": 2})[0], 400)  # episode without season
        self.assertEqual(self.post(999999, {})[0], 404)
        status, data, _ = self.req("/api/media/bulk", "POST", {"action": "present",
                                                               "ids": [self.movie_id, self.show_id]})
        self.assertEqual(status, 200, data)
        self.assertEqual(sorted(data["done"]), sorted([self.movie_id, self.show_id]))
        self.assertEqual(self.app.db.get_media(self.movie_id)["status"], "done")
        self.assertEqual(self.eps()[(2, 1)], "done")
        from synoarr.web import INDEX_HTML, INDEX_JS
        for needle in ("presBtn(", "markPresent(", "d.unpres", "stuck_hours"):
            self.assertIn(needle, INDEX_JS)
        self.assertIn('data-bk="present"', INDEX_HTML)

    def test_download_entry_shows_stuck_days(self) -> None:
        from synoarr.app import App
        g = {"id": 1, "media_id": None, "status": "sent", "release_title": "X", "size": 0,
             "progress_at": days_ago(2), "progress_bytes": 0, "created_at": now_iso()}
        task = DSTask(id="1", title="X", status="downloading", size=GiB, downloaded=1024)
        self.assertGreaterEqual(App._download_entry(g, task, {}, True)["stuck_hours"], 47)
        task.downloaded = GiB // 2
        self.assertIsNone(App._download_entry(g, task, {}, True).get("stuck_hours"))
