"""Commandes Telegram (synoarr/telegram_bot.py) contre une fausse API Bot et une vraie App (config.ini temporaire)."""
from __future__ import annotations

import datetime as _dt
import json
import os
import shutil
import tempfile
import threading
import time
import unittest
from typing import Any, Dict, List
from unittest import mock

from synoarr import telegram_bot
from synoarr.app import App
from synoarr.config import load_config
from synoarr.models import Release
from synoarr.notify import TelegramNotifier
from synoarr.telegram_bot import ANNOUNCE_ID, HELP_TEXT, TelegramBot

from fakes import FakeServer

TOKEN = "123456789:AAHsecretTOKEN_abc-XYZ"
CHAT = "42"


class FakeBotApi(object):
    """getUpdates (respecte offset, attend un peu quand rien n'arrive) et sendMessage."""

    def __init__(self) -> None:
        self.updates = []  # type: List[Dict[str, Any]]
        self.lock = threading.Lock()
        self.srv = FakeServer(self.handler)

    def push(self, text: str, chat: Any = CHAT) -> None:
        with self.lock:
            uid = 100 + len(self.updates)
            self.updates.append({"update_id": uid, "message": {"message_id": uid, "chat": {"id": int(chat)},
                                                               "text": text}})

    def handler(self, req):
        prefix = "/bot%s/" % TOKEN
        if not req.path.startswith(prefix):
            return 401, {}, {"ok": False, "description": "Unauthorized"}
        method = req.path[len(prefix):]
        data = json.loads(req.body.decode("utf-8")) if req.body else {}
        if method == "getUpdates":
            offset = data.get("offset") or 0
            with self.lock:
                res = [u for u in self.updates if u["update_id"] >= offset]
            if not res and data.get("timeout"):
                time.sleep(0.05)
            return 200, {}, {"ok": True, "result": res}
        if method == "sendMessage":
            return 200, {}, {"ok": True, "result": {"message_id": 1}}
        return 404, {}, {"ok": False}

    def sent(self) -> List[Dict[str, Any]]:
        return [json.loads(r.body.decode("utf-8")) for r in list(self.srv.requests)
                if r.path.endswith("/sendMessage") and r.body]

    def wait_sent(self, count: int, timeout: float = 5.0) -> List[Dict[str, Any]]:
        deadline = time.time() + timeout
        while time.time() < deadline and len(self.sent()) < count:
            time.sleep(0.02)
        return self.sent()


class BotTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self.api = FakeBotApi()
        self.api.srv.__enter__()
        self.tmp = tempfile.mkdtemp(prefix="synoarr-bot-")
        self.patches = [mock.patch.object(App, "telegram_api_url", self.api.srv.url),
                        mock.patch.object(TelegramNotifier, "coalesce_seconds", 0.05)]
        for p in self.patches:
            p.start()
        cfg_path = os.path.join(self.tmp, "config.ini")
        with open(cfg_path, "w", encoding="utf-8") as fh:
            fh.write("[general]\ndata_dir = ./data\nlog_level = WARNING\n[paths]\ndownload_dir = %s\n"
                     "movies_dir = %s/video/Films\nseries_dir = %s/video/TvShows\n[web]\nenabled = false\n"
                     "[telegram]\nbot_token = %s\nchat_id = %s\nevents = error\n"
                     % (self.tmp, self.tmp, self.tmp, TOKEN, CHAT))
        self.app = App(load_config(cfg_path))
        self.bot = TelegramBot(self.app, TOKEN, CHAT, api_url=self.api.srv.url)
        self.bot.poll_timeout = 1

    def tearDown(self) -> None:
        self.bot.stop()
        if getattr(self.app, "bot", None) is not None:
            self.app.bot.stop()
        self.app._flush_notifier(2)
        self.app._stop.set()
        time.sleep(0.1)
        self.app.db.close()
        for p in self.patches:
            p.stop()
        self.api.srv.__exit__(None, None, None)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def say(self, text: str, chat: Any = CHAT):
        return self.bot.handle_update({"update_id": 1, "message": {"chat": {"id": int(chat)}, "text": text}})


