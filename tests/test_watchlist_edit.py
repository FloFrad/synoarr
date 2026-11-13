"""Watchlist add/delete: Plex discover search + add/remove, TMDb candidates, local entries, hidden entries, CLI."""
from __future__ import annotations

import copy
import io
import logging
import os
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout
from typing import Any, Dict, List
from unittest import mock

from synoarr import cli
from synoarr import tmdb as tmdb_mod
from synoarr.app import App
from synoarr.config import load_config
from synoarr.httpclient import HttpClient
from synoarr.models import DSTask
from synoarr.plex import PlexClient

from tests.fakes import FakeServer

TOKEN = "PLEXTOKEN-secret"

PLEX_CATALOG = {
    "m1": {"ratingKey": "m1", "key": "/library/metadata/m1", "guid": "plex://movie/m1", "type": "movie",
           "title": "The Matrix", "year": 1999, "summary": "Un pirate découvre la vérité. " * 30,
           "thumb": "https://metadata-static.plex.tv/m1.jpg",
           "Guid": [{"id": "imdb://tt0133093"}, {"id": "tmdb://603"}], "Genre": [{"tag": "Science-Fiction"}]},
    "m2": {"ratingKey": "m2", "type": "movie", "title": "Matrix Resurrections", "year": 2021,
           "thumb": "/library/metadata/m2/thumb/123"},  # no Guid, relative thumb (needs the token)
    "s1": {"ratingKey": "s1", "key": "/library/metadata/s1", "guid": "plex://show/s1", "type": "show",
           "title": "Lupin", "year": 2021, "Guid": [{"id": "tvdb://383180"}, {"id": "tmdb://96677"}]},
}
PLEX_SEASONS = {"s1": [{"ratingKey": "s1-1", "type": "season", "index": 1}]}
PLEX_EPISODES = {"s1-1": [{"type": "episode", "index": 1, "parentIndex": 1, "title": "Chapitre 1",
                           "originallyAvailableAt": "2021-01-08"},
                          {"type": "episode", "index": 2, "parentIndex": 1, "title": "Chapitre 2",
                           "originallyAvailableAt": "2021-01-08"}]}

TMDB_MULTI = [
    {"media_type": "movie", "id": 603, "title": "Matrix", "original_title": "The Matrix",
     "release_date": "1999-03-31", "overview": "Résumé TMDb", "poster_path": "/p603.jpg"},
    {"media_type": "movie", "id": 604, "title": "Matrix Reloaded", "release_date": "2003-05-15",
     "overview": "Suite", "poster_path": "/p604.jpg"},
    {"media_type": "person", "id": 1, "name": "Keanu Reeves"},
    {"media_type": "tv", "id": 5000, "name": "Matrix Chronicles", "first_air_date": "2010-01-01", "overview": ""},
]


