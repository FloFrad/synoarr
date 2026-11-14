from __future__ import annotations

import json
import logging
import os
import shutil
import tempfile
import time
import unittest
from typing import Any, Dict, List

from synoarr import __version__
from synoarr.config import Config
from synoarr.db import Database
from synoarr.httpclient import HttpClient
from synoarr.organizer import Organizer
from synoarr.subtitles import (SubtitleService, existing_subtitle_languages, name_similarity,
                               normalize_language, opensubtitles_hash)

from tests.fakes import FakeServer

API_KEY = "KEY-secret-123"
PASSWORD = "pw-very-secret"


def _independent_hash(path: str) -> str:
    """Reference implementation written differently from the module (int.from_bytes, byte loop)."""
    size = os.path.getsize(path)
    with open(path, "rb") as fh:
        data = fh.read()
    total = size
    for block in (data[:65536], data[size - 65536:]):
        for i in range(0, 65536, 8):
            total = (total + int.from_bytes(block[i:i + 8], "little")) % (1 << 64)
    return format(total, "016x")


def sub(file_id: int, lang: str = "en", release: str = "", hash_match: bool = False, downloads: int = 0,
        hi: bool = False, forced: bool = False, files: int = 1) -> Dict[str, Any]:
    return {"id": str(file_id), "type": "subtitle", "attributes": {
        "language": lang, "release": release, "moviehash_match": hash_match, "download_count": downloads,
        "hearing_impaired": hi, "foreign_parts_only": forced, "ratings": 0.0,
        "files": [{"file_id": file_id + k, "file_name": release + ".srt"} for k in range(files)]}}


class FakeOpenSubtitles(object):
    def __init__(self) -> None:
        self.results = []  # type: List[Dict[str, Any]]
        self.remaining = 10
        self.search_status = 200
        self.download_status = 200
        self.login_status = 200
        self.downloads = []  # type: List[int]
        self.srv = FakeServer(self.handle)

    def __enter__(self) -> "FakeOpenSubtitles":
        self.srv.__enter__()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.srv.__exit__(*exc)

    @property
    def base_url(self) -> str:
        return self.srv.url + "/api/v1"

    def reqs(self, path: str) -> List[Any]:
        return [r for r in self.srv.requests if r.path == "/api/v1" + path]

    def handle(self, req: Any):
        if req.path == "/api/v1/login" and req.method == "POST":
            if self.login_status != 200:
                return self.login_status, {}, {"message": "bad credentials"}
            return 200, {}, {"token": "tok-abc", "base_url": "api.opensubtitles.com", "status": 200,
                             "user": {"allowed_downloads": 20}}
        if req.path == "/api/v1/subtitles" and req.method == "GET":
            if self.search_status != 200:
                return self.search_status, {}, {"message": "error"}
            langs = req.query.get("languages", "").split(",")
            data = []
            for item in self.results:
                a = dict(item["attributes"])
                if a["language"] not in langs:
                    continue
                if a.get("moviehash_match") and req.query.get("moviehash") is None:
                    a["moviehash_match"] = False
                data.append({"id": item["id"], "type": "subtitle", "attributes": a})
            return 200, {}, {"total_count": len(data), "data": data}
        if req.path == "/api/v1/download" and req.method == "POST":
            if self.download_status != 200:
                return self.download_status, {}, {"message": "quota", "reset_time_utc": "2000-01-01T00:00:00.000Z"}
            if self.remaining <= 0:
                return 406, {}, {"message": "You have downloaded your allowed 20 subtitles for 24h",
                                 "remaining": 0}
            body = json.loads(req.body.decode("utf-8"))
            self.downloads.append(int(body["file_id"]))
            self.remaining -= 1
            return 200, {}, {"link": self.srv.url + "/file/%d.srt" % body["file_id"], "file_name": "x.srt",
                             "requests": 1, "remaining": self.remaining, "reset_time": "23 hours"}
        if req.path.startswith("/file/"):
            fid = req.path[len("/file/"):-4]
            return 200, {"Content-Type": "application/x-subrip"}, \
                ("1\n00:00:01,000 --> 00:00:02,000\nHello %s\n" % fid).encode("utf-8")
        return 404, {}, b""


