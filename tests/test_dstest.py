"""« Test d'envoi Download Station » and magnet encoding repair."""
from __future__ import annotations

import os
import unittest

from synoarr import dstest
from synoarr.bencode import bencode, magnet_info, normalize_magnet, torrent_info
from synoarr.httpclient import HttpClient
from synoarr.models import DSTask
from tests.fakes import FakeServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
H = "a" * 40


def sample_torrent(name="ubuntu-26.04.1-live-server-amd64.iso"):
    info = {b"name": name.encode(), b"piece length": 16384, b"pieces": b"\0" * 20, b"length": 10}
    return bencode({b"announce": b"https://torrent.ubuntu.com/announce",
                    b"announce-list": [[b"https://torrent.ubuntu.com/announce"], [b"udp://t2:6969/announce"]],
                    b"info": info})


class FakeDS:
    """Download Station that starts (or not) depending on the method."""

    def __init__(self, working=("file", "url", "magnet")):
        self.working = set(working)
        self.tasks = []
        self.deleted = []
        self.added = []

    def _task(self, method, title, info_hash=None):
        ok = method in self.working
        self.tasks.append(DSTask(id="t%d" % len(self.added), title=title, status="downloading" if ok else "waiting",
                                 downloaded=500 if ok else 0, peers=12 if ok else 0, info_hash=info_hash))

    def add_torrent_file(self, data, filename):
        self.added.append(("file", data))
        self._task("file", torrent_info(data)[0])
        return "DownloadStation2"

    def add_url(self, url):
        method = "magnet" if url.startswith("magnet:") else "url"
        self.added.append((method, url))
        name, h = magnet_info(url) if method == "magnet" else ("ubuntu-26.04.1-live-server-amd64.iso", None)
        self._task(method, name if method == "url" else (h or ""), h)
        return "DownloadStation2"

    def list_tasks(self):
        return list(self.tasks)

    def delete_task(self, tid):
        self.deleted.append(tid)
        self.tasks = [t for t in self.tasks if t.id != tid]


class DsTestTests(unittest.TestCase):
    def setUp(self):
        self.data = sample_torrent()

        def handler(req):
            if req.path == "/":
                return 200, {"Content-Type": "text/html"}, '<a href="24.04/">24.04/</a><a href="26.04/">26.04/</a>'
            if req.path == "/26.04/":
                return 200, {"Content-Type": "text/html"}, (
                    '<a href="ubuntu-26.04-desktop-amd64.iso.torrent">x</a>'
                    '<a href="ubuntu-26.04.1-live-server-amd64.iso.torrent">y</a>'
                    '<a href="ubuntu-26.04-live-server-amd64.iso.torrent">z</a>')
            if req.path.endswith(".torrent"):
                return 200, {"Content-Type": "application/x-bittorrent"}, self.data
            return 404, {}, b""
        self.srv = FakeServer(handler).__enter__()
        self.addCleanup(self.srv.__exit__, None, None, None)
        self.http = HttpClient(retries=0)
        self.lines = []

    def emit(self, level, msg):
        self.lines.append((level, msg))

    def run_test(self, ds):
        return dstest.run(ds, self.http, self.emit, torrent_url="", wait=1, poll=0.01, sleep=lambda s: None) \
            if False else dstest.run(ds, self.http, self.emit, torrent_url=self.found, wait=1, poll=0.01,
                                     sleep=lambda s: None)

    def test_find_latest_server_torrent(self):
        url = dstest.find_ubuntu_torrent(self.http, self.srv.url + "/")
        self.assertTrue(url.endswith("/26.04/ubuntu-26.04.1-live-server-amd64.iso.torrent"), url)

    def test_magnet_from_torrent_keeps_hash_name_trackers(self):
        m = dstest.magnet_from_torrent(self.data)
        name, h = magnet_info(m)
        self.assertEqual((name, h), torrent_info(self.data))
        self.assertEqual(m.count("&tr="), 2)

    def test_all_methods_work(self):
        self.found = dstest.find_ubuntu_torrent(self.http, self.srv.url + "/")
        ds = FakeDS()
        res = self.run_test(ds)
        self.assertEqual(res["working"], ["file", "url", "magnet"])
        self.assertEqual(len(ds.deleted), 3)  # no test task left behind
        self.assertEqual(ds.added[0], ("file", self.data))  # original bytes
        self.assertTrue(any("Tout fonctionne" in m for _l, m in self.lines))

    def test_magnets_do_not_start(self):
        self.found = dstest.find_ubuntu_torrent(self.http, self.srv.url + "/")
        res = self.run_test(FakeDS(working=("file", "url")))
        self.assertEqual(res["working"], ["file", "url"])
        self.assertTrue(any("DHT" in m for _l, m in self.lines))

    def test_nothing_starts(self):
        self.found = dstest.find_ubuntu_torrent(self.http, self.srv.url + "/")
        res = self.run_test(FakeDS(working=()))
        self.assertEqual(res["working"], [])
        self.assertTrue(any("réseau BitTorrent" in m for _l, m in self.lines))


class NormalizeMagnetTests(unittest.TestCase):
    def test_clean_magnet_untouched(self):
        m = "magnet:?xt=urn:btih:%s&dn=Night%%20of%%20the%%20Living%%20Dead&tr=udp%%3A%%2F%%2Ft%%3A1" % H
        self.assertEqual(normalize_magnet(m), (m, None))

    def test_html_entities_and_double_encoding_repaired(self):
        bad = ("magnet:?xt=urn:btih:%s&amp;dn=Night%%2520of%%2520the%%2520Living%%2520Dead"
               "&amp;tr=udp%%253A%%252F%%252Ft%%253A1" % H)
        fixed, reason = normalize_magnet(bad)
        self.assertIsNotNone(reason)
        self.assertEqual(magnet_info(fixed), ("Night of the Living Dead", H))
        self.assertIn("tr=udp%3A%2F%2Ft%3A1", fixed)

    def test_attached_internet_archive_torrent_parses(self):
        path = "/root/.claude/uploads/0c811b68-94dd-5139-a96c-735babe14f29/26f83e11-NightOfTheLivingDead-MPEG.torrent"
        if not os.path.exists(path):
            self.skipTest("fichier d'exemple absent")
        with open(path, "rb") as fh:
            data = fh.read()
        self.assertEqual(torrent_info(data)[0], "NightOfTheLivingDead-MPEG")
        self.assertIn("NightOfTheLivingDead-MPEG", dstest.magnet_from_torrent(data))


if __name__ == "__main__":
    unittest.main()
