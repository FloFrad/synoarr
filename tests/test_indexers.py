from __future__ import annotations

import hashlib
import socket
import unittest

from synoarr.bencode import bencode, magnet_info, torrent_info
from synoarr.httpclient import HttpClient
from synoarr.indexers import CATALOG, Indexer, create_indexer, detect_type
from synoarr.indexers.base import parse_categories, parse_size
from synoarr.indexers.internet_archive import InternetArchiveIndexer
from synoarr.indexers.rss import RssIndexer
from synoarr.indexers.torznab import TorznabIndexer, clean_query, endpoint_candidates
from synoarr.models import SearchRequest
from tests.fakes import FakeServer

API_KEY = "s3cr3tk3y123"
HASH = "c12fe1c06bba254a9dc9f519b335aa7c1367a88a"
XML = {"Content-Type": "application/rss+xml; charset=utf-8"}

CAPS_XML = """<?xml version="1.0" encoding="UTF-8"?>
<caps>
  <server version="1.0" title="Jackett" />
  <limits max="100" default="50" />
  <searching>
    <search available="yes" supportedParams="q" />
    <tv-search available="yes" supportedParams="q,season,ep,tvdbid" />
    <movie-search available="yes" supportedParams="q,imdbid" />
    <music-search available="no" supportedParams="q" />
  </searching>
  <categories>
    <category id="2000" name="Movies"><subcat id="2040" name="Movies/HD" /></category>
    <category id="5000" name="TV"><subcat id="5040" name="TV/HD" /></category>
  </categories>
</caps>"""


def torznab_feed(items):
    body = []
    for it in items:
        body.append("""
    <item>
      <title>{title}</title>
      <guid>{base}/details/{n}</guid>
      <jackettindexer id="test">Test</jackettindexer>
      <comments>{base}/details/{n}</comments>
      <pubDate>Sat, 09 Nov 2024 10:00:00 +0100</pubDate>
      <size>{size}</size>
      <link>{base}/dl/{n}?jackett_apikey={key}&amp;file=x</link>
      <category>2000</category>
      <category>2040</category>
      <enclosure url="{base}/dl/{n}?jackett_apikey={key}&amp;file=x" length="{size}" type="application/x-bittorrent" />
      <torznab:attr name="category" value="2000" />
      <torznab:attr name="category" value="2040" />
      <torznab:attr name="seeders" value="{seeders}" />
      <torznab:attr name="peers" value="{peers}" />
      <torznab:attr name="infohash" value="{hash}" />
      <torznab:attr name="magneturl" value="magnet:?xt=urn:btih:{hash}&amp;dn=x" />
      <torznab:attr name="downloadvolumefactor" value="0" />
    </item>""".format(key=API_KEY, **it))
    return """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:atom="http://www.w3.org/2005/Atom" xmlns:torznab="http://torznab.com/schemas/2015/feed">
  <channel>
    <atom:link href="http://127.0.0.1/api" rel="self" type="application/rss+xml" />
    <title>Test</title>
    <description>Test torznab</description>
    <link>http://127.0.0.1/</link>
    {items}
  </channel>
</rss>""".format(items="".join(body))


def make_torrent_bytes(name="Night.of.the.Living.Dead.1968.1080p.BluRay.x264-GRP"):
    info = {"name": name, "piece length": 262144, "pieces": hashlib.sha1(b"a").digest(), "length": 1234567890}
    meta = {"announce": "http://tracker.example/announce", "info": info}
    return bencode(meta), hashlib.sha1(bencode(info)).hexdigest()


def free_port_url():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return "http://127.0.0.1:%d" % port