class Base(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="synoarr-subs-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.movies = os.path.join(self.tmp, "Vidéos", "Films")
        self.series = os.path.join(self.tmp, "Vidéos", "Séries")
        self.dl = os.path.join(self.tmp, "Download")
        for d in (self.movies, self.series, self.dl):
            os.makedirs(d)
        self.cfg = Config()
        self.cfg.general.data_dir = os.path.join(self.tmp, "data")
        self.cfg.paths.download_dir = self.dl
        self.cfg.paths.movies_dir = self.movies
        self.cfg.paths.series_dir = self.series
        self.cfg.paths.min_age_minutes = 0
        s = self.cfg.subtitles
        s.enabled, s.api_key, s.username, s.password, s.languages = True, API_KEY, "bob", PASSWORD, ["en"]
        self.db = Database(":memory:")
        self.addCleanup(self.db.close)
        self.api = FakeOpenSubtitles()
        self.api.__enter__()
        self.addCleanup(self.api.__exit__, None, None, None)

    def service(self, tmdb: Any = None) -> SubtitleService:
        svc = SubtitleService(self.cfg, self.db, HttpClient(timeout=5, retries=0), tmdb)
        svc.base_url = self.api.base_url
        svc.min_video_size = 0
        svc.request_pause = 0
        return svc

    def video(self, rel: str, size: int = 200000, mtime: float = None, root: str = None) -> str:
        path = os.path.join(root or self.movies, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as fh:
            fh.write(bytes((i * 7 + len(rel)) % 256 for i in range(size)))
        if mtime is not None:
            os.utime(path, (mtime, mtime))
        return path


class HashTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="synoarr-hash-")
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def write(self, data: bytes) -> str:
        path = os.path.join(self.tmp, "v.mkv")
        with open(path, "wb") as fh:
            fh.write(data)
        return path

    def test_zero_file_hash_is_its_size(self) -> None:
        self.assertEqual(opensubtitles_hash(self.write(b"\0" * 131072)), "0000000000020000")

    def test_known_vector(self) -> None:
        # head = 8192 words of value 1, tail = 8192 words of 0xFFFFFFFFFFFFFFFF (overflow wraps mod 2^64)
        path = self.write(b"\x01" + b"\0" * 7 + b"\x01\0\0\0\0\0\0\0" * 8191 + b"\xff" * 65536)
        expected = (131072 + 8192 * 1 + 8192 * 0xFFFFFFFFFFFFFFFF) % (1 << 64)
        self.assertEqual(opensubtitles_hash(path), "%016x" % expected)
        self.assertEqual(opensubtitles_hash(path), "0000000000020000")  # 8192 + 8192*(-1) = 0

    def test_synthetic_file_matches_reference(self) -> None:
        data = bytes((i * 31 + (i >> 8) * 17) % 256 for i in range(300001))
        path = self.write(data)
        self.assertEqual(opensubtitles_hash(path), _independent_hash(path))
        self.assertEqual(len(opensubtitles_hash(path)), 16)

    def test_too_small_or_missing(self) -> None:
        self.assertIsNone(opensubtitles_hash(self.write(b"x" * 1000)))
        self.assertIsNone(opensubtitles_hash(os.path.join(self.tmp, "absent.mkv")))


class HelpersTest(unittest.TestCase):
    def test_languages_and_existing(self) -> None:
        self.assertEqual(normalize_language("ENG"), "en")
        self.assertEqual(normalize_language("fre"), "fr")
        self.assertEqual(normalize_language("pt-BR"), "pt-br")
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        video = os.path.join(tmp, "Film (2010).mkv")
        for name in ("Film (2010).mkv", "Film (2010).fr.srt", "Film (2010).en.forced.srt", "Film (2010).de.nfo",
                     "Autre (2011).it.srt", "Film (2010).eng.sdh.ass"):
            open(os.path.join(tmp, name), "w").close()
        self.assertEqual(existing_subtitle_languages(video, ["srt", "ass"]), {"fr", "en"})
        self.assertGreater(name_similarity("Film.2010.1080p.BluRay.x264-GRP", "Film 2010 1080p BluRay x264 GRP"), 0.9)


class SelectionTest(Base):
    def test_choose_order(self) -> None:
        svc = self.service()
        cands = [sub(1, release="Film.2010.720p.HDTV", downloads=9000),
                 sub(2, release="Film.2010.1080p.BluRay.x264-GRP", downloads=10),
                 sub(3, lang="fr", release="Film.2010.1080p.BluRay.x264-GRP", hash_match=True),
                 sub(4, release="whatever", forced=True, hash_match=True),
                 sub(5, release="cd", files=2, hash_match=True)]
        self.assertEqual(svc.choose(cands, "en", "Film.2010.1080p.BluRay.x264-GRP")["file_id"], 2)
        cands.append(sub(6, release="other", hash_match=True))
        self.assertEqual(svc.choose(cands, "en", "Film.2010.1080p.BluRay.x264-GRP")["file_id"], 6)
        self.assertEqual(svc.choose(cands, "fr", "x")["file_id"], 3)
        self.assertIsNone(svc.choose(cands, "de", "x"))
        # same name: download_count decides; hearing impaired penalised unless wanted
        cands = [sub(10, release="A", downloads=5), sub(20, release="A", downloads=50, hi=True)]
        self.assertEqual(svc.choose(cands, "en", "A")["file_id"], 10)
        self.cfg.subtitles.hearing_impaired = True
        self.assertEqual(svc.choose(cands, "en", "A")["file_id"], 20)


class FetchTest(Base):
    def test_fetch_movie_by_imdb_downloads_and_saves(self) -> None:
        path = self.video("Film (2010)/Film (2010).mkv")
        self.api.results = [sub(100, release="Film.2010.1080p", downloads=3), sub(200, release="Film.2010", hash_match=True),
                            sub(300, lang="fr", release="Film.2010")]
        svc = self.service()
        res = svc.fetch_for(path, kind="movie", imdb="tt0012345", languages=["en"])
        self.assertEqual(res["status"], "found", res)
        target = os.path.join(self.movies, "Film (2010)", "Film (2010).en.srt")
        self.assertEqual(res["downloaded"], [target])
        with open(target, encoding="utf-8") as fh:
            self.assertIn("Hello 200", fh.read())
        self.assertEqual(self.api.downloads, [200])
        q = self.api.reqs("/subtitles")[0]
        self.assertEqual(q.query["imdb_id"], "12345")
        self.assertEqual(q.query["languages"], "en")
        self.assertEqual(q.query["type"], "movie")
        self.assertEqual(q.query["moviehash"], opensubtitles_hash(path))
        self.assertEqual(list(q.query), sorted(q.query))  # parameters sorted (no API redirect)
        self.assertEqual(q.headers["api-key"], API_KEY)
        self.assertEqual(q.headers["user-agent"], "synoarr v%s" % __version__)
        login = self.api.reqs("/login")
        self.assertEqual(len(login), 1)
        self.assertEqual(json.loads(login[0].body.decode()), {"username": "bob", "password": PASSWORD})
        dl = self.api.reqs("/download")[0]
        self.assertEqual(dl.headers["authorization"], "Bearer tok-abc")
        self.assertEqual(json.loads(dl.body.decode())["file_id"], 200)
        # second call: already present, no request at all
        n = len(self.api.srv.requests)
        self.assertEqual(svc.fetch_for(path, kind="movie", imdb="tt0012345")["status"], "present")
        self.assertEqual(len(self.api.srv.requests), n)
        self.assertEqual(self.db.recent_events(1)[0]["kind"], "subtitles")

    def test_episode_uses_parent_ids_and_numbers(self) -> None:
        path = self.video("Show/Saison 01/Show - S01E02.mkv", root=self.series)
        self.api.results = [sub(7, release="Show.S01E02.720p")]
        self.cfg.subtitles.languages = ["en", "fr"]
        res = self.service().fetch_for(path, kind="episode", tmdb="1399", season=1, episode=2)
        self.assertEqual(res["status"], "partial")
        self.assertEqual(res["missing"], ["fr"])
        q = self.api.reqs("/subtitles")[0].query
        self.assertEqual((q["parent_tmdb_id"], q["season_number"], q["episode_number"], q["type"]),
                         ("1399", "1", "2", "episode"))
        self.assertEqual(q["languages"], "en,fr")
        self.assertTrue(os.path.exists(os.path.join(self.series, "Show", "Saison 01", "Show - S01E02.en.srt")))

    def test_never_overwrites(self) -> None:
        path = self.video("Film (2010)/Film (2010).mkv")
        existing = os.path.join(self.movies, "Film (2010)", "Film (2010).en.srt")
        with open(existing, "w", encoding="utf-8") as fh:
            fh.write("mine")
        svc = self.service()
        self.assertEqual(svc.fetch_for(path, imdb="tt1")["status"], "present")
        self.assertEqual(self.api.srv.requests, [])
        self.assertIsNone(svc._save(path, "en", b"other"))
        with open(existing, encoding="utf-8") as fh:
            self.assertEqual(fh.read(), "mine")
        # an existing "eng" subtitle counts as English too
        os.remove(existing)
        open(os.path.join(self.movies, "Film (2010)", "Film (2010).eng.srt"), "w").close()
        self.assertEqual(svc.fetch_for(path, imdb="tt1")["status"], "present")

    def test_no_ids_query_by_title_and_fallback_without_hash(self) -> None:
        path = self.video("Inconnu Film (1999)/Inconnu Film (1999).mkv")
        self.api.results = [sub(9, release="Inconnu.Film.1999")]
        res = self.service().fetch_for(path)
        self.assertEqual(res["status"], "found", res)
        q = self.api.reqs("/subtitles")[0].query
        self.assertEqual((q["query"], q["year"]), ("inconnu film", "1999"))
        self.assertIn("moviehash", q)
        # nothing found with the hash: second search without it
        self.api.results = []
        path2 = self.video("Autre (2001)/Autre (2001).mkv")
        n = len(self.api.reqs("/subtitles"))
        self.assertEqual(self.service().fetch_for(path2)["status"], "none")
        reqs = self.api.reqs("/subtitles")[n:]
        self.assertEqual(len(reqs), 2)
        self.assertIn("moviehash", reqs[0].query)
        self.assertNotIn("moviehash", reqs[1].query)

    def test_identify_from_database_and_tmdb(self) -> None:
        mid = self.db.upsert_media("plex://1", "movie", "The Matrix", 1999, "tt0133093", "603", None)
        path = self.video("The Matrix (1999)/The Matrix (1999).mkv")
        self.api.results = [sub(1, release="The.Matrix.1999")]
        self.service().fetch_for(path)
        self.assertEqual(self.api.reqs("/subtitles")[0].query["imdb_id"], "133093")
        # by media id (import hook)
        path2 = self.video("Matrix copy/The Matrix (1999).mp4")
        self.service().process_item(path2, {"kind": "movie", "media_id": mid, "season": None, "episodes": []})
        self.assertEqual(self.api.reqs("/subtitles")[-1].query["imdb_id"], "133093")

        class Tmdb(object):
            enabled = True

            def search_movie(self, title, year):
                return {"title": "Other", "year": 2005, "tmdb": 4242} if title == "Other" else None

            def search_show(self, title):
                return {"title": "Show", "tmdb": 77}
        path3 = self.video("Other (2005)/Other (2005).mkv")
        self.service(Tmdb()).fetch_for(path3)
        self.assertEqual(self.api.reqs("/subtitles")[-1].query["tmdb_id"], "4242")
        path4 = self.video("Show/Season 02/Show - S02E05.mkv", root=self.series)
        self.service(Tmdb()).fetch_for(path4)
        q = self.api.reqs("/subtitles")[-1].query
        self.assertEqual((q["parent_tmdb_id"], q["season_number"], q["episode_number"]), ("77", "2", "5"))

    def test_errors_never_raise_and_never_log_secrets(self) -> None:
        path = self.video("Film (2010)/Film (2010).mkv")
        self.api.search_status = 500
        records = []  # type: List[logging.LogRecord]

        class H(logging.Handler):
            def emit(self, record: logging.LogRecord) -> None:
                records.append(record)
        h = H(logging.DEBUG)
        root = logging.getLogger()
        old = root.level
        root.addHandler(h)
        root.setLevel(logging.DEBUG)
        try:
            svc = self.service()
            self.assertEqual(svc.fetch_for(path, imdb="tt1")["status"], "error")
            self.api.search_status = 200
            self.api.results = [sub(1)]
            self.api.login_status = 401  # bad credentials: anonymous download
            self.assertEqual(svc.fetch_for(path, imdb="tt1")["status"], "found")
        finally:
            root.removeHandler(h)
            root.setLevel(old)
        text = "\n".join(r.getMessage() for r in records)
        self.assertNotIn(API_KEY, text)
        self.assertNotIn(PASSWORD, text)
        self.assertNotIn("authorization", self.api.reqs("/download")[0].headers)

    def test_disabled_without_key(self) -> None:
        self.cfg.subtitles.api_key = ""
        path = self.video("Film (2010)/Film (2010).mkv")
        self.assertEqual(self.service().fetch_for(path)["status"], "disabled")
        self.assertEqual(self.api.srv.requests, [])


class QuotaTest(Base):
    def test_remaining_zero_stops_scan_for_the_day(self) -> None:
        now = time.time()
        for i, name in enumerate(("A", "B", "C")):
            self.video("%s (2000)/%s (2000).mkv" % (name, name), mtime=now - 1000 * (3 - i))
        self.api.results = [sub(1, release="x")]
        self.api.remaining = 1
        svc = self.service()
        stats = svc.scan_library(limit=10)
        self.assertTrue(stats["quota_reached"])
        self.assertEqual(stats["downloaded"], 1)
        self.assertTrue(os.path.exists(os.path.join(self.movies, "A (2000)", "A (2000).en.srt")))  # oldest
        self.assertEqual(len(self.api.reqs("/download")), 1)
        self.assertGreater(svc.quota_until(), time.time())
        n = len(self.api.srv.requests)
        self.assertEqual(svc.fetch_for(os.path.join(self.movies, "B (2000)", "B (2000).mkv"))["status"], "quota")
        self.assertTrue(svc.scan_library()["quota_reached"])
        self.assertEqual(len(self.api.srv.requests), n)

    def test_http_406_sets_quota(self) -> None:
        path = self.video("A (2000)/A (2000).mkv")
        self.api.results = [sub(1)]
        self.api.remaining = 0
        svc = self.service()
        self.assertEqual(svc.fetch_for(path, imdb="tt1")["status"], "quota")
        self.assertGreater(svc.quota_until(), time.time())

    def test_http_429_pauses(self) -> None:
        path = self.video("A (2000)/A (2000).mkv")
        self.api.search_status = 429
        svc = self.service()
        self.assertEqual(svc.fetch_for(path, imdb="tt1")["status"], "quota")
        self.assertTrue(svc.quota_reached())
        self.assertEqual(svc.quota_until(), 0.0)  # not a daily quota


class ScanTest(Base):
    def test_scan_oldest_first_limit_and_cache(self) -> None:
        now = time.time()
        old = self.video("Old (1990)/Old (1990).mkv", mtime=now - 3000)
        mid = self.video("Show/Saison 01/Show - S01E01.mkv", root=self.series, mtime=now - 2000)
        new = self.video("New (2020)/New (2020).mkv", mtime=now - 1000)
        self.video("Done (2001)/Done (2001).mkv", mtime=now - 5000)
        open(os.path.join(self.movies, "Done (2001)", "Done (2001).en.srt"), "w").close()
        self.video("Old (1990)/Extras/trailer.mkv")
        self.video("Old (1990)/Old (1990)-sample.mkv")
        svc = self.service()
        self.assertEqual(sorted(svc.library_videos()), sorted([old, mid, new, os.path.join(
            self.movies, "Done (2001)", "Done (2001).mkv")]))
        stats = svc.scan_library(limit=2)  # nothing found on the fake API
        self.assertEqual((stats["videos"], stats["missing"], stats["searched"]), (4, 3, 2))
        searched = [r.query.get("query") for r in self.api.reqs("/subtitles")]
        self.assertEqual(searched[0], "old")
        self.assertIn("show", searched)
        self.assertNotIn("new", searched)
        # cache: the two videos without result are not searched again right away
        n = len(self.api.reqs("/subtitles"))
        stats = svc.scan_library(limit=5)
        self.assertEqual(stats["searched"], 1)
        self.assertEqual(self.api.reqs("/subtitles")[n].query.get("query"), "new")
        self.assertEqual(svc.scan_library(limit=5, force=True)["searched"], 3)


class HookTest(Base):
    def test_organizer_calls_back_once_per_imported_video(self) -> None:
        os.makedirs(os.path.join(self.dl, "Film.2010.1080p.BluRay.x264-GRP"))
        src = os.path.join(self.dl, "Film.2010.1080p.BluRay.x264-GRP", "Film.2010.1080p.BluRay.x264-GRP.mkv")
        with open(src, "wb") as fh:
            fh.write(b"v" * 300000)
        with open(src[:-4] + ".fr.srt", "w", encoding="utf-8") as fh:
            fh.write("1\n00:00:01,000 --> 00:00:02,000\nBonjour\n")
        old = time.time() - 3600
        for p in (src, src[:-4] + ".fr.srt", os.path.dirname(src)):
            os.utime(p, (old, old))
        calls = []  # type: List[Any]
        org = Organizer(self.cfg, self.db, None, None)
        org.on_file_imported = lambda dst, meta: calls.append((dst, meta, sorted(os.listdir(os.path.dirname(dst)))))
        org.run_once()
        self.assertEqual(len(calls), 1, calls)
        dst, meta, siblings = calls[0]
        self.assertTrue(dst.startswith(self.movies) and dst.endswith(".mkv"))
        self.assertEqual(meta["kind"], "movie")
        self.assertEqual(meta["source_name"], "Film.2010.1080p.BluRay.x264-GRP")
        self.assertTrue(any(s.endswith(".fr.srt") for s in siblings))  # release subtitles already in place
        org.run_once()
        self.assertEqual(len(calls), 1)

    def test_enqueue_fetches_in_background(self) -> None:
        mid = self.db.upsert_media("plex://9", "movie", "Film", 2010, "tt777", None, None)
        path = self.video("Film (2010)/Film (2010).mkv")
        self.api.results = [sub(5, release="Film.2010")]
        svc = self.service()
        svc.enqueue(path, {"kind": "movie", "media_id": mid, "season": None, "episodes": [],
                           "source_name": "Film.2010.1080p"})
        self.assertTrue(svc.wait_idle(10))
        self.assertTrue(os.path.exists(os.path.join(self.movies, "Film (2010)", "Film (2010).en.srt")))
        self.assertEqual(self.api.reqs("/subtitles")[0].query["imdb_id"], "777")
        self.cfg.subtitles.enabled = False
        svc.enqueue(path, {})  # disabled: ignored
        self.assertEqual(svc._queue.qsize(), 0)

    def test_app_wiring_and_command(self) -> None:
        from synoarr import app as app_mod
        from synoarr.config import load_config
        cfg_path = os.path.join(self.tmp, "config.ini")
        with open(cfg_path, "w", encoding="utf-8") as fh:
            fh.write("[general]\ndata_dir = ./data\n[paths]\ndownload_dir = %s\nmovies_dir = %s\nseries_dir = %s\n"
                     "[subtitles]\nenabled = true\napi_key = %s\nlanguages = EN, fr\nscan_interval_hours = 6\n"
                     % (self.dl, self.movies, self.series, API_KEY))
        cfg = load_config(cfg_path)
        self.assertEqual(cfg.warnings, [])
        self.assertEqual((cfg.subtitles.scan_interval_hours, cfg.subtitles.scan_limit), (6.0, 20))
        app = app_mod.App(cfg)
        try:
            self.assertIn("subtitles_scan", app_mod.COMMANDS)
            self.assertEqual(app.subtitles.languages(), ["en", "fr"])
            self.assertEqual(app.organizer.on_file_imported, app.subtitles.enqueue)
            app.subtitles.base_url = self.api.base_url
            app.subtitles.request_pause = 0
            job, created = app.start_command("subtitles_scan")
            self.assertTrue(created)
            deadline = time.time() + 10
            while job.running and time.time() < deadline:
                time.sleep(0.02)
            self.assertEqual(job.status, "done", job.to_dict())
            self.assertEqual(job.result["videos"], 0)
        finally:
            app._stop.set()
            app.db.close()


if __name__ == "__main__":
    unittest.main()
