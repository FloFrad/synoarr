"""F2 – notifications Telegram (synoarr/notify.py) contre une fausse API Telegram (tests/fakes.FakeServer)."""
from __future__ import annotations

import contextlib
import io
import json
import logging
import os
import shutil
import socket
import tempfile
import time
import unittest
from typing import Any, Dict, List
from unittest import mock

from synoarr import notify
from synoarr.app import App
from synoarr.config import load_config
from synoarr.db import Database
from synoarr.httpclient import HttpClient, redact
from synoarr.notify import TelegramNotifier, classify, format_event, merge

from fakes import FakeServer

TOKEN = "123456789:AAHsecretTOKEN_abc-XYZ"


class FakeTelegram(object):
    """Fausse API Bot : /bot<TOKEN>/sendMessage et /bot<TOKEN>/getUpdates."""

    def __init__(self) -> None:
        self.updates = []  # type: List[Dict[str, Any]]
        self.fail_429 = 0
        self.srv = FakeServer(self.handler)

    def handler(self, req):
        prefix = "/bot%s/" % TOKEN
        if not req.path.startswith(prefix):
            return 401, {}, {"ok": False, "error_code": 401, "description": "Unauthorized"}
        method = req.path[len(prefix):]
        if method == "getUpdates":
            return 200, {}, {"ok": True, "result": self.updates}
        if method == "sendMessage":
            data = json.loads(req.body.decode("utf-8"))
            if self.fail_429 > 0:
                self.fail_429 -= 1
                return 429, {}, {"ok": False, "error_code": 429, "description": "Too Many Requests: retry after 0",
                                 "parameters": {"retry_after": 0}}
            if str(data.get("chat_id")) == "404":
                return 400, {}, {"ok": False, "error_code": 400, "description": "Bad Request: chat not found"}
            return 200, {}, {"ok": True, "result": {"message_id": len(self.sent())}}
        return 404, {}, {"ok": False, "error_code": 404, "description": "Not Found"}

    def sent(self) -> List[Dict[str, Any]]:
        out = []
        for r in list(self.srv.requests):
            if r.path.endswith("/sendMessage") and r.body:
                out.append(json.loads(r.body.decode("utf-8")))
        return out

    def ok_sent(self) -> List[Dict[str, Any]]:
        return [m for m in self.sent() if str(m.get("chat_id")) != "404"]

    def wait(self, count: int, timeout: float = 5.0) -> List[Dict[str, Any]]:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if len(self.sent()) >= count:
                break
            time.sleep(0.02)
        return self.sent()


class TelegramTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self.tg = FakeTelegram()
        self.tg.srv.__enter__()
        self.notifiers = []  # type: List[TelegramNotifier]

    def tearDown(self) -> None:
        for n in self.notifiers:
            n.close(timeout=2)
        self.tg.srv.__exit__(None, None, None)

    def make(self, chat_id: str = "42", token: str = TOKEN, **kw: Any) -> TelegramNotifier:
        events = kw.pop("events", notify.DEFAULT_EVENTS)
        n = TelegramNotifier(token, chat_id, HttpClient(timeout=5, retries=0), events=events, api_url=self.tg.srv.url)
        n.coalesce_seconds = kw.pop("coalesce", 0.05)
        for k, v in kw.items():
            setattr(n, k, v)
        self.notifiers.append(n)
        return n


