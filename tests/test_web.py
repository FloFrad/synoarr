from __future__ import annotations

import base64
import json
import os
import shutil
import tempfile
import unittest
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

import stat
import sys
import threading
import time
from unittest import mock

from synoarr.app import App
from synoarr.config import Config, load_config
from synoarr.db import Database
from synoarr.httpclient import HttpError
from synoarr.organizer import Organizer
from synoarr.web import MASK, WebServer, mask_secrets, tail_log
from tests.fakes import FakeServer
from tests.test_e2e import MOVIE, World, write

CATALOG = [
    {"id": "ia", "name": "Internet Archive", "type": "internet_archive", "url": "https://archive.org",
     "description": "Films du domaine public", "categories": ""},
    {"id": "demo", "name": "Démo RSS", "type": "rss", "url": "https://example.org/rss",
     "description": "Flux de démonstration", "categories": ""},
]


class FakeIndexerManager:
    def __init__(self, db: Database):
        self.db = db
        self.tested = []  # type: List[int]

    def _get(self, indexer_id: int) -> Dict[str, Any]:
        row = self.db.get_indexer(indexer_id)
        if not row:
            raise ValueError("Indexer introuvable")
        return row

    def list(self) -> List[Dict[str, Any]]:
        return self.db.list_indexers()

    def catalog(self) -> List[Dict[str, Any]]:
        names = {r["name"] for r in self.db.list_indexers()}
        return [dict(c, installed=c["name"] in names) for c in CATALOG]

    def add_from_catalog(self, catalog_id: str) -> Dict[str, Any]:
        for c in CATALOG:
            if c["id"] == catalog_id:
                return self._get(self.db.add_indexer(c["name"], c["type"], c["url"]))
        raise ValueError("Entrée du catalogue inconnue : %s" % catalog_id)

    def add_url(self, name: str, url: str, api_key: str = "", categories: str = "") -> Dict[str, Any]:
        if self.db.get_indexer(name):
            raise ValueError("Un indexer nommé « %s » existe déjà" % name)
        return self._get(self.db.add_indexer(name, "torznab", url, api_key, categories))

    def test(self, indexer_id: int) -> Tuple[bool, str]:
        row = self._get(indexer_id)
        self.tested.append(indexer_id)
        return (True, "Connexion réussie à %s" % row["name"])

    def set_enabled(self, indexer_id: int, enabled: bool) -> None:
        self._get(indexer_id)
        self.db.update_indexer(indexer_id, enabled=1 if enabled else 0)

    def delete(self, indexer_id: int) -> None:
        self._get(indexer_id)
        self.db.delete_indexer(indexer_id)


class FakeApp:
    def __init__(self, db: Database):
        self.cfg = Config()
        self.db = db
        self.indexer_manager = FakeIndexerManager(db)
        self.calls = []  # type: List[Tuple[str, Any]]
        self.fail_status = False

    def run_watchlist_now(self) -> None:
        self.calls.append(("watchlist", None))

    def run_process_now(self) -> None:
        self.calls.append(("process", None))

    def search_now(self, media_id: int) -> None:
        self.calls.append(("search", media_id))

    def set_media_status(self, media_id: int, status: str) -> None:
        self.calls.append(("status", (media_id, status)))
        self.db.set_media_status(media_id, status)

    def status(self) -> Dict[str, Any]:
        if self.fail_status:
            raise RuntimeError("boom")
        return {"version": "1.0.0", "started_at": "2026-11-13 10:00:00", "last_watchlist_run": None,
                "last_process_run": "2026-11-13 10:05:00", "last_errors": ["Plex injoignable"],
                "dry_run": False, "plex_configured": True, "ds_configured": False}


def seed(db: Database) -> Dict[str, int]:
    movie = db.upsert_media("plex://movie/1", "movie", "Le Voyage dans la Lune", 1902, "tt0000417", None, None)
    show = db.upsert_media("plex://show/2", "show", "Sherlock", 2010, None, None, "176941")
    for ep, st in ((1, "done"), (2, "grabbed"), (3, "wanted")):
        db.upsert_episode(show, 1, ep, "Épisode %d" % ep, "2010-07-%02d" % (24 + ep), st)
    db.upsert_episode(show, 2, 1, None, None, "wanted")
    db.add_grab(show, 1, 2, "Sherlock.S01E02.1080p.WEB-DL", "Sherlock.S01E02.1080p.WEB-DL", "Jackett", "abc", 1500000000)
    db.add_event("grab", "Envoyé à Download Station : Sherlock S01E02")
    idx = db.add_indexer("Jackett", "torznab", "http://jackett:9117/api/v2.0/indexers/all/results/torznab?apikey=SECRET1",
                         "SECRET2", "2000,5000")
    return {"movie": movie, "show": show, "indexer": idx}


class WebTestBase(unittest.TestCase):
    username = ""
    password = ""

    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="synoarr-web-")
        self.db = Database(os.path.join(self.tmp, "Vidéos", "synoarr.db"))
        self.ids = seed(self.db)
        self.app = FakeApp(self.db)
        self.server = WebServer(self.app, "127.0.0.1", 0, self.username, self.password)
        self.server.start()
        self.base = "http://127.0.0.1:%d" % self.server.port

    def tearDown(self) -> None:
        self.server.stop()
        self.db.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def req(self, path: str, method: str = "GET", body: Any = None, raw: Optional[bytes] = None,
            auth: Optional[Tuple[str, str]] = None, headers: Optional[Dict[str, str]] = None) -> Tuple[int, Any, Any]:
        data = raw if raw is not None else (json.dumps(body).encode("utf-8") if body is not None else None)
        r = urllib.request.Request(self.base + path, data=data, method=method)
        if data is not None:
            r.add_header("Content-Type", "application/json")
        if auth is None and self.username:
            auth = (self.username, self.password)
        if auth:
            r.add_header("Authorization", "Basic " + base64.b64encode(("%s:%s" % auth).encode("utf-8")).decode())
        for k, v in (headers or {}).items():
            r.add_header(k, v)
        try:
            with urllib.request.urlopen(r, timeout=10) as resp:
                status, hdrs, payload = resp.status, resp.headers, resp.read()
        except urllib.error.HTTPError as exc:
            status, hdrs, payload = exc.code, exc.headers, exc.read()
        ctype = hdrs.get("Content-Type", "")
        if "json" in ctype:
            return status, json.loads(payload.decode("utf-8")), hdrs
        return status, payload.decode("utf-8"), hdrs