class CommandsTest(BotTestBase):
    def test_help_lists_every_command(self) -> None:
        answer = self.say("/help")
        self.assertEqual(answer, HELP_TEXT)
        for name in ("/statut", "/telechargements", "/watchlist", "/avenir", "/recherche", "/envoyer", "/ajouter",
                     "/sync", "/traiter", "/rss", "/espace", "/maj", "/help"):
            self.assertIn(name, HELP_TEXT)
        for handler in self.bot.handlers:
            self.assertIn("/" + handler, HELP_TEXT)
        sent = self.api.sent()
        self.assertEqual(len(sent), 1)
        self.assertEqual(str(sent[0]["chat_id"]), CHAT)
        self.assertEqual(sent[0]["parse_mode"], "HTML")

    def test_aliases_and_bot_suffix(self) -> None:
        self.assertEqual(self.say("/start"), HELP_TEXT)
        self.assertEqual(self.say("/help@synoarr_bot"), HELP_TEXT)
        self.assertIn("synoarr", self.say("/status"))

    def test_other_chat_is_ignored(self) -> None:
        self.assertIsNone(self.say("/sync", chat=999))
        self.assertEqual(self.api.sent(), [])

    def test_unknown_and_plain_text(self) -> None:
        self.assertIn("Commande inconnue", self.say("/foo"))
        self.assertIn("/help", self.say("bonjour"))

    def test_status(self) -> None:
        answer = self.say("/statut")
        self.assertIn("Indexers actifs", answer)
        self.assertIn("Téléchargements en cours : 0", answer)

    def test_watchlist_and_upcoming(self) -> None:
        mid = self.app.db.upsert_media("plex://m1", "movie", "Night of the Living Dead", 1968, None, None, None)
        self.app.db.execute("UPDATE media SET on_watchlist = 1, status = 'wanted' WHERE id = ?", (mid,))
        sid = self.app.db.upsert_media("plex://s1", "show", "The Office", 2005, None, None, None)
        self.app.db.execute("UPDATE media SET on_watchlist = 1, status = 'monitored' WHERE id = ?", (sid,))
        tomorrow = (_dt.date.today() + _dt.timedelta(days=1)).isoformat()
        self.app.db.upsert_episode(sid, 2, 5, "Halloween", tomorrow)
        wl = self.say("/watchlist")
        self.assertIn("1 film(s)", wl)
        self.assertIn("Night of the Living Dead (1968)", wl)
        up = self.say("/avenir")
        self.assertIn("The Office S02E05", up)
        self.assertIn(tomorrow, up)

    def test_upcoming_empty(self) -> None:
        self.assertIn("Rien de prévu", self.say("/avenir"))

    def test_downloads_empty(self) -> None:
        self.assertIn("Aucun téléchargement", self.say("/dl"))

    def test_downloads_list(self) -> None:
        with mock.patch.object(self.app, "downloads_status", return_value={"items": [
                {"media_title": "The Office", "release_title": "x", "season": 2, "episode": 5, "progress": 42.5,
                 "status": "downloading", "speed_down": 2 * 1024 * 1024}]}):
            answer = self.say("/telechargements")
        self.assertIn("The Office S02E05", answer)
        self.assertIn("42 %", answer)
        self.assertIn("2.0 Mo/s", answer)

    def test_search_then_grab(self) -> None:
        rel = Release(title="Night.of.the.Living.Dead.1968.1080p", indexer="Internet Archive",
                      download_url="http://x/a.torrent", size=2 * 1024 ** 3, seeders=12)
        bad = Release(title="Other.2001.CAM", indexer="IA", download_url="http://x/b.torrent")
        with mock.patch.object(self.app.searcher, "search_report",
                               return_value=[(rel, 100, "ok"), (bad, None, "rejeté")]) as rep:
            answer = self.say("/recherche Night of the Living Dead 1968")
        self.assertEqual(rep.call_args[0][0].title, "Night of the Living Dead")
        self.assertIn("1. Night.of.the.Living.Dead.1968.1080p", answer)
        self.assertNotIn("CAM", answer)
        self.assertIn("2.0 Go", answer)
        self.assertIn("12 seeders", answer)
        with mock.patch.object(self.app.searcher, "grab", return_value=True) as grab:
            answer = self.say("/envoyer 1")
        self.assertIn("Envoyé à Download Station", answer)
        self.assertIs(grab.call_args[0][0], rel)
        self.assertTrue(grab.call_args[1]["resend"])

    def test_grab_failure_and_bad_number(self) -> None:
        self.assertIn("lancez d'abord", self.say("/envoyer 3"))
        self.assertIn("Usage", self.say("/envoyer abc"))
        self.bot._results = [Release(title="A.2000", indexer="IA", magnet="magnet:?xt=urn:btih:" + "a" * 40)]

        def fail(*_a: Any, **kw: Any) -> bool:
            kw["error_out"].append("Download Station injoignable")
            return False
        with mock.patch.object(self.app.searcher, "grab", side_effect=fail):
            self.assertIn("Download Station injoignable", self.say("/envoyer 1"))

    def test_search_without_result(self) -> None:
        self.assertIn("Précisez", self.say("/recherche"))
        with mock.patch.object(self.app.searcher, "search_report", return_value=[]):
            self.assertIn("Aucune release acceptable", self.say("/recherche Inconnu 1999"))

    def test_add_flow(self) -> None:
        cands = [{"title": "Dune", "year": 2021, "type": "movie"},
                 {"title": "Dune", "year": 2000, "type": "show", "in_watchlist": True}]
        with mock.patch.object(self.app, "search_titles", return_value={"results": cands}):
            answer = self.say("/ajouter Dune")
        self.assertIn("1. Dune (2021) — film", answer)
        self.assertIn("2. Dune (2000) — série · déjà dans la liste", answer)
        with mock.patch.object(self.app, "add_to_watchlist",
                               return_value={"id": 7, "title": "Dune", "year": 2021, "status": "wanted"}) as add, \
                mock.patch.object(self.app, "search_now") as search:
            answer = self.say("/ajouter 1")
        self.assertIn("Ajouté : Dune (2021)", answer)
        self.assertEqual(add.call_args[0][0]["year"], 2021)
        search.assert_called_once_with(7)

    def test_sync_process_rss(self) -> None:
        with mock.patch.object(self.app, "run_watchlist_now", return_value=True):
            self.assertIn("lancées", self.say("/sync"))
        with mock.patch.object(self.app, "run_watchlist_now", return_value=False):
            self.assertIn("déjà en cours", self.say("/sync"))
        with mock.patch.object(self.app, "run_process_now", return_value=True):
            self.assertIn("lancé", self.say("/traiter"))
        with mock.patch.object(self.app, "start_command", return_value=(None, True)) as cmd:
            self.assertIn("RSS", self.say("/rss"))
        cmd.assert_called_once_with("rss_sync")

    def test_space(self) -> None:
        answer = self.say("/espace")  # the folders do not exist: nearest existing parent is used
        self.assertIn("libres sur", answer)

    def test_error_is_reported_not_raised(self) -> None:
        with mock.patch.object(self.app, "search_titles", side_effect=ValueError("Saisissez un titre")):
            self.api.push("/ajouter x")
            self.bot.start()
            sent = self.api.wait_sent(1)
        # first start skipped the backlog: nothing executed
        self.assertEqual(sent, [])
        self.api.push("/ajouter y")
        with mock.patch.object(self.app, "search_titles", side_effect=ValueError("Saisissez un titre")):
            sent = self.api.wait_sent(1)
        self.assertEqual(len(sent), 1)
        self.assertIn("Saisissez un titre", sent[0]["text"])

    def test_long_message_is_split(self) -> None:
        parts = TelegramBot._split("\n".join("ligne %d %s" % (i, "x" * 80) for i in range(200)))
        self.assertGreater(len(parts), 1)
        self.assertTrue(all(len(p) <= 4000 for p in parts))


