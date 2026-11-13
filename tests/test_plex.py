from __future__ import annotations

import logging
import unittest

from synoarr.httpclient import HttpClient, HttpError
from synoarr.plex import PlexClient, extract_guids, parse_guid, refresh_library
from tests.fakes import FakeServer



def setUpModule():
    logging.disable(logging.CRITICAL)


def tearDownModule():
    logging.disable(logging.NOTSET)

TOKEN = "tok-secret"


def _mc(metadata, total=None, offset=0):
    mc = {"size": len(metadata), "offset": offset, "identifier": "tv.plex.provider.discover",
          "librarySectionID": "watchlist", "Metadata": metadata}
    if total is not None:
        mc["totalSize"] = total
    return {"MediaContainer": mc}


WATCHLIST = [
    {"ratingKey": "5d776b59ad5437001f79c6f8", "key": "/library/metadata/5d776b59ad5437001f79c6f8",
     "guid": "plex://movie/5d776b59ad5437001f79c6f8", "type": "movie", "title": "Amélie", "year": 2001,
     "originalTitle": "Le Fabuleux Destin d'Amélie Poulain",
     "Guid": [{"id": "imdb://tt0211915"}, {"id": "tmdb://194"}, {"id": "tvdb://233"}]},
    {"ratingKey": "5d9c086c46115600200aa2fe", "key": "/library/metadata/5d9c086c46115600200aa2fe/children",
     "guid": "plex://show/5d9c086c46115600200aa2fe", "type": "show", "title": "The Office", "year": "2005"},
    {"ratingKey": "aaa", "type": "collection", "title": "Some collection"},
    {"ratingKey": "5d77683ef59e580021a2ca0f", "guid": "plex://movie/5d77683ef59e580021a2ca0f", "type": "movie",
     "title": "Heat", "originallyAvailableAt": "1995-12-15", "Guid": [{"id": "imdb://tt0113277"}]},
]


def make_handler(state):
    def handler(req):
        if req.headers.get("x-plex-token") not in (TOKEN, None) and req.path.startswith("/library") \
                and not req.path.endswith("/refresh"):
            return 401, {}, b"Unauthorized"
        if req.path == "/library/sections/watchlist/all":
            if state.get("watchlist_404"):
                return 404, {}, b"not found"
            if not req.headers.get("x-plex-token"):
                return 401, {}, b"Unauthorized"
            start = int(req.query.get("X-Plex-Container-Start", 0))
            size = int(req.query.get("X-Plex-Container-Size", 50))
            size = min(size, state.get("page", 2))  # server caps page size
            page = WATCHLIST[start:start + size]
            return 200, {}, _mc(page, total=len(WATCHLIST), offset=start)
        if req.path == "/library/metadata/5d9c086c46115600200aa2fe":
            if state.get("metadata_404"):
                return 404, {}, b""
            return 200, {}, _mc([{"ratingKey": "5d9c086c46115600200aa2fe", "type": "show", "title": "The Office",
                                  "Guid": [{"id": "imdb://tt0386676"}, {"id": "tvdb://73244"},
                                           {"id": "tmdb://2316"}]}])
        if req.path == "/library/metadata/show1/children":
            return 200, {}, _mc([
                {"ratingKey": "s0", "type": "season", "index": 0, "title": "Specials"},
                {"ratingKey": "s1", "key": "/library/metadata/s1/children", "type": "season", "index": 1,
                 "title": "Season 1"},
                {"key": "/library/metadata/s2/children", "type": "season", "index": "2", "title": "Season 2"},
            ], total=3)
        if req.path == "/library/metadata/s0/children":
            raise AssertionError("specials must not be fetched")
        if req.path == "/library/metadata/s1/children":
            start = int(req.query.get("X-Plex-Container-Start", 0))
            eps = [{"ratingKey": "e%d" % i, "type": "episode", "index": i, "parentIndex": 1,
                    "title": "Episode %d" % i, "originallyAvailableAt": "2020-01-%02d" % i} for i in range(1, 4)]
            # paginate by 2 regardless of requested size
            return 200, {}, _mc(eps[start:start + 2], total=3, offset=start)
        if req.path == "/library/metadata/s2/children":
            # XML answer (Plex ignoring Accept), no parentIndex
            xml = ('<?xml version="1.0" encoding="UTF-8"?><MediaContainer size="1" totalSize="1">'
                   '<Video ratingKey="e21" type="episode" index="1" title="Rentrée" '
                   'originallyAvailableAt="2021-09-01"/></MediaContainer>')
            return 200, {"Content-Type": "application/xml"}, xml
        if req.path == "/api/v2/user":
            if req.headers.get("x-plex-token") != TOKEN:
                return 401, {}, {"errors": [{"code": 1001, "message": "User could not be authenticated"}]}
            return 200, {}, {"id": 1, "uuid": "abc", "username": "jean", "email": "jean@example.org"}
        if req.path == "/api/v2/pins" and req.method == "POST":
            state["pin_request"] = req
            return 201, {}, {"id": 4242, "code": "AB12", "product": "synoarr", "trusted": False,
                             "authToken": None, "expiresIn": 1800}
        if req.path == "/api/v2/pins/4242":
            state["pin_checks"] = state.get("pin_checks", 0) + 1
            token = "newtoken" if state["pin_checks"] >= 2 else None
            return 200, {}, {"id": 4242, "code": "AB12", "authToken": token}
        if req.path == "/api/v2/pins/999":
            return 404, {}, {"errors": [{"code": 1020, "message": "Code not found or expired"}]}
        if req.path == "/library/sections/all/refresh":
            state["refresh_token"] = req.headers.get("x-plex-token")
            return 200, {}, b""
        return 404, {}, b"not found"
    return handler


