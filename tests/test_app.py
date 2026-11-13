from __future__ import annotations

import logging
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

from synoarr import app as app_mod
from synoarr.app import App, BusyError, Job, decode_config_bytes, request_from_query, restart_argv
from synoarr.config import load_config


class HelpersTest(unittest.TestCase):
    def test_restart_argv(self) -> None:
        with mock.patch.object(sys, "argv", ["synoarr.py", "run", "--config", "/c.ini"]):
            self.assertEqual(restart_argv(), [sys.executable, "synoarr.py", "run", "--config", "/c.ini"])
        with mock.patch.object(sys, "argv", ["/app/synoarr/__main__.py", "run"]):
            self.assertEqual(restart_argv(), [sys.executable, "-m", "synoarr", "run"])

    def test_decode_config_bytes(self) -> None:
        self.assertEqual(decode_config_bytes("é\r\nx".encode("utf-8")), ("é\nx", "utf-8"))
        self.assertEqual(decode_config_bytes(b"\xef\xbb\xbf[a]\n"), ("[a]\n", "utf-8"))
        self.assertEqual(decode_config_bytes("[a]\r\né".encode("utf-16")), ("[a]\né", "utf-16"))
        self.assertEqual(decode_config_bytes("Vidéos".encode("cp1252")), ("Vidéos", "cp1252"))

    def test_request_from_query(self) -> None:
        r = request_from_query("Night of the Living Dead 1968")
        self.assertEqual((r.kind, r.year), ("movie", 1968))
        r = request_from_query("Test Show S02E01")
        self.assertEqual((r.kind, r.season, r.episode), ("episode", 2, 1))
        r = request_from_query("Test Show S02")
        self.assertEqual((r.kind, r.season), ("season", 2))

    def test_job_output_is_bounded(self) -> None:
        job = Job(1, "check", "Vérifier", "check")
        for i in range(Job.MAX_OUTPUT + 10):
            job.emit("info", str(i))
        self.assertEqual(len(job.output), Job.MAX_OUTPUT + 1)
        self.assertIn("tronquée", job.output[-1][1])


class AppJobsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="synoarr-app-")
        self.cfg_path = os.path.join(self.tmp, "config.ini")
        with open(self.cfg_path, "w", encoding="utf-8") as fh:
            fh.write("[general]\ndata_dir = ./data\n[paths]\ndownload_dir = %s\n" % self.tmp)
        self.app = App(load_config(self.cfg_path))

    def tearDown(self) -> None:
        self.app._stop.set()
        self.app.db.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def wait(self, job: Job) -> Job:
        deadline = time.time() + 10
        while job.running and time.time() < deadline:
            time.sleep(0.02)
        return job

    def test_job_captures_its_own_log_lines_only(self) -> None:
        other = threading.Event()

        def target(job):
            logging.getLogger("synoarr.test").warning("ligne de la tâche")
            other.wait(5)
            return {"ok": True}
        root = logging.getLogger()
        old_level = root.level
        root.setLevel(logging.INFO)
        try:
            job, created = self.app._start_job("x", "Test", "x", target)
            self.assertTrue(created)
            logging.getLogger("synoarr.test").warning("ligne d'un autre thread")
            other.set()
            self.wait(job)
        finally:
            root.setLevel(old_level)
        self.assertEqual(job.status, "done")
        self.assertEqual(job.result, {"ok": True})
        self.assertEqual(job.output, [("warn", "ligne de la tâche")])
        self.assertFalse(any(isinstance(h, app_mod._JobLogHandler) for h in root.handlers))

    def test_failing_job(self) -> None:
        def target(job):
            raise ValueError("Pas possible")
        job, _ = self.app._start_job("x", "Test", "x", target)
        self.wait(job)
        self.assertEqual((job.status, job.error), ("error", "Pas possible"))
        self.assertIsNotNone(job.finished_at)

    def test_reload_refused_while_a_job_runs(self) -> None:
        release = threading.Event()
        job, _ = self.app._start_job("search", "Recherche", "search:x", lambda j: release.wait(5))
        try:
            with self.assertRaises(BusyError):
                self.app.reload_config()
        finally:
            release.set()
        self.wait(job)
        with open(self.cfg_path, "a", encoding="utf-8") as fh:
            fh.write("min_age_minutes = 9\n")
        self.assertFalse(self.app.reload_config()["restart_required"])
        self.assertEqual(self.app.cfg.paths.min_age_minutes, 9)
        self.assertEqual(self.app.organizer.cfg.paths.min_age_minutes, 9)

    def test_reload_waits_for_the_process_lock(self) -> None:
        with self.app._process_lock:  # daemon cycle importing files
            with self.assertRaises(BusyError):
                with self.app._exclusive(timeout=0.1):
                    self.fail("ne doit pas s'exécuter")
        with self.app._exclusive(timeout=0.1):
            self.assertTrue(self.app._process_lock.locked())
        self.assertFalse(self.app._process_lock.locked())

    def test_match_media(self) -> None:
        db = self.app.db
        m1 = db.upsert_media("k1", "movie", "Night of the Living Dead", 1968, None, None, None)
        db.upsert_media("k2", "movie", "Night of the Living Dead", 1990, None, None, None)
        s1 = db.upsert_media("k3", "show", "Test Show", 2020, None, None, None)
        media, season, eps = self.app._match_media("Night.of.the.Living.Dead.1968.1080p.BluRay")
        self.assertEqual(media["id"], m1)
        self.assertIsNone(self.app._match_media("Night.of.the.Living.Dead.1080p.BluRay")[0])  # ambigu
        media, season, eps = self.app._match_media("Test.Show.S02E01E02.720p")
        self.assertEqual((media["id"], season, eps), (s1, 2, [1, 2]))
        self.assertIsNone(self.app._match_media("Other.Show.S01E01")[0])


if __name__ == "__main__":
    unittest.main()