class WebApiTest(WebTestBase):
    def test_port_is_bound(self) -> None:
        self.assertGreater(self.server.port, 0)

    def test_index_page(self) -> None:
        status, html, hdrs = self.req("/")
        self.assertEqual(status, 200)
        self.assertIn("text/html", hdrs["Content-Type"])
        for word in ("Tableau de bord", "Watchlist", "Indexers", "Historique", "Synchroniser la Watchlist",
                     "Commandes", "Journal", "Configuration", "Rechercher tout", "Simulation du traitement",
                     "Vérifier la configuration", "Tester tous les indexers", "Recherche manuelle", "Connexion Plex",
                     "Redémarrer synoarr", "Télécharger le journal", "Restaurer la sauvegarde précédente",
                     "Envoyer à Download Station", "Interface web non protégée"):
            self.assertIn(word, html)
        self.assertNotIn("http://", html.split("<script>")[0].replace('href="data:,"', ""))  # aucun CDN
        self.assertIn("script-src 'sha256-", hdrs["Content-Security-Policy"])
        self.assertLess(len(html.splitlines()), 500)
        self.assertLess(len(html), 120 * 1024)

    def test_status(self) -> None:
        status, data, _ = self.req("/api/status")
        self.assertEqual(status, 200)
        self.assertEqual(data["version"], "1.0.0")
        self.assertEqual(data["last_errors"], ["Plex injoignable"])
        self.assertEqual(data["counts"]["movies"], 1)
        self.assertEqual(data["counts"]["shows"], 1)
        self.assertEqual(data["counts"]["episodes_wanted"], 2)
        self.assertEqual(data["counts"]["downloading"], 1)
        self.assertEqual(data["counts"]["indexers_enabled"], 1)

    def test_media_and_episodes(self) -> None:
        status, data, _ = self.req("/api/media")
        self.assertEqual(status, 200)
        by_type = {m["type"]: m for m in data}
        self.assertEqual(by_type["show"]["episodes"], {"total": 4, "done": 1, "grabbed": 1, "wanted": 2})
        self.assertNotIn("episodes", by_type["movie"])
        status, eps, _ = self.req("/api/media/%d/episodes" % self.ids["show"])
        self.assertEqual(status, 200)
        self.assertEqual([(e["season"], e["episode"]) for e in eps], [(1, 1), (1, 2), (1, 3), (2, 1)])
        self.assertEqual(self.req("/api/media/999/episodes")[0], 404)

    def test_media_search(self) -> None:
        status, data, _ = self.req("/api/media/%d/search" % self.ids["movie"], "POST")
        self.assertEqual(status, 200)
        self.assertTrue(data["ok"])
        self.assertIn(("search", self.ids["movie"]), self.app.calls)
        self.assertEqual(self.req("/api/media/999/search", "POST")[0], 404)

    def test_media_status(self) -> None:
        mid, sid = self.ids["movie"], self.ids["show"]
        status, data, _ = self.req("/api/media/%d/status" % mid, "POST", {"status": "ignored"})
        self.assertEqual((status, data["status"]), (200, "ignored"))
        self.assertEqual(self.db.get_media(mid)["status"], "ignored")
        # "monitored" sur un film -> "wanted" ; "wanted" sur une série -> "monitored"
        self.assertEqual(self.req("/api/media/%d/status" % mid, "POST", {"status": "monitored"})[1]["status"], "wanted")
        self.assertEqual(self.req("/api/media/%d/status" % sid, "POST", {"status": "wanted"})[1]["status"], "monitored")
        status, data, _ = self.req("/api/media/%d/status" % mid, "POST", {"status": "nope"})
        self.assertEqual(status, 400)
        self.assertIn("error", data)
        self.assertEqual(self.req("/api/media/%d/status" % mid, "POST", ["ignored"])[0], 400)
        self.assertEqual(self.req("/api/media/999/status", "POST", {"status": "ignored"})[0], 404)

    def test_indexers_masked(self) -> None:
        status, data, _ = self.req("/api/indexers")
        self.assertEqual(status, 200)
        self.assertEqual(data[0]["api_key"], MASK)
        text = json.dumps(data)
        self.assertNotIn("SECRET1", text)
        self.assertNotIn("SECRET2", text)
        self.assertIn("jackett:9117", data[0]["url"])

    def test_mask_helper(self) -> None:
        self.assertEqual(mask_secrets({"api_key": ""})["api_key"], "")
        self.assertEqual(mask_secrets({"url": "http://u:p@h/x?a=1"})["url"], "http://h/x?a=1")

    def test_catalog_and_add(self) -> None:
        status, cat, _ = self.req("/api/catalog")
        self.assertEqual(status, 200)
        self.assertEqual([c["installed"] for c in cat], [False, False])
        status, row, _ = self.req("/api/indexers", "POST", {"catalog_id": "ia"})
        self.assertEqual(status, 200)
        self.assertEqual(row["name"], "Internet Archive")
        self.assertTrue(self.req("/api/catalog")[1][0]["installed"])
        status, data, _ = self.req("/api/indexers", "POST", {"catalog_id": "inconnu"})
        self.assertEqual(status, 400)
        self.assertIn("inconnue", data["error"])

    def test_add_by_url(self) -> None:
        body = {"name": "Mon Torznab", "url": "http://t/api", "api_key": "TOPSECRET", "categories": "2000"}
        status, row, _ = self.req("/api/indexers", "POST", body)
        self.assertEqual(status, 200)
        self.assertEqual(row["api_key"], MASK)
        self.assertEqual(self.db.get_indexer("Mon Torznab")["api_key"], "TOPSECRET")
        status, data, _ = self.req("/api/indexers", "POST", body)  # doublon -> ValueError -> 400
        self.assertEqual(status, 400)
        self.assertIn("existe déjà", data["error"])
        self.assertEqual(self.req("/api/indexers", "POST", {"name": "x"})[0], 400)

    def test_indexer_test_enable_delete(self) -> None:
        iid = self.ids["indexer"]
        status, data, _ = self.req("/api/indexers/%d/test" % iid, "POST")
        self.assertEqual((status, data["ok"]), (200, True))
        self.assertIn("Connexion réussie", data["message"])
        status, data, _ = self.req("/api/indexers/%d/enabled" % iid, "POST", {"enabled": False})
        self.assertEqual((status, data["enabled"]), (200, False))
        self.assertEqual(self.db.get_indexer(iid)["enabled"], 0)
        self.assertEqual(self.req("/api/indexers/%d/enabled" % iid, "POST", {"enabled": "oui"})[0], 400)
        self.assertEqual(self.req("/api/indexers/999/test", "POST")[0], 400)
        status, data, _ = self.req("/api/indexers/%d" % iid, "DELETE")
        self.assertEqual((status, data["ok"]), (200, True))
        self.assertIsNone(self.db.get_indexer(iid))

    def test_grabs_and_events(self) -> None:
        status, grabs, _ = self.req("/api/grabs")
        self.assertEqual(status, 200)
        self.assertEqual(grabs[0]["media_title"], "Sherlock")
        status, events, _ = self.req("/api/events?limit=5")
        self.assertEqual(status, 200)
        self.assertEqual(events[0]["kind"], "grab")
        self.assertEqual(self.req("/api/events?limit=abc")[0], 400)

    def test_run_endpoints(self) -> None:
        self.assertEqual(self.req("/api/run/watchlist", "POST")[0], 200)
        self.assertEqual(self.req("/api/run/process", "POST", {})[0], 200)
        self.assertEqual([c[0] for c in self.app.calls], ["watchlist", "process"])

    def test_errors(self) -> None:
        status, data, _ = self.req("/api/run/watchlist", "POST", raw=b"{pas du json")
        self.assertEqual(status, 400)
        self.assertEqual(data["error"], "JSON invalide")
        self.assertEqual(self.req("/api/inconnu")[0], 404)
        self.assertEqual(self.req("/autre")[0], 404)
        self.assertEqual(self.req("/api/media/abc/episodes")[0], 404)
        self.assertEqual(self.req("/api/status", "DELETE")[0], 405)
        self.assertEqual(self.req("/api/run/process", "POST", headers={"Origin": "http://evil.example"})[0], 403)
        self.assertEqual(self.app.calls, [])

    def test_exception_gives_500_and_server_survives(self) -> None:
        self.app.fail_status = True
        with self.assertLogs("synoarr.web", "ERROR"):
            status, data, _ = self.req("/api/status")
        self.assertEqual(status, 500)
        self.assertIn("error", data)
        self.app.fail_status = False
        self.assertEqual(self.req("/api/status")[0], 200)

    def test_media_language_override(self) -> None:
        mid = self.ids["movie"]
        status, data, _ = self.req("/api/media/%d/language" % mid, "POST", {"languages": "fr"})
        self.assertEqual((status, data["languages"]), (200, "fr"))
        self.assertEqual(self.db.get_media(mid)["language_override"], "fr")
        status, data, _ = self.req("/api/media/%d/language" % mid, "POST", {"languages": " EN ; fr "})
        self.assertEqual((status, data["languages"]), (200, "en,fr"))
        self.assertEqual(self.db.get_media(mid)["language_override"], "en,fr")
        for bad in ("français", "e", "en,,fr1", 3):
            self.assertEqual(self.req("/api/media/%d/language" % mid, "POST", {"languages": bad})[0], 400, bad)
        self.assertEqual(self.req("/api/media/%d/language" % mid, "POST", {"languages": ""})[0], 200)
        self.assertIsNone(self.db.get_media(mid)["language_override"])
        self.assertEqual(self.req("/api/media/999/language", "POST", {"languages": "fr"})[0], 404)
        movie = [m for m in self.req("/api/media")[1] if m["id"] == mid][0]
        self.assertEqual(movie["languages"], self.app.cfg.quality.languages)  # FakeApp : pas de searcher

    def test_no_auth_required_when_not_configured(self) -> None:
        self.assertFalse(self.server.auth_enabled)
        self.assertEqual(self.req("/api/status", auth=("x", "y"))[0], 200)


