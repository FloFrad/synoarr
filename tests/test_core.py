"""Tests for parser, config, db and httpclient (core modules)."""
from __future__ import annotations

import os
import tempfile
import unittest

from synoarr import config as config_mod
from synoarr.db import Database
from synoarr.httpclient import HttpClient, HttpError, redact
from synoarr.parser import normalize_title, parse, titles_match
from tests.fakes import FakeServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class ParserTests(unittest.TestCase):
    def check(self, name, title, year=None, seasons=None, episodes=None, res=None, langs=None, pack=False):
        p = parse(name)
        self.assertEqual(p.title, title, name)
        self.assertEqual(p.year, year, name)
        self.assertEqual(p.seasons, seasons or [], name)
        self.assertEqual(p.episodes, episodes or [], name)
        if res is not None:
            self.assertEqual(p.resolution, res, name)
        if langs is not None:
            self.assertEqual(p.languages, langs, name)
        self.assertEqual(p.is_season_pack, pack, name)

    def test_episodes(self):
        self.check("The.Last.of.Us.S01E03.1080p.WEB-DL.MULTI.x264-GRP.mkv", "The Last of Us", None, [1], [3],
                   "1080p", ["multi"])
        self.check("Show.Name.1x05.HDTV", "Show Name", None, [1], [5])
        self.check("The.Mandalorian.S02E01E02.720p", "The Mandalorian", None, [2], [1, 2], "720p")
        self.check("Show Name - S01E02 - Pilot.mkv", "Show Name", None, [1], [2])
        self.check("Doctor.Who.2005.S13E01.1080p", "Doctor Who", 2005, [13], [1])
        self.check("Show.S01E01-E03.720p", "Show", None, [1], [1, 2, 3])

    def test_packs(self):
        self.check("Breaking Bad S02 1080p BluRay x265", "Breaking Bad", None, [2], [], "1080p", pack=True)
        self.check("The.Office.US.S01-S03.Complete.720p", "The Office US", None, [1, 2, 3], [], pack=True)
        self.check("[Group] Some Anime - Season 2 - 1080p", "Some Anime", None, [2], [], pack=True)
        self.check("Show Saison 3 FRENCH 720p", "Show", None, [3], [], langs=["fr"], pack=True)

    def test_movies(self):
        self.check("Dune.Part.Two.2024.MULTI.TRUEFRENCH.1080p.BluRay.x264-XXX", "Dune Part Two", 2024,
                   res="1080p", langs=["multi"])
        self.check("Blade.Runner.2049.2017.2160p.UHD.BluRay", "Blade Runner 2049", 2017, res="2160p")
        self.check("2001.A.Space.Odyssey.1968.1080p", "2001 A Space Odyssey", 1968)
        self.check("Night of the Living Dead (1968)", "Night of the Living Dead", 1968)
        self.check("Film.2020.iTALiAN.1080p.WEBRiP", "Film", 2020, langs=["it"])
        self.check("Le.Fabuleux.Destin.d.Amelie.Poulain.2001.FRENCH.720p.BluRay",
                   "Le Fabuleux Destin d Amelie Poulain", 2001, langs=["fr"])

    def test_flags(self):
        self.assertTrue(parse("Movie.2019.1080p.sample.mkv").is_sample)
        self.assertEqual(parse("Movie.Title.2019.CAM.x264").source, "cam")
        self.assertEqual(parse("Serie.S03E05.VOSTFR.720p.WEB").languages, ["vostfr"])
        self.assertEqual(parse("/volume1/Download/Some.Movie.2010.mkv").extension, "mkv")

    def test_titles(self):
        self.assertEqual(normalize_title("L'Étrange Noël & Co."), "letrange noel and co")
        self.assertTrue(titles_match("The Office US", "The Office (US)"))
        self.assertTrue(titles_match("Amelie", "Amélie"))
        self.assertTrue(titles_match("Spider Man", "Spider-Man"))
        self.assertFalse(titles_match("Dune", "Dune Part Two"))
        self.assertTrue(titles_match("Le Fabuleux Destin", "X", ["Le fabuleux destin"]))
        self.assertFalse(titles_match("", "X"))