class UpdateCommandTest(BotTestBase):
    def fake_update(self, check: Dict[str, Any], apply: Dict[str, Any]):
        calls = []  # type: List[str]

        def run(job: Any, command: str) -> Dict[str, Any]:
            calls.append(command)
            if command == "update_check":
                job.emit("info", "2 fichier(s) modifié(s)")
                return check
            job.emit("ok", "Fichiers remplacés")
            return dict(apply)
        return mock.patch.object(self.app, "_cmd_update", side_effect=run), calls

    def test_check_available_then_install(self) -> None:
        patch, calls = self.fake_update({"available": True, "changed": ["a.py", "b.py"]},
                                        {"updated": True, "restarting": True})
        with patch:
            answer = self.say("/maj")
            self.assertIn("Mise à jour disponible", answer)
            self.assertIn("2 fichier(s) modifié(s)", answer)
            self.assertIn("/maj oui", answer)
            answer = self.say("/update oui")
        self.assertEqual(calls, ["update_check", "update"])
        self.assertIn("Mise à jour installée", answer)
        self.assertIn("Fichiers remplacés", answer)
        self.assertEqual(self.api.sent()[1]["text"], "⏳ Mise à jour de synoarr en cours…")
        self.assertEqual(self.app.db.get_kv("telegram_update_pending"), "1")
        # after the restart: one confirmation, then nothing more
        self.app.db.set_kv("telegram_announced", ANNOUNCE_ID)
        self.bot.announce()
        self.bot.announce()
        texts = [m["text"] for m in self.api.sent()]
        self.assertEqual(sum("a redémarré" in t for t in texts), 1)
        self.assertIsNone(self.app.db.get_kv("telegram_update_pending"))

    def test_up_to_date(self) -> None:
        patch, _calls = self.fake_update({"available": False}, {"updated": False})
        with patch:
            self.assertIn("synoarr est à jour", self.say("/maj"))
            self.assertIn("déjà à jour", self.say("/maj oui"))
        self.assertIsNone(self.app.db.get_kv("telegram_update_pending"))

    def test_failure_and_usage(self) -> None:
        patch, _calls = self.fake_update({"ok": False, "message": "GitHub injoignable"},
                                         {"ok": False, "message": "GitHub injoignable"})
        with patch:
            self.assertIn("Vérification impossible", self.say("/maj"))
            self.assertIn("Mise à jour impossible", self.say("/maj oui"))
        self.assertIsNone(self.app.db.get_kv("telegram_update_pending"))
        self.assertIn("Usage", self.say("/maj peut-être"))


