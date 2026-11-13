from __future__ import annotations

import json
import logging
import unittest

from synoarr.downloadstation import DownloadStation, DownloadStationError, map_status, normalize_destination
from synoarr.httpclient import HttpClient
from tests.fakes import FakeServer



def setUpModule():
    logging.disable(logging.CRITICAL)


def tearDownModule():
    logging.disable(logging.NOTSET)

TORRENT = b"d8:announce30:http://tracker.example/announce4:infod4:name8:file.mkv12:piece lengthi16384ee"

ALL_APIS = {
    "SYNO.API.Auth": {"maxVersion": 7, "minVersion": 1, "path": "entry.cgi"},
    "SYNO.API.Info": {"maxVersion": 1, "minVersion": 1, "path": "query.cgi"},
    "SYNO.DownloadStation.Task": {"maxVersion": 3, "minVersion": 1, "path": "DownloadStation/task.cgi"},
    "SYNO.DownloadStation2.Task": {"maxVersion": 2, "minVersion": 1, "path": "entry.cgi", "requestFormat": "JSON"},
    "SYNO.DownloadStation.Info": {"maxVersion": 2, "minVersion": 1, "path": "DownloadStation/info.cgi"},
}


def dsm_value(raw):
    """How DSM's entry.cgi reads a parameter: JSON first, else the raw string."""
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return raw


def ok(data=None):
    out = {"success": True}
    if data is not None:
        out["data"] = data
    return 200, {"Content-Type": "application/json"}, out


def err(code):
    return 200, {"Content-Type": "application/json"}, {"success": False, "error": {"code": code}}


