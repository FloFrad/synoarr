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
        if api == "SYNO.DownloadStation2.Task" and req.method == "POST":
            sid = req.query.get("_sid")  # DS2 POST: sid expected in the query string
        if sid not in self.sids:
            return err(119)
        if self.expire_next:
            self.expire_next -= 1
            self.sids.discard(sid)
            return err(119)
        if api == "SYNO.DownloadStation2.Task":
            return self.ds2(req, p)
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
        if p.get("create_list") != "false":
            return err(120)
        try:
            typ = json.loads(p["type"])
            dest = json.loads(p["destination"]) if "destination" in p else None
        except ValueError:
            return err(120)
        if dest is not None and not self._check_dest(dest):
            return err(120)
        files = {k: v for k, v in req.multipart.items() if v[0]}
        if typ == "file":
            names = json.loads(p["file"])
            if not names or names[0] not in files:
                return err(120)
        elif typ == "url":
            if not json.loads(p["url"]):
                return err(120)
        else:
            return err(120)
        self.created.append(("ds2", p, files))
        return ok({"list_id": [], "task_id": ["synoarr/SYNODLTaskListDownload1"]})

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
        self.assertEqual(p["file"], '["torrent"]')
        self.assertEqual(p["destination"], '"Download/synoarr"')
        self.assertEqual(p["create_list"], "false")
        self.assertEqual(files["torrent"], ("My.Show.S01E01.torrent", TORRENT))
        req = [r for r in self.srv.requests if r.method == "POST" and r.query.get("_sid")][-1]
        self.assertEqual(req.path, "/webapi/entry.cgi")
        self.assertIn("multipart/form-data", req.headers["content-type"])

    def test_add_url_ds2_unicode_destination(self):
        ds = self.ds(destination="Vidéos")
        magnet = "magnet:?xt=urn:btih:0123456789abcdef0123456789abcdef01234567&dn=Film+%C3%A9t%C3%A9"
        ds.add_url(magnet)
        api, p, _ = self.dsm.created[0]
        self.assertEqual(api, "ds2")
        self.assertEqual(json.loads(p["url"]), [magnet])
        self.assertEqual(json.loads(p["destination"]), "Vidéos")
        self.assertEqual(p["type"], '"url"')

    def test_legacy_fallback_when_ds2_missing(self):
        del self.dsm.apis["SYNO.DownloadStation2.Task"]
        ds = self.ds()
        ds.add_torrent_file(TORRENT, "file")
        ds.add_url("https://example.org/a.torrent")
        (a1, p1, f1), (a2, p2, f2) = self.dsm.created
        self.assertEqual(a1, "legacy")
        self.assertEqual(f1["file"], ("file.torrent", TORRENT))
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
