from __future__ import annotations

import logging
import unittest

from synoarr.httpclient import HttpClient
from synoarr.tmdb import TmdbClient
from tests.fakes import FakeServer



def setUpModule():
    logging.disable(logging.CRITICAL)


def tearDownModule():
    logging.disable(logging.NOTSET)


def handler(req):
    q = req.query
    if q.get("api_key") != "k3y" and req.headers.get("authorization") != "Bearer eyJabc":
        return 401, {}, {"status_code": 7, "status_message": "Invalid API key"}
    if req.path == "/search/movie":
        if q.get("query") == "Heat":
            if q.get("year") == "1996":
                return 200, {}, {"page": 1, "results": [], "total_results": 0}
            return 200, {}, {"page": 1, "results": [
                {"id": 11111, "title": "Heat Wave", "original_title": "Heat Wave", "release_date": "1990-01-01"},
                {"id": 949, "title": "Heat", "original_title": "Heat", "release_date": "1995-12-15"},
            ]}
        return 200, {}, {"page": 1, "results": []}
    if req.path == "/search/tv":
        if q.get("query") == "Doctor Who":
            return 200, {}, {"results": [
                {"id": 121, "name": "Doctor Who", "original_name": "Doctor Who", "first_air_date": "1963-11-23"},
                {"id": 57243, "name": "Doctor Who", "original_name": "Doctor Who", "first_air_date": "2005-03-26"},
            ]}
        return 200, {}, {"results": []}
    if req.path == "/tv/1399/season/1/episode/1":
        if q.get("language") == "en-US":
            return 200, {}, {"name": "Winter Is Coming"}
        return 200, {}, {"name": "Épisode 1"}
    if req.path == "/tv/1399/season/1/episode/2":
        return 200, {}, {"name": "La Route royale"}
    if req.path == "/tv/1399":
        return 200, {}, {"id": 1399, "name": "Game of Thrones", "seasons": [
            {"season_number": 0, "episode_count": 1}, {"season_number": 1, "episode_count": 2},
            {"season_number": 2, "episode_count": 1}]}
    if req.path == "/tv/1399/season/1":
        return 200, {}, {"episodes": [
            {"episode_number": 2, "season_number": 1, "name": "La Route royale", "air_date": "2011-04-24"},
            {"episode_number": 1, "season_number": 1, "name": "L'hiver vient", "air_date": "2011-04-17"}]}
    if req.path == "/tv/1399/season/2":
        return 200, {}, {"episodes": [{"episode_number": 1, "season_number": 2, "name": "Épisode 1",
                                       "air_date": None}]}
    if req.path == "/tv/1399/season/0":
        raise AssertionError("specials must not be fetched")
    return 404, {}, {"status_code": 34}


class TmdbTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = FakeServer(handler).__enter__()

    @classmethod
    def tearDownClass(cls):
        cls.srv.__exit__(None, None, None)

    def client(self, key="k3y", lang="fr-FR"):
        c = TmdbClient(key, lang, HttpClient(timeout=5, retries=0))
        c.base_url = self.srv.url
        return c

    def test_disabled(self):
        c = self.client(key="")
        self.assertFalse(c.enabled)
        self.assertIsNone(c.search_movie("Heat", 1995))
        self.assertEqual(c.show_episodes("1"), [])

    def test_search_movie_picks_exact_title_and_retries_without_year(self):
        r = self.client().search_movie("Heat", 1996)
        self.assertEqual(r["tmdb"], "949")
        self.assertEqual(r["title"], "Heat")
        self.assertEqual(r["year"], 1995)
        self.assertEqual(self.srv.requests[-1].query["language"], "fr-FR")
        self.assertIsNone(self.client().search_movie("Unknown", None))

    def test_bearer_token(self):
        r = self.client(key="eyJabc").search_movie("Heat", None)
        self.assertEqual(r["tmdb"], "949")
        self.assertNotIn("api_key", self.srv.requests[-1].query)

    def test_bad_key_returns_none(self):
        self.assertIsNone(self.client(key="bad").search_movie("Heat", None))

    def test_search_show_year_in_title(self):
        r = self.client().search_show("Doctor Who (2005)")
        self.assertEqual(r["tmdb"], "57243")
        self.assertEqual(r["year"], 2005)
        r = self.client().search_show("Doctor Who")
        self.assertEqual(r["tmdb"], "121")

    def test_episode_title_with_english_fallback(self):
        c = self.client()
        self.assertEqual(c.episode_title("1399", 1, 1), "Winter Is Coming")
        self.assertEqual(c.episode_title("1399", 1, 2), "La Route royale")
        self.assertIsNone(c.episode_title("1399", 9, 9))

    def test_show_episodes(self):
        eps = self.client().show_episodes("1399")
        self.assertEqual([(e.season, e.episode) for e in eps], [(1, 1), (1, 2), (2, 1)])
        self.assertEqual(eps[0].title, "L'hiver vient")
        self.assertEqual(eps[0].air_date, "2011-04-17")
        self.assertIsNone(eps[2].title)
        self.assertIsNone(eps[2].air_date)
        self.assertEqual(self.client().show_episodes("404"), [])


if __name__ == "__main__":
    unittest.main()