class World:
    def __init__(self) -> None:
        self.watchlist = []  # type: List[str]  # rating keys in the Plex Watchlist
        self.search_shape = "nested"
        self.add_fail = False
        self.remove_fail = False
        self.search_fail = False

    # ------------------------------------------------------------------ plex
    def plex(self, req: Any):
        if req.headers.get("x-plex-token") != TOKEN:
            return 401, {}, b"unauthorized"
        p = req.path
        if req.method == "PUT" and p == "/actions/addToWatchlist":
            if self.add_fail:
                return 503, {}, b"down"
            key = req.query.get("ratingKey")
            if key not in PLEX_CATALOG:
                return 404, {}, b"nope"  # 404 on discover and on the metadata fallback
            if key not in self.watchlist:
                self.watchlist.append(key)
            return 200, {}, b""
        if req.method == "PUT" and p == "/actions/removeFromWatchlist":
            if self.remove_fail:
                return 503, {}, b"down"
            key = req.query.get("ratingKey")
            if key in self.watchlist:
                self.watchlist.remove(key)
            return 200, {}, b""
        if p == "/library/search":
            if self.search_fail:
                return 503, {}, b"down"
            q = req.query.get("query", "").lower()
            items = [copy.deepcopy(v) for v in PLEX_CATALOG.values() if q in v["title"].lower()]
            if self.search_shape == "nested":
                mc = {"size": 2, "SearchResults": [
                    {"id": "plex", "title": "Plex", "size": 0},
                    {"id": "external", "title": "Plus de façons de regarder", "size": len(items),
                     "SearchResult": [{"score": 0.9, "Metadata": it} for it in items]}]}
            else:
                mc = {"size": len(items), "Metadata": items}
            return 200, {}, {"MediaContainer": mc}
        if p == "/library/sections/watchlist/all":
            items = [copy.deepcopy(PLEX_CATALOG[k]) for k in self.watchlist]
            return 200, {}, {"MediaContainer": {"totalSize": len(items), "size": len(items), "Metadata": items}}
        if p.startswith("/library/metadata/"):
            rest = p[len("/library/metadata/"):]
            if rest.endswith("/children"):
                key = rest[:-len("/children")]
                items = PLEX_SEASONS.get(key) or PLEX_EPISODES.get(key) or []
                return 200, {}, {"MediaContainer": {"size": len(items), "totalSize": len(items), "Metadata": items}}
            if rest in PLEX_CATALOG:
                return 200, {}, {"MediaContainer": {"Metadata": [PLEX_CATALOG[rest]]}}
        return 404, {}, b"not found"

    # ------------------------------------------------------------------ tmdb
    def tmdb(self, req: Any):
        p = req.path[len("/tmdb"):]
        q = req.query.get("query", "").lower()
        hits = [r for r in TMDB_MULTI if q in str(r.get("title") or r.get("name") or "").lower()]
        if p == "/search/multi":
            return 200, {}, {"results": hits}
        if p == "/search/movie":
            return 200, {}, {"results": [dict(r, media_type=None) for r in hits if r["media_type"] == "movie"]}
        if p == "/search/tv":
            if "local" in q:
                return 200, {}, {"results": [{"id": 5000, "name": "Local Show", "first_air_date": "2010-01-01"}]}
            return 200, {}, {"results": [dict(r, media_type=None) for r in hits if r["media_type"] == "tv"]}
        if p == "/movie/603/external_ids":
            return 200, {}, {"imdb_id": "tt0133093"}
        if p == "/movie/604/external_ids":
            return 200, {}, {"imdb_id": "tt0234215"}
        if p == "/tv/5000/external_ids":
            return 200, {}, {"imdb_id": None, "tvdb_id": 777}
        if p in ("/movie/603", "/movie/604"):
            return 200, {}, {"genres": [{"id": 878, "name": "Science-Fiction"}], "belongs_to_collection": None}
        if p == "/tv/5000":
            return 200, {}, {"genres": [{"id": 18, "name": "Drame"}],
                             "seasons": [{"season_number": 0}, {"season_number": 1}]}
        if p == "/tv/5000/season/1":
            return 200, {}, {"episodes": [
                {"season_number": 1, "episode_number": 1, "name": "Pilote", "air_date": "2010-01-01"},
                {"season_number": 1, "episode_number": 2, "name": "Épisode 2", "air_date": "2010-01-08"}]}
        return 404, {}, {"status_message": "not found"}

    def handler(self, req: Any):
        if req.path.startswith("/tmdb"):
            return self.tmdb(req)
        return self.plex(req)


class FakeDS:
    def __init__(self, tasks: List[DSTask]) -> None:
        self.tasks = tasks
        self.deleted = []  # type: List[str]

    def list_tasks(self) -> List[DSTask]:
        return list(self.tasks)

    def delete_task(self, task_id: str) -> None:
        self.deleted.append(task_id)


def _close_log_handlers() -> None:
    root = logging.getLogger()
    for h in list(root.handlers):
        root.removeHandler(h)
        h.close()