class WebAuthTest(WebTestBase):
    username = "admin"
    password = "mot-de-passe-é"

    def test_requires_auth(self) -> None:
        for path in ("/", "/api/status", "/api/indexers"):
            status, _, hdrs = self.req(path, auth=("", ""))
            self.assertEqual(status, 401, path)
            self.assertIn("Basic", hdrs["WWW-Authenticate"])
        self.assertEqual(self.req("/api/status", auth=("admin", "faux"))[0], 401)
        self.assertEqual(self.req("/api/status", auth=("Admin", self.password))[0], 401)
        self.assertEqual(self.req("/api/status", headers={"Authorization": "Basic !!!"}, auth=("", ""))[0], 401)

    def test_good_credentials(self) -> None:
        self.assertEqual(self.req("/api/status")[0], 200)
        self.assertEqual(self.req("/")[0], 200)
        self.assertEqual(self.req("/api/run/watchlist", "POST")[0], 200)


class WebCsrfAndJobsTest(WebTestBase):
    def test_cross_site_form_post_is_refused(self) -> None:
        status, _, _ = self.req("/api/run/process", "POST", raw=b'{"x": 1}',
                                headers={"Origin": "http://evil.example", "Content-Type": "text/plain"})
        self.assertEqual(status, 403)
        status, _, _ = self.req("/api/indexers/%d" % self.ids["indexer"], "DELETE",
                                headers={"Origin": "http://evil.example"})
        self.assertEqual(status, 403)
        self.assertEqual(self.app.calls, [])

    def test_reverse_proxy_origin_is_accepted(self) -> None:
        # DSM / nginx reverse proxy: the browser talks to https://media.example.org, synoarr sees Host=127.0.0.1
        status, _, _ = self.req("/api/run/process", "POST", raw=b"",
                                headers={"Origin": "https://media.example.org",
                                         "X-Forwarded-Host": "media.example.org"})
        self.assertEqual(status, 200)
        # proxy that forwards neither Host nor X-Forwarded-Host: the JSON body of the page is still accepted
        status, _, _ = self.req("/api/run/watchlist", "POST", body={},
                                headers={"Origin": "https://media.example.org"})
        self.assertEqual(status, 200)
        self.assertEqual([c[0] for c in self.app.calls], ["process", "watchlist"])

    def test_same_origin_by_ip_and_port(self) -> None:
        host = "127.0.0.1:%d" % self.server.port
        status, _, _ = self.req("/api/run/process", "POST", raw=b"", headers={"Origin": "http://" + host})
        self.assertEqual(status, 200)

    def test_job_already_running_message(self) -> None:
        self.app.run_watchlist_now = lambda: False  # type: ignore[assignment]
        status, data, _ = self.req("/api/run/watchlist", "POST", body={})
        self.assertEqual(status, 200)
        self.assertIn("déjà en cours", data["message"])



class LogTailTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="synoarr-log-")
        self.path = os.path.join(self.tmp, "synoarr.log")

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_big_file_is_read_from_the_end(self) -> None:
        with open(self.path, "w", encoding="utf-8") as fh:
            for i in range(30000):
                fh.write("2026-11-13 10:00:%02d %-7s synoarr.x: ligne %d é\n" % (i % 60, "INFO" if i % 10 else "ERROR", i))
        data = tail_log(self.path, 5)
        self.assertEqual([l["text"].split("ligne ")[1] for l in data["lines"]],
                         ["29995 é", "29996 é", "29997 é", "29998 é", "29999 é"])
        self.assertTrue(data["partial"])
        data = tail_log(self.path, 3, "ERROR", max_bytes=100000)
        self.assertEqual([l["level"] for l in data["lines"]], ["ERROR"] * 3)
        self.assertTrue(data["lines"][-1]["text"].endswith("ligne 29990 é"))
        self.assertEqual(tail_log(self.path, 10, "DEBUG", "ligne 29003 é")["matched"], 1)
        data = tail_log(self.path, 10, "DEBUG", "ligne 3 é", max_bytes=1024 * 1024)
        self.assertEqual(data["matched"], 0)  # au-delà du dernier Mo : non lu
        self.assertFalse(tail_log(os.path.join(self.tmp, "absent.log"))["exists"])


# --------------------------------------------------------------------------- real App
class RealAppWebBase(unittest.TestCase):
    """Vraie App (config.ini temporaire) + services simulés (tests/test_e2e.World) + WebServer."""
    username = ""
    password = ""
    extra_config = ""

    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="synoarr-webapp-")
        self.world = World()
        self.world.pin_checks = 0
        orig_handler = self.world.handler

        def handler(req):  # + plex.tv PIN
            if req.path == "/api/v2/pins" and req.method == "POST":
                return 201, {}, {"id": 42, "code": "AB12"}
            if req.path == "/api/v2/pins/42":
                self.world.pin_checks += 1
                return 200, {}, {"id": 42, "code": "AB12",
                                 "authToken": "NEWTOKEN" if self.world.pin_checks >= 2 else None}
            return orig_handler(req)
        self.srv = FakeServer(handler).__enter__()
        self.world.base = self.srv.url
        self.dl = os.path.join(self.tmp, "volume1", "Download")
        self.movies = os.path.join(self.tmp, "volume1", "Vidéos", "Films")
        self.series = os.path.join(self.tmp, "volume1", "Vidéos", "Séries")
        os.makedirs(self.dl)
        self.cfg_path = os.path.join(self.tmp, "config.ini")
        self.cfg_text = """; configuration de test (commentaire conservé)
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
port = 8778
{extra}""".format(url=self.srv.url, dl=self.dl, movies=self.movies, series=self.series, extra=self.extra_config)
        with open(self.cfg_path, "w", encoding="utf-8") as fh:
            fh.write(self.cfg_text)
        os.chmod(self.cfg_path, 0o640)
        urls = {"discover_url": self.srv.url, "metadata_url": self.srv.url, "plextv_url": self.srv.url}
        self.patches = [mock.patch.object(App, "plex_urls", urls), mock.patch.object(App, "pin_poll_interval", 0.05),
                        mock.patch.object(Organizer, "min_video_size", 0)]
        for p in self.patches:
            p.start()
        self.app = App(load_config(self.cfg_path))
        self.server = WebServer(self.app, "127.0.0.1", 0, self.username, self.password)
        self.server.start()
        self.app.web = self.server
        self.base = "http://127.0.0.1:%d" % self.server.port

    def tearDown(self) -> None:
        self.app._stop.set()
        self.server.stop()
        self.app.db.close()
        for p in self.patches:
            p.stop()
        self.srv.__exit__(None, None, None)
        shutil.rmtree(self.tmp, ignore_errors=True)

    req = WebTestBase.req

    def add_indexer(self) -> None:
        self.app.indexer_manager.add_url("Fake", self.srv.url + "/torznab", type_="torznab")

    def start(self, command: str, **kw: Any) -> Dict[str, Any]:
        status, data, _ = self.req("/api/jobs", "POST", dict(kw, command=command))
        self.assertEqual(status, 200, data)
        return data

    def wait(self, job_id: int, timeout: float = 20) -> Dict[str, Any]:
        deadline = time.time() + timeout
        while time.time() < deadline:
            status, job, _ = self.req("/api/jobs/%d" % job_id)
            self.assertEqual(status, 200, job)
            if job["status"] != "running":
                return job
            time.sleep(0.05)
        self.fail("tâche %d toujours en cours" % job_id)

    def run_cmd(self, command: str, **kw: Any) -> Dict[str, Any]:
        return self.wait(self.start(command, **kw)["job"]["id"])

    def block_search(self) -> threading.Event:
        """search_report bloquant : une recherche manuelle reste « en cours » jusqu'à release.set()."""
        release = threading.Event()
        orig = self.app.searcher.search_report

        def slow(req):
            release.wait(10)
            return orig(req)
        self.app.searcher.search_report = slow
        self.addCleanup(release.set)
        return release