class NotifierTest(TelegramTestBase):
    def test_send_now_payload(self) -> None:
        n = self.make()
        ok, msg = n.send_now("Bonjour")
        self.assertTrue(ok, msg)
        req = self.tg.srv.requests[-1]
        self.assertEqual(req.method, "POST")
        self.assertEqual(req.path, "/bot%s/sendMessage" % TOKEN)
        self.assertIn("application/json", req.headers.get("content-type", ""))
        self.assertEqual(json.loads(req.body.decode("utf-8")),
                         {"chat_id": "42", "text": "Bonjour", "parse_mode": "HTML", "disable_web_page_preview": True})

    def test_async_send_and_html_escaping(self) -> None:
        n = self.make()
        n.send("Film <b>gras</b> & « Vidéos »")
        sent = self.tg.wait(1)
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0]["text"], "Film &lt;b&gt;gras&lt;/b&gt; &amp; « Vidéos »")

    def test_format_event_escapes_message_keeps_markup(self) -> None:
        text = format_event("grab", "Envoyé à Download Station : Tom & Jerry <2024> ← Rel [idx]")
        self.assertTrue(text.startswith("📥 <b>Envoyé à Download Station</b>\n"))
        self.assertIn("Tom &amp; Jerry &lt;2024&gt;", text)
        self.assertNotIn("<2024>", text)
        long = format_event("import", "é&" * 3000)
        self.assertLess(len(long), notify.MAX_LENGTH)
        self.assertFalse(long.rstrip("…").endswith(("&", "&a", "&am", "&amp")))

    def test_long_plain_text_is_cut_before_escaping(self) -> None:
        n = self.make()
        ok, msg = n.send_now("<" * 5000)
        self.assertTrue(ok, msg)
        text = self.tg.sent()[-1]["text"]
        self.assertLessEqual(len(text), notify.MAX_LENGTH)
        self.assertTrue(text.endswith("&lt;…"))

    def test_disabled_without_token_or_chat_id(self) -> None:
        for token, chat in (("", "42"), (TOKEN, ""), ("", "")):
            n = self.make(chat_id=chat, token=token)
            self.assertFalse(n.enabled)
            n.send("rien")
            n.on_event("grab", "Envoyé à Download Station : X", "INFO")
            ok, msg = n.send_now("rien")
            self.assertFalse(ok)
            self.assertIn("pas configuré", msg)
        time.sleep(0.2)
        self.assertEqual(self.tg.sent(), [])
        self.assertEqual(self.tg.srv.requests, [])

    def test_from_config_disabled_and_events(self) -> None:
        cfg = load_config(None)
        self.assertEqual(cfg.telegram.events, ["grab", "import", "error"])
        self.assertFalse(notify.from_config(cfg.telegram).enabled)
        cfg.telegram.bot_token, cfg.telegram.chat_id = TOKEN, "42"
        cfg.telegram.events = ["Grab", "update", "bogus"]
        n = notify.from_config(cfg.telegram)
        self.assertTrue(n.enabled)
        self.assertEqual(n.events, ["grab", "update"])

    def test_telegram_error_text_reported(self) -> None:
        n = self.make(chat_id="404")
        ok, msg = n.send_now("x")
        self.assertFalse(ok)
        self.assertIn("chat not found", msg)
        self.assertNotIn(TOKEN, msg)
        bad = self.make(token="999:WRONGtoken_zz")
        ok, msg = bad.send_now("x")
        self.assertFalse(ok)
        self.assertIn("Unauthorized", msg)
        self.assertIn("bot_token", msg)
        self.assertNotIn("WRONGtoken", msg)

    def test_retry_after_429(self) -> None:
        self.tg.fail_429 = 1
        n = self.make()
        ok, msg = n.send_now("x")
        self.assertTrue(ok, msg)
        self.assertEqual(len(self.tg.sent()), 2)

    def test_token_never_in_logs_or_errors(self) -> None:
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        sock.close()
        n = TelegramNotifier(TOKEN, "42", HttpClient(timeout=2, retries=1, backoff=0.01),
                             api_url="http://127.0.0.1:%d" % port)
        n.coalesce_seconds = 0.01
        self.notifiers.append(n)
        with self.assertLogs("synoarr", level="DEBUG") as cm:
            ok, msg = n.send_now("x")
            n.send("y")  # thread : avertissement dans le journal
            n.close(timeout=5)
        self.assertFalse(ok)
        self.assertIn("injoignable", msg)
        self.assertNotIn(TOKEN, msg)
        self.assertNotIn("AAHsecret", msg)
        joined = "\n".join(cm.output)
        self.assertIn("Notification Telegram non envoyée", joined)
        self.assertNotIn(TOKEN, joined)
        self.assertNotIn("AAHsecret", joined)
        self.assertIn("/bot***", joined)

    def test_redact_helpers(self) -> None:
        url = "https://api.telegram.org/bot%s/sendMessage" % TOKEN
        self.assertEqual(redact(url), "https://api.telegram.org/bot***/sendMessage")
        self.assertEqual(redact("https://x/bottles/list?a=1"), "https://x/bottles/list?a=1")
        n = TelegramNotifier(TOKEN, "1", HttpClient())
        self.assertNotIn(TOKEN, n.redact("erreur %s et %s" % (url, TOKEN)))

    def test_burst_is_coalesced(self) -> None:
        n = self.make(coalesce=0.3)
        for i in range(5):
            n.send("message %d" % i)
        sent = self.tg.wait(1)
        time.sleep(0.4)
        sent = self.tg.sent()
        self.assertEqual(len(sent), 1)
        for i in range(5):
            self.assertIn("message %d" % i, sent[0]["text"])

    def test_rate_limit_waits_then_merges(self) -> None:
        n = self.make(max_per_minute=2)
        n.send("a")
        self.tg.wait(1)
        n.send("b")
        self.tg.wait(2)
        deadline = time.time() + 5
        while n.sent_count < 2 and time.time() < deadline:
            time.sleep(0.01)
        self.assertEqual(n.sent_count, 2)
        # 2 messages déjà envoyés dans la minute : la place suivante se libère dans ~0,6 s
        now = time.time()
        with n._lock:
            n._sent = [now - 59.4, now - 59.3]
        for x in ("c", "d", "e"):
            n.send(x)
        time.sleep(0.3)
        self.assertEqual(len(self.tg.sent()), 2)  # limite atteinte : rien n'est parti
        sent = self.tg.wait(3)
        time.sleep(0.2)
        sent = self.tg.sent()
        self.assertEqual(len(sent), 3)
        self.assertEqual(sent[2]["text"], "c\n\nd\n\ne")

    def test_merge_limits_length(self) -> None:
        msgs = ["x" * 1000 for _ in range(10)]
        text = merge(msgs)
        self.assertLessEqual(len(text), notify.MAX_LENGTH)
        self.assertIn("autre(s) notification(s)", text)
        self.assertEqual(merge(["seul"]), "seul")

    def test_get_chats(self) -> None:
        self.tg.updates = [
            {"update_id": 1, "message": {"chat": {"id": 987654, "type": "private", "first_name": "Jean",
                                                  "last_name": "Dupont", "username": "jdupont"}, "text": "/start"}},
            {"update_id": 2, "message": {"chat": {"id": 987654, "type": "private", "first_name": "Jean"}}},
            {"update_id": 3, "my_chat_member": {"chat": {"id": -100123, "type": "supergroup", "title": "Famille"}}},
        ]
        n = self.make(chat_id="")
        ok, chats, err = n.get_chats()
        self.assertTrue(ok, err)
        self.assertEqual(chats, [{"id": 987654, "name": "Jean Dupont (@jdupont)", "type": "private"},
                                 {"id": -100123, "name": "Famille", "type": "supergroup"}])
        ok, chats, err = self.make(chat_id="", token="").get_chats()
        self.assertFalse(ok)
        self.assertIn("BotFather", err)