class PlexSearchParsingTest(unittest.TestCase):
    def setUp(self) -> None:
        self.world = World()
        self.srv = FakeServer(self.world.handler).__enter__()
        self.plex = PlexClient(TOKEN, "cid", HttpClient(timeout=5, retries=0))
        self.plex.discover_url = self.srv.url
        self.plex.metadata_url = self.srv.url + "/meta"

    def tearDown(self) -> None:
        self.srv.__exit__(None, None, None)

    def check_results(self, res: List[Dict[str, Any]]) -> None:
        self.assertEqual([c["rating_key"] for c in res], ["m1", "m2"])
        m1, m2 = res
        self.assertEqual((m1["source"], m1["type"], m1["title"], m1["year"]), ("plex", "movie", "The Matrix", 1999))
        self.assertEqual((m1["imdb"], m1["tmdb"], m1["tvdb"]), ("tt0133093", "603", None))
        self.assertEqual(m1["poster_url"], "https://metadata-static.plex.tv/m1.jpg")
        self.assertLessEqual(len(m1["overview"]), 301)
        self.assertTrue(m1["overview"].endswith("…"))
        self.assertIsNone(m2["poster_url"])  # relative thumb would need the token
        self.assertIsNone(m2["imdb"])

    def test_nested_search_results_shape(self) -> None:
        self.check_results(self.plex.search("matrix"))
        req = self.srv.requests[-1]
        self.assertEqual(req.path, "/library/search")
        self.assertEqual(req.query["query"], "matrix")
        self.assertEqual(req.query["searchTypes"], "movies,tv")
        self.assertEqual(req.query["searchProviders"], "discover")
        self.assertEqual(req.query["includeMetadata"], "1")
        self.assertEqual(req.query["includeGuids"], "1")
        self.assertEqual(req.headers["x-plex-token"], TOKEN)
        self.assertNotIn(TOKEN, req.raw_path)
        self.assertEqual(req.headers["accept"], "application/json")
        self.plex.search("lupin", "show")
        self.assertEqual(self.srv.requests[-1].query["searchTypes"], "tv")
        self.assertEqual(self.plex.search("matrix", "show"), [])  # type filter

    def test_flat_metadata_shape(self) -> None:
        self.world.search_shape = "flat"
        self.check_results(self.plex.search("matrix", "movie"))
        self.assertEqual(self.srv.requests[-1].query["searchTypes"], "movies")

    def test_add_and_remove_use_put(self) -> None:
        self.plex.add_to_watchlist("m1")
        req = self.srv.requests[-1]
        self.assertEqual((req.method, req.path, req.query), ("PUT", "/actions/addToWatchlist", {"ratingKey": "m1"}))
        self.assertEqual(self.world.watchlist, ["m1"])
        self.plex.remove_from_watchlist("m1")
        req = self.srv.requests[-1]
        self.assertEqual((req.method, req.path), ("PUT", "/actions/removeFromWatchlist"))
        self.assertEqual(self.world.watchlist, [])


class _AppTestBase(unittest.TestCase):
    tmdb_key = "tmdbkey"

    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="synoarr-wle-")
        self.world = World()
        self.srv = FakeServer(self.world.handler).__enter__()
        self.movies = os.path.join(self.tmp, "Films")
        self.cfg_path = os.path.join(self.tmp, "config.ini")
        with open(self.cfg_path, "w", encoding="utf-8") as fh:
            fh.write("[general]\ndata_dir = ./data\nlog_level = WARNING\n[plex]\ntoken = %s\n[tmdb]\napi_key = %s\n"
                     "[paths]\ndownload_dir = %s\nmovies_dir = %s\nseries_dir = %s\n[web]\nenabled = false\n"
                     % (TOKEN, self.tmdb_key, self.tmp, self.movies, os.path.join(self.tmp, "Séries")))
        urls = {"discover_url": self.srv.url, "metadata_url": self.srv.url + "/meta", "plextv_url": self.srv.url}
        self.patch = mock.patch.object(App, "plex_urls", urls)
        self.patch.start()
        self.app = App(load_config(self.cfg_path))
        self.app.http.retries = 0
        self.app.tmdb.base_url = self.srv.url + "/tmdb"

    def tearDown(self) -> None:
        self.patch.stop()
        self.app._stop.set()
        self.app.db.close()
        self.srv.__exit__(None, None, None)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def media_rows(self) -> List[Dict[str, Any]]:
        return self.app.db.list_media(include_hidden=True)

    def puts(self, path: str) -> List[Any]:
        return [r for r in self.srv.requests if r.method == "PUT" and r.path == path]