class WebCommandsTest(RealAppWebBase):
    def test_job_lifecycle_and_duplicates(self) -> None:
        self.add_indexer()
        job = self.run_cmd("test_indexers")
        self.assertEqual(job["status"], "done")
        self.assertEqual(job["result"]["results"][0]["name"], "Fake")
        self.assertTrue(job["result"]["results"][0]["ok"])
        self.assertEqual(job["output"][0]["level"], "ok")
        self.assertIn("Fake", job["output"][0]["text"])

        release = self.block_search()
        first = self.start("search", query="Night of the Living Dead 1968")
        self.assertTrue(first["created"])
        self.assertEqual(first["job"]["status"], "running")
        again = self.start("search", query="night of the living dead 1968")
        self.assertFalse(again["created"])
        self.assertIn("déjà en cours", again["message"])
        self.assertEqual(again["job"]["id"], first["job"]["id"])
        other = self.start("search", query="Test Show S02E01")
        self.assertTrue(other["created"])
        status, jobs, _ = self.req("/api/jobs")
        self.assertEqual(status, 200)
        self.assertEqual([j["id"] for j in jobs][:2], [other["job"]["id"], first["job"]["id"]])
        self.assertNotIn("output", jobs[0])
        release.set()
        self.assertEqual(self.wait(first["job"]["id"])["status"], "done")
        self.assertEqual(self.wait(other["job"]["id"])["status"], "done")

        self.assertEqual(self.req("/api/jobs/9999")[0], 404)
        self.assertEqual(self.req("/api/jobs", "POST", {"command": "rm -rf"})[0], 400)
        status, data, _ = self.req("/api/jobs", "POST", {"command": "search", "query": "  "})
        self.assertEqual(status, 400)
        self.assertIn("Saisissez", data["error"])

    def test_registry_keeps_the_last_20_jobs(self) -> None:
        for _ in range(25):
            self.run_cmd("test_indexers")
        jobs = self.req("/api/jobs")[1]
        self.assertEqual(len(jobs), 20)
        self.assertEqual(self.req("/api/jobs/1")[0], 404)

    def test_check(self) -> None:
        self.add_indexer()
        job = self.run_cmd("check")
        self.assertEqual(job["status"], "done", job)
        levels = {o["level"] for o in job["output"]}
        texts = "\n".join(o["text"] for o in job["output"])
        self.assertNotIn("err", levels, texts)
        self.assertTrue(job["result"]["ok"])
        self.assertIn("Download Station", texts)
        self.assertIn("Watchlist Plex : 2 élément(s)", texts)
        self.assertIn("Indexer Fake", texts)
        self.assertIn("warn", levels)  # TMDb : pas de clé
        self.assertIn("Rechercher tout", texts)  # conseils adaptés à l'interface web
        self.assertNotIn("sudo", texts)

    def test_check_reports_problems(self) -> None:
        self.app.ds.password = "faux"
        self.app.cfg.downloadstation.password = "faux"
        job = self.run_cmd("check")
        self.assertFalse(job["result"]["ok"])
        errs = [o["text"] for o in job["output"] if o["level"] == "err"]
        self.assertTrue(any("Download Station" in e for e in errs), errs)
        self.assertTrue(any("Aucun indexer" in e and "onglet Indexers" in e for e in errs), errs)

    def test_sync_search_all_and_process(self) -> None:
        self.add_indexer()
        job = self.run_cmd("sync")
        self.assertEqual(job["result"], {"count": 2})
        self.assertEqual(len(self.app.db.list_media()), 2)
        self.assertEqual(self.world.uploads, [])
        job = self.run_cmd("search_all", force=True)
        self.assertEqual(job["status"], "done", job)
        self.assertEqual(job["label"], "Rechercher tout (forcé)")
        self.assertEqual(job["result"]["grabbed"], 3)
        self.assertEqual(len(self.world.uploads), 3)
        self.assertIsNotNone(self.app.status()["last_watchlist_run"])
        job = self.run_cmd("process")
        self.assertEqual(job["result"]["dry_run"], False)
        self.assertIsNotNone(self.app.status()["last_process_run"])

    def test_sync_without_plex(self) -> None:
        self.app.plex = None
        job = self.run_cmd("sync")
        self.assertEqual(job["status"], "error")
        self.assertIn("Connexion Plex", job["error"])

    def test_dry_run_plan_then_process(self) -> None:
        src = os.path.join(self.dl, MOVIE, MOVIE + ".mkv")
        write(src)
        job = self.run_cmd("dry_run")
        self.assertEqual(job["status"], "done", job)
        self.assertTrue(job["result"]["dry_run"])
        planned = [a for a in job["result"]["actions"] if a["status"] == "planned"]
        self.assertEqual(len(planned), 1, job["result"])
        self.assertEqual(planned[0]["src"], src)
        self.assertTrue(planned[0]["dst"].startswith(self.movies), planned[0])
        self.assertIn("Night of the Living Dead", planned[0]["dst"])
        self.assertTrue(os.path.isfile(src))
        self.assertFalse(os.path.exists(planned[0]["dst"]))
        job = self.run_cmd("process")
        self.assertTrue(os.path.isfile(planned[0]["dst"]), job)
        self.assertIn("imported", [a["status"] for a in job["result"]["actions"]])

    def test_manual_search_and_grab(self) -> None:
        self.add_indexer()
        self.run_cmd("sync")
        job = self.run_cmd("search", query="Night of the Living Dead 1968")
        self.assertEqual(job["status"], "done", job)
        res = job["result"]
        self.assertEqual(res["kind"], "movie")
        by_title = {r["title"]: r for r in res["results"]}
        self.assertIsNotNone(by_title[MOVIE]["score"])
        self.assertIsNone(by_title["Night.of.the.Living.Dead.1968.CAM.x264-BAD"]["score"])
        self.assertNotIn("download_url", json.dumps(res))  # liens privés (passkey) gardés côté serveur
        jid = job["id"]
        status, data, _ = self.req("/api/jobs/%d/grab" % jid, "POST", {"index": by_title[MOVIE]["index"]})
        self.assertEqual(status, 200, data)
        self.assertTrue(data["ok"], data)
        self.assertIn("Night of the Living Dead", data["message"])
        self.assertEqual(len(self.world.uploads), 1)
        movie = self.app.db.get_media_by_key("m1")
        self.assertEqual(movie["status"], "grabbed")
        self.assertEqual(self.app.db.list_grabs()[0]["media_id"], movie["id"])
        status, data, _ = self.req("/api/jobs/%d/grab" % jid, "POST", {"index": by_title[MOVIE]["index"]})
        self.assertEqual((status, data["ok"]), (200, False))  # déjà envoyé
        self.assertEqual(len(self.world.uploads), 1)
        self.assertEqual(self.req("/api/jobs/%d/grab" % jid, "POST", {"index": 999})[0], 400)
        self.assertEqual(self.req("/api/jobs/%d/grab" % jid, "POST", {})[0], 400)
        other = self.run_cmd("test_indexers")
        self.assertEqual(self.req("/api/jobs/%d/grab" % other["id"], "POST", {"index": 0})[0], 400)
        self.assertEqual(self.req("/api/jobs/9999/grab", "POST", {"index": 0})[0], 404)

    def test_manual_grab_episode_marks_it(self) -> None:
        self.add_indexer()
        self.run_cmd("sync")
        job = self.run_cmd("search", query="Test Show S02E01")
        idx = [r for r in job["result"]["results"] if r["title"].startswith("Test.Show.S02E01.1080p")][0]["index"]
        status, data, _ = self.req("/api/jobs/%d/grab" % job["id"], "POST", {"index": idx})
        self.assertTrue(data["ok"], data)
        show = self.app.db.get_media_by_key("s1")
        self.assertEqual(self.app.db.get_episode(show["id"], 2, 1)["status"], "grabbed")
        self.assertEqual(self.app.db.get_episode(show["id"], 1, 1)["status"], "wanted")

    def test_manual_grab_in_dry_run(self) -> None:
        self.add_indexer()
        self.app.cfg.general.dry_run = True
        job = self.run_cmd("search", query="Night of the Living Dead 1968")
        status, data, _ = self.req("/api/jobs/%d/grab" % job["id"], "POST", {"index": 0})
        self.assertEqual((status, data["ok"]), (200, False))
        self.assertIn("simulation", data["message"])
        self.assertEqual(self.world.uploads, [])

    def test_plex_pin_flow(self) -> None:
        status, data, _ = self.req("/api/plex/login", "POST")
        self.assertEqual(status, 200, data)
        self.assertEqual((data["code"], data["url"]), ("AB12", "https://plex.tv/link"))
        again = self.req("/api/plex/login", "POST")[1]  # déjà en cours : même code
        self.assertEqual(again["job"]["id"], data["job"]["id"])
        self.assertFalse(again["created"])
        job = self.wait(data["job"]["id"])
        self.assertEqual(job["status"], "done", job)
        self.assertTrue(job["result"]["linked"])
        self.assertEqual(self.app.db.get_kv("plex_token"), "NEWTOKEN")
        self.assertEqual(self.app.plex.token, "NEWTOKEN")
        self.assertIs(self.app.searcher.plex, self.app.plex)
        texts = [o["text"] for o in job["output"]]
        self.assertTrue(any("prioritaire" in t for t in texts), texts)  # [plex] token renseigné
        self.assertTrue(any(t.startswith("Jeton Plex enregistré") for t in texts), texts)
        with mock.patch("synoarr.plex.PlexClient.start_pin_login", side_effect=HttpError("timeout", None)):
            status, data, _ = self.req("/api/plex/login", "POST")
        self.assertEqual(status, 502)
        self.assertIn("plex.tv injoignable", data["error"])

    def test_plex_pin_expired(self) -> None:
        with mock.patch("synoarr.plex.PlexClient.check_pin",
                        side_effect=HttpError("Code PIN Plex expiré ou inconnu : recommencez la connexion.", 404)):
            data = self.req("/api/plex/login", "POST")[1]
            job = self.wait(data["job"]["id"])
        self.assertEqual(job["status"], "error")
        self.assertIn("expiré", job["error"])

    def test_restart(self) -> None:
        called = threading.Event()
        calls = []

        def fake_execv(path, argv):
            calls.append((path, argv))
            called.set()
        with mock.patch("os.execv", side_effect=fake_execv), mock.patch("os._exit") as exit_:
            status, data, _ = self.req("/api/restart", "POST")
            self.assertEqual(status, 200)
            self.assertIn("Redémarrage", data["message"])
            self.assertTrue(called.wait(10))
        self.assertEqual(calls[0][0], sys.executable)
        self.assertEqual(calls[0][1][0], sys.executable)
        exit_.assert_not_called()
        time.sleep(0.1)
        with self.assertRaises(Exception):  # interface arrêtée avant execv
            urllib.request.urlopen(self.base + "/api/status", timeout=2)