class TorznabServer:
    """Fake Jackett/Prowlarr. api_path: path where the API answers."""

    def __init__(self, api_path="/api", items=None, require_key=True, caps=True, empty_for=()):
        self.api_path = api_path
        self.items = items
        self.require_key = require_key
        self.caps = caps
        self.empty_for = empty_for
        self.server = FakeServer(self.handle)

    def handle(self, req):
        base = self.server.url
        if req.path.startswith("/dl/"):
            n = req.path.rsplit("/", 1)[-1]
            if n == "magnet":
                return 302, {"Location": "magnet:?xt=urn:btih:%s&dn=Redirected" % HASH}, b""
            data, _ = make_torrent_bytes()
            return 200, {"Content-Type": "application/x-bittorrent"}, data
        if req.path.rstrip("/") != self.api_path.rstrip("/"):
            return 404, {"Content-Type": "text/html"}, "<html><body>Not found</body></html>"
        if self.require_key and req.query.get("apikey") != API_KEY:
            return 200, XML, '<?xml version="1.0" encoding="UTF-8"?><error code="100" description="Invalid API Key" />'
        t = req.query.get("t")
        if t == "caps":
            if not self.caps:
                return 200, XML, '<error code="203" description="Function not available" />'
            return 200, XML, CAPS_XML
        if t in self.empty_for:
            return 200, XML, torznab_feed([])
        items = self.items
        if items is None:
            items = [
                {"title": "Night.of.the.Living.Dead.1968.1080p.BluRay.x264-GRP", "n": 1, "size": 1500000000,
                 "seeders": 12, "peers": 15, "hash": HASH, "base": base},
                {"title": "Night of the Living Dead 1990 720p BluRay", "n": 2, "size": 900000000,
                 "seeders": 3, "peers": 3, "hash": "b" * 40, "base": base},
            ]
        else:
            items = [dict(it, base=base) for it in items]
        return 200, XML, torznab_feed(items)

    def __enter__(self):
        self.server.__enter__()
        return self

    def __exit__(self, *exc):
        self.server.__exit__(*exc)

    @property
    def url(self):
        return self.server.url

    @property
    def requests(self):
        return self.server.requests


def http_client():
    return HttpClient(timeout=5, retries=0)


MOVIE = SearchRequest(kind="movie", title="Night of the Living Dead", year=1968, imdb="tt0063350")