class ClassifyTest(unittest.TestCase):
    def test_kinds(self) -> None:
        self.assertEqual(classify("grab", "Envoyé à Download Station : X ← R [idx]"), ["grab"])
        self.assertEqual(classify("grab", "Source choisie manuellement pour X : R"), ["grab"])
        self.assertEqual(classify("grab", "[dry-run] X → R"), [])
        self.assertEqual(classify("grab", "Échec de l'envoi de R : boom", "ERROR"), ["error"])
        self.assertEqual(classify("grab", "Amélioration : X ← R"), ["upgrade", "grab"])
        self.assertEqual(classify("upgrade", "Amélioration de qualité envoyée à Download Station : X ← R [i]"),
                         ["upgrade", "grab"])
        self.assertEqual(classify("upgrade", "Amélioration importée : X.1080p.mkv remplace X.720p.mkv"), ["upgrade"])
        self.assertEqual(classify("import", "Importé : R -> X (2020).mkv"), ["import"])
        self.assertEqual(classify("import", "Import impossible de /a : disque plein", "ERROR"), ["error"])
        self.assertEqual(classify("download", "Échec du téléchargement R : aucune source", "ERROR"), ["error"])
        self.assertEqual(classify("download", "2 téléchargement(s) supprimé(s)"), [])
        self.assertEqual(classify("watchlist", "Ajouté depuis la Watchlist : X (2020)"), ["watchlist"])
        self.assertEqual(classify("watchlist", "Ajouté manuellement (entrée locale) : X"), ["watchlist"])
        self.assertEqual(classify("watchlist", "Réactivé : X"), ["watchlist"])
        self.assertEqual(classify("watchlist", "Doublon masqué : X"), [])
        self.assertEqual(classify("watchlist", "Retiré de la Watchlist : X"), [])
        self.assertEqual(classify("watchlist", "Erreur : boom", "ERROR"), [])
        self.assertEqual(classify("update", "synoarr mis à jour (update) : redémarrage"), ["update"])
        self.assertEqual(classify("search", "Erreur de recherche", "ERROR"), [])
        self.assertEqual(classify("indexer", "Indexer ajouté : X"), [])

    def test_upgrade_falls_back_to_grab(self) -> None:
        n = TelegramNotifier(TOKEN, "1", HttpClient(), events=["grab"])
        with mock.patch.object(n, "send") as send:
            n.on_event("upgrade", "Amélioration de qualité envoyée à Download Station : X ← R [i]")
            self.assertTrue(send.call_args[0][0].startswith("📥"))
            send.reset_mock()
            n.on_event("upgrade", "Amélioration importée : X.1080p.mkv remplace X.720p.mkv")
            send.assert_not_called()
        n2 = TelegramNotifier(TOKEN, "1", HttpClient(), events=["upgrade", "grab"])
        with mock.patch.object(n2, "send") as send:
            n2.on_event("upgrade", "Amélioration de qualité envoyée à Download Station : X ← R [i]")
        self.assertTrue(send.call_args[0][0].startswith("⬆️"))