class WebLogsTest(RealAppWebBase):
    def setUp(self) -> None:
        RealAppWebBase.setUp(self)
        self.log = os.path.join(self.tmp, "data", "synoarr.log")
        with open(self.log, "w", encoding="utf-8") as fh:
            fh.write("2026-11-13 10:00:00 DEBUG   synoarr.a: détail\n"
                     "2026-11-13 10:00:01 INFO    synoarr.b: Watchlist Plex : 2 élément(s)\n"
                     "2026-11-13 10:00:02 WARNING synoarr.c: Indexer lent\n"
                     "2026-11-13 10:00:03 ERROR   synoarr.d: Échec de l'envoi\n"
                     "Traceback (most recent call last):\n  File \"x.py\", line 1\nValueError: boum\n"
                     "2026-11-13 10:00:04 INFO    synoarr.b: Recherche terminée\n")
        p = mock.patch.object(self.app, "log_file", return_value=self.log)
        p.start()
        self.addCleanup(p.stop)

    def test_tail_and_filters(self) -> None:
        status, data, _ = self.req("/api/logs")
        self.assertEqual(status, 200)
        self.assertEqual(len(data["lines"]), 5)
        self.assertEqual(data["lines"][-1]["text"][-18:], "Recherche terminée")
        self.assertIn("ValueError: boum", data["lines"][3]["text"])  # traceback attaché à son entrée
        data = self.req("/api/logs?lines=2")[1]
        self.assertEqual([l["level"] for l in data["lines"]], ["ERROR", "INFO"])
        self.assertEqual(data["matched"], 5)
        data = self.req("/api/logs?level=WARNING")[1]
        self.assertEqual([l["level"] for l in data["lines"]], ["WARNING", "ERROR"])
        data = self.req("/api/logs?level=info&q=" + urllib.parse.quote("WATCHLIST"))[1]
        self.assertEqual(len(data["lines"]), 1)
        data = self.req("/api/logs?q=boum")[1]
        self.assertEqual([l["level"] for l in data["lines"]], ["ERROR"])
        self.assertEqual(self.req("/api/logs?lines=abc")[0], 400)
        self.assertEqual(self.req("/api/logs?level=FOO")[0], 400)

    def test_download(self) -> None:
        status, text, hdrs = self.req("/api/logs/download")
        self.assertEqual(status, 200)
        self.assertIn("text/plain", hdrs["Content-Type"])
        self.assertIn('attachment; filename="synoarr.log"', hdrs["Content-Disposition"])
        with open(self.log, encoding="utf-8") as fh:
            self.assertEqual(text, fh.read())
        self.assertEqual(self.req("/api/logs/download?n=1")[0], 404)
        self.assertEqual(self.req("/api/logs/download?n=../x")[0], 400)
        with open(self.log + ".1", "w", encoding="utf-8") as fh:
            fh.write("ancien\n")
        status, text, hdrs = self.req("/api/logs/download?n=1")
        self.assertEqual((status, text), (200, "ancien\n"))

    def test_missing_log(self) -> None:
        os.remove(self.log)
        data = self.req("/api/logs")[1]
        self.assertFalse(data["exists"])
        self.assertEqual(data["lines"], [])


class WebConfigTest(RealAppWebBase):
    def read(self) -> str:
        with open(self.cfg_path, encoding="utf-8") as fh:
            return fh.read()

    def test_get(self) -> None:
        status, data, _ = self.req("/api/config")
        self.assertEqual(status, 200)
        self.assertEqual(data["text"], self.cfg_text)
        self.assertEqual(data["path"], self.cfg_path)
        self.assertEqual(data["encoding"], "utf-8")
        self.assertFalse(data["auth_enabled"])
        self.assertEqual(data["backups"], [])
        self.assertEqual(data["restart_reasons"], [])

    def test_invalid_config_is_refused(self) -> None:
        bad = self.cfg_text.replace("min_age_minutes = 0", "min_age_minutes = 0\naction = téléporter")
        status, data, _ = self.req("/api/config", "POST", {"text": bad})
        self.assertEqual(status, 400)
        self.assertIn("n'a pas été modifié", data["error"])
        self.assertTrue(any("[paths] action" in p for p in data["problems"]), data)
        status, data, _ = self.req("/api/config", "POST", {"text": "[paths\nfoo"})
        self.assertEqual(status, 400)
        self.assertIn("syntaxe", data["problems"][0])
        self.assertNotIn(".tmp", data["problems"][0])
        status, data, _ = self.req("/api/config", "POST",
                                   {"text": self.cfg_text.replace("min_age_minutes = 0", "min_age_minutes = vite")})
        self.assertEqual(status, 400)
        self.assertEqual(self.req("/api/config", "POST", {"text": "  "})[0], 400)
        self.assertEqual(self.req("/api/config", "POST", {})[0], 400)
        self.assertEqual(self.read(), self.cfg_text)
        self.assertEqual(sorted(os.listdir(self.tmp)), ["config.ini", "data", "volume1"])  # ni .bak ni .tmp
        self.assertEqual(self.app.cfg.paths.min_age_minutes, 0)

    def test_save_applies_and_keeps_backups(self) -> None:
        old_ds, old_searcher, old_org = self.app.ds, self.app.searcher, self.app.organizer
        new = self.cfg_text.replace("min_age_minutes = 0", "min_age_minutes = 7\nfaute_de_frappe = 1")
        status, data, _ = self.req("/api/config", "POST", {"text": new})
        self.assertEqual(status, 200, data)
        self.assertFalse(data["restart_required"])
        self.assertTrue(any("faute_de_frappe" in w for w in data["warnings"]), data["warnings"])
        self.assertEqual(self.read(), new)
        self.assertEqual(stat.S_IMODE(os.stat(self.cfg_path).st_mode), 0o640)
        with open(self.cfg_path + ".bak1", encoding="utf-8") as fh:
            self.assertEqual(fh.read(), self.cfg_text)
        # reload: new values visible everywhere, database and web server kept
        self.assertEqual(self.app.cfg.paths.min_age_minutes, 7)
        self.assertIs(self.app.organizer.cfg, self.app.cfg)
        self.assertIs(self.app.searcher.cfg, self.app.cfg)
        self.assertIsNot(self.app.ds, old_ds)
        self.assertIsNot(self.app.searcher, old_searcher)
        self.assertIsNot(self.app.organizer, old_org)
        self.assertIs(self.app.searcher.stop_event, self.app._stop)
        self.assertEqual(self.app.cfg.path, self.cfg_path)
        self.assertEqual(self.req("/api/status")[0], 200)
        # web settings: restart required
        status, data, _ = self.req("/api/config", "POST", {"text": new.replace("port = 8778", "port = 8779")})
        self.assertTrue(data["restart_required"])
        self.assertEqual(data["restart_reasons"], ["[web] port"])
        self.assertIn("redémarrage nécessaire", data["message"])
        self.assertEqual(self.req("/api/status")[1]["restart_required"], ["[web] port"])
        self.assertEqual(len(data["backups"]), 2)
        # restore = previous version (port 8778), the replaced one becomes .bak1
        status, data, _ = self.req("/api/config/restore", "POST")
        self.assertEqual(status, 200, data)
        self.assertIn("restaurée", data["message"])
        self.assertFalse(data["restart_required"])
        self.assertEqual(self.read(), new)
        self.assertEqual(self.app.cfg.web.port, 8778)
        with open(self.cfg_path + ".bak1", encoding="utf-8") as fh:
            self.assertIn("port = 8779", fh.read())
        # at most 5 backups
        for i in range(8):
            self.assertEqual(self.req("/api/config", "POST",
                                      {"text": new.replace("min_age_minutes = 7", "min_age_minutes = %d" % i)})[0], 200)
        names = sorted(f for f in os.listdir(self.tmp) if f.startswith("config.ini"))
        self.assertEqual(names, ["config.ini"] + ["config.ini.bak%d" % i for i in range(1, 6)])
        self.assertEqual(self.app.cfg.paths.min_age_minutes, 7)

    def test_restore_without_backup(self) -> None:
        status, data, _ = self.req("/api/config/restore", "POST")
        self.assertEqual(status, 400)
        self.assertIn("Aucune sauvegarde", data["error"])

    def test_busy_gives_409(self) -> None:
        self.block_search()
        self.start("search", query="x")
        new = self.cfg_text.replace("min_age_minutes = 0", "min_age_minutes = 3")
        status, data, _ = self.req("/api/config", "POST", {"text": new})
        self.assertEqual(status, 409)
        self.assertIn("tâche est en cours", data["error"])
        self.assertEqual(self.read(), self.cfg_text)
        self.assertEqual(sorted(os.listdir(self.tmp)), ["config.ini", "data", "volume1"])

    def test_windows_encoding_is_saved_as_utf8(self) -> None:
        text = self.cfg_text.replace("(commentaire conservé)", "(Vidéos é)")
        with open(self.cfg_path, "wb") as fh:
            fh.write(text.replace("\n", "\r\n").encode("cp1252"))
        data = self.req("/api/config")[1]
        self.assertEqual((data["encoding"], data["text"]), ("cp1252", text))
        self.assertEqual(self.req("/api/config", "POST", {"text": data["text"]})[0], 200)
        with open(self.cfg_path, "rb") as fh:
            self.assertEqual(fh.read().decode("utf-8"), text)

    def test_no_config_file(self) -> None:
        self.app.cfg.path = None
        status, data, _ = self.req("/api/config")
        self.assertEqual(status, 400)
        self.assertIn("config.ini", data["error"])