class TorznabTests(unittest.TestCase):
    def make(self, url, **kw):
        row = {"id": 1, "name": "Jackett", "type": "torznab", "url": url, "api_key": API_KEY, "categories": ""}
        row.update(kw)
        return create_indexer(row, http_client())

    def test_endpoint_candidates(self):
        self.assertEqual(endpoint_candidates("http://h:9696/1/api"), ["http://h:9696/1/api"])
        self.assertEqual(endpoint_candidates("http://h:9696/1/api/"), ["http://h:9696/1/api/"])
        self.assertEqual(endpoint_candidates("http://h:9117/api/v2.0/indexers/all/results/torznab/"),
                         ["http://h:9117/api/v2.0/indexers/all/results/torznab/",
                          "http://h:9117/api/v2.0/indexers/all/results/torznab/api"])
        self.assertEqual(endpoint_candidates("http://h/torznab?x=1"),
                         ["http://h/torznab?x=1", "http://h/torznab/api?x=1"])

    def test_url_without_api_suffix(self):
        with TorznabServer(api_path="/torznab/api") as srv:
            idx = self.make(srv.url + "/torznab")
            self.assertIsInstance(idx, TorznabIndexer)
            results = idx.search(MOVIE)
            self.assertEqual(len(results), 2)
            r = results[0]
            self.assertEqual(r.title, "Night.of.the.Living.Dead.1968.1080p.BluRay.x264-GRP")
            self.assertEqual(r.indexer, "Jackett")
            self.assertTrue(r.download_url.startswith(srv.url + "/dl/1"))
            self.assertTrue(r.magnet.startswith("magnet:?xt=urn:btih:"))
            self.assertEqual(r.size, 1500000000)
            self.assertEqual(r.seeders, 12)
            self.assertEqual(r.leechers, 3)
            self.assertEqual(r.info_hash, HASH)
            self.assertEqual(r.info_url, srv.url + "/details/1")
            self.assertIn(2000, r.categories)
            # the movie search used imdbid without "tt", the api key and the movie category
            searches = [q.query for q in srv.requests if q.query.get("t") == "movie"]
            self.assertEqual(searches[0]["imdbid"], "0063350")
            self.assertNotIn("q", searches[0])
            self.assertEqual(searches[0]["apikey"], API_KEY)
            self.assertEqual(searches[0]["cat"], "2000")
            self.assertTrue(all(q.path == "/torznab/api" for q in srv.requests if q.query.get("t") == "movie"))
            # second search reuses the endpoint that worked: no more request to /torznab
            n = len(srv.requests)
            idx.search(MOVIE)
            self.assertTrue(all(q.path == "/torznab/api" for q in srv.requests[n:]))

    def test_url_as_is_works(self):
        with TorznabServer(api_path="/api/v2.0/indexers/all/results/torznab") as srv:
            idx = self.make(srv.url + "/api/v2.0/indexers/all/results/torznab/")
            self.assertEqual(len(idx.search(MOVIE)), 2)
            self.assertTrue(all(q.path.rstrip("/").endswith("/torznab") for q in srv.requests))

    def test_url_with_api_suffix_not_doubled(self):
        with TorznabServer(api_path="/1/api") as srv:
            idx = self.make(srv.url + "/1/api")
            self.assertEqual(len(idx.search(MOVIE)), 2)
            self.assertTrue(all(q.path == "/1/api" for q in srv.requests))

    def test_imdb_fallback_to_query(self):
        with TorznabServer(empty_for=()) as srv:
            calls = []
            orig = srv.handle

            def handle(req):
                calls.append(dict(req.query))
                if req.query.get("t") == "movie" and "imdbid" in req.query:
                    return 200, XML, torznab_feed([])
                return orig(req)
            srv.server.handler = handle
            idx = self.make(srv.url + "/api")
            self.assertEqual(len(idx.search(MOVIE)), 2)
            movie_calls = [c for c in calls if c.get("t") == "movie"]
            self.assertEqual(len(movie_calls), 2)
            self.assertEqual(movie_calls[1]["q"], "Night of the Living Dead 1968")

    def test_unrelated_id_results_fall_back_to_query(self):
        # a tracker ignoring imdbid answers with its latest releases: the text search must still run
        with TorznabServer(empty_for=()) as srv:
            orig = srv.handle

            def handle(req):
                if req.query.get("t") == "movie" and "imdbid" in req.query:
                    return 200, XML, torznab_feed([{"title": "Some.Other.Movie.2023.1080p.WEB", "n": 9,
                                                    "size": 1000, "seeders": 1, "peers": 1, "hash": "c" * 40,
                                                    "base": srv.url}])
                return orig(req)
            srv.server.handler = handle
            idx = self.make(srv.url + "/api")
            titles = [r.title for r in idx.search(MOVIE)]
            self.assertIn("Night.of.the.Living.Dead.1968.1080p.BluRay.x264-GRP", titles)
            self.assertEqual(len([q for q in srv.requests if q.query.get("t") == "movie"]), 2)

    def test_fallback_to_generic_search(self):
        with TorznabServer(empty_for=("movie",)) as srv:
            idx = self.make(srv.url + "/api")
            self.assertEqual(len(idx.search(MOVIE)), 2)
            generic = [q.query for q in srv.requests if q.query.get("t") == "search"]
            self.assertEqual(generic[0]["q"], "Night of the Living Dead 1968")

    def test_no_caps_still_searches(self):
        with TorznabServer(caps=False) as srv:
            idx = self.make(srv.url + "/api")
            self.assertEqual(len(idx.search(MOVIE)), 2)
            first = [q.query for q in srv.requests if q.query.get("t") not in ("caps",)][0]
            self.assertEqual(first["t"], "movie")
            self.assertEqual(first["q"], "Night of the Living Dead 1968")  # no imdbid without caps

    def test_tv_search_params(self):
        items = [{"title": "The.Last.of.Us.S01E03.1080p.WEB-DL.x264-GRP", "n": 3, "size": 2000000000,
                  "seeders": 100, "peers": 120, "hash": HASH}]
        with TorznabServer(items=items) as srv:
            idx = self.make(srv.url + "/api", categories="2000,5040,5000")
            req = SearchRequest(kind="episode", title="The Last of Us", season=1, episode=3, tvdb="392256")
            self.assertEqual(len(idx.search(req)), 1)
            q = [r.query for r in srv.requests if r.query.get("t") == "tvsearch"][0]
            self.assertEqual(q["tvdbid"], "392256")
            self.assertEqual(q["season"], "1")
            self.assertEqual(q["ep"], "3")
            self.assertEqual(q["cat"], "5040,5000")

    def test_tv_search_by_query(self):
        with TorznabServer(items=[]) as srv:
            idx = self.make(srv.url + "/api")
            req = SearchRequest(kind="season", title="The Last of Us", season=2)
            self.assertEqual(idx.search(req), [])
            tv = [r.query for r in srv.requests if r.query.get("t") == "tvsearch"]
            self.assertEqual(tv[0]["q"], "The Last of Us")
            self.assertEqual(tv[0]["season"], "2")
            self.assertNotIn("ep", tv[0])
            self.assertEqual(tv[0]["cat"], "5000")
            generic = [r.query for r in srv.requests if r.query.get("t") == "search"]
            self.assertEqual(generic[0]["q"], "The Last of Us S02")

    def test_season_discovery_without_season(self):
        with TorznabServer(items=[]) as srv:
            idx = self.make(srv.url + "/api")
            idx.search(SearchRequest(kind="season", title="The Last of Us"))
            tv = [r.query for r in srv.requests if r.query.get("t") == "tvsearch"]
            self.assertEqual(len(tv), 1)
            self.assertEqual(tv[0]["q"], "The Last of Us")
            self.assertNotIn("season", tv[0])
            self.assertNotIn("ep", tv[0])

    def test_invalid_api_key(self):
        with TorznabServer() as srv:
            idx = self.make(srv.url + "/api", api_key="wrongkey1234")
            with self.assertLogs("synoarr.indexers.base", level="WARNING") as logs:
                self.assertEqual(idx.search(MOVIE), [])
            text = "\n".join(logs.output)
            self.assertIn("100", text)
            self.assertNotIn("wrongkey1234", text)
            ok, msg = idx.test()
            self.assertFalse(ok)
            self.assertIn("clé API", msg)
            self.assertNotIn("wrongkey1234", msg)

    def test_test_ok(self):
        with TorznabServer(api_path="/x/api") as srv:
            ok, msg = self.make(srv.url + "/x").test()
            self.assertTrue(ok, msg)
            self.assertIn("2 catégories", msg)

    def test_html_page_returns_empty(self):
        def handler(req):
            return 200, {"Content-Type": "text/html"}, "<!DOCTYPE html><html><body>Login</body></html>"
        with FakeServer(handler) as srv:
            idx = self.make(srv.url + "/api")
            with self.assertLogs("synoarr.indexers.base", level="WARNING"):
                self.assertEqual(idx.search(MOVIE), [])
            self.assertFalse(idx.test()[0])

    def test_server_error_returns_empty(self):
        with FakeServer(lambda req: (500, {}, b"boom")) as srv:
            idx = self.make(srv.url + "/api")
            with self.assertLogs("synoarr.indexers.base", level="WARNING"):
                self.assertEqual(idx.search(MOVIE), [])

    def test_connection_refused_returns_empty(self):
        idx = self.make(free_port_url() + "/api")
        with self.assertLogs("synoarr.indexers.base", level="WARNING") as logs:
            self.assertEqual(idx.search(MOVIE), [])
        self.assertNotIn(API_KEY, "\n".join(logs.output))
        ok, msg = idx.test()
        self.assertFalse(ok)
        self.assertNotIn(API_KEY, msg)

    def test_garbage_items_skipped(self):
        feed = """<?xml version="1.0"?><rss xmlns:torznab="http://torznab.com/schemas/2015/feed"><channel>
          <item><title></title><link>http://x/1</link></item>
          <item><title>No link</title></item>
          <item><title>Only magnet</title><link>magnet:?xt=urn:btih:%s</link>
             <torznab:attr name="seeders" value="abc"/><torznab:attr name="size" value=""/></item>
        </channel></rss>""" % HASH
        with FakeServer(lambda req: (200, XML, feed)) as srv:
            res = self.make(srv.url + "/api").search(SearchRequest(kind="movie", title="Only magnet"))
            self.assertEqual(len(res), 1)
            self.assertEqual(res[0].magnet, "magnet:?xt=urn:btih:%s" % HASH)
            self.assertIsNone(res[0].download_url)
            self.assertEqual(res[0].info_hash, HASH)
            self.assertIsNone(res[0].seeders)
            self.assertEqual(res[0].size, 0)

    def test_download_torrent_and_magnet_redirect(self):
        with TorznabServer() as srv:
            http = http_client()
            # .torrent link -> real bencoded file
            resp = http.get(srv.url + "/dl/1?jackett_apikey=x")
            name, info_hash = torrent_info(resp.body)
            self.assertEqual(name, "Night.of.the.Living.Dead.1968.1080p.BluRay.x264-GRP")
            self.assertEqual(info_hash, make_torrent_bytes()[1])
            # Jackett/Prowlarr links that redirect to a magnet
            resp = http.get(srv.url + "/dl/magnet")
            self.assertTrue(resp.redirect_location and resp.redirect_location.startswith("magnet:"))
            self.assertEqual(magnet_info(resp.redirect_location), ("Redirected", HASH))