class FakeDSM:
    """Minimal DSM 7 Web API simulation."""

    def __init__(self):
        self.apis = dict(ALL_APIS)
        self.password = "p@ss w0rd&é"
        self.otp = False
        self.query_broken = False
        self.sids = set()
        self.counter = 0
        self.expire_next = 0  # number of upcoming DS calls answered with 119
        self.ds2_create_error = None
        self.ds2_create_data = None  # e.g. {"list_id": ["btlist_1"], "task_id": []}
        self.default_destination = "Download"
        self.list_files = [{"index": 0, "name": "a.mkv", "size": 900}, {"index": 1, "name": "b.nfo", "size": 1}]
        self.list_calls = []  # (api, method, params) of SYNO.DownloadStation2.Task.List*
        self.shares = ("Download", "Download/synoarr", "Vidéos")
        self.created = []  # (api, params, files)
        self.deleted = []
        self.logins = []
        self.tasks = [
            {"id": "dbid_1", "title": "Show.S01E01.1080p.WEB.x264", "status": "finished", "size": "1500000000",
             "type": "bt", "username": "synoarr",
             "additional": {"detail": {"destination": "Download"}, "transfer": {"size_downloaded": 1500000000}}},
            {"id": "dbid_2", "title": "Movie.2020.720p", "status": 2, "size": 800, "type": "bt",
             "additional": {"detail": {"destination": "Download/synoarr"}, "transfer": {"size_downloaded": "400"}}},
            {"id": "dbid_3", "title": "Broken", "status": "error", "size": 0, "type": "bt",
             "status_extra": {"error_detail": "broken_link"}, "additional": {}},
        ]

    # ----------------------------------------------------------------- routing
    def __call__(self, req):
        p = req.params()
        if req.path == "/webapi/query.cgi":
            if self.query_broken:
                return 404, {}, b"<html>not found</html>"
            assert p["api"] == "SYNO.API.Info" and p["method"] == "query"
            return ok(self.apis)
        api = p.get("api", "")
        if req.path in ("/webapi/entry.cgi", "/webapi/auth.cgi") and api == "SYNO.API.Auth":
            return self.auth(req, p)
        info = self.apis.get(api)
        if info is None or req.path != "/webapi/" + info["path"]:
            return err(102)
        sid = p.get("_sid")
        if info["path"] == "entry.cgi" and req.method == "POST":
            sid = req.query.get("_sid")  # DS2 POST: sid expected in the query string
        if sid not in self.sids:
            return err(119)
        if self.expire_next:
            self.expire_next -= 1
            self.sids.discard(sid)
            return err(119)
        if api == "SYNO.DownloadStation2.Task":
            return self.ds2(req, p)
        if api.startswith("SYNO.DownloadStation2.Task.List"):
            self.list_calls.append((api, p["method"], p))
            if p["method"] == "get":
                if dsm_value(p.get("list_id")) != "btlist_1":
                    return err(120)
                return ok({"title": "X", "size": 901, "type": "bt", "files": self.list_files})
            if p["method"] == "download":
                return ok({"task_id": "dbid_99"})
            return err(103)
        if api == "SYNO.DownloadStation.Info" and p["method"] == "getconfig":
            return ok({"default_destination": self.default_destination, "emule_enabled": False})
        return self.legacy(req, p)

    def auth(self, req, p):
        self.logins.append((req.method, p.get("version")))
        if p.get("method") != "login" or p.get("format") != "sid" or p.get("session") != "DownloadStation":
            return err(101)
        if p.get("account") != "synoarr" or p.get("passwd") != self.password:
            return err(400)
        if self.otp:
            return err(403)
        self.counter += 1
        sid = "sid%d" % self.counter
        self.sids.add(sid)
        return ok({"sid": sid, "did": "x", "is_portal_port": False})

    def _check_dest(self, dest):
        return dest in self.shares

    def ds2(self, req, p):
        if p["method"] != "create" or p["version"] != "2":
            return err(103)
        if self.ds2_create_error:
            return err(self.ds2_create_error)
        if dsm_value(p.get("create_list")) is not False:
            return err(120)
        typ = dsm_value(p.get("type"))
        dest = dsm_value(p["destination"]) if "destination" in p else None
        if dest is not None and (not isinstance(dest, str) or not self._check_dest(dest)):
            return err(120)
        files = {k: v for k, v in req.multipart.items() if v[0]}
        if typ == "file":
            names = dsm_value(p["file"])
            if req.method != "POST" or not isinstance(names, list) or not names or names[0] not in files:
                return err(120)
        elif typ == "url":
            urls = dsm_value(p.get("url"))
            if not urls:
                return err(120)
        else:
            return err(120)
        self.created.append(("ds2", p, files))
        return ok(self.ds2_create_data or {"list_id": [], "task_id": ["synoarr/SYNODLTaskListDownload1"]})

    def legacy(self, req, p):
        method = p["method"]
        if method == "create":
            if req.method != "POST":
                return err(101)
            dest = p.get("destination")
            if dest is not None and not self._check_dest(dest):
                return err(403)
            files = {k: v for k, v in req.multipart.items() if v[0]}
            if not p.get("uri") and "file" not in files:
                return err(101)
            self.created.append(("legacy", p, files))
            return ok()
        if method == "list":
            assert p.get("additional") == "detail,transfer"
            return ok({"offset": 0, "total": len(self.tasks), "tasks": self.tasks})
        if method == "delete":
            if p.get("force_complete") != "false":
                return err(101)
            ids = p["id"].split(",")
            self.deleted.extend(ids)
            return ok([{"error": 0 if i.startswith("dbid_") else 404, "id": i} for i in ids])
        return err(103)


class DownloadStationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.holder = {}
        cls.srv = FakeServer(lambda req: cls.holder["dsm"](req)).__enter__()

    @classmethod
    def tearDownClass(cls):
        cls.srv.__exit__(None, None, None)

    def setUp(self):
        self.dsm = FakeDSM()
        self.holder["dsm"] = self.dsm
        del self.srv.requests[:]

    def ds(self, password=None, destination="/Download/synoarr/"):
        return DownloadStation(self.srv.url + "/", "synoarr", password or self.dsm.password, destination,
                               HttpClient(timeout=5, retries=0))

    # ----------------------------------------------------------------- helpers
    def test_helpers(self):
        self.assertEqual(normalize_destination("/volume1/Download/x/"), "Download/x")
        self.assertEqual(normalize_destination("Download"), "Download")
        self.assertEqual(normalize_destination(" /Vidéos// "), "Vidéos")
        self.assertEqual(map_status(5), "finished")
        self.assertEqual(map_status("8"), "filehosting_waiting")
        self.assertEqual(map_status(10), "error")
        self.assertEqual(map_status(105), "error")
        self.assertEqual(map_status("Seeding"), "seeding")

    # ----------------------------------------------------------------- login
    def test_login_discovery_and_version(self):
        ds = self.ds()
        ds.login()
        self.assertEqual(ds._sid, "sid1")
        self.assertEqual(self.dsm.logins, [("POST", "6")])
        # password never in a URL
        for r in self.srv.requests:
            self.assertNotIn("passwd", r.raw_path)

    def test_login_wrong_password(self):
        with self.assertRaises(DownloadStationError) as ctx:
            self.ds(password="bad").login()
        self.assertEqual(ctx.exception.code, 400)
        self.assertIn("incorrect", str(ctx.exception))

    def test_login_2fa(self):
        self.dsm.otp = True
        ok_, msg = self.ds().test()
        self.assertFalse(ok_)
        self.assertIn("2FA", msg)
        self.assertIn("sans 2FA", msg)

    def test_query_failure_uses_defaults(self):
        self.dsm.query_broken = True
        ds = self.ds()
        ds.login()  # default path auth.cgi, version 3
        self.assertEqual(self.dsm.logins, [("POST", "3")])
        self.assertTrue(any(r.path == "/webapi/auth.cgi" for r in self.srv.requests))

    def test_connection_refused(self):
        ds = DownloadStation("http://127.0.0.1:1", "synoarr", "x", "Download", HttpClient(timeout=2, retries=0))
        ok_, msg = ds.test()
        self.assertFalse(ok_)
        self.assertIn("Impossible de joindre", msg)

    def test_not_installed(self):
        del self.dsm.apis["SYNO.DownloadStation.Task"]
        del self.dsm.apis["SYNO.DownloadStation2.Task"]
        ok_, msg = self.ds().test()
        self.assertFalse(ok_)
        self.assertIn("Centre de paquets", msg)
        with self.assertRaises(DownloadStationError):
            self.ds().add_url("magnet:?xt=urn:btih:abc")

    # ----------------------------------------------------------------- create
    def test_add_torrent_file_ds2(self):
        ds = self.ds()
        ds.add_torrent_file(TORRENT, "My.Show.S01E01.torrent")
        self.assertEqual(len(self.dsm.created), 1)
        api, p, files = self.dsm.created[0]
        self.assertEqual(api, "ds2")
        self.assertEqual(p["type"], '"file"')
        self.assertEqual(p["file"], '["fileData"]')  # Prowlarr's field name
        self.assertEqual(p["destination"], '"Download/synoarr"')
        self.assertEqual(p["create_list"], "false")
        self.assertEqual(files["fileData"], ("My.Show.S01E01.torrent", TORRENT))
        req = [r for r in self.srv.requests if r.method == "POST" and r.query.get("_sid")][-1]
        self.assertEqual(req.path, "/webapi/entry.cgi")
        self.assertEqual(set(req.query), {"_sid"})
        self.assertIn("multipart/form-data", req.headers["content-type"])
        self.assertEqual(int(req.headers["content-length"]), len(req.body))
        # byte layout of the parts, like NzbDrone's HttpRequestBuilder (CRLF, octet-stream file part)
        boundary = req.headers["content-type"].split("boundary=", 1)[1].encode()
        self.assertIn(b'--' + boundary + b'\r\nContent-Disposition: form-data; name="create_list"\r\n\r\nfalse\r\n',
                      req.body)
        self.assertIn(b'Content-Disposition: form-data; name="fileData"; filename="My.Show.S01E01.torrent"\r\n'
                      b'Content-Type: application/octet-stream\r\n\r\n' + TORRENT + b'\r\n--' + boundary + b'--\r\n',
                      req.body)
        order = [n for n in ("api", "version", "method", "type", "file", "create_list", "destination", "fileData")]
        positions = [req.body.index(('name="%s"' % n).encode()) for n in order]
        self.assertEqual(positions, sorted(positions))
        self.assertEqual(ds.last_create["data"], {"list_id": [], "task_id": ["synoarr/SYNODLTaskListDownload1"]})
        self.assertEqual(self.dsm.list_calls, [])

    def test_add_url_ds2_like_prowlarr(self):
        ds = self.ds()
        magnet = "magnet:?xt=urn:btih:0123456789abcdef0123456789abcdef01234567&dn=A+B&tr=udp%3A%2F%2Ft.example%3A80"
        ds.add_url(magnet)
        api, p, _ = self.dsm.created[0]
        self.assertEqual(api, "ds2")
        req = [r for r in self.srv.requests if r.query.get("method") == "create"][-1]
        self.assertEqual(req.method, "GET")
        self.assertEqual(req.body, b"")
        self.assertEqual(req.query, {"_sid": "sid1", "api": "SYNO.DownloadStation2.Task", "version": "2",
                                     "method": "create", "type": "url", "url": magnet, "create_list": "false",
                                     "destination": "Download/synoarr"})
        self.assertTrue(req.raw_path.startswith("/webapi/entry.cgi?_sid=sid1&api=SYNO.DownloadStation2.Task&"))
        # percent-encoding of .NET's Uri.EscapeDataString: "+" -> %2B, ":" -> %3A, "/" -> %2F, never "+"
        self.assertIn("&url=magnet%3A%3Fxt%3Durn%3Abtih%3A0123456789abcdef0123456789abcdef01234567%26dn%3DA%2BB%26"
                      "tr%3Dudp%253A%252F%252Ft.example%253A80&create_list=false&destination=Download%2Fsynoarr",
                      req.raw_path)
        ds.add_url("magnet:?xt=urn:btih:abc&dn=A B")
        self.assertIn("dn%3DA%20B", [r for r in self.srv.requests if r.query.get("method") == "create"][-1].raw_path)

    def test_add_url_very_long_magnet_is_posted(self):
        ds = self.ds()
        magnet = "magnet:?xt=urn:btih:0123456789abcdef0123456789abcdef01234567" + "".join(
            "&tr=udp%%3A%%2F%%2Ftracker%d.example.org%%3A1337%%2Fannounce" % i for i in range(150))
        ds.add_url(magnet)
        req = [r for r in self.srv.requests if r.params().get("method") == "create"][-1]
        self.assertEqual(req.method, "POST")
        self.assertEqual(set(req.query), {"_sid"})
        self.assertEqual(req.form["url"], magnet)
        self.assertEqual(req.form["type"], "url")
        self.assertEqual(self.dsm.created[0][0], "ds2")

    def test_numeric_destination_stays_a_string(self):
        self.dsm.shares = ("2024",)
        self.ds(destination="2024").add_url("magnet:?xt=urn:btih:abc")
        self.assertEqual(self.dsm.created[0][1]["destination"], '"2024"')

    def test_empty_destination_uses_download_station_default(self):
        # Prowlarr (TorrentDownloadStation.GetDownloadDirectory) always passes a destination: the configured
        # one or SYNO.DownloadStation.Info getconfig "default_destination"
        ds = self.ds(destination="")
        ds.add_torrent_file(TORRENT, "x.torrent")
        ds.add_url("magnet:?xt=urn:btih:abc")
        (a1, p1, _), (a2, p2, _) = self.dsm.created
        self.assertEqual((a1, a2), ("ds2", "ds2"))
        self.assertEqual(p1["destination"], '"Download"')
        self.assertEqual(p2["destination"], "Download")
        getconfig = [r for r in self.srv.requests if r.params().get("method") == "getconfig"]
        self.assertEqual(len(getconfig), 1)  # cached
        self.assertEqual(getconfig[0].path, "/webapi/DownloadStation/info.cgi")

    def test_empty_destination_default_unreadable(self):
        del self.dsm.apis["SYNO.DownloadStation.Info"]
        ds = self.ds(destination="")
        ds.add_torrent_file(TORRENT, "x.torrent")
        api, p, _ = self.dsm.created[0]
        self.assertEqual(api, "ds2")
        self.assertNotIn("destination", p)

    def test_pending_file_list_is_confirmed(self):
        self.dsm.apis["SYNO.DownloadStation2.Task.List"] = {"maxVersion": 2, "minVersion": 1, "path": "entry.cgi"}
        self.dsm.ds2_create_data = {"list_id": ["btlist_1"], "task_id": []}
        ds = self.ds()
        self.assertEqual(ds.add_torrent_file(TORRENT, "x.torrent"), "DownloadStation2")
        (a1, m1, p1), (a2, m2, p2) = self.dsm.list_calls
        self.assertEqual((a1, m1), ("SYNO.DownloadStation2.Task.List", "get"))
        self.assertEqual(a2, "SYNO.DownloadStation2.Task.List")  # no .Polling API on this NAS
        self.assertEqual(m2, "download")
        self.assertEqual(json.loads(p2["list_id"]), "btlist_1")
        self.assertEqual(json.loads(p2["selected"]), [0, 1])
        self.assertEqual(json.loads(p2["destination"]), "Download/synoarr")
        self.assertEqual(p2["create_subfolder"], "true")

    def test_pending_file_list_polling_api_and_failure_tolerated(self):
        self.dsm.apis["SYNO.DownloadStation2.Task.List"] = {"maxVersion": 2, "minVersion": 1, "path": "entry.cgi"}
        self.dsm.apis["SYNO.DownloadStation2.Task.List.Polling"] = {"maxVersion": 2, "minVersion": 1,
                                                                    "path": "entry.cgi"}
        self.dsm.ds2_create_data = {"list_id": ["btlist_1"], "task_id": []}
        self.ds().add_url("magnet:?xt=urn:btih:abc")
        self.assertEqual(self.dsm.list_calls[-1][:2], ("SYNO.DownloadStation2.Task.List.Polling", "download"))
        # unknown list: get fails (list_id= then id=), download refused -> no exception
        self.dsm.ds2_create_data = {"list_id": "other", "task_id": []}
        del self.dsm.list_calls[:]
        self.assertEqual(self.ds().add_url("magnet:?xt=urn:btih:abd"), "DownloadStation2")
        self.assertEqual([c[1] for c in self.dsm.list_calls], ["get", "get", "download"])
        self.assertNotIn("selected", self.dsm.list_calls[-1][2])

    def test_ds2_sid_only_in_query_string(self):
        # Sonarr's DownloadStationTaskProxyV2: _sid in the query, api/version/method + params in the body
        self.ds().add_torrent_file(TORRENT, "x.torrent")
        req = [r for r in self.srv.requests if r.method == "POST" and r.path == "/webapi/entry.cgi"
               and r.query.get("_sid")][-1]
        body_fields = {k for k, v in req.multipart.items() if not v[0]}
        self.assertNotIn("_sid", body_fields)
        self.assertTrue({"api", "version", "method", "type", "file", "destination", "create_list"} <= body_fields)

    def test_url_without_scheme(self):
        host = self.srv.url.split("://", 1)[1]
        ds = DownloadStation(host, "synoarr", self.dsm.password, "Download", HttpClient(timeout=5, retries=0))
        self.assertTrue(ds.url.startswith("http://"))
        ok_, msg = ds.test()
        self.assertTrue(ok_, msg)

    def test_add_url_ds2_unicode_destination(self):
        ds = self.ds(destination="Vidéos")
        magnet = "magnet:?xt=urn:btih:0123456789abcdef0123456789abcdef01234567&dn=Film+%C3%A9t%C3%A9"
        ds.add_url(magnet)
        api, p, _ = self.dsm.created[0]
        self.assertEqual(api, "ds2")
        self.assertEqual(p["url"], magnet)
        self.assertEqual(p["destination"], "Vidéos")
        self.assertEqual(p["type"], "url")

    def test_legacy_fallback_when_ds2_missing(self):
        del self.dsm.apis["SYNO.DownloadStation2.Task"]
        ds = self.ds()
        ds.add_torrent_file(TORRENT, "file")
        ds.add_url("https://example.org/a.torrent")
        (a1, p1, f1), (a2, p2, f2) = self.dsm.created
        self.assertEqual(a1, "legacy")
        self.assertEqual(f1["file"], ("file.torrent", TORRENT))
        self.assertIn(b"Content-Type: application/octet-stream", [r for r in self.srv.requests
                                                                  if r.path.endswith("task.cgi")][0].body)
        self.assertEqual(p1["destination"], "Download/synoarr")
        self.assertEqual(p1["version"], "3")
        self.assertEqual(p2["uri"], "https://example.org/a.torrent")
        self.assertEqual(p2["destination"], "Download/synoarr")

    def test_legacy_fallback_when_ds2_errors(self):
        self.dsm.ds2_create_error = 101
        self.ds().add_url("magnet:?xt=urn:btih:abc")
        self.assertEqual(self.dsm.created[0][0], "legacy")

    def test_bad_destination_message(self):
        ds = self.ds(destination="NoSuchShare/sub")
        with self.assertRaises(DownloadStationError) as ctx:
            ds.add_torrent_file(TORRENT, "x.torrent")
        msg = str(ctx.exception)
        self.assertIn("[downloadstation] destination", msg)
        self.assertIn("NoSuchShare/sub", msg)
        self.assertEqual(ctx.exception.code, 403)  # legacy fallback also failed: destination missing

    def test_session_expired_relogin(self):
        ds = self.ds()
        ds.login()
        self.dsm.expire_next = 1
        ds.add_torrent_file(TORRENT, "x.torrent")
        self.assertEqual(len(self.dsm.logins), 2)
        self.assertEqual(self.dsm.created[0][0], "ds2")
        self.assertEqual(ds._sid, "sid2")
        # unknown sid (e.g. DSM rebooted) on list
        ds._sid = "stale"
        self.assertEqual(len(ds.list_tasks()), 3)
        self.assertEqual(len(self.dsm.logins), 3)

    def test_session_expired_twice_fails(self):
        ds = self.ds()
        self.dsm.expire_next = 5
        with self.assertRaises(DownloadStationError) as ctx:
            ds.list_tasks()
        self.assertEqual(ctx.exception.code, 119)

    # ----------------------------------------------------------------- list/delete
    def test_list_tasks(self):
        tasks = self.ds().list_tasks()
        t1, t2, t3 = tasks
        self.assertEqual((t1.id, t1.status, t1.size, t1.downloaded, t1.destination),
                         ("dbid_1", "finished", 1500000000, 1500000000, "Download"))
        self.assertTrue(t1.is_complete)
        self.assertEqual(t2.status, "downloading")
        self.assertEqual(t2.downloaded, 400)
        self.assertTrue(t2.is_active)
        self.assertEqual(t3.status, "error")
        self.assertEqual(t3.error, "broken_link")
        self.assertIsNone(t3.destination)

    def test_list_tasks_transfer_details(self):
        self.dsm.tasks = [
            {"id": "dbid_7", "title": "Movie.2020.1080p", "status": "downloading", "size": "2000", "type": "bt",
             "additional": {"detail": {"destination": "Download", "connected_seeders": 12, "connected_leechers": "3",
                                       "total_peers": "40", "create_time": "1700000000",
                                       "uri": "magnet:?xt=urn:btih:ABCDEF0123456789ABCDEF0123456789ABCDEF01&dn=x"},
                            "transfer": {"size_downloaded": "500", "size_uploaded": 100,
                                         "speed_download": "2048", "speed_upload": 512}}},
            {"id": "dbid_8", "title": "Old", "status": "seeding", "size": 10, "type": "bt",
             "additional": {"detail": {"connected_seeders": "n/a", "uri": "https://tracker/x.torrent?passkey=S"},
                            "transfer": {"size_downloaded": 10, "speed_download": None}}},
            {"id": "dbid_9", "title": "Bare", "status": "waiting"},
        ]
        t7, t8, t9 = self.ds().list_tasks()
        self.assertEqual((t7.speed_down, t7.speed_up, t7.seeders, t7.leechers, t7.peers, t7.uploaded, t7.created),
                         (2048, 512, 12, 3, 40, 100, 1700000000))
        self.assertEqual(t7.info_hash, "abcdef0123456789abcdef0123456789abcdef01")
        self.assertEqual(t7.progress, 25.0)
        self.assertIsNone(t8.seeders)  # non-numeric value tolerated
        self.assertIsNone(t8.speed_down)
        self.assertIsNone(t8.info_hash)
        self.assertEqual(t8.progress, 100.0)  # seeding = complete
        self.assertFalse(hasattr(t8, "uri"))  # the URI (may hold a passkey) is not kept
        self.assertEqual((t9.speed_down, t9.seeders, t9.created, t9.progress), (None, None, None, 0.0))

    def test_list_tasks_ds2_fields(self):
        from synoarr.downloadstation import DownloadStation as D
        t = D._to_task({"id": "x", "title": "T", "status": 2, "size": 100,
                        "additional": {"detail": {"connected_seeders": 2, "total_peers": 9},
                                       "transfer": {"downloaded_size": 50, "download_speed": 10,
                                                    "upload_speed": 1, "uploaded_size": 5}}})
        self.assertEqual((t.status, t.downloaded, t.speed_down, t.speed_up, t.uploaded, t.seeders, t.peers),
                         ("downloading", 50, 10, 1, 5, 2, 9))

    def test_delete_task(self):
        ds = self.ds()
        ds.delete_task("dbid_2")
        ds.delete_task("gone")  # 404 per-item: already deleted, no exception
        self.assertEqual(self.dsm.deleted, ["dbid_2", "gone"])
        req = [r for r in self.srv.requests if r.query.get("method") == "delete"][0]
        self.assertEqual(req.query["force_complete"], "false")

    def test_test_ok(self):
        ok_, msg = self.ds().test()
        self.assertTrue(ok_, msg)
        self.assertIn("3 tâche", msg)


if __name__ == "__main__":
    unittest.main()