class WebConfigAuthTest(RealAppWebBase):
    username = "admin"
    password = "pw"

    def test_config_behind_auth(self) -> None:
        self.assertEqual(self.req("/api/config", auth=("", ""))[0], 401)
        self.assertEqual(self.req("/api/logs", auth=("", ""))[0], 401)
        self.assertEqual(self.req("/api/restart", "POST", auth=("", ""))[0], 401)
        status, data, _ = self.req("/api/config")
        self.assertTrue(data["auth_enabled"])
        status, _, _ = self.req("/api/config", "POST", raw=json.dumps({"text": self.cfg_text}).encode(),
                                headers={"Origin": "http://evil.example", "Content-Type": "text/plain"})
        self.assertEqual(status, 403)


if __name__ == "__main__":
    unittest.main()


class HostCheckTests(unittest.TestCase):
    def test_host_is_local(self) -> None:
        from synoarr.web import host_is_local
        for h in ("192.168.1.10:8778", "[::1]:8778", "localhost:8778", "diskstation:8778", "nas.local", ""):
            self.assertTrue(host_is_local(h), h)
        for h in ("evil.example.com", "attacker.com:8778", "rebind.evil.net"):
            self.assertFalse(host_is_local(h), h)


# ------------------------------------------------- progression, changer de source, priorité, Prowlarr
from synoarr.bencode import bencode as _bencode  # noqa: E402


def _torrent(name: str) -> bytes:
    info = {b"name": name.encode("utf-8"), b"piece length": 16384, b"pieces": b"\0" * 20, b"length": 4321}
    return _bencode({b"announce": b"http://tracker.invalid/announce", b"info": info})