RSS_EZRSS = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:torrent="http://xmlns.ezrss.it/0.1/">
<channel>
  <title>Public domain torrents</title>
  <link>{base}/</link>
  <item>
    <title>Nosferatu 1922 720p BluRay x264</title>
    <link>{base}/details/10</link>
    <enclosure url="/files/nosferatu.torrent" length="734003200" type="application/x-bittorrent"/>
    <pubDate>Mon, 01 Jan 2024 00:00:00 +0000</pubDate>
    <torrent:contentLength>734003200</torrent:contentLength>
    <torrent:infoHash>{hash_upper}</torrent:infoHash>
    <torrent:seeds>42</torrent:seeds>
    <torrent:peers>50</torrent:peers>
  </item>
  <item>
    <title>The Cabinet of Dr Caligari 1920 1080p</title>
    <link>magnet:?xt=urn:btih:{hash}&amp;dn=Caligari</link>
  </item>
</channel>
</rss>"""

RSS_NYAA = """<?xml version="1.0" encoding="UTF-8"?>
<rss xmlns:atom="http://www.w3.org/2005/Atom" xmlns:nyaa="https://nyaa.si/xmlns/nyaa" version="2.0">
<channel><title>Feed</title>
  <item>
    <title>Nosferatu (1922) [1080p] [MULTI]</title>
    <link>{base}/download/1.torrent</link>
    <guid isPermaLink="true">{base}/view/1</guid>
    <nyaa:seeders>7</nyaa:seeders>
    <nyaa:leechers>1</nyaa:leechers>
    <nyaa:infoHash>{hash}</nyaa:infoHash>
    <nyaa:size>1.5 GiB</nyaa:size>
  </item>