class WatchlistEditTest(_AppTestBase):

    # ------------------------------------------------------------------ search
    def test_search_titles_merges_plex_and_tmdb(self) -> None:
        res = self.app.search_titles("matrix")
        self.assertEqual(res["warnings"], [])
        titles = [(c["source"], c["title"]) for c in res["results"]]
        self.assertEqual(titles, [("plex", "The Matrix"), ("plex", "Matrix Resurrections"),
                                  ("tmdb", "Matrix Reloaded"), ("tmdb", "Matrix Chronicles")])
        m1 = res["results"][0]
        self.assertEqual(m1["poster_url"], "https://metadata-static.plex.tv/m1.jpg")  # Plex data kept
        self.assertFalse(m1["in_watchlist"])
        reloaded = res["results"][2]
        self.assertEqual((reloaded["tmdb"], reloaded["year"], reloaded["type"]), ("604", 2003, "movie"))
        self.assertEqual(reloaded["poster_url"], "https://image.tmdb.org/t/p/w185/p604.jpg")
        self.assertIsNone(reloaded["rating_key"])
        self.assertEqual(res["results"][3]["type"], "show")
        # year filter + movie only
        res = self.app.search_titles("matrix", kind="movie", year=1999)
        self.assertEqual([c["title"] for c in res["results"]], ["The Matrix"])
        tmdb_req = [r for r in self.srv.requests if r.path == "/tmdb/search/movie"][-1]
        self.assertEqual(tmdb_req.query["year"], "1999")

    def test_search_titles_never_raises_on_service_errors(self) -> None:
        self.world.search_fail = True
        res = self.app.search_titles("matrix")
        self.assertTrue(any("Plex" in w for w in res["warnings"]))
        self.assertEqual([c["source"] for c in res["results"]], ["tmdb"] * 3)
        for w in res["warnings"]:
            self.assertNotIn(TOKEN, w)
        with self.assertRaises(ValueError):
            self.app.search_titles("  ")
        with self.assertRaises(ValueError):
            self.app.search_titles("matrix", kind="music")
        with self.assertRaises(ValueError):
            self.app.search_titles("matrix", year="abc")

    def test_in_watchlist_flag(self) -> None:
        self.world.watchlist = ["m1"]
        self.app.searcher.sync_watchlist()
        res = self.app.search_titles("matrix")
        flags = {c["title"]: c["in_watchlist"] for c in res["results"]}
        self.assertTrue(flags["The Matrix"])
        self.assertFalse(flags["Matrix Reloaded"])
        self.assertEqual(res["results"][0]["media_id"], self.app.db.get_media_by_key("m1")["id"])

    # --------------------------------------------------------------------- add
    def test_add_plex_item_then_sync_makes_no_duplicate(self) -> None:
        cand = self.app.search_titles("matrix")["results"][0]
        row = self.app.add_to_watchlist(cand)
        self.assertEqual(row["plex_key"], "m1")
        self.assertTrue(row["plex_added"])
        self.assertFalse(row["already_present"])
        self.assertIsNone(row["warning"])
        self.assertEqual((row["imdb"], row["tmdb"], row["status"]), ("tt0133093", "603", "wanted"))
        self.assertEqual(row["genres"], "Science-Fiction")
        self.assertEqual(len(self.puts("/actions/addToWatchlist")), 1)
        self.assertEqual(self.world.watchlist, ["m1"])
        self.assertEqual(self.app.searcher.sync_watchlist(), 1)
        rows = self.media_rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]["plex_key"], rows[0]["on_watchlist"]), ("m1", 1))
        # adding it again returns the existing row
        again = self.app.add_to_watchlist(cand)
        self.assertEqual(again["id"], row["id"])
        self.assertTrue(again["already_present"])
        self.assertEqual(len(self.media_rows()), 1)

    def test_add_plex_show_fetches_episodes(self) -> None:
        cand = self.app.search_titles("lupin", kind="show")["results"][0]
        row = self.app.add_to_watchlist(cand)
        self.assertEqual((row["plex_key"], row["type"], row["status"]), ("s1", "show", "monitored"))
        eps = [(e["season"], e["episode"]) for e in self.app.db.list_episodes(row["id"])]
        self.assertEqual(eps, [(1, 1), (1, 2)])

    def test_plex_failure_creates_local_entry_that_survives_sync(self) -> None:
        self.world.add_fail = True
        cand = self.app.search_titles("matrix")["results"][0]
        row = self.app.add_to_watchlist(cand)
        self.assertTrue(row["plex_key"].startswith("local:"), row["plex_key"])
        self.assertFalse(row["plex_added"])
        self.assertIn("Watchlist Plex impossible", row["warning"])
        self.assertNotIn(TOKEN, row["warning"])
        self.assertEqual((row["imdb"], row["tmdb"]), ("tt0133093", "603"))
        self.app.searcher.sync_watchlist()  # Plex Watchlist empty
        local = self.app.db.get_media(row["id"])
        self.assertEqual((local["on_watchlist"], local["status"]), (1, "wanted"))
        # the user adds it to the Plex Watchlist himself later: the local entry becomes the Plex item
        self.world.watchlist = ["m1"]
        self.app.searcher.sync_watchlist()
        rows = self.media_rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]["id"], rows[0]["plex_key"]), (row["id"], "m1"))

    def test_tmdb_candidate_is_local_with_ids(self) -> None:
        res = self.app.search_titles("matrix")["results"]
        reloaded = next(c for c in res if c["title"] == "Matrix Reloaded")
        row = self.app.add_to_watchlist(reloaded)
        self.assertEqual(row["plex_key"], "local:tmdb:movie:604")
        self.assertEqual((row["imdb"], row["year"], row["genres"]), ("tt0234215", 2003, "Science-Fiction"))
        self.assertIsNone(row["plex_added"])
        self.assertEqual(self.puts("/actions/addToWatchlist"), [])

    def test_local_show_gets_episodes_from_tmdb_and_survives_sync(self) -> None:
        row = self.app.add_manual("show", "Local Show", 2010, tmdb="5000")
        self.assertTrue(row["plex_key"].startswith("local:"))
        self.assertEqual(row["tvdb"], "777")
        eps = self.app.db.list_episodes(row["id"])
        self.assertEqual([(e["season"], e["episode"], e["title"]) for e in eps],
                         [(1, 1, "Pilote"), (1, 2, None)])
        self.app.db.execute("DELETE FROM episodes WHERE media_id = ?", (row["id"],))
        self.app.searcher.sync_watchlist()
        self.assertEqual(self.app.db.get_media(row["id"])["on_watchlist"], 1)
        self.assertEqual(len(self.app.db.list_episodes(row["id"])), 2)  # refreshed by the sync
        # no Plex request for a local entry
        self.assertFalse([r for r in self.srv.requests if "local" in r.path])

    def test_manual_entry_without_ids_finds_tmdb_id(self) -> None:
        row = self.app.add_manual("show", "Local Show")
        self.assertEqual(row["tmdb"], "5000")
        self.assertEqual(len(self.app.db.list_episodes(row["id"])), 2)
        with self.assertRaises(ValueError):
            self.app.add_manual("music", "X")
        with self.assertRaises(ValueError):
            self.app.add_manual("movie", "")
        with self.assertRaises(ValueError):
            self.app.add_manual("movie", "X", imdb="abc")
        with self.assertRaises(ValueError):
            self.app.add_manual("movie", "X", year="19x9")

    def test_sync_without_plex_still_refreshes_local_entries(self) -> None:
        row = self.app.add_manual("show", "Local Show", tmdb="5000")
        self.app.db.execute("DELETE FROM episodes WHERE media_id = ?", (row["id"],))
        self.app.searcher.plex = None
        self.app.plex = None
        self.assertEqual(self.app.searcher.sync_watchlist(), 0)
        self.assertEqual(len(self.app.db.list_episodes(row["id"])), 2)

    # ------------------------------------------------------------------ no duplicate on manual add
    def test_manual_add_matches_existing_row_by_title_and_year(self) -> None:
        self.world.watchlist = ["m1"]
        self.app.searcher.sync_watchlist()
        movie = self.app.db.get_media_by_key("m1")  # The Matrix (1999), with ids
        for year in (1999, 2000, None):
            with self.subTest(year=year):
                row = self.app.add_manual("movie", "the matrix", year)
                self.assertEqual(row["id"], movie["id"])
                self.assertTrue(row["already_present"])
                self.assertFalse(row["reactivated"])
        self.assertEqual(len(self.media_rows()), 1)
        # another work: year too far, a different id of the same kind, or another type
        far = self.app.add_manual("movie", "The Matrix", 2010)
        other_id = self.app.add_manual("movie", "The Matrix", 1999, imdb="tt9999999")
        show = self.app.add_manual("show", "The Matrix", 1999)
        self.assertEqual(len({movie["id"], far["id"], other_id["id"], show["id"]}), 4)
        self.assertFalse(any(r["already_present"] for r in (far, other_id, show)))

    def test_manual_add_matches_original_title_and_movie_year_rules(self) -> None:
        mid = self.app.db.upsert_media("p9", "movie", "Le Fabuleux Destin d'Amélie Poulain", 2001, None, None, None,
                                       "Amélie")
        row = self.app.add_manual("movie", "Amelie", 2001)
        self.assertEqual((row["id"], row["already_present"]), (mid, True))
        # movie: an existing row without year is not matched when the add gives a year...
        heat = self.app.db.upsert_media("p8", "movie", "Heat", None, None, None, None)
        self.assertNotEqual(self.app.add_manual("movie", "Heat", 1995)["id"], heat)
        # ...but an add without year matches a row with one
        heat95 = self.app.db.get_media_by_key("p8")
        self.assertEqual(self.app.add_manual("movie", "Heat")["id"], heat95["id"])

    def test_manual_add_reactivates_hidden_show(self) -> None:
        sid = self.app.db.upsert_media("sx", "show", "Test Show", 2015, None, None, None)
        self.app.db.set_media_hidden(sid, True)
        self.app.db.set_media_status(sid, "ignored")
        row = self.app.add_manual("show", "Test Show")  # no year: shows match on title only
        self.assertEqual(row["id"], sid)
        self.assertTrue(row["already_present"])
        self.assertTrue(row["reactivated"])
        self.assertEqual((row["hidden"], row["status"]), (None, "monitored"))
        self.assertEqual(len(self.media_rows()), 1)

    def test_local_entry_resolving_to_an_existing_tmdb_id_is_not_duplicated(self) -> None:
        sid = self.app.db.upsert_media("s5", "show", "Série locale", 2010, None, "5000", None)  # French Plex title
        # different title (no title match) but TMDb resolves it to the same show (tmdb 5000)
        row = self.app.add_manual("show", "Local Show")
        self.assertEqual(row["id"], sid)
        self.assertTrue(row["already_present"])
        self.assertEqual([r["id"] for r in self.media_rows()], [sid])
        # an older local entry resolving later (sync) to the id of a Plex row: masked + ignored, Plex row kept
        lid = self.app.db.upsert_media("local:abc", "show", "Local Show", None, None, None, None)
        self.app.searcher.sync_local_items()
        dup = self.app.db.get_media(lid)
        self.assertEqual((dup["tmdb"], dup["hidden"], dup["status"]), ("5000", 1, "ignored"))
        self.assertIsNone(self.app.db.get_media(sid)["hidden"])
        self.assertEqual([m["id"] for m in self.app.db.list_media()], [sid])

    # ------------------------------------------------------------------ remove
    def test_remove_plex_item_deletes_row_and_keeps_history(self) -> None:
        self.world.watchlist = ["s1"]
        self.app.searcher.sync_watchlist()
        show = self.app.db.get_media_by_key("s1")
        self.assertEqual(len(self.app.db.list_episodes(show["id"])), 2)
        gid = self.app.db.add_grab(show["id"], 1, None, "Lupin.S01.1080p", "Lupin.S01.1080p", "X", None, 1)
        self.app.db.set_grab_status(gid, "imported")
        res = self.app.remove_from_watchlist(show["id"])
        self.assertEqual(res, {"removed": True, "plex_removed": True, "warning": None, "downloads_deleted": 0})
        self.assertEqual(self.puts("/actions/removeFromWatchlist")[0].query, {"ratingKey": "s1"})
        self.assertIsNone(self.app.db.get_media(show["id"]))
        self.assertEqual(self.app.db.list_episodes(show["id"]), [])
        grab = self.app.db.query_one("SELECT * FROM grabs WHERE id = ?", (gid,))
        self.assertIsNotNone(grab)
        self.assertIsNone(grab["media_id"])
        self.assertEqual(grab["status"], "imported")
        self.app.searcher.sync_watchlist()  # Plex no longer returns it
        self.assertEqual(self.media_rows(), [])
        with self.assertRaises(ValueError):
            self.app.remove_from_watchlist(show["id"])

    def test_remove_failure_hides_until_re_added(self) -> None:
        self.world.watchlist = ["m1"]
        self.app.searcher.sync_watchlist()
        movie = self.app.db.get_media_by_key("m1")
        self.world.remove_fail = True
        res = self.app.remove_from_watchlist(movie["id"])
        self.assertTrue(res["removed"])
        self.assertFalse(res["plex_removed"])
        self.assertIn("Watchlist Plex", res["warning"])
        self.assertNotIn(TOKEN, res["warning"])
        row = self.app.db.get_media(movie["id"])
        self.assertEqual((row["hidden"], row["status"]), (1, "ignored"))
        self.assertEqual(self.app.db.list_media(), [])
        # Plex still returns it: stays hidden and ignored
        self.app.searcher.sync_watchlist()
        row = self.app.db.get_media(movie["id"])
        self.assertEqual((row["hidden"], row["status"], row["on_watchlist"]), (1, "ignored", 1))
        self.assertEqual(self.app.db.list_media(), [])
        self.assertEqual(self.app.searcher.run_once().get("searched"), 0)
        # re-added by the user: visible and wanted again, same row
        cand = self.app.search_titles("matrix")["results"][0]
        self.assertFalse(cand["in_watchlist"])
        again = self.app.add_to_watchlist(cand)
        self.assertEqual(again["id"], movie["id"])
        self.assertTrue(again["already_present"])
        self.assertEqual((again["hidden"], again["status"]), (None, "wanted"))
        self.assertEqual(len(self.app.db.list_media()), 1)

    def test_hidden_entry_is_purged_once_gone_from_plex(self) -> None:
        self.world.watchlist = ["m1"]
        self.app.searcher.sync_watchlist()
        movie = self.app.db.get_media_by_key("m1")
        res = self.app.remove_from_watchlist(movie["id"], remove_from_plex=False)
        self.assertIsNone(res["plex_removed"])
        self.assertIn("reste dans votre Watchlist Plex", res["warning"])
        self.assertEqual(self.puts("/actions/removeFromWatchlist"), [])
        self.assertEqual(self.app.db.get_media(movie["id"])["hidden"], 1)
        self.world.watchlist = []  # removed from Plex by the user
        self.app.searcher.sync_watchlist()
        self.assertIsNone(self.app.db.get_media(movie["id"]))

    def test_remove_local_entry(self) -> None:
        row = self.app.add_manual("movie", "Mon film", 1990)
        res = self.app.remove_from_watchlist(row["id"])
        self.assertEqual((res["removed"], res["plex_removed"], res["warning"]), (True, None, None))
        self.assertIsNone(self.app.db.get_media(row["id"]))
        self.assertEqual(self.puts("/actions/removeFromWatchlist"), [])

    def test_delete_downloads_only_deletes_ds_tasks(self) -> None:
        os.makedirs(self.movies)
        library_file = os.path.join(self.movies, "Mon film (1990).mkv")
        with open(library_file, "wb") as fh:
            fh.write(b"\0" * 10)
        row = self.app.add_manual("movie", "Mon film", 1990)
        other = self.app.add_manual("movie", "Autre film", 2000)
        db = self.app.db
        g_sent = db.add_grab(row["id"], None, None, "Mon.Film.1990.1080p.WEB", "Mon.Film.1990.1080p.WEB", "X", None, 1)
        g_done = db.add_grab(row["id"], None, None, "Mon.Film.1990.720p", "Mon.Film.1990.720p", "X", None, 1)
        db.set_grab_status(g_done, "imported")
        g_other = db.add_grab(other["id"], None, None, "Autre.Film.2000.1080p", "Autre.Film.2000.1080p", "X", None, 1)
        ds = FakeDS([DSTask(id="dbid_1", title="Mon.Film.1990.1080p.WEB", status="downloading"),
                     DSTask(id="dbid_2", title="Mon.Film.1990.720p", status="seeding"),
                     DSTask(id="dbid_3", title="Autre.Film.2000.1080p", status="downloading")])
        self.app.ds = self.app.searcher.ds = ds  # type: ignore[assignment]
        res = self.app.remove_from_watchlist(row["id"], delete_downloads=True)
        self.assertEqual(res["downloads_deleted"], 1)
        self.assertEqual(ds.deleted, ["dbid_1"])
        sent = db.query_one("SELECT * FROM grabs WHERE id = ?", (g_sent,))
        self.assertEqual((sent["status"], sent["message"], sent["media_id"]), ("failed", "supprimé avec l'entrée", None))
        self.assertEqual(db.query_one("SELECT status FROM grabs WHERE id = ?", (g_done,))["status"], "imported")
        self.assertEqual(db.query_one("SELECT status FROM grabs WHERE id = ?", (g_other,))["status"], "sent")
        self.assertTrue(os.path.exists(library_file))
        # without Download Station: a warning, the entry is removed anyway
        self.app.ds = None
        res = self.app.remove_from_watchlist(other["id"], delete_downloads=True)
        self.assertTrue(res["removed"])
        self.assertIn("Download Station", res["warning"])

    def test_migration_adds_hidden_column(self) -> None:
        cols = {r["name"] for r in self.app.db.query("PRAGMA table_info(media)")}
        self.assertIn("hidden", cols)