class DatabaseListenerTest(unittest.TestCase):
    def test_listener_called_and_errors_swallowed(self) -> None:
        db = Database(":memory:")
        seen = []

        def boom(kind, message, level):
            raise RuntimeError("listener cassé")
        db.add_listener(boom)
        db.add_listener(lambda k, m, lv: seen.append((k, m, lv)))
        db.add_listener(boom)  # pas de doublon
        db.add_event("grab", "x")
        db.add_event("download", "y", "ERROR")
        self.assertEqual(seen, [("grab", "x", "INFO"), ("download", "y", "ERROR")])
        self.assertEqual(len(db.recent_events()), 2)
        db.remove_listener(boom)
        db.remove_listener(boom)
        db.close()


class AppTelegramTest(TelegramTestBase):
    """Vraie App (config.ini temporaire) : écouteur sur db.add_event, rechargement, commande telegram_test."""

    def setUp(self) -> None:
        super().setUp()
        self.tmp = tempfile.mkdtemp(prefix="synoarr-tg-")
        self.cfg_path = os.path.join(self.tmp, "config.ini")
        self.patches = [mock.patch.object(App, "telegram_api_url", self.tg.srv.url),
                        mock.patch.object(TelegramNotifier, "coalesce_seconds", 0.05)]
        for p in self.patches:
            p.start()
        self.app = None  # type: Any

    def tearDown(self) -> None:
        if self.app is not None:
            self.app._flush_notifier(2)
            self.app._stop.set()
            self.app.db.close()
        for p in self.patches:
            p.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)
        super().tearDown()

    def write(self, telegram: str) -> None:
        with open(self.cfg_path, "w", encoding="utf-8") as fh:
            fh.write("[general]\ndata_dir = ./data\nlog_level = WARNING\n[paths]\ndownload_dir = %s\n"
                     "[web]\nenabled = false\n[telegram]\n%s\n" % (self.tmp, telegram))

    def start(self, telegram: str) -> App:
        self.write(telegram)
        self.app = App(load_config(self.cfg_path))
        return self.app

    def run_cmd(self, command: str):
        job, created = self.app.start_command(command)
        self.assertTrue(created)
        deadline = time.time() + 10
        while job.running and time.time() < deadline:
            time.sleep(0.02)
        return job

    def test_events_are_notified_according_to_config(self) -> None:
        app = self.start("bot_token = %s\nchat_id = 42\nevents = grab, import, error, watchlist" % TOKEN)
        db = app.db
        db.add_event("grab", "Envoyé à Download Station : Tom & Jerry ← Tom.and.Jerry.1080p [Idx]")
        db.add_event("grab", "[dry-run] X → Y")
        db.add_event("import", "Importé : Tom.and.Jerry.1080p -> Tom & Jerry (2021).mkv")
        db.add_event("download", "Échec du téléchargement Rel.X : aucune source après 6 h", "ERROR")
        db.add_event("watchlist", "Ajouté depuis la Watchlist : Dune (2021)")
        db.add_event("watchlist", "Doublon masqué : X")
        db.add_event("update", "synoarr mis à jour (update) : redémarrage")  # pas dans events
        db.add_event("indexer", "Indexer ajouté : X")
        app._flush_notifier(5)
        text = "\n\n".join(m["text"] for m in self.tg.sent())
        self.assertTrue(all(m["chat_id"] == "42" and m["parse_mode"] == "HTML" for m in self.tg.sent()))
        self.assertIn("📥 <b>Envoyé à Download Station</b>\nEnvoyé à Download Station : Tom &amp; Jerry", text)
        self.assertIn("✅ <b>Importé dans la bibliothèque</b>", text)
        self.assertIn("❌ <b>Échec</b>\nÉchec du téléchargement Rel.X", text)
        self.assertIn("👀 <b>Ajouté à la Watchlist</b>\nAjouté depuis la Watchlist : Dune (2021)", text)
        for absent in ("dry-run", "Doublon", "mis à jour", "Indexer"):
            self.assertNotIn(absent, text)

    def test_disabled_config_sends_nothing(self) -> None:
        app = self.start("bot_token = %s\nchat_id =" % TOKEN)
        self.assertFalse(app.notifier.enabled)
        app.db.add_event("grab", "Envoyé à Download Station : X ← Y [Z]")
        time.sleep(0.2)
        self.assertEqual(self.tg.srv.requests, [])

    def test_reload_config_recreates_notifier(self) -> None:
        app = self.start("bot_token =\nchat_id =")
        first = app.notifier
        self.assertFalse(first.enabled)
        self.write("bot_token = %s\nchat_id = 777\nevents = update" % TOKEN)
        app.reload_config()
        self.assertIsNot(app.notifier, first)
        self.assertTrue(app.notifier.enabled)
        self.assertEqual(len(app.db._listeners), 1)  # écouteur branché une seule fois
        app.db.add_event("update", "synoarr mis à jour (update) : redémarrage")
        app.db.add_event("grab", "Envoyé à Download Station : X ← Y [Z]")  # plus dans events
        sent = self.tg.wait(1)
        time.sleep(0.15)
        sent = self.tg.sent()
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0]["chat_id"], "777")
        self.assertTrue(sent[0]["text"].startswith("🔄 <b>Mise à jour de synoarr</b>"))

    def test_telegram_test_command_sends_message(self) -> None:
        self.start("bot_token = %s\nchat_id = 42" % TOKEN)
        job = self.run_cmd("telegram_test")
        self.assertEqual(job.status, "done")
        self.assertEqual(job.result, {"ok": True})
        self.assertIn("Message de test envoyé", " ".join(t for _, t in job.output))
        self.assertIn("Message de test", self.tg.sent()[-1]["text"])

    def test_telegram_test_command_reports_telegram_error(self) -> None:
        self.start("bot_token = %s\nchat_id = 404" % TOKEN)
        job = self.run_cmd("telegram_test")
        self.assertFalse(job.result["ok"])
        out = " ".join(t for _, t in job.output)
        self.assertIn("chat not found", out)
        self.assertNotIn(TOKEN, out)

    def test_telegram_test_command_lists_chat_ids(self) -> None:
        self.tg.updates = [{"update_id": 5, "message": {"chat": {"id": 31337, "type": "private",
                                                                 "first_name": "Marie"}}}]
        self.start("bot_token = %s\nchat_id =" % TOKEN)
        job = self.run_cmd("telegram_test")
        self.assertTrue(job.result["ok"])
        out = " ".join(t for _, t in job.output)
        self.assertIn("chat_id = 31337 — Marie (private)", out)
        self.assertEqual(self.tg.sent(), [])
        self.tg.updates = []
        job = self.run_cmd("telegram_test")
        self.assertFalse(job.result["ok"])
        self.assertIn("/start", " ".join(t for _, t in job.output))

    def test_telegram_test_without_token(self) -> None:
        self.start("bot_token =\nchat_id =")
        job = self.run_cmd("telegram_test")
        self.assertFalse(job.result["ok"])
        self.assertIn("@BotFather", " ".join(t for _, t in job.output))

    def test_cli_telegram_test(self) -> None:
        from synoarr import cli
        self.tg.updates = [{"update_id": 5, "message": {"chat": {"id": 4242, "type": "private",
                                                                 "first_name": "Luc"}}}]
        self.write("bot_token = %s\nchat_id =" % TOKEN)
        buf = io.StringIO()
        created = []  # type: List[Any]
        orig = cli._app

        def make_app(args, console=True):
            created.append(orig(args, console))
            return created[-1]
        with mock.patch("synoarr.app.setup_logging"), mock.patch.object(cli, "_app", make_app), \
                contextlib.redirect_stdout(buf):
            code = cli.main(["telegram-test", "--config", self.cfg_path])
        for a in created:
            a.db.close()
        self.assertEqual(code, 0, buf.getvalue())
        self.assertIn("chat_id = 4242 — Luc", buf.getvalue())


if __name__ == "__main__":  # pragma: no cover
    logging.basicConfig()
    unittest.main()