class ParserReviewTests(unittest.TestCase):
    def test_french_release_names(self):
        cases = [
            ("Lupin.S01.COMPLETE.FRENCH.1080p.WEB.x264", "Lupin", [1], [], True, ["fr"]),
            ("Kaamelott Saison 1 FRENCH DVDRIP", "Kaamelott", [1], [], True, ["fr"]),
            ("Les.Revenants.S02E03.VFF.720p.HDTV", "Les Revenants", [2], [3], False, ["fr"]),
            ("Le.Bureau.des.Legendes.S05.MULTi.1080p.WEB.H264-FRATERNiTY", "Le Bureau des Legendes", [5], [],
             True, ["multi"]),
            ("Intouchables.2011.TRUEFRENCH.1080p.BluRay.x264", "Intouchables", [], [], False, ["fr"]),
            ("Mr.Robot.Saison.1.VOSTFR", "Mr Robot", [1], [], True, ["vostfr"]),
            ("Amelie.MULTi.VFF.1080p", "Amelie", [], [], False, ["multi"]),
            ("Kaamelott.FRENCH.HDLight", "Kaamelott", [], [], False, ["fr"]),
        ]
        for name, title, seasons, episodes, pack, langs in cases:
            p = parse(name)
            self.assertEqual((p.title, p.seasons, p.episodes, p.is_season_pack, p.languages),
                             (title, seasons, episodes, pack, langs), name)
        p = parse("Kaamelott.iNTEGRALE.FRENCH.DVDRiP.XviD")
        self.assertEqual((p.title, p.is_complete), ("Kaamelott", True))

    def test_language_words_inside_titles(self):
        self.assertEqual(parse("The.French.Dispatch.2021.1080p").title, "The French Dispatch")
        self.assertEqual(parse("The.French.Dispatch.MULTi.x264-GRP").title, "The French Dispatch")
        self.assertEqual(parse("The.French.Connection.1971.FRENCH.1080p").languages, ["fr"])

    def test_future_number_is_not_a_year(self):
        p = parse("Blade.Runner.2049.1080p.BluRay")
        self.assertEqual((p.title, p.year), ("Blade Runner 2049", None))
        self.assertFalse(titles_match(p.title, "Blade Runner"))

    def test_titles_apostrophes_and_country(self):
        self.assertTrue(titles_match(parse("L.Arme.Fatale.1987.FRENCH").title, "L'Arme fatale"))
        self.assertTrue(titles_match("LArme Fatale", "L'Arme fatale"))
        self.assertTrue(titles_match("Greys Anatomy", "Grey's Anatomy"))
        self.assertTrue(titles_match("The Office US", "The Office"))
        self.assertTrue(titles_match("The Office", "The Office (US)"))
        self.assertFalse(titles_match("The Office UK", "The Office (US)"))