class WebDownloadsTest(RealAppWebBase):
    def grab_all(self) -> None:
        self.add_indexer()
        self.run_cmd("sync")
        job = self.run_cmd("search_all", force=True)
        self.assertEqual(job["result"]["grabbed"], 3, job)

    def media(self, key: str) -> Dict[str, Any]:
        mid = self.app.db.get_media_by_key(key)["id"]
        return [m for m in self.req("/api/media")[1] if m["id"] == mid][0]

    def list_calls(self) -> int:
        return sum(1 for r in self.srv.requests if r.params().get("method") == "list")

    def test_progress_in_media_and_dashboard(self) -> None:
        self.grab_all()
        movie = self.media("m1")
        self.assertEqual(len(movie["downloads"]), 1)
        d = movie["downloads"][0]
        self.assertEqual(d["release_title"], MOVIE)
        self.assertEqual((d["status"], d["progress"], d["size"], d["downloaded"]), ("downloading", 25.0, 1000, 250))
        self.assertEqual((d["speed_down"], d["speed_up"], d["seeders"], d["leechers"], d["peers"]),
                         (50, 5, 7, 2, 31))
        self.assertEqual(d["eta_seconds"], 15)
        self.assertTrue(d["active"])
        self.assertTrue(d["task_id"])
        self.assertEqual(d["indexer"], "Fake")
        show = self.media("s1")
        self.assertEqual(sorted((x["season"], x["episode"]) for x in show["downloads"]), [(1, None), (2, 1)])
        status, dl, _ = self.req("/api/downloads")
        self.assertEqual(status, 200)
        self.assertEqual((dl["ok"], dl["warning"], dl["active"], len(dl["items"])), (True, None, 3, 3))
        # finished / seeding task, unknown task
        with self.world.lock:
            for t in self.world.tasks.values():
                if t["title"] == MOVIE:
                    t["status"] = "seeding"
                    t["additional"]["transfer"]["size_downloaded"] = 1000
        self.app.invalidate_downloads()
        d = self.media("m1")["downloads"][0]
        self.assertEqual((d["status"], d["progress"], d["active"], d["eta_seconds"]), ("seeding", 100.0, False, None))
        with self.world.lock:
            self.world.tasks.clear()
        self.app.invalidate_downloads()
        self.assertEqual(self.media("m1")["downloads"][0]["status"], "introuvable")

    def test_status_cache_spares_dsm(self) -> None:
        self.grab_all()
        self.app.invalidate_downloads()
        before = self.list_calls()
        for _ in range(4):
            self.req("/api/media")
            self.req("/api/downloads")
        self.assertEqual(self.list_calls() - before, 1)

    def test_ds_unreachable_gives_inconnu_and_one_warning(self) -> None:
        movie = self.app.db.upsert_media("m9", "movie", "Heat", 1995, None, None, None)
        self.app.db.add_grab(movie, None, None, "Heat.1995.1080p", "Heat.1995.1080p", "Fake", None, 10)

        def boom() -> List[Any]:
            raise HttpError("connexion refusée", None)
        self.app.ds.list_tasks = boom  # type: ignore[assignment]
        status, ms, _ = self.req("/api/media")
        self.assertEqual(status, 200, ms)
        d = [m for m in ms if m["id"] == movie][0]["downloads"][0]
        self.assertEqual(d["status"], "inconnu")
        self.assertIsNone(d["error"])
        status, dl, _ = self.req("/api/downloads")
        self.assertEqual(status, 200)
        self.assertFalse(dl["ok"])
        self.assertIn("injoignable", dl["warning"])
        self.app.ds = None
        self.app.invalidate_downloads()
        self.assertIn("pas configuré", self.req("/api/downloads")[1]["warning"])

    def test_failed_grab_still_wanted_is_listed(self) -> None:
        movie = self.app.db.upsert_media("m9", "movie", "Heat", 1995, None, None, None)
        gid = self.app.db.add_grab(movie, None, None, "Heat.1995.CAM", "Heat.1995.CAM", "Fake", None, 10)
        self.app.db.set_grab_status(gid, "failed", "broken_link")
        d = [m for m in self.req("/api/media")[1] if m["id"] == movie][0]["downloads"]
        self.assertEqual([(x["status"], x["error"], x["grab_id"]) for x in d], [("failed", "broken_link", gid)])
        self.app.db.set_media_status(movie, "done")
        self.assertEqual([m for m in self.req("/api/media")[1] if m["id"] == movie][0]["downloads"], [])

    def sources(self, media_id: int, **body: Any) -> Dict[str, Any]:
        status, data, _ = self.req("/api/media/%d/sources" % media_id, "POST", body)
        self.assertEqual(status, 200, data)
        return self.wait(data["job"]["id"])

    def test_change_source_from_search_results(self) -> None:
        self.grab_all()
        movie = self.app.db.get_media_by_key("m1")
        old = [g for g in self.app.db.list_grabs() if g["media_id"] == movie["id"]][0]
        old_task = [t["id"] for t in self.world.tasks.values() if t["title"] == MOVIE][0]
        job = self.sources(movie["id"])
        self.assertEqual(job["status"], "done", job)
        res = job["result"]
        self.assertEqual((res["kind"], res["languages"]), ("movie", ["en"]))
        by_title = {r["title"]: r for r in res["results"]}
        self.assertIsNotNone(by_title[MOVIE]["score"])
        self.assertEqual(by_title[MOVIE]["priority"], 25)
        ita = by_title["Night.of.the.Living.Dead.1968.iTALiAN.1080p.BluRay-ITA"]
        self.assertIsNone(ita["score"])
        self.assertNotIn("download_url", json.dumps(res))
        url = "/api/media/%d/replace" % movie["id"]
        body = {"grab_id": old["id"], "job_id": job["id"], "index": ita["index"], "blocklist_old": True}
        status, data, _ = self.req(url, "POST", body)
        self.assertEqual(status, 400, data)
        self.assertIn("rejetée", data["error"])
        show = self.app.db.get_media_by_key("s1")
        status, data, _ = self.req("/api/media/%d/replace" % show["id"], "POST",
                                   {"season": 1, "job_id": job["id"], "index": 0})
        self.assertEqual(status, 400)
        self.assertIn("autre élément", data["error"])
        body["accept_rejected"] = True
        status, data, _ = self.req(url, "POST", body)
        self.assertEqual(status, 200, data)
        self.assertTrue(data["ok"], data)
        self.assertIn("ancienne tâche supprimée", data["message"])
        self.assertIn(old_task, self.world.deleted)
        grabs = {g["id"]: g for g in self.app.db.list_grabs()}
        self.assertEqual((grabs[old["id"]]["status"], grabs[old["id"]]["message"]), ("failed", "remplacé manuellement"))
        new = grabs[data["grab_id"]]
        self.assertEqual((new["status"], new["media_id"], new["release_title"]), ("sent", movie["id"], ita["title"]))
        self.assertTrue(self.app.db.is_blocked(MOVIE))
        self.assertEqual(self.app.db.get_media(movie["id"])["status"], "grabbed")
        dls = self.media("m1")["downloads"]
        self.assertEqual([(x["grab_id"], x["status"]) for x in dls], [(new["id"], "downloading")])

    def test_change_source_with_magnet_and_torrent_file(self) -> None:
        self.grab_all()
        show = self.app.db.get_media_by_key("s1")
        grabs = {(g["season"], g["episode"]): g for g in self.app.db.list_grabs() if g["media_id"] == show["id"]}
        url = "/api/media/%d/replace" % show["id"]
        magnet = "magnet:?xt=urn:btih:%s&dn=Test.Show.S02E01.MAGNET" % ("ab" * 20)
        status, data, _ = self.req(url, "POST", {"grab_id": grabs[(2, 1)]["id"], "link": magnet, "delete_old": False})
        self.assertEqual(status, 200, data)
        self.assertTrue(data["ok"], data)
        self.assertNotIn("supprimée", data["message"])
        new = self.app.db.query_one("SELECT * FROM grabs WHERE id = ?", (data["grab_id"],))
        self.assertEqual((new["season"], new["episode"], new["indexer"], new["release_title"]),
                         (2, 1, "manuel", "Test.Show.S02E01.MAGNET"))
        d = [x for x in self.media("s1")["downloads"] if x["grab_id"] == new["id"]][0]
        self.assertEqual(d["status"], "downloading")  # task found (title = dn / info hash)
        # .torrent upload for the season pack
        b64 = base64.b64encode(_torrent("Test.Show.S01.720p.UPLOAD")).decode()
        status, data, _ = self.req(url, "POST", {"grab_id": grabs[(1, None)]["id"], "torrent_base64": b64})
        self.assertEqual(status, 200, data)
        self.assertTrue(data["ok"], data)
        self.assertIn("Test.Show.S01.720p.UPLOAD", [n for n, _d in self.world.uploads][-1])
        new = self.app.db.query_one("SELECT * FROM grabs WHERE id = ?", (data["grab_id"],))
        self.assertEqual((new["season"], new["episode"], new["size"]), (1, None, 4321))
        # wanted episode without grab: data URL accepted too
        status, data, _ = self.req(url, "POST", {"season": 1, "episode": 3, "torrent_base64":
                                                 "data:application/x-bittorrent;base64," +
                                                 base64.b64encode(_torrent("Test.Show.S01E03.X")).decode()})
        self.assertTrue(data["ok"], data)
        # validation errors (French, 400)
        for body, needle in (({"season": 1, "link": "magnet:?dn=x"}, "magnet"),
                             ({"season": 1, "link": "ftp://x/y.torrent"}, "Lien non reconnu"),
                             ({"season": 1, "torrent_base64": "%%%"}, "base64"),
                             ({"season": 1, "torrent_base64": base64.b64encode(b"<html>").decode()}, "invalide"),
                             ({"season": 1, "link": magnet, "torrent_base64": b64}, "une seule source"),
                             ({"link": magnet}, "saison"),
                             ({"season": 1}, "une seule source"),
                             ({"grab_id": 99999, "link": magnet}, "introuvable")):
            with self.subTest(body=body):
                status, data, _ = self.req(url, "POST", body)
                self.assertEqual(status, 400, data)
                self.assertIn(needle, data["error"])

    def test_failed_send_keeps_the_old_download(self) -> None:
        self.grab_all()
        movie = self.app.db.get_media_by_key("m1")
        old = [g for g in self.app.db.list_grabs() if g["media_id"] == movie["id"]][0]
        n_tasks = len(self.world.tasks)
        status, data, _ = self.req("/api/media/%d/replace" % movie["id"], "POST",
                                   {"grab_id": old["id"], "link": self.srv.url + "/absent/file.torrent"})
        self.assertEqual(status, 200, data)
        self.assertFalse(data["ok"])
        self.assertIn("Échec", data["message"])
        self.assertEqual(self.app.db.query_one("SELECT status FROM grabs WHERE id = ?", (old["id"],))["status"], "sent")
        self.assertEqual((len(self.world.tasks), self.world.deleted), (n_tasks, []))

    def test_dry_run_changes_nothing(self) -> None:
        self.grab_all()
        self.app.cfg.general.dry_run = True
        movie = self.app.db.get_media_by_key("m1")
        old = [g for g in self.app.db.list_grabs() if g["media_id"] == movie["id"]][0]
        status, data, _ = self.req("/api/media/%d/replace" % movie["id"], "POST",
                                   {"grab_id": old["id"], "link": "magnet:?xt=urn:btih:" + "c" * 40})
        self.assertEqual((status, data["ok"]), (200, False))
        self.assertIn("simulation", data["message"])
        self.assertEqual(self.app.db.query_one("SELECT status FROM grabs WHERE id = ?", (old["id"],))["status"], "sent")
        self.assertEqual(self.world.deleted, [])

    def test_sources_validation(self) -> None:
        self.run_cmd("sync")
        show = self.app.db.get_media_by_key("s1")
        status, data, _ = self.req("/api/media/%d/sources" % show["id"], "POST", {})
        self.assertEqual(status, 400)
        self.assertIn("saison", data["error"])
        self.assertEqual(self.req("/api/media/%d/sources" % show["id"], "POST", {"season": "x"})[0], 400)
        self.assertEqual(self.req("/api/media/9999/sources", "POST", {})[0], 404)
        job = self.sources(show["id"], season=2, episode=1)
        self.assertEqual(job["result"]["kind"], "episode")
        self.assertIn("S02E01", job["label"])

    def test_page_has_new_ui(self) -> None:
        status, html, _ = self.req("/")
        for needle in ("Changer de source", "Téléchargements en cours", "Priorité", "Importer depuis Prowlarr",
                       "torrent_base64", 'id="srcm"'):
            self.assertIn(needle, html)


