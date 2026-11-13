"""End-to-end test: Plex Watchlist -> Torznab -> Download Station -> rename/move, all against fake servers.

Everything (Plex discover provider, plex.tv, a Torznab indexer, the .torrent downloads and DSM's Web API)
is served by one local HTTP server, and the real App object is used.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import sys
import tempfile
import threading
import unittest
from contextlib import redirect_stdout

from synoarr import cli
from synoarr.app import App
from synoarr.bencode import bencode
from synoarr.config import load_config
from tests.fakes import FakeServer

GiB = 1024 ** 3
MOVIE = "Night.of.the.Living.Dead.1968.1080p.BluRay.x264-GRP"
PACK = "Test.Show.S01.1080p.WEB-DL.x264-GRP"
EPISODE = "Test.Show.S02E01.1080p.WEB-DL.x264-GRP.mkv"

RELEASES = [
    (MOVIE, 8 * GiB, 50),
    ("Night.of.the.Living.Dead.1968.CAM.x264-BAD", 1 * GiB, 500),
    ("Night.of.the.Living.Dead.1968.iTALiAN.1080p.BluRay-ITA", 8 * GiB, 80),
    (PACK, 10 * GiB, 20),
    (EPISODE[:-4], int(1.5 * GiB), 10),
    ("Test.Show.S02E01.720p.HDTV.x264-LOW", int(0.7 * GiB), 30),
]


def torrent_bytes(name):
    if name.endswith(".mkv"):
        info = {b"name": name.encode(), b"piece length": 16384, b"pieces": b"\0" * 20, b"length": 1000}
    else:
        info = {b"name": name.encode(), b"piece length": 16384, b"pieces": b"\0" * 20,
                b"files": [{b"length": 1000, b"path": [b"video.mkv"]}]}
    return bencode({b"announce": b"http://tracker.invalid/announce", b"info": info})


class World:
    """State of the fake services."""

    def __init__(self):
        self.lock = threading.Lock()
        self.tasks = {}  # id -> dict
        self.deleted = []
        self.uploads = []
        self.base = ""

    # ------------------------------------------------------------- Plex
    def plex(self, req):
        p = req.path
        if p == "/api/v2/user":
            return 200, {}, {"username": "flo", "title": "flo"}
        if p == "/library/sections/watchlist/all":
            items = [
                {"ratingKey": "m1", "key": "/library/metadata/m1", "guid": "plex://movie/m1", "type": "movie",
                 "title": "Night of the Living Dead", "year": 1968,
                 "Guid": [{"id": "imdb://tt0063350"}, {"id": "tmdb://10331"}]},
                {"ratingKey": "s1", "key": "/library/metadata/s1", "guid": "plex://show/s1", "type": "show",
                 "title": "Test Show", "year": 2020, "Guid": [{"id": "tvdb://123"}]},
            ]
            start = int(req.query.get("X-Plex-Container-Start", "0"))
            size = int(req.query.get("X-Plex-Container-Size", "100"))
            page = items[start:start + size]
            return 200, {}, {"MediaContainer": {"size": len(page), "totalSize": len(items), "Metadata": page}}
        if p == "/library/metadata/s1/children":
            seasons = [{"ratingKey": "s1s1", "type": "season", "index": 1, "title": "Season 1"},
                       {"ratingKey": "s1s2", "type": "season", "index": 2, "title": "Season 2"}]
            return 200, {}, {"MediaContainer": {"size": 2, "totalSize": 2, "Metadata": seasons}}
        if p == "/library/metadata/s1s1/children":
            eps = [{"ratingKey": "e%d" % i, "type": "episode", "index": i, "parentIndex": 1,
                    "title": "Episode %d" % i, "originallyAvailableAt": "2020-01-0%d" % i} for i in (1, 2, 3)]
            return 200, {}, {"MediaContainer": {"size": 3, "totalSize": 3, "Metadata": eps}}
        if p == "/library/metadata/s1s2/children":
            eps = [{"ratingKey": "f1", "type": "episode", "index": 1, "parentIndex": 2, "title": "Return",
                    "originallyAvailableAt": "2021-05-01"},
                   {"ratingKey": "f2", "type": "episode", "index": 2, "parentIndex": 2, "title": "Future",
                    "originallyAvailableAt": "2099-01-01"}]
            return 200, {}, {"MediaContainer": {"size": 2, "totalSize": 2, "Metadata": eps}}
        if p.startswith("/library/metadata/"):
            return 200, {}, {"MediaContainer": {"Metadata": [{"Guid": []}]}}
        return None

    # ---------------------------------------------------------- Torznab
    def torznab(self, req):
        t = req.query.get("t")
        if t == "caps":
            return 200, {"Content-Type": "application/xml"}, (
                '<?xml version="1.0" encoding="UTF-8"?><caps><server title="Fake"/>'
                '<searching><search available="yes" supportedParams="q"/>'
                '<tv-search available="yes" supportedParams="q,season,ep"/>'
                '<movie-search available="yes" supportedParams="q,imdbid"/></searching>'
                '<categories><category id="2000" name="Movies"/><category id="5000" name="TV"/></categories></caps>')
        items = []
        for title, size, seeders in RELEASES:
            items.append(
                '<item><title>%s</title><guid>%s</guid><link>%s/dl/%s.torrent</link><size>%d</size>'
                '<enclosure url="%s/dl/%s.torrent" length="%d" type="application/x-bittorrent"/>'
                '<torznab:attr name="seeders" value="%d"/><torznab:attr name="peers" value="%d"/></item>'
                % (title, title, self.base, title, size, self.base, title, size, seeders, seeders + 5))
        xml = ('<?xml version="1.0" encoding="UTF-8"?><rss version="2.0" '
               'xmlns:torznab="http://torznab.com/schemas/2015/feed"><channel><title>Fake</title>%s'
               '</channel></rss>' % "".join(items))
        return 200, {"Content-Type": "application/rss+xml"}, xml

    def download(self, req):
        title = req.path[len("/dl/"):-len(".torrent")]
        name = EPISODE if title == EPISODE[:-4] else title
        return 200, {"Content-Type": "application/x-bittorrent"}, torrent_bytes(name)

    # -------------------------------------------------------------- DSM
    def dsm(self, req):
        params = req.params()
        api, method = params.get("api"), params.get("method")
        if req.path == "/webapi/query.cgi":
            return 200, {}, {"success": True, "data": {
                "SYNO.API.Auth": {"path": "entry.cgi", "minVersion": 1, "maxVersion": 7},
                "SYNO.DownloadStation2.Task": {"path": "entry.cgi", "minVersion": 1, "maxVersion": 2},
                "SYNO.DownloadStation.Task": {"path": "DownloadStation/task.cgi", "minVersion": 1, "maxVersion": 3},
                "SYNO.DownloadStation.Info": {"path": "DownloadStation/info.cgi", "minVersion": 1,
                                              "maxVersion": 2}}}
        if api == "SYNO.API.Auth":
            if method == "login":
                if params.get("account") == "synoarr" and params.get("passwd") == "secret":
                    return 200, {}, {"success": True, "data": {"sid": "SID123"}}
                return 200, {}, {"success": False, "error": {"code": 400}}
            return 200, {}, {"success": True}
        sid_ok = params.get("_sid") == "SID123"
        if not sid_ok:
            return 200, {}, {"success": False, "error": {"code": 119}}
        if api == "SYNO.DownloadStation2.Task" and method == "create":
            files = req.multipart
            dest = json.loads(params.get("destination", '""'))
            with self.lock:
                for _field, (fn, content) in files.items():
                    if fn:
                        self.uploads.append((fn, dest))
                        name = content.split(b"4:name", 1)[1]
                        length, rest = name.split(b":", 1)
                        title = rest[:int(length)].decode()
                        self._new_task(title, dest)
                if json.loads(params.get("type", '""')) == "url":
                    from synoarr.bencode import magnet_info
                    for uri in json.loads(params.get("url", "[]")):
                        dn, _h = magnet_info(uri)
                        self._new_task(dn or (_h or uri), dest, uri=uri)
            return 200, {}, {"success": True, "data": {"list_id": [], "task_id": list(self.tasks)[-1:]}}
        if api == "SYNO.DownloadStation.Task" and method == "list":
            with self.lock:
                tasks = list(self.tasks.values())
            return 200, {}, {"success": True, "data": {"total": len(tasks), "offset": 0, "tasks": tasks}}
        if api == "SYNO.DownloadStation.Task" and method == "delete":
            ids = params.get("id", "").split(",")
            with self.lock:
                for i in ids:
                    self.tasks.pop(i, None)
                    self.deleted.append(i)
            return 200, {}, {"success": True, "data": [{"id": i, "error": 0} for i in ids]}
        if api == "SYNO.DownloadStation.Info":
            return 200, {}, {"success": True, "data": {"version": 4000, "version_string": "4.0.0"}}
        return 200, {}, {"success": False, "error": {"code": 103}}

    def _new_task(self, title, dest, uri=None):
        """Download Station task as listed by SYNO.DownloadStation.Task (additional=detail,transfer)."""
        tid = "dbid_%d" % (len(self.tasks) + len(self.deleted) + 1)
        detail = {"destination": dest, "connected_seeders": 7, "connected_leechers": 2, "total_peers": 31,
                  "create_time": 1700000000}
        if uri:
            detail["uri"] = uri
        self.tasks[tid] = {"id": tid, "title": title, "status": "downloading", "size": 1000, "type": "bt",
                           "additional": {"detail": detail,
                                          "transfer": {"size_downloaded": 250, "size_uploaded": 10,
                                                       "speed_download": 50, "speed_upload": 5}}}
        return tid

    def handler(self, req):
        if req.path.startswith("/webapi/"):
            return self.dsm(req)
        if req.path.startswith("/torznab"):
            return self.torznab(req)
        if req.path.startswith("/dl/"):
            return self.download(req)
        res = self.plex(req)
        if res is not None:
            return res
        return 404, {}, b"not found"


def write(path, size=2048):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(b"\0" * size)


class EndToEndTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="synoarr-e2e-")
        self.world = World()
        self.srv = FakeServer(self.world.handler).__enter__()
        self.world.base = self.srv.url
        self.dl = os.path.join(self.tmp, "volume1", "Download")
        self.movies = os.path.join(self.tmp, "volume1", "Vidéos", "Films")
        self.series = os.path.join(self.tmp, "volume1", "Vidéos", "Séries")
        os.makedirs(self.dl)
        self.cfg_path = os.path.join(self.tmp, "config.ini")
        with open(self.cfg_path, "w", encoding="utf-8") as fh:
            fh.write("""