class PollingTest(BotTestBase):
    def test_backlog_skipped_then_commands_executed_once(self) -> None:
        self.api.push("/sync")  # typed before synoarr listened: must not run
        with mock.patch.object(self.app, "run_watchlist_now", return_value=True) as sync:
            self.bot.start()
            deadline = time.time() + 3
            while self.app.db.get_kv("telegram_offset") is None and time.time() < deadline:
                time.sleep(0.02)
            self.assertEqual(self.app.db.get_kv("telegram_offset"), "101")
            self.api.push("/help")
            self.api.push("/statut", chat=999)  # stranger: ignored
            sent = self.api.wait_sent(1)
            time.sleep(0.3)
        sync.assert_not_called()
        self.assertEqual(len(self.api.sent()), 1)
        self.assertEqual(sent[0]["text"], HELP_TEXT)
        self.assertEqual(self.app.db.get_kv("telegram_offset"), "103")

    def test_offset_survives_restart(self) -> None:
        self.app.db.set_kv("telegram_offset", "100")
        self.api.push("/help")  # update 100, received while synoarr was restarting: executed
        self.bot.start()
        self.assertEqual(len(self.api.wait_sent(1)), 1)


class AnnounceAndWiringTest(BotTestBase):
    def test_announce_once(self) -> None:
        self.bot.announce()
        self.bot.announce()
        sent = self.api.sent()
        self.assertEqual(len(sent), 1)
        self.assertIn("est à jour", sent[0]["text"])
        self.assertIn("/help", sent[0]["text"])
        self.assertEqual(self.app.db.get_kv("telegram_announced"), ANNOUNCE_ID)

    def test_app_setup_bot_starts_announces_and_survives_reload(self) -> None:
        self.app._setup_bot()
        bot = self.app.bot
        self.assertIsNotNone(bot)
        self.assertEqual(len(self.api.wait_sent(1)), 1)  # announcement
        self.app._build_components()  # config reload with the same token: same bot
        self.assertIs(self.app.bot, bot)
        self.app.cfg.telegram.commands = False
        self.app._build_components()
        self.assertIsNone(self.app.bot)
        self.assertTrue(bot._stop.is_set())

    def test_disabled_without_token(self) -> None:
        self.app.cfg.telegram.bot_token = ""
        self.assertIsNone(telegram_bot.from_app(self.app))

    def test_config_option(self) -> None:
        self.assertTrue(self.app.cfg.telegram.commands)


if __name__ == "__main__":
    unittest.main()