class WebIndexerPriorityTest(RealAppWebBase):
    def cli(self, *argv: str) -> Tuple[int, str]:
        import io
        from contextlib import redirect_stdout
        from synoarr import cli
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = cli.main(list(argv) + ["--config", self.cfg_path])
        return rc, buf.getvalue()

    def test_priority_endpoint_and_order(self) -> None:
        self.add_indexer()
        status, row, _ = self.req("/api/indexers", "POST", {"name": "Second", "url": self.srv.url + "/torznab",
                                                            "priority": "40"})
        self.assertEqual(status, 200, row)
        self.assertEqual(row["priority"], 40)
        fake = self.app.db.get_indexer("Fake")
        self.assertEqual(fake["priority"], 25)
        status, data, _ = self.req("/api/indexers/%d/priority" % fake["id"], "POST", {"priority": 50})
        self.assertEqual(status, 200, data)
        self.assertEqual([i["name"] for i in self.req("/api/indexers")[1]], ["Second", "Fake"])
        self.assertEqual([i.name for i in self.app.indexer_manager.active()], ["Second", "Fake"])
        for bad in (0, 51, "3", True, None, 2.5):
            with self.subTest(bad=bad):
                status, data, _ = self.req("/api/indexers/%d/priority" % fake["id"], "POST", {"priority": bad})
                self.assertEqual(status, 400, data)
                self.assertIn("Priorité invalide", data["error"])
        self.assertEqual(self.req("/api/indexers/999/priority", "POST", {"priority": 3})[0], 400)
        status, data, _ = self.req("/api/indexers", "POST", {"name": "X", "url": self.srv.url + "/torznab",
                                                             "priority": "99"})
        self.assertEqual(status, 400)
        self.assertIn("Priorité invalide", data["error"])

    def test_manual_search_shows_priority(self) -> None:
        self.add_indexer()
        self.app.indexer_manager.set_priority("Fake", 3)
        job = self.run_cmd("search", query="Night of the Living Dead 1968")
        self.assertEqual({r["priority"] for r in job["result"]["results"]}, {3})

    def test_cli_priority(self) -> None:
        self.add_indexer()
        rc, out = self.cli("indexer", "priority", "Fake", "7")
        self.assertEqual(rc, 0, out)
        self.assertIn("Priorité de l'indexer Fake : 7", out)
        self.assertEqual(self.app.db.get_indexer("Fake")["priority"], 7)
        rc, out = self.cli("indexer", "list")
        self.assertIn("prio", out)
        self.assertRegex(out, r"Fake\s+torznab\s+actif\s+7 ")
        rc, out = self.cli("indexer", "priority", "Fake", "0")
        self.assertEqual(rc, 1)
        self.assertIn("Priorité invalide", out)
        rc, out = self.cli("indexer", "priority", "Fake")
        self.assertEqual(rc, 2)
        rc, out = self.cli("indexer", "add-url", "Autre", self.srv.url + "/torznab", "--type", "torznab",
                           "--priority", "12")
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.app.db.get_indexer("Autre")["priority"], 12)


class FakeProwlarr:
    KEY = "PKEY123"

    def __init__(self) -> None:
        self.indexers = [
            {"id": 1, "name": "YTS", "protocol": "torrent", "enable": True, "priority": 10},
            {"id": 2, "name": "NZBgeek", "protocol": "usenet", "enable": True, "priority": 25},
            {"id": 3, "name": "Off", "protocol": "torrent", "enable": False, "priority": 25},
            {"id": 4, "name": "1337x", "protocol": "torrent", "enable": True, "priority": 99},
        ]

    def __call__(self, req: Any) -> Tuple[int, Dict[str, str], Any]:
        key = req.headers.get("x-api-key") or req.query.get("apikey")
        if req.path == "/api/v1/indexer":
            if key != self.KEY:
                return 401, {}, {"error": "Unauthorized"}
            return 200, {}, self.indexers
        if req.path.endswith("/api") and req.query.get("t") == "caps":
            if key != self.KEY:
                return 401, {}, b"bad key"
            return 200, {"Content-Type": "application/xml"}, (
                '<?xml version="1.0"?><caps><server title="Prowlarr"/><searching><search available="yes"/>'
                '</searching></caps>')
        return 404, {}, b"not found"


class WebProwlarrImportTest(RealAppWebBase):
    def setUp(self) -> None:
        super().setUp()
        self.prowlarr = FakeProwlarr()
        self.psrv = FakeServer(self.prowlarr).__enter__()
        self.addCleanup(self.psrv.__exit__, None, None, None)

    def test_import_update_and_errors(self) -> None:
        base = self.psrv.url
        hostport = base.split("://", 1)[1]
        status, data, _ = self.req("/api/indexers/import-prowlarr", "POST", {"url": hostport + "/", "api_key": FakeProwlarr.KEY})
        self.assertEqual(status, 200, data)
        self.assertEqual(sorted(data["added"]), ["1337x (Prowlarr)", "YTS (Prowlarr)"])
        self.assertEqual(data["updated"], [])
        self.assertEqual(len(data["skipped"]), 2)
        self.assertTrue(any("Usenet" in s for s in data["skipped"]))
        self.assertIn("2 ajouté(s)", data["message"])
        yts = self.app.db.get_indexer("YTS (Prowlarr)")
        self.assertEqual((yts["type"], yts["url"], yts["api_key"], yts["priority"]),
                         ("torznab", base + "/1/api", FakeProwlarr.KEY, 10))
        self.assertEqual(self.app.db.get_indexer("1337x (Prowlarr)")["priority"], 50)  # clamped
        ok, msg = self.app.indexer_manager.test(yts["id"])
        self.assertTrue(ok, msg)
        self.assertNotIn(FakeProwlarr.KEY, json.dumps(self.req("/api/indexers")[1]))
        # second import: update (priority changed in Prowlarr), key given in the URL
        self.prowlarr.indexers[0]["priority"] = 2
        status, data, _ = self.req("/api/indexers/import-prowlarr", "POST",
                                   {"url": base + "/api/v1/indexer?apikey=" + FakeProwlarr.KEY, "api_key": ""})
        self.assertEqual(status, 200, data)
        self.assertEqual((data["added"], sorted(data["updated"])), ([], ["1337x (Prowlarr)", "YTS (Prowlarr)"]))
        self.assertEqual(self.app.db.get_indexer("YTS (Prowlarr)")["priority"], 2)
        self.assertEqual(len(self.app.db.list_indexers()), 2)
        # errors
        status, data, _ = self.req("/api/indexers/import-prowlarr", "POST", {"url": base, "api_key": "faux"})
        self.assertEqual(status, 400)
        self.assertIn("Clé API Prowlarr refusée", data["error"])
        status, data, _ = self.req("/api/indexers/import-prowlarr", "POST", {"url": base, "api_key": ""})
        self.assertEqual(status, 400)
        self.assertIn("clé API", data["error"])
        status, data, _ = self.req("/api/indexers/import-prowlarr", "POST", {"url": self.srv.url, "api_key": "k"})
        self.assertEqual(status, 400)  # not a Prowlarr (404)
        self.assertIn("127.0.0.1:9696", data["error"])
        import socket
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        dead = "http://127.0.0.1:%d" % s.getsockname()[1]
        s.close()
        status, data, _ = self.req("/api/indexers/import-prowlarr", "POST", {"url": dead, "api_key": "k"})
        self.assertEqual(status, 400)
        self.assertIn("injoignable", data["error"])
        self.assertIn("http://127.0.0.1:9696", data["error"])
        self.assertEqual(self.req("/api/indexers/import-prowlarr", "POST", {"api_key": "k"})[0], 400)

    def test_cli_import(self) -> None:
        rc, out = WebIndexerPriorityTest.cli(self, "indexer", "import-prowlarr", self.psrv.url, "--api-key",  # type: ignore[arg-type]
                                             FakeProwlarr.KEY)
        self.assertEqual(rc, 0, out)
        self.assertIn("2 ajouté(s)", out)
        self.assertIn("YTS (Prowlarr)", out)
        self.assertNotIn(FakeProwlarr.KEY, out)
        rc, out = WebIndexerPriorityTest.cli(self, "indexer", "import-prowlarr", self.psrv.url, "--api-key", "x")  # type: ignore[arg-type]
        self.assertEqual(rc, 1)
        self.assertIn("refusée", out)