class NoTmdbLocalShowTest(unittest.TestCase):
    """Without TMDb key, a local show has no episode list (season pack mode)."""

    def test_local_show_without_tmdb(self) -> None:
        tmp = tempfile.mkdtemp(prefix="synoarr-wle-")
        try:
            cfg_path = os.path.join(tmp, "config.ini")
            with open(cfg_path, "w", encoding="utf-8") as fh:
                fh.write("[general]\ndata_dir = ./data\n[paths]\ndownload_dir = %s\n" % tmp)
            app = App(load_config(cfg_path))
            try:
                res = app.search_titles("x")
                self.assertEqual(res["results"], [])
                self.assertTrue(any("ajout manuel" in w for w in res["warnings"]))
                row = app.add_manual("show", "Série locale", 2015)
                self.assertTrue(row["plex_key"].startswith("local:"))
                self.assertEqual(app.db.list_episodes(row["id"]), [])
                app.searcher.sync_watchlist()
                self.assertEqual(app.db.get_media(row["id"])["status"], "monitored")
            finally:
                app.db.close()
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class WatchlistCliTest(_AppTestBase):
    def cli(self, *argv: str):
        orig_init = tmdb_mod.TmdbClient.__init__
        srv_url = self.srv.url

        def patched(self_, *a, **kw):
            orig_init(self_, *a, **kw)
            self_.base_url = srv_url + "/tmdb"
        buf = io.StringIO()
        try:
            with mock.patch.object(tmdb_mod.TmdbClient, "__init__", patched), redirect_stdout(buf):
                rc = cli.main(list(argv) + ["--config", self.cfg_path])
        finally:
            _close_log_handlers()
        return rc, buf.getvalue()

    def test_cli_add_list_remove(self) -> None:
        rc, out = self.cli("watchlist", "add", "matrix", "--type", "movie", "--pick", "1")
        self.assertEqual(rc, 0, out)
        self.assertIn("  1  film   The Matrix", out)
        self.assertIn("Watchlist Plex", out)
        self.assertEqual(self.world.watchlist, ["m1"])
        rc, out = self.cli("watchlist", "add", "matrix", "--pick", "3", "--local")
        self.assertEqual(rc, 0, out)
        self.assertIn("entrée locale", out)
        rc, out = self.cli("watchlist", "add", "Film perso", "--manual", "--type", "movie", "--year", "1985")
        self.assertEqual(rc, 0, out)
        rc, out = self.cli("watchlist", "--no-sync")
        self.assertEqual(rc, 0, out)
        self.assertIn("Film perso", out)
        self.assertEqual(out.count("(local)"), 2, out)
        movie = self.app.db.get_media_by_key("m1")
        rc, out = self.cli("watchlist", "remove", str(movie["id"]))
        self.assertEqual(rc, 0, out)
        self.assertIn("retiré de synoarr et de la Watchlist Plex", out)
        self.assertEqual(self.world.watchlist, [])
        self.assertIsNone(self.app.db.get_media(movie["id"]))
        rc, out = self.cli("watchlist", "remove", "9999")
        self.assertEqual(rc, 1)
        self.assertIn("introuvable", out)

    def test_cli_keep_plex_and_errors(self) -> None:
        self.world.watchlist = ["m1"]
        self.app.searcher.sync_watchlist()
        movie = self.app.db.get_media_by_key("m1")
        rc, out = self.cli("watchlist", "remove", str(movie["id"]), "--keep-plex")
        self.assertEqual(rc, 0, out)
        self.assertIn("[!]", out)
        self.assertEqual(self.world.watchlist, ["m1"])
        rc, out = self.cli("watchlist", "--no-sync")
        self.assertNotIn("The Matrix", out)
        rc, out = self.cli("watchlist", "add", "matrix", "--pick", "99")
        self.assertEqual(rc, 2)
        with mock.patch("sys.stdin") as stdin:
            stdin.isatty.return_value = False
            rc, out = self.cli("watchlist", "add", "matrix")
        self.assertEqual(rc, 2)
        self.assertIn("--pick", out)
        rc, out = self.cli("watchlist", "add", "Film perso", "--manual")
        self.assertEqual(rc, 2)
        rc, out = self.cli("watchlist", "add", "zzz", "--type", "show")
        self.assertEqual(rc, 1, out)
        self.assertIn("Aucun résultat", out)
        self.assertIn("--manual", out)
