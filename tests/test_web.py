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

from synoarr.config import Config
from synoarr.db import Database
from synoarr.web import MASK, WebServer, mask_secrets

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
        for word in ("Tableau de bord", "Watchlist", "Indexers", "Historique", "Synchroniser la Watchlist"):
            self.assertIn(word, html)
        self.assertNotIn("http://", html.split("<script>")[0].replace('href="data:,"', ""))  # aucun CDN
        self.assertIn("script-src 'sha256-", hdrs["Content-Security-Policy"])
        self.assertLess(len(html.splitlines()), 400)

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


if __name__ == "__main__":
    unittest.main()