class GuidParsingTest(unittest.TestCase):
    def test_parse_guid(self):
        self.assertEqual(parse_guid("imdb://tt0111161"), ("imdb", "tt0111161"))
        self.assertEqual(parse_guid("tmdb://278"), ("tmdb", "278"))
        self.assertEqual(parse_guid("tvdb://81189"), ("tvdb", "81189"))
        self.assertEqual(parse_guid("com.plexapp.agents.imdb://tt0111161?lang=en"), ("imdb", "tt0111161"))
        self.assertEqual(parse_guid("com.plexapp.agents.themoviedb://278?lang=fr"), ("tmdb", "278"))
        self.assertEqual(parse_guid("com.plexapp.agents.thetvdb://81189/1/2?lang=en"), ("tvdb", "81189"))
        self.assertIsNone(parse_guid("plex://movie/5d776b59ad5437001f79c6f8"))
        self.assertIsNone(parse_guid("local://123"))
        self.assertIsNone(parse_guid(""))

    def test_extract_guids(self):
        self.assertEqual(extract_guids({"Guid": [{"id": "imdb://tt1"}, {"id": "tmdb://2"}]}),
                         {"imdb": "tt1", "tmdb": "2"})
        self.assertEqual(extract_guids({"guid": "com.plexapp.agents.imdb://tt9?lang=en"}), {"imdb": "tt9"})
        self.assertEqual(extract_guids({"Guid": {"id": "tvdb://5"}}), {"tvdb": "5"})


class PlexClientTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.state = {}
        cls.srv = FakeServer(make_handler(cls.state)).__enter__()

    @classmethod
    def tearDownClass(cls):
        cls.srv.__exit__(None, None, None)

    def setUp(self):
        self.state.clear()
        del self.srv.requests[:]
        self.http = HttpClient(timeout=5, retries=0)
        self.plex = self._client(TOKEN)

    def _client(self, token):
        p = PlexClient(token, "client-123", self.http)
        p.discover_url = self.srv.url
        p.metadata_url = self.srv.url + "/meta"  # distinct path to detect the fallback
        p.plextv_url = self.srv.url
        return p

    def test_watchlist_pagination_and_guids(self):
        items = self.plex.watchlist()
        self.assertEqual([i.title for i in items], ["Amélie", "The Office", "Heat"])
        amelie, office, heat = items
        self.assertEqual(amelie.type, "movie")
        self.assertEqual(amelie.year, 2001)
        self.assertEqual(amelie.rating_key, "5d776b59ad5437001f79c6f8")
        self.assertEqual(amelie.guids, {"imdb": "tt0211915", "tmdb": "194", "tvdb": "233"})
        self.assertEqual(amelie.original_title, "Le Fabuleux Destin d'Amélie Poulain")
        # no Guid in the list -> fetched from /library/metadata/{key}
        self.assertEqual(office.type, "show")
        self.assertEqual(office.year, 2005)
        self.assertEqual(office.guids, {"imdb": "tt0386676", "tvdb": "73244", "tmdb": "2316"})
        self.assertEqual(heat.year, 1995)
        wl_reqs = [r for r in self.srv.requests if r.path == "/library/sections/watchlist/all"]
        self.assertEqual(len(wl_reqs), 2)  # 4 items, server pages of 2
        self.assertEqual(wl_reqs[1].query["X-Plex-Container-Start"], "2")
        self.assertEqual(wl_reqs[0].query["includeGuids"], "1")
        h = wl_reqs[0].headers
        self.assertEqual(h["x-plex-token"], TOKEN)
        self.assertEqual(h["x-plex-client-identifier"], "client-123")
        self.assertEqual(h["x-plex-product"], "synoarr")
        self.assertEqual(h["accept"], "application/json")

    def test_watchlist_fallback_metadata_provider_on_404(self):
        self.state["watchlist_404"] = True
        with self.assertRaises(HttpError):
            self.plex.watchlist()
        paths = [r.path for r in self.srv.requests]
        self.assertIn("/library/sections/watchlist/all", paths)
        self.assertIn("/meta/library/sections/watchlist/all", paths)

    def test_get_guids_fallback(self):
        self.state["metadata_404"] = True
        with self.assertRaises(HttpError):
            self.plex.get_guids("5d9c086c46115600200aa2fe")
        self.assertEqual(self.srv.requests[-1].path, "/meta/library/metadata/5d9c086c46115600200aa2fe")
        self.state["metadata_404"] = False
        self.assertEqual(self.plex.get_guids("5d9c086c46115600200aa2fe")["tvdb"], "73244")

    def test_watchlist_raises_on_bad_token(self):
        with self.assertRaises(HttpError) as ctx:
            self._client("").watchlist()
        self.assertEqual(ctx.exception.status, 401)

    def test_show_episodes(self):
        eps = self.plex.show_episodes("show1")
        self.assertEqual([(e.season, e.episode) for e in eps], [(1, 1), (1, 2), (1, 3), (2, 1)])
        self.assertEqual(eps[0].title, "Episode 1")
        self.assertEqual(eps[0].air_date, "2020-01-01")
        self.assertEqual(eps[3].title, "Rentrée")
        self.assertEqual(eps[3].air_date, "2021-09-01")
        self.assertNotIn("/library/metadata/s0/children", [r.path for r in self.srv.requests])

    def test_test_ok(self):
        ok, msg = self.plex.test()
        self.assertTrue(ok, msg)
        self.assertIn("jean", msg)
        self.assertIn("4 élément", msg)

    def test_test_bad_token(self):
        ok, msg = self._client("wrong").test()
        self.assertFalse(ok)
        self.assertIn("401", msg)
        ok, msg = self._client("").test()
        self.assertFalse(ok)

    def test_pin_flow(self):
        anon = self._client("")
        pin_id, code = anon.start_pin_login()
        self.assertEqual((pin_id, code), (4242, "AB12"))
        req = self.state["pin_request"]
        self.assertEqual(req.form.get("strong"), "false")
        self.assertEqual(req.headers["x-plex-product"], "synoarr")
        self.assertEqual(req.headers["x-plex-client-identifier"], "client-123")
        self.assertEqual(req.headers["accept"], "application/json")
        self.assertIsNone(anon.check_pin(pin_id))
        self.assertEqual(anon.check_pin(pin_id), "newtoken")
        with self.assertRaises(HttpError):
            anon.check_pin(999)

    def test_refresh_library(self):
        self.assertTrue(refresh_library(self.srv.url, "srvtoken", self.http))
        self.assertEqual(self.state["refresh_token"], "srvtoken")
        self.assertFalse(refresh_library(self.srv.url + "/nope", "x", self.http))
        self.assertFalse(refresh_library("", "x", self.http))
        self.assertTrue(self.plex.refresh_library(self.srv.url))
        self.assertEqual(self.state["refresh_token"], TOKEN)


if __name__ == "__main__":
    unittest.main()