</channel></rss>"""

ATOM = """<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>Atom torrents</title>
  <entry>
    <title type="text">Nosferatu.1922.1080p.BluRay.x264-GRP</title>
    <id>urn:uuid:1</id>
    <updated>2024-01-01T00:00:00Z</updated>
    <link rel="alternate" href="{base}/page/1"/>
    <link rel="enclosure" type="application/x-bittorrent" length="2000000000" href="{base}/get/1.torrent"/>
  </entry>
  <entry>
    <title>Metropolis.1927.1080p.BluRay.x264-GRP</title>
    <link rel="enclosure" href="{base}/get/2.torrent"/>
  </entry>
</feed>"""

NOSFERATU = SearchRequest(kind="movie", title="Nosferatu", year=1922)


class RssTests(unittest.TestCase):
    def serve(self, template):
        holder = {}

        def handler(req):
            if req.path == "/feed":
                return 200, XML, template.format(base=holder["base"], hash=HASH, hash_upper=HASH.upper())
            return 404, {}, b""
        srv = FakeServer(handler)
        holder["base"] = srv.url
        return srv

    def make(self, url):
        return create_indexer({"id": 2, "name": "Flux", "type": "rss", "url": url}, http_client())

    def test_ezrss_feed(self):
        with self.serve(RSS_EZRSS) as srv:
            idx = self.make(srv.url + "/feed")
            self.assertIsInstance(idx, RssIndexer)
            res = idx.search(NOSFERATU)
            self.assertEqual(len(res), 1)  # Caligari filtered out
            r = res[0]
            self.assertEqual(r.download_url, srv.url + "/files/nosferatu.torrent")  # relative URL resolved
            self.assertEqual(r.size, 734003200)
            self.assertEqual(r.info_hash, HASH)
            self.assertEqual(r.seeders, 42)
            self.assertEqual(r.leechers, 8)
            self.assertEqual(r.info_url, srv.url + "/details/10")
            cal = idx.search(SearchRequest(kind="movie", title="The Cabinet of Dr. Caligari", year=1920))
            self.assertEqual(len(cal), 1)
            self.assertIsNone(cal[0].download_url)
            self.assertEqual(cal[0].info_hash, HASH)
            ok, msg = idx.test()
            self.assertTrue(ok)
            self.assertIn("2", msg)

    def test_nyaa_feed(self):
        with self.serve(RSS_NYAA) as srv:
            res = self.make(srv.url + "/feed").search(NOSFERATU)
            self.assertEqual(len(res), 1)
            self.assertEqual(res[0].size, int(1.5 * 1024 ** 3))
            self.assertEqual(res[0].seeders, 7)
            self.assertEqual(res[0].leechers, 1)
            self.assertEqual(res[0].download_url, srv.url + "/download/1.torrent")
            self.assertEqual(res[0].info_url, srv.url + "/view/1")

    def test_atom_feed(self):
        with self.serve(ATOM) as srv:
            res = self.make(srv.url + "/feed").search(NOSFERATU)
            self.assertEqual(len(res), 1)
            self.assertEqual(res[0].download_url, srv.url + "/get/1.torrent")
            self.assertEqual(res[0].size, 2000000000)
            self.assertEqual(res[0].info_url, srv.url + "/page/1")

    def test_broken_feeds(self):
        with self.serve(RSS_EZRSS) as srv:
            idx = self.make(srv.url + "/missing")
            with self.assertLogs("synoarr.indexers.base", level="WARNING"):
                self.assertEqual(idx.search(NOSFERATU), [])
            self.assertFalse(idx.test()[0])
        for body in ("<rss><channel><item><title>x</title>", "not xml at all", "{\"json\": true}",
                     "<!DOCTYPE html><html></html>",
                     '<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "aaaa">]><rss>&a;</rss>'):
            with FakeServer(lambda req, b=body: (200, XML, b)) as srv:
                with self.assertLogs("synoarr.indexers.base", level="WARNING"):
                    self.assertEqual(self.make(srv.url + "/feed").search(NOSFERATU), [])


IA_JSON = {
    "responseHeader": {"status": 0, "QTime": 12, "params": {}},
    "response": {"numFound": 4, "start": 0, "docs": [
        {"identifier": "night_of_the_living_dead", "title": "Night of the Living Dead", "year": "1968",
         "item_size": 1395864371, "downloads": 1234567},
        {"identifier": "NightOfTheLivingDead1968", "title": ["Night Of The Living Dead (1968)"], "year": 1968,
         "item_size": "950000000", "downloads": 5000},
        {"identifier": "night_of_the_living_dead_1990", "title": "Night of the Living Dead: Reanimated",
         "downloads": 10},
        {"identifier": "no_title"},
    ]},
}


class InternetArchiveTests(unittest.TestCase):
    def test_search_and_urls(self):
        def handler(req):
            if req.path == "/advancedsearch.php":
                return 200, {}, IA_JSON
            return 404, {}, b""
        with FakeServer(handler) as srv:
            idx = create_indexer({"id": 3, "name": "Internet Archive", "type": "internet_archive",
                                  "url": srv.url + "/"}, http_client())
            self.assertIsInstance(idx, InternetArchiveIndexer)
            res = idx.search(SearchRequest(kind="movie", title="Night of the Living Dead", year=1968))
            self.assertEqual(len(res), 2)
            r = res[0]
            self.assertEqual(r.title, "Night of the Living Dead (1968)")
            self.assertEqual(r.download_url,
                             srv.url + "/download/night_of_the_living_dead/night_of_the_living_dead_archive.torrent")
            self.assertEqual(r.info_url, srv.url + "/details/night_of_the_living_dead")
            self.assertIsNone(r.seeders)
            self.assertEqual(r.size, 1395864371)
            self.assertEqual(r.indexer, "Internet Archive")
            self.assertEqual(res[1].title, "Night Of The Living Dead (1968)")  # year not duplicated
            self.assertEqual(res[1].size, 950000000)
            # request
            req = srv.requests[0]
            self.assertEqual(req.query["q"], "title:(Night of the Living Dead) AND mediatype:(movies)")
            self.assertEqual(req.query["output"], "json")
            self.assertEqual(req.query["rows"], "50")
            self.assertIn("fl[]=identifier", req.raw_path.replace("%5B%5D", "[]"))
            for field in ("title", "year", "item_size", "downloads"):
                self.assertIn("fl[]=" + field, req.raw_path.replace("%5B%5D", "[]"))
            ok, msg = idx.test()
            self.assertTrue(ok, msg)

    def test_query_escaping(self):
        idx = InternetArchiveIndexer({"name": "IA", "type": "internet_archive", "url": "https://archive.org"},
                                     http_client())
        q = idx.build_query(SearchRequest(kind="movie", title='Dr. Strangelove: or How I "Learned" (AND) Stop'))
        self.assertEqual(q, "title:(Dr. Strangelove or How I Learned and Stop) AND mediatype:(movies)")
        self.assertEqual(idx.torrent_url("a b"), "https://archive.org/download/a%20b/a%20b_archive.torrent")

    def test_default_url(self):
        idx = InternetArchiveIndexer({"name": "IA", "type": "internet_archive", "url": ""}, http_client())
        self.assertEqual(idx.search_url(), "https://archive.org/advancedsearch.php")

    def test_errors(self):
        for status, body in ((200, "<html>maintenance</html>"), (503, "down"), (200, {"error": "x"})):
            with FakeServer(lambda req, s=status, b=body: (s, {}, b)) as srv:
                idx = create_indexer({"name": "IA", "type": "internet_archive", "url": srv.url}, http_client())
                with self.assertLogs("synoarr.indexers.base", level="WARNING"):
                    self.assertEqual(idx.search(MOVIE), [])
                self.assertFalse(idx.test()[0])


class DetectAndFactoryTests(unittest.TestCase):
    def test_archive_org_without_network(self):
        http = HttpClient(timeout=0.1, retries=0)
        self.assertEqual(detect_type("https://archive.org", "", http), "internet_archive")
        self.assertEqual(detect_type("https://www.archive.org/", "", http), "internet_archive")
        self.assertEqual(detect_type("archive.org", "", http), "internet_archive")

    def test_torznab(self):
        with TorznabServer(api_path="/api") as srv:
            self.assertEqual(detect_type(srv.url + "/api", API_KEY, http_client()), "torznab")
            self.assertEqual(detect_type(srv.url, API_KEY, http_client()), "torznab")  # /api appended
            self.assertEqual(detect_type(srv.url, "badkey", http_client()), "torznab")  # error element

    def test_rss(self):
        def handler(req):
            if req.path == "/feed" and "t" not in req.query:
                return 200, XML, ATOM.format(base="http://x")
            return 404, {}, b"not found"
        with FakeServer(handler) as srv:
            self.assertEqual(detect_type(srv.url + "/feed", "", http_client()), "rss")

    def test_rss_feed_ignoring_query_params(self):
        # many feeds ignore unknown parameters and return the feed for ?t=caps too
        with FakeServer(lambda req: (200, XML, RSS_EZRSS.format(base="http://x", hash=HASH, hash_upper=HASH))) as srv:
            self.assertEqual(detect_type(srv.url + "/rss?passkey=abc", "", http_client()), "rss")

    def test_unknown(self):
        with FakeServer(lambda req: (200, {"Content-Type": "text/html"}, "<html><body>hi</body></html>")) as srv:
            with self.assertRaises(ValueError) as ctx:
                detect_type(srv.url, "mykey12345", http_client())
            self.assertIn("Impossible", str(ctx.exception))
            self.assertNotIn("mykey12345", str(ctx.exception))
        with self.assertRaises(ValueError):
            detect_type(free_port_url() + "/api", "", http_client())
        with self.assertRaises(ValueError):
            detect_type("", "", http_client())
        with self.assertRaises(ValueError):
            detect_type("ftp://example.org/feed", "", http_client())

    def test_create_indexer(self):
        http = http_client()
        self.assertIsInstance(create_indexer({"name": "a", "type": "TORZNAB", "url": "http://x"}, http), Indexer)
        with self.assertRaises(ValueError):
            create_indexer({"name": "a", "type": "newznab", "url": "http://x"}, http)
        idx = create_indexer({"name": "a", "type": "rss", "url": "http://x", "categories": "2000, 5000;5040",
                              "priority": None, "enabled": 0}, http)
        self.assertEqual(idx.categories, [2000, 5000, 5040])
        self.assertEqual(idx.priority, 25)
        self.assertFalse(idx.enabled)

    def test_catalog_is_legal_and_valid(self):
        self.assertTrue(CATALOG)
        ids = set()
        for entry in CATALOG:
            for key in ("id", "name", "type", "url", "description", "categories"):
                self.assertIn(key, entry)
            self.assertIn(entry["type"], ("torznab", "rss", "internet_archive"))
            self.assertNotIn(entry["id"], ids)
            ids.add(entry["id"])
        ia = [e for e in CATALOG if e["type"] == "internet_archive"]
        self.assertEqual(ia[0]["url"], "https://archive.org")
        self.assertIsInstance(create_indexer(dict(ia[0]), http_client()), InternetArchiveIndexer)

    def test_helpers(self):
        self.assertEqual(parse_size("700 MB"), 700000000)
        self.assertEqual(parse_size("1,5 GiB"), int(1.5 * 1024 ** 3))
        self.assertEqual(parse_size("12345"), 12345)
        self.assertEqual(parse_size("big"), 0)
        self.assertEqual(parse_size(None), 0)
        self.assertEqual(parse_categories("2000,abc, 5000,2000"), [2000, 5000])
        self.assertEqual(parse_categories(None), [])

    def test_clean_query(self):
        self.assertEqual(clean_query("Ocean's Eleven 2001"), "Oceans Eleven 2001")
        self.assertEqual(clean_query("Les Misérables"), "Les Miserables")
        self.assertEqual(clean_query("Spider-Man: No Way Home"), "Spider-Man No Way Home")
        self.assertEqual(clean_query("Fast & Furious"), "Fast and Furious")
        self.assertEqual(clean_query("The Office (US) S01E02"), "The Office US S01E02")


if __name__ == "__main__":
    unittest.main()