[general]
data_dir = ./data
log_level = WARNING
[plex]
token = PLEXTOKEN
[downloadstation]
url = {url}
username = synoarr
password = secret
destination = Download
[paths]
download_dir = {dl}
movies_dir = {movies}
series_dir = {series}
min_age_minutes = 0
[web]
enabled = false
""".format(url=self.srv.url, dl=self.dl, movies=self.movies, series=self.series))

    def tearDown(self):
        try:
            self.app.db.close()
        except Exception:
            pass
        self.srv.__exit__(None, None, None)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def make_app(self):
        cfg = load_config(self.cfg_path)
        app = App(cfg)
        app.plex.discover_url = self.srv.url
        app.plex.metadata_url = self.srv.url
        app.plex.plextv_url = self.srv.url
        app.organizer.min_video_size = 0
        self.app = app
        return app

    def test_full_flow(self):
        app = self.make_app()
        app.indexer_manager.add_url("Fake", self.srv.url + "/torznab")  # type auto-detected
        self.assertEqual(app.db.get_indexer("Fake")["type"], "torznab")

        # 1) Watchlist sync + search + send to Download Station
        app.job_watchlist()
        self.assertEqual(app._errors, [])
        grabs = {g["release_title"]: g for g in app.db.list_grabs()}
        self.assertEqual(set(grabs), {MOVIE, PACK, EPISODE[:-4]}, grabs.keys())
        self.assertEqual(sorted(t["title"] for t in self.world.tasks.values()), sorted([MOVIE, PACK, EPISODE]))
        self.assertTrue(all(dest == "Download" for _fn, dest in self.world.uploads))
        movie = app.db.get_media_by_key("m1")
        show = app.db.get_media_by_key("s1")
        self.assertEqual(movie["status"], "grabbed")
        self.assertEqual(movie["imdb"], "tt0063350")
        eps = {(e["season"], e["episode"]): e["status"] for e in app.db.list_episodes(show["id"])}
        self.assertEqual(eps, {(1, 1): "grabbed", (1, 2): "grabbed", (1, 3): "grabbed",
                               (2, 1): "grabbed", (2, 2): "wanted"})

        # a second cycle must not grab anything again
        app.job_watchlist()
        self.assertEqual(len(app.db.list_grabs()), 3)
        self.assertEqual(len(self.world.uploads), 3)

        # 2) downloads in progress are left alone
        write(os.path.join(self.dl, MOVIE, "Night.of.the.Living.Dead.1968.1080p.BluRay.x264-GRP.mkv"))
        self.assertEqual([a for a in app.job_process() if a["status"] == "imported"], [])
        self.assertTrue(os.path.isdir(os.path.join(self.dl, MOVIE)))

        # 3) Download Station finishes everything
        write(os.path.join(self.dl, MOVIE, "sample.mkv"), 100)
        write(os.path.join(self.dl, MOVIE, "movie.nfo"), 10)
        write(os.path.join(self.dl, MOVIE, "Night.of.the.Living.Dead.1968.1080p.BluRay.x264-GRP.srt"), 10)
        for i in (1, 2, 3):
            write(os.path.join(self.dl, PACK, "Test.Show.S01E0%d.1080p.WEB-DL.x264-GRP.mkv" % i))
        write(os.path.join(self.dl, EPISODE))
        with self.world.lock:
            for t in self.world.tasks.values():
                t["status"] = "seeding" if t["title"] == PACK else "finished"
        actions = app.job_process()
        imported = [a for a in actions if a["status"] == "imported"]
        self.assertGreaterEqual(len(imported), 5, actions)

        # new layout: video/Films/Titre (Année).mkv (no sub-folder without a collection)
        m = os.path.join(self.movies, "Night of the Living Dead (1968)")
        self.assertTrue(os.path.isfile(m + ".mkv"), os.listdir(self.movies) if os.path.isdir(self.movies) else None)
        self.assertTrue(os.path.isfile(m + ".srt"))
        # video/TvShows/Série/Saison 01/Série - S01E02 - Titre épisode.mkv (titles from Plex)
        titles = {(1, 1): "Episode 1", (1, 2): "Episode 2", (1, 3): "Episode 3", (2, 1): "Return"}
        for (s, e), t in titles.items():
            p = os.path.join(self.series, "Test Show", "Saison %02d" % s, "Test Show - S%02dE%02d - %s.mkv" % (s, e, t))
            self.assertTrue(os.path.isfile(p), p)
        # sources cleaned, tasks removed from Download Station
        self.assertEqual(sorted(os.listdir(self.dl)), [])
        self.assertEqual(self.world.tasks, {})
        self.assertEqual(len(self.world.deleted), 3)
        # statuses
        self.assertEqual(app.db.get_media_by_key("m1")["status"], "done")
        eps = {(e["season"], e["episode"]): e["status"] for e in app.db.list_episodes(show["id"])}
        self.assertEqual(eps, {(1, 1): "done", (1, 2): "done", (1, 3): "done", (2, 1): "done", (2, 2): "wanted"})
        self.assertEqual({g["status"] for g in app.db.list_grabs()}, {"imported"})

        # 4) a new sync does not reset anything and finds the files in the library
        app.job_watchlist()
        self.assertEqual(len(self.world.uploads), 3)
        self.assertEqual(app.db.get_media_by_key("m1")["status"], "done")

    def test_failed_download_is_blocklisted_and_replaced(self):
        app = self.make_app()
        app.indexer_manager.add_url("Fake", self.srv.url + "/torznab", type_="torznab")
        app.job_watchlist()
        movie_task = next(t for t in self.world.tasks.values() if t["title"] == MOVIE)
        movie_task["status"] = "error"
        movie_task["status_extra"] = {"error_detail": "broken_link"}
        # force a new search immediately
        app.db.execute("UPDATE media SET last_search_at = NULL")
        app.job_watchlist()
        self.assertTrue(app.db.is_blocked(MOVIE))
        failed = [g for g in app.db.list_grabs() if g["status"] == "failed"]
        self.assertEqual([g["release_title"] for g in failed], [MOVIE])
        # the only other acceptable movie release is Italian-only or CAM -> nothing else grabbed
        self.assertEqual(app.db.get_media_by_key("m1")["status"], "wanted")

    def test_cli_check_and_dry_run(self):
        app = self.make_app()
        app.indexer_manager.add_url("Fake", self.srv.url + "/torznab")
        app.db.close()
        # check uses the real plex.tv URLs -> patch the class defaults through env-free monkeypatching
        from synoarr import plex as plex_mod
        orig_init = plex_mod.PlexClient.__init__

        def patched(self_, *a, **kw):
            orig_init(self_, *a, **kw)
            self_.discover_url = self_.metadata_url = self_.plextv_url = self.srv.url
        plex_mod.PlexClient.__init__ = patched
        try:
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = cli.main(["check", "--config", self.cfg_path])
            out = buf.getvalue()
            self.assertEqual(rc, 0, out)
            self.assertIn("Download Station", out)
            self.assertNotIn("[ERR]", out)
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = cli.main(["search", "--config", self.cfg_path, "--dry-run"])
            self.assertEqual(rc, 0, buf.getvalue())
            self.assertEqual(self.world.uploads, [])
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = cli.main(["search", "Night of the Living Dead 1968", "--config", self.cfg_path])
            self.assertIn(MOVIE, buf.getvalue())
            self.assertIn("rej", buf.getvalue())
        finally:
            plex_mod.PlexClient.__init__ = orig_init
            import logging
            for h in list(logging.getLogger().handlers):
                logging.getLogger().removeHandler(h)
                h.close()


class AppAndCliRobustnessTest(EndToEndTest):
    def _cli(self, *argv):
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = cli.main(list(argv) + ["--config", self.cfg_path])
        import logging
        for h in list(logging.getLogger().handlers):
            logging.getLogger().removeHandler(h)
            h.close()
        return rc, buf.getvalue()

    def test_import_is_not_blocked_by_a_long_search(self):
        app = self.make_app()
        started, release = threading.Event(), threading.Event()

        def slow_run_once(media_id=None, force=False):
            started.set()
            release.wait(10)
            return {"searched": 0, "grabbed": 0}
        app.searcher.run_once = slow_run_once
        app.searcher.sync_watchlist = lambda: 0
        self.assertTrue(app.run_watchlist_now())
        self.assertTrue(started.wait(5))
        self.assertFalse(app.run_watchlist_now())  # a second click does not queue another cycle
        done = threading.Event()
        threading.Thread(target=lambda: (app.job_process(), done.set()), daemon=True).start()
        self.assertTrue(done.wait(5), "le traitement attend la fin de la recherche")
        release.set()
        app.shutdown(timeout=5)
        with self.assertRaises(Exception):
            app.db.query("SELECT 1")  # closed once the jobs are finished

    def test_readonly_database_gives_a_sudo_hint(self):
        import sqlite3
        from unittest import mock
        with mock.patch("synoarr.app.Database",
                        side_effect=sqlite3.OperationalError("attempt to write a readonly database")), \
                mock.patch.object(cli, "_not_root", return_value=True):
            rc, out = self._cli("watchlist", "--no-sync")
        self.assertEqual(rc, 1)
        self.assertIn("non accessible en écriture", out)
        self.assertIn("sudo", out)
        self.assertNotIn("Traceback", out)

    def test_network_errors_are_explained(self):
        from unittest import mock
        from synoarr.httpclient import HttpError
        with mock.patch("synoarr.plex.PlexClient.start_pin_login", side_effect=HttpError("timeout", None)):
            rc, out = self._cli("plex-login")
        self.assertEqual(rc, 1)
        self.assertIn("plex.tv injoignable", out)
        with mock.patch("synoarr.searcher.Searcher.sync_watchlist", side_effect=HttpError("Plex 503", 503)):
            rc, out = self._cli("watchlist")
        self.assertEqual(rc, 0)
        self.assertIn("Watchlist Plex illisible", out)
        with mock.patch("synoarr.cli.cmd_status", side_effect=RuntimeError("bug")):
            rc, out = self._cli("status")
        self.assertEqual(rc, 1)
        self.assertIn("Erreur inattendue", out)

    def test_check_reports_unwritable_data_dir(self):
        from unittest import mock
        with mock.patch.object(cli, "_check_data_dir", return_value="Dossier de données /x non accessible"), \
                mock.patch.object(cli, "_not_root", return_value=True):
            rc, out = self._cli("check")
        self.assertEqual(rc, 1)
        self.assertIn("sudo", out)

    def test_config_option_before_the_command(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = cli.main(["--config", self.cfg_path, "watchlist", "--no-sync"])
        for h in list(__import__("logging").getLogger().handlers):
            __import__("logging").getLogger().removeHandler(h)
            h.close()
        self.assertEqual(rc, 0, buf.getvalue())
        self.assertIn("Watchlist vide", buf.getvalue())
        args = cli.build_parser().parse_args(["--config", "/a.ini", "check", "--dry-run"])
        self.assertEqual((args.config, args.dry_run), ("/a.ini", True))
        args = cli.build_parser().parse_args(["check", "-c", "/b.ini"])
        self.assertEqual((args.config, args.dry_run, args.verbose), ("/b.ini", False, False))

    def test_init_gives_the_file_back_to_the_sudo_user(self):
        from unittest import mock
        target = os.path.join(self.tmp, "new", "config.ini")
        calls = []
        with mock.patch.dict(os.environ, {"SUDO_UID": "1026", "SUDO_GID": "100"}), \
                mock.patch.object(cli, "_not_root", return_value=False), \
                mock.patch("os.chown", side_effect=lambda p, u, g: calls.append((p, u, g))):
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = cli.main(["init", "--config", target])
        self.assertEqual(rc, 0)
        self.assertTrue(os.path.isfile(target))
        self.assertIn((target, 1026, 100), calls)
        self.assertIn((os.path.dirname(target), 1026, 100), calls)
        self.assertIn("sudo python3 synoarr.py check", buf.getvalue())

    def test_docker_first_run_creates_the_config_and_waits(self):
        from unittest import mock
        target = os.path.join(self.tmp, "config", "config.ini")
        os.makedirs(os.path.dirname(target))
        args = cli.build_parser().parse_args(["run", "--config", target])
        sleeps = []

        def fake_sleep(sec):
            sleeps.append(sec)
            if len(sleeps) == 2:  # the user saves the file
                os.utime(target, (1, 1))
        with mock.patch.object(cli, "_in_docker", return_value=True), mock.patch("time.sleep", fake_sleep):
            buf = io.StringIO()
            with redirect_stdout(buf):
                self.assertTrue(cli._wait_for_docker_config(args))
        self.assertTrue(os.path.isfile(target))
        self.assertIn("Configuration créée", buf.getvalue())
        with mock.patch.object(cli, "_in_docker", return_value=True):
            self.assertFalse(cli._wait_for_docker_config(args))  # exists now: no wait

    def test_check_flags_missing_share_and_empty_password(self):
        from unittest import mock
        with open(self.cfg_path, encoding="utf-8") as fh:
            text = fh.read().replace("password = secret", "password =")
        with open(self.cfg_path, "w", encoding="utf-8") as fh:
            fh.write(text)
        with mock.patch.object(cli, "_share_problem",
                               side_effect=lambda d: "Dossier partagé /volume1/Videos introuvable"
                               if d == self.movies else None):
            rc, out = self._cli("check")
        self.assertEqual(rc, 1)
        self.assertIn("Dossier partagé /volume1/Videos introuvable", out)
        self.assertFalse(os.path.exists(self.movies))
        self.assertIn("password est vide", out)

    def test_indexer_add_returns_1_when_the_test_fails(self):
        rc, out = self._cli("indexer", "add-url", "Mort", self.srv.url + "/nothing", "--type", "torznab")
        self.assertEqual(rc, 1, out)

    def test_dsm_requests_bypass_proxies(self):
        from synoarr.app import is_local_url
        for url in ("http://127.0.0.1:5000", "https://localhost:5001", "http://192.168.1.10:5000",
                    "http://diskstation:5000", "http://nas.local:5000"):
            self.assertTrue(is_local_url(url), url)
        self.assertFalse(is_local_url("https://mynas.synology.me:5001"))
        import urllib.request
        from unittest import mock

        def proxies(client):
            return [h.proxies for h in client._opener.handlers if isinstance(h, urllib.request.ProxyHandler)
                    and h.proxies]
        with mock.patch.dict(os.environ, {"http_proxy": "http://proxy.invalid:3128",
                                          "https_proxy": "http://proxy.invalid:3128"}):
            app = self.make_app()
            self.assertEqual(proxies(app.ds_http), [])  # DSM on 127.0.0.1: direct
            self.assertNotEqual(proxies(app.http), [])  # Internet services keep the proxy


if __name__ == "__main__":
    unittest.main()
