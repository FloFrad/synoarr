"""Self-update: GitHub archive download, safety checks, backup, rollback."""
from __future__ import annotations

import io
import os
import shutil
import tarfile
import tempfile
import unittest
from unittest import mock

from synoarr import updater as upd_mod
from synoarr.httpclient import HttpClient
from synoarr.updater import Updater, UpdateError
from tests.fakes import FakeServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def make_tarball(files, top="synoarr-main", extra=None):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for rel, content in files.items():
            data = content.encode("utf-8") if isinstance(content, str) else content
            info = tarfile.TarInfo("%s/%s" % (top, rel))
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
        for info, data in (extra or []):
            tar.addfile(info, io.BytesIO(data) if data is not None else None)
    return buf.getvalue()


class UpdaterTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="synoarr-upd-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.root = os.path.join(self.tmp, "install")
        shutil.copytree(os.path.join(ROOT, "synoarr"), os.path.join(self.root, "synoarr"),
                        ignore=shutil.ignore_patterns("__pycache__"))
        shutil.copy2(os.path.join(ROOT, "synoarr.py"), self.root)
        os.makedirs(os.path.join(self.root, "scripts"))
        with open(os.path.join(self.root, "scripts", "start.sh"), "w") as fh:
            fh.write("old\n")
        with open(os.path.join(self.root, "config.ini"), "w") as fh:
            fh.write("[general]\nmine = yes\n")
        os.makedirs(os.path.join(self.root, "data"))
        with open(os.path.join(self.root, "data", "synoarr.db"), "w") as fh:
            fh.write("db")
        self.data = os.path.join(self.root, "data")
        # remote = current code + changes
        self.remote = {}
        for base, _dirs, files in os.walk(os.path.join(self.root, "synoarr")):
            for f in files:
                p = os.path.join(base, f)
                with open(p, "rb") as fh:
                    self.remote[os.path.relpath(p, self.root).replace(os.sep, "/")] = fh.read()
        with open(os.path.join(self.root, "synoarr.py"), "rb") as fh:
            self.remote["synoarr.py"] = fh.read()
        self.remote["scripts/start.sh"] = "new\n"
        self.remote["NEWFILE.md"] = "hello"
        self.remote["config.ini"] = "[general]\nfrom_github = 1\n"  # must never overwrite the user's file
        self.payload = make_tarball(self.remote)
        self.srv = FakeServer(lambda req: (200, {"Content-Type": "application/gzip"}, self.payload)).__enter__()
        self.addCleanup(self.srv.__exit__, None, None, None)
        self.upd = Updater(self.data, HttpClient(retries=0), root=self.root)
        self.upd.archive_url = self.srv.url + "/archive.tar.gz"
        self.lines = []
        self.emit = lambda level, msg: self.lines.append((level, msg))
        p = mock.patch.object(upd_mod, "in_docker", return_value=False)
        p.start()
        self.addCleanup(p.stop)

    def read(self, rel):
        with open(os.path.join(self.root, rel)) as fh:
            return fh.read()

    def test_check_then_apply_then_rollback(self):
        res = self.upd.check(self.emit)
        self.assertTrue(res["available"])
        self.assertEqual(res["changed"], ["scripts/start.sh"])
        self.assertEqual(res["added"], ["NEWFILE.md"])
        self.assertEqual(self.read("scripts/start.sh"), "old\n")  # check modifies nothing
        res = self.upd.apply(self.emit)
        self.assertTrue(res["updated"])
        self.assertEqual(self.read("scripts/start.sh"), "new\n")
        self.assertTrue(os.access(os.path.join(self.root, "scripts", "start.sh"), os.X_OK))
        self.assertEqual(self.read("NEWFILE.md"), "hello")
        self.assertEqual(self.read("config.ini"), "[general]\nmine = yes\n")
        self.assertEqual(self.read("data/synoarr.db"), "db")
        self.assertFalse([d for d in os.listdir(self.data) if d.startswith("update-tmp")])
        self.assertFalse(self.upd.apply(self.emit)["updated"])  # second run: already up to date
        self.upd.rollback(self.emit)
        self.assertEqual(self.read("scripts/start.sh"), "old\n")
        with self.assertRaises(UpdateError):
            self.upd.rollback(self.emit)

    def test_broken_new_version_is_not_installed(self):
        self.remote["synoarr/cli.py"] = "def broken(:\n"
        self.payload = make_tarball(self.remote)
        with self.assertRaises(UpdateError) as ctx:
            self.upd.apply(self.emit)
        self.assertIn("rien n'a été modifié", str(ctx.exception))
        self.assertEqual(self.read("scripts/start.sh"), "old\n")

    def test_malicious_archives_are_refused(self):
        evil = tarfile.TarInfo("synoarr-main/../../evil.txt")
        evil.size = 3
        self.payload = make_tarball(self.remote, extra=[(evil, b"bad")])
        with self.assertRaises(UpdateError):
            self.upd.apply(self.emit)
        link = tarfile.TarInfo("synoarr-main/link")
        link.type = tarfile.SYMTYPE
        link.linkname = "/etc/passwd"
        self.payload = make_tarball(self.remote, extra=[(link, None)])
        with self.assertRaises(UpdateError):
            self.upd.apply(self.emit)
        self.payload = b"<html>not found</html>"
        with self.assertRaises(UpdateError):
            self.upd.check(self.emit)
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "evil.txt")))

    def test_docker_mode_only_explains(self):
        with mock.patch.object(upd_mod, "in_docker", return_value=True):
            res = self.upd.apply(self.emit)
        self.assertEqual(res["mode"], "docker")
        self.assertFalse(res["updated"])
        self.assertTrue(any("Container Manager" in m for _l, m in self.lines))
        self.assertEqual(self.read("scripts/start.sh"), "old\n")


if __name__ == "__main__":
    unittest.main()