class ConfigTests(unittest.TestCase):
    def test_example_config_is_valid(self):
        cfg = config_mod.load_config(os.path.join(ROOT, "config.example.ini"))
        self.assertEqual(cfg.validate(), [])
        self.assertEqual(cfg.quality.resolutions, ["1080p", "720p"])
        self.assertEqual(cfg.quality.languages, ["en"])
        self.assertEqual(cfg.quality.languages_animation, ["fr"])
        self.assertEqual(cfg.paths.movies_dir, "/volume1/video/Films")
        self.assertEqual(cfg.paths.series_dir, "/volume1/video/TvShows")
        self.assertEqual(cfg.naming.movie_format, "{title} ({year})")
        self.assertEqual(cfg.downloadstation.destination, "Download")
        self.assertFalse(cfg.general.dry_run)
        self.assertTrue(cfg.data_dir.endswith("data"))

    def test_overrides_and_errors(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "c.ini")
            with open(p, "w", encoding="utf-8") as fh:
                fh.write("[general]\ndry_run = oui\n[downloadstation]\ndestination = /Download/sub/\n"
                         "password = a;b#c\n[paths]\naction = MOVE\n")
            os.environ["SYNOARR_WEB_PORT"] = "9999"
            try:
                cfg = config_mod.load_config(p)
            finally:
                del os.environ["SYNOARR_WEB_PORT"]
            self.assertTrue(cfg.general.dry_run)
            self.assertEqual(cfg.downloadstation.destination, "Download/sub")
            self.assertEqual(cfg.downloadstation.password, "a;b#c")
            self.assertEqual(cfg.paths.action, "move")
            self.assertEqual(cfg.web.port, 9999)
            self.assertEqual(cfg.data_dir, os.path.join(d, "data"))
            with open(p, "w", encoding="utf-8") as fh:
                fh.write("[web]\nport = abc\n")
            with self.assertRaises(config_mod.ConfigError):
                config_mod.load_config(p)
            with open(p, "w", encoding="utf-8") as fh:
                fh.write("[naming]\nmovie_format = {titre}\n[paths]\naction = zip\n")
            self.assertEqual(len(config_mod.load_config(p).validate()), 2)
        with self.assertRaises(config_mod.ConfigError):
            config_mod.load_config("/nonexistent/config.ini")

    def _write(self, d, data):
        p = os.path.join(d, "c.ini")
        with open(p, "wb") as fh:
            fh.write(data)
        return p

    def test_windows_edited_file(self):
        with tempfile.TemporaryDirectory() as d:
            text = ("[downloadstation]\r\n; utilisateur dédié\r\nusername = flo\r\npassword = p@ss #1;x\r\n"
                    "[paths]\r\nmovies_dir = \"/volume1/Vidéos/Films/\"\r\n[web]\r\nport = 8778\r\n")
            for data in (text.encode("utf-8"), b"\xef\xbb\xbf" + text.encode("utf-8"), text.encode("utf-16"),
                         text.encode("cp1252")):
                cfg = config_mod.load_config(self._write(d, data))
                self.assertEqual(cfg.downloadstation.username, "flo")
                self.assertEqual(cfg.downloadstation.password, "p@ss #1;x")  # secrets kept verbatim
                self.assertEqual(cfg.paths.movies_dir, "/volume1/Vidéos/Films")
                self.assertEqual(cfg.web.port, 8778)

    def test_password_starting_with_comment_char(self):
        with tempfile.TemporaryDirectory() as d:
            for pwd in ("#secret%x", ";abc", "abc ;def", "a #b"):
                cfg = config_mod.load_config(self._write(d, ("[downloadstation]\npassword = %s\n" % pwd).encode()))
                self.assertEqual(cfg.downloadstation.password, pwd)

    def test_defaults_match_example(self):
        example = config_mod.load_config(os.path.join(ROOT, "config.example.ini"))
        default = config_mod.Config()
        self.assertEqual(default.paths.movies_dir, example.paths.movies_dir)
        self.assertEqual(default.paths.series_dir, example.paths.series_dir)

    def test_missing_share_root(self):
        self.assertEqual(config_mod.missing_share_root("/volume99/Videos/Films"), "/volume99/Videos")
        self.assertIsNone(config_mod.missing_share_root("/tmp/x/y"))

    def test_unknown_keys_are_reported(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = config_mod.load_config(self._write(d, b"[paths]\nmovie_dir = /x\n[Plex]\ntoken = a\n"))
            self.assertEqual(len(cfg.warnings), 2, cfg.warnings)
            self.assertIn("movie_dir", cfg.warnings[0] + cfg.warnings[1])


class DbTests(unittest.TestCase):
    def test_roundtrip(self):
        db = Database(":memory:")
        mid = db.upsert_media("k1", "show", "Show", 2020, None, "1", None)
        self.assertEqual(db.upsert_media("k1", "show", "Show", None, "tt1", None, None), mid)
        self.assertEqual(db.get_media(mid)["imdb"], "tt1")
        self.assertEqual(db.get_media(mid)["year"], 2020)
        db.upsert_episode(mid, 1, 1, "Pilot", "2020-01-01")
        db.upsert_episode(mid, 1, 2, None, None)
        db.set_episode_status(mid, 1, None, "grabbed", only_if=("wanted",))
        self.assertEqual([e["status"] for e in db.list_episodes(mid)], ["grabbed", "grabbed"])
        gid = db.add_grab(mid, 1, None, "Show.S01.1080p", "Show.S01.1080p-GRP", "ia", "abc", 10)
        self.assertEqual(db.find_grab_by_name("show.s01.1080p-grp")["id"], gid)
        db.mark_off_watchlist([])
        self.assertEqual(db.get_media(mid)["on_watchlist"], 0)
        db.block_release("Bad.Release")
        self.assertTrue(db.is_blocked("bad.release"))
        db.set_kv("a", "1")
        db.set_kv("a", "2")
        self.assertEqual(db.get_kv("a"), "2")
        iid = db.add_indexer("IA", "internet_archive", "https://archive.org")
        self.assertEqual(db.get_indexer("ia")["id"], iid)
        self.assertEqual(db.get_indexer(str(iid))["name"], "IA")
        db.add_event("x", "hello")
        self.assertEqual(db.recent_events(1)[0]["message"], "hello")


class HttpTests(unittest.TestCase):
    def test_client(self):
        def handler(req):
            if req.path == "/json":
                return 200, {}, {"q": req.query.get("q")}
            if req.path == "/magnet":
                return 302, {"Location": "magnet:?xt=urn:btih:abc"}, b""
            if req.path == "/upload":
                fn, content = req.multipart["torrent"]
                return 200, {}, {"fn": fn, "len": len(content), "api": req.params().get("api")}
            if req.path == "/redirect":
                return 302, {"Location": "/json?q=redir"}, b""
            return 404, {}, b"nope"

        with FakeServer(handler) as srv:
            http = HttpClient(retries=0)
            self.assertEqual(http.get(srv.url + "/json", params={"q": "é x"}).json(), {"q": "é x"})
            self.assertEqual(http.get(srv.url + "/redirect").json(), {"q": "redir"})
            r = http.get(srv.url + "/magnet")
            self.assertEqual(r.redirect_location, "magnet:?xt=urn:btih:abc")
            r = http.post(srv.url + "/upload", data={"api": "X"}, files={"torrent": ("a.torrent", b"d1:ae", "x")})
            self.assertEqual(r.json(), {"fn": "a.torrent", "len": 5, "api": "X"})
            with self.assertRaises(HttpError) as ctx:
                http.get(srv.url + "/missing")
            self.assertEqual(ctx.exception.status, 404)
            self.assertEqual(http.get(srv.url + "/missing", raise_for_status=False).status, 404)
        with self.assertRaises(HttpError):
            HttpClient(retries=0, timeout=2).get("http://127.0.0.1:1/")

    def test_redact(self):
        self.assertNotIn("SECRET", redact("http://x/api?apikey=SECRET&t=caps"))
        self.assertNotIn("SECRET", redact("http://x/?X-Plex-Token=SECRET"))


if __name__ == "__main__":
    unittest.main()


class InitTests(unittest.TestCase):
    def test_init_generates_a_web_password(self):
        import contextlib
        import io as _io
        from synoarr import cli
        with tempfile.TemporaryDirectory() as d:
            target = os.path.join(d, "config.ini")
            buf = _io.StringIO()
            with contextlib.redirect_stdout(buf):
                self.assertEqual(cli.main(["init", "--config", target]), 0)
            cfg = config_mod.load_config(target)
            self.assertEqual(cfg.web.username, "admin")
            self.assertGreaterEqual(len(cfg.web.password), 10)
            self.assertIn(cfg.web.password, buf.getvalue())
            self.assertEqual(cfg.downloadstation.username, "")  # only the [web] section is filled
            self.assertEqual(cfg.validate(), [])
