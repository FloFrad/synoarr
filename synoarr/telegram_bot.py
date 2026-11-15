"""Pilot synoarr from Telegram: the bot answers commands sent in the configured chat ONLY.

Long polling (getUpdates) in a background thread, stdlib only. Every message from another chat is ignored
(never answered, logged once), so nobody else can drive synoarr through the bot.
"""
from __future__ import annotations

import datetime as _dt
import html
import logging
import os
import shutil
import threading
import time
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Optional

from . import __version__
from .httpclient import HttpClient

if TYPE_CHECKING:  # pragma: no cover
    from .app import App

log = logging.getLogger(__name__)

ANNOUNCE_ID = "telegram-commands-3"  # bump to announce a new batch of commands once
HELP_TEXT = """<b>Commandes synoarr</b>
/menu — menu à boutons
/statut — état de synoarr
/telechargements — téléchargements en cours
/watchlist — résumé de la Watchlist
/avenir — épisodes et films des 7 prochains jours
/recherche <i>titre</i> — chercher une release (ex. /recherche Night of the Living Dead 1968)
/envoyer <i>n</i> — envoyer le résultat n° n de la dernière recherche
/ajouter <i>titre</i> — chercher un film ou une série à ajouter
/ajouter <i>n</i> — ajouter le résultat n° n
/sync — synchroniser la Watchlist et lancer les recherches
/traiter — ranger les téléchargements terminés
/rss — lire les flux RSS des indexers
/espace — espace libre de la bibliothèque
/maj — vérifier les mises à jour de synoarr
/maj oui — installer la mise à jour et redémarrer
/help — cette aide"""

ALIASES = {"start": "help", "aide": "help", "status": "statut", "dl": "telechargements",
           "downloads": "telechargements", "search": "recherche", "grab": "envoyer", "add": "ajouter",
           "process": "traiter", "space": "espace", "upcoming": "avenir", "update": "maj", "miseajour": "maj",
           "mettreajour": "maj"}
YES = ("oui", "o", "yes", "y", "ok", "go")
LEVEL_ICON = {"ok": "✅", "err": "❌", "warn": "⚠️", "info": "•"}

# Telegram's own "Menu" button (next to the text field) lists these commands (setMyCommands)
BOT_COMMANDS = [("menu", "Menu à boutons"), ("statut", "État de synoarr"),
                ("telechargements", "Téléchargements en cours"), ("watchlist", "Résumé de la Watchlist"),
                ("avenir", "Les 7 prochains jours"), ("recherche", "Chercher une release : /recherche titre"),
                ("ajouter", "Ajouter un film ou une série : /ajouter titre"),
                ("sync", "Synchroniser la Watchlist"), ("traiter", "Ranger les téléchargements terminés"),
                ("rss", "Lire les flux RSS"), ("espace", "Espace libre"), ("maj", "Mises à jour de synoarr"),
                ("help", "Aide")]
# buttons of /menu: (label, callback data). "cmd:x" runs /x, "ask:x" asks for the argument of /x first
MENU_BUTTONS = [[("📊 Statut", "cmd:statut"), ("📥 Téléchargements", "cmd:telechargements")],
                [("👀 Watchlist", "cmd:watchlist"), ("📅 À venir", "cmd:avenir")],
                [("🔎 Rechercher", "ask:recherche"), ("➕ Ajouter", "ask:ajouter")],
                [("🔄 Synchroniser", "cmd:sync"), ("📦 Ranger", "cmd:traiter")],
                [("📡 Flux RSS", "cmd:rss"), ("💾 Espace", "cmd:espace")],
                [("🆕 Mises à jour", "cmd:maj"), ("❓ Aide", "cmd:help")]]
ASK_TEXT = {"recherche": "🔎 Quel titre rechercher ? (ex. Night of the Living Dead 1968)",
            "ajouter": "➕ Quel film ou quelle série ajouter ? (ex. Dune)"}
MENU_ROW = [("📋 Menu", "cmd:menu")]


def keyboard(rows: List[List[Any]]) -> Dict[str, Any]:
    return {"inline_keyboard": [[{"text": label, "callback_data": data} for label, data in row] for row in rows]}


def numbered_buttons(prefix: str, icon: str, count: int, per_row: int = 4) -> List[List[Any]]:
    btns = [("%s %d" % (icon, i), "cmd:%s %d" % (prefix, i)) for i in range(1, count + 1)]
    return [btns[i:i + per_row] for i in range(0, len(btns), per_row)]


class Answer(str):
    """A reply text (behaves as str) that may carry buttons (Telegram reply_markup)."""
    markup = None  # type: Optional[Dict[str, Any]]

    def __new__(cls, text: str, markup: Optional[Dict[str, Any]] = None) -> "Answer":
        obj = str.__new__(cls, text)
        obj.markup = markup
        return obj


def esc(value: Any) -> str:
    return html.escape(str(value), quote=False)


def human(n: Any) -> str:
    try:
        size = float(n or 0)
    except (TypeError, ValueError):
        return "?"
    for unit in ("o", "Ko", "Mo", "Go", "To"):
        if size < 1024 or unit == "To":
            return ("%.0f %s" if unit in ("o", "Ko") else "%.1f %s") % (size, unit)
        size /= 1024
    return "?"


class TelegramBot(object):
    poll_timeout = 25  # seconds of long polling per getUpdates call
    allowed_updates = ["message", "callback_query"]  # callback_query: taps on buttons

    def __init__(self, app: "App", bot_token: str, chat_id: str, api_url: str = "https://api.telegram.org",
                 http: Optional[HttpClient] = None):
        self.app = app
        self.bot_token = (bot_token or "").strip()
        self.chat_id = str(chat_id or "").strip()
        self.api_url = api_url.rstrip("/")
        self.http = http or HttpClient(timeout=self.poll_timeout + 15, retries=0)
        self._stop = threading.Event()
        self._thread = None  # type: Optional[threading.Thread]
        self._results = []  # type: List[Any]   # last /recherche results (Release objects)
        self._candidates = []  # type: List[Dict[str, Any]]  # last /ajouter candidates
        self._warned = set()  # type: set
        self._pending = None  # type: Optional[str]  # command waiting for its argument (menu Rechercher/Ajouter)
        self.handlers = {
            "menu": self.cmd_menu, "help": self.cmd_help, "statut": self.cmd_status, "telechargements": self.cmd_downloads,
            "watchlist": self.cmd_watchlist, "avenir": self.cmd_upcoming, "recherche": self.cmd_search,
            "envoyer": self.cmd_grab, "ajouter": self.cmd_add, "sync": self.cmd_sync, "traiter": self.cmd_process,
            "rss": self.cmd_rss, "espace": self.cmd_space, "maj": self.cmd_update,
        }  # type: Dict[str, Callable[[str], str]]

    @property
    def enabled(self) -> bool:
        return bool(self.bot_token and self.chat_id)

    # ------------------------------------------------------------------ API
    def _call(self, method: str, payload: Dict[str, Any], timeout: Optional[float] = None) -> Any:
        url = "%s/bot%s/%s" % (self.api_url, self.bot_token, method)
        try:
            resp = self.http.post(url, json_body=payload, timeout=timeout or self.http.timeout,
                                  raise_for_status=False)
            data = resp.json()
        except Exception as exc:
            log.debug("Telegram %s impossible : %s", method, str(exc).replace(self.bot_token, "***"))
            return None
        if isinstance(data, dict) and data.get("ok"):
            return data.get("result")
        log.debug("Telegram %s refusé : %s", method, str(data)[:200].replace(self.bot_token, "***"))
        return None

    def reply(self, text: str, markup: Optional[Dict[str, Any]] = None) -> bool:
        if markup is None:
            markup = getattr(text, "markup", None)
        parts = self._split(str(text))
        for i, part in enumerate(parts):
            payload = {"chat_id": self.chat_id, "text": part, "parse_mode": "HTML",
                       "disable_web_page_preview": True}  # type: Dict[str, Any]
            if markup and i == len(parts) - 1:  # buttons under the last part only
                payload["reply_markup"] = markup
            if self._call("sendMessage", payload, timeout=20) is None:
                return False
        return True

    def register_commands(self) -> bool:
        """Fill Telegram's "Menu" button (left of the text field) with the commands, for this chat only."""
        return self._call("setMyCommands", {
            "commands": [{"command": c, "description": d} for c, d in BOT_COMMANDS],
            "scope": {"type": "chat", "chat_id": self.chat_id}}, timeout=20) is not None

    @staticmethod
    def _split(text: str, limit: int = 4000) -> List[str]:
        parts, cur = [], ""
        for line in text.split("\n"):
            if len(cur) + len(line) + 1 > limit and cur:
                parts.append(cur)
                cur = ""
            cur += ("\n" if cur else "") + line[:limit]
        return parts + ([cur] if cur else [])

    # -------------------------------------------------------------- polling
    def start(self) -> None:
        if not self.enabled or self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, name="telegram-bot", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _offset(self) -> Optional[int]:
        raw = self.app.db.get_kv("telegram_offset")
        try:
            return int(raw) if raw else None
        except ValueError:
            return None

    def _run(self) -> None:
        offset = self._offset()
        if offset is None:  # first start: never execute commands typed before synoarr listened
            updates = self._call("getUpdates", {"timeout": 0, "allowed_updates": self.allowed_updates},
                                 timeout=20) or []
            if updates:
                offset = max(u.get("update_id", 0) for u in updates if isinstance(u, dict)) + 1
                self.app.db.set_kv("telegram_offset", str(offset))
        errors = 0
        while not self._stop.is_set():
            payload = {"timeout": self.poll_timeout, "allowed_updates": self.allowed_updates}  # type: Dict[str, Any]
            if offset is not None:
                payload["offset"] = offset
            updates = self._call("getUpdates", payload)
            if updates is None:
                errors += 1
                self._stop.wait(min(60, 5 * errors))
                continue
            errors = 0
            for upd in updates:
                if not isinstance(upd, dict):
                    continue
                offset = int(upd.get("update_id", 0)) + 1
                self.app.db.set_kv("telegram_offset", str(offset))
                try:
                    self.handle_update(upd)
                except Exception as exc:  # one bad command must never stop the bot
                    log.warning("Commande Telegram en erreur : %s", exc)
                    self.reply("❌ Erreur : %s" % esc(exc))

    def handle_update(self, upd: Dict[str, Any]) -> Optional[str]:
        """Execute one update; returns the reply text (also sent), or None when ignored."""
        if upd.get("callback_query"):
            return self.handle_callback(upd["callback_query"])
        msg = upd.get("message") or {}
        chat = msg.get("chat") or {}
        text = (msg.get("text") or "").strip()
        if not self._authorized(chat):
            return None
        pending, self._pending = self._pending, None
        if text.startswith("/"):
            head, _, args = text[1:].partition(" ")
            answer = self.run_command(head.split("@", 1)[0], args)
        elif pending and text:  # answer to "Quel titre rechercher ?"
            answer = self.run_command(pending, text)
        else:
            answer = Answer("Tapez /menu (boutons) ou /help pour la liste des commandes.", keyboard(MENU_BUTTONS))
        self.reply(answer)
        return answer

    def handle_callback(self, cq: Dict[str, Any]) -> Optional[str]:
        """A tap on a button: same effect as typing the command."""
        chat = (cq.get("message") or {}).get("chat") or {}
        if not self._authorized(chat):
            return None
        self._call("answerCallbackQuery", {"callback_query_id": cq.get("id")}, timeout=20)  # stop the spinner
        kind, _, rest = str(cq.get("data") or "").partition(":")
        name, _, args = rest.partition(" ")
        self._pending = None
        if kind == "ask" and name in ASK_TEXT:
            self._pending = name
            answer = Answer(ASK_TEXT[name], {"force_reply": True, "input_field_placeholder": "Titre"})
        elif kind == "cmd":
            answer = self.run_command(name, args)
        else:
            return None
        self.reply(answer)
        return answer

    def _authorized(self, chat: Dict[str, Any]) -> bool:
        if str(chat.get("id")) == self.chat_id:
            return True
        cid = chat.get("id")
        if cid not in self._warned:
            self._warned.add(cid)
            log.warning("Telegram : message ignoré d'une conversation non autorisée (chat %s)", cid)
        return False

    def run_command(self, name: str, args: str = "") -> str:
        name = ALIASES.get(name.lower(), name.lower())
        handler = self.handlers.get(name)
        return handler(args.strip()) if handler else "Commande inconnue : /%s — tapez /help" % esc(name)

    def announce(self) -> None:
        """At startup: confirm an update asked with /maj oui, and tell once that the commands are available."""
        if not self.enabled:
            return
        self.register_commands()
        if self.app.db.get_kv("telegram_update_pending"):
            if self.reply("✅ <b>synoarr a redémarré</b> après la mise à jour (version %s)." % esc(__version__)):
                self.app.db.set_kv("telegram_update_pending", None)
        if self.app.db.get_kv("telegram_announced") == ANNOUNCE_ID:
            return
        if self.reply("🔄 <b>synoarr %s est à jour</b>\nVous pouvez maintenant piloter synoarr depuis Telegram, "
                      "avec les boutons ci-dessous ou /menu.\n\n%s" % (esc(__version__), HELP_TEXT),
                      keyboard(MENU_BUTTONS)):
            self.app.db.set_kv("telegram_announced", ANNOUNCE_ID)

    # ------------------------------------------------------------- commands
    def cmd_menu(self, args: str) -> str:
        return Answer("📋 <b>Menu synoarr</b>\nChoisissez une action :", keyboard(MENU_BUTTONS))

    def cmd_help(self, args: str) -> str:
        return Answer(HELP_TEXT, keyboard(MENU_BUTTONS))

    def cmd_status(self, args: str) -> str:
        st = self.app.status()
        db = self.app.db
        wanted_movies = len([m for m in db.list_media(type_="movie", on_watchlist_only=True)
                             if m["status"] == "wanted"])
        wanted_eps = (db.query_one("SELECT COUNT(*) AS n FROM episodes e JOIN media m ON m.id = e.media_id "
                                   "WHERE e.status = 'wanted' AND m.on_watchlist = 1 AND m.status = 'monitored' "
                                   "AND (e.air_date IS NULL OR e.air_date <= date('now'))") or {}).get("n", 0)
        sending = len(db.list_grabs(status="sent"))
        lines = ["ℹ️ <b>synoarr %s</b>" % esc(st.get("version", __version__)),
                 "Plex : %s · Download Station : %s · TMDb : %s" % (
                     "✅" if st.get("plex_configured") else "❌", "✅" if st.get("ds_configured") else "❌",
                     "✅" if st.get("tmdb_configured") else "—"),
                 "Indexers actifs : %s" % st.get("indexers", "?"),
                 "Recherchés : %d film(s), %d épisode(s)" % (wanted_movies, wanted_eps),
                 "Téléchargements en cours : %d" % sending,
                 "Dernière synchro Watchlist : %s" % esc(st.get("last_watchlist_run") or "jamais")]
        if st.get("dry_run"):
            lines.append("⚠️ mode simulation (dry_run) actif")
        return "\n".join(lines)

    def cmd_downloads(self, args: str) -> str:
        d = self.app.downloads_status()
        items = d.get("items") or []
        if not items:
            return "Aucun téléchargement en cours." + (" (%s)" % esc(d["warning"]) if d.get("warning") else "")
        lines = ["📥 <b>Téléchargements</b>"]
        for x in items[:20]:
            pct = "%d %%" % x["progress"] if x.get("progress") is not None else "?"
            speed = " · %s/s" % human(x["speed_down"]) if x.get("speed_down") else ""
            what = x.get("media_title") or x.get("release_title")
            if x.get("season") is not None:
                what += " S%02d" % x["season"] + ("E%02d" % x["episode"] if x.get("episode") is not None else "")
            lines.append("• %s — %s (%s)%s" % (esc(what), pct, esc(x.get("status")), speed))
        return Answer("\n".join(lines), keyboard([[("🔄 Actualiser", "cmd:telechargements")] + MENU_ROW]))

    def cmd_watchlist(self, args: str) -> str:
        medias = self.app.db.list_media(on_watchlist_only=True)
        movies = [m for m in medias if m["type"] == "movie"]
        shows = [m for m in medias if m["type"] == "show"]
        done = len([m for m in movies if m["status"] == "done"])
        wanted = [m for m in movies if m["status"] == "wanted"]
        lines = ["👀 <b>Watchlist</b> : %d film(s) (%d importé(s)), %d série(s)" % (len(movies), done, len(shows))]
        if wanted:
            lines.append("Films recherchés :")
            lines += ["• %s%s" % (esc(m["title"]), " (%s)" % m["year"] if m.get("year") else "") for m in wanted[:15]]
        return "\n".join(lines)

    def cmd_upcoming(self, args: str) -> str:
        today = _dt.date.today()
        end = today + _dt.timedelta(days=7)
        rows = self.app.db.query(
            "SELECT m.title, e.season, e.episode, e.air_date, e.status FROM episodes e JOIN media m ON m.id = e.media_id "
            "WHERE m.on_watchlist = 1 AND m.status = 'monitored' AND e.air_date >= ? AND e.air_date <= ? "
            "ORDER BY e.air_date, m.title LIMIT 40", (today.isoformat(), end.isoformat()))
        if not rows:
            return "Rien de prévu ces 7 prochains jours."
        lines = ["📅 <b>À venir (7 jours)</b>"]
        for r in rows:
            lines.append("• %s — %s S%02dE%02d" % (esc(r["air_date"]), esc(r["title"]), r["season"], r["episode"]))
        return "\n".join(lines)

    def cmd_search(self, args: str) -> str:
        if not args:
            return "Précisez un titre : /recherche Night of the Living Dead 1968"
        from .app import request_from_query  # same parsing as the web manual search
        req = request_from_query(args)
        report = self.app.searcher.search_report(req)
        ok = [(r, s, why) for r, s, why in report if s is not None][:8]
        self._results = [r for r, _s, _w in ok]
        if not ok:
            return "Aucune release acceptable pour « %s » (%d résultat(s) rejeté(s))." % (esc(args), len(report))
        lines = ["🔎 <b>%s</b> — %d acceptable(s) :" % (esc(req.describe()), len(ok))]
        for i, (r, _s, _w) in enumerate(ok, 1):
            kind = "magnet" if r.magnet and not r.download_url else ".torrent"
            lines.append("%d. %s\n   %s · %s seeders · %s · %s" % (
                i, esc(r.title), human(r.size), "?" if r.seeders is None else r.seeders, esc(r.indexer), kind))
        lines.append("Envoyer : bouton 📥 ou /envoyer <i>n</i>")
        return Answer("\n".join(lines), keyboard(numbered_buttons("envoyer", "📥", len(ok)) + [MENU_ROW]))

    def cmd_grab(self, args: str) -> str:
        try:
            n = int(args)
        except ValueError:
            return "Usage : /envoyer <i>n</i> (numéro d'un résultat de /recherche)"
        if not 1 <= n <= len(self._results):
            return "Numéro inconnu : lancez d'abord /recherche <i>titre</i>"
        rel = self._results[n - 1]
        media, season, episode = None, None, None
        try:
            media, season, eps = self.app._match_media(rel.title)
            episode = eps[0] if eps and len(eps) == 1 else None
        except Exception:
            media = None
        errors = []  # type: List[str]
        if self.app.searcher.grab(rel, media, season, episode, error_out=errors, resend=True):
            return "📥 Envoyé à Download Station : %s" % esc(rel.title)
        return "❌ Échec : %s" % esc(errors[0] if errors else "envoi refusé")

    def cmd_add(self, args: str) -> str:
        if args.isdigit():
            n = int(args)
            if not 1 <= n <= len(self._candidates):
                return "Numéro inconnu : lancez d'abord /ajouter <i>titre</i>"
            row = self.app.add_to_watchlist(self._candidates[n - 1], True)
            if row.get("id") and row.get("status") in ("wanted", "monitored"):
                self.app.search_now(int(row["id"]))
            label = "%s%s" % (row.get("title"), " (%s)" % row["year"] if row.get("year") else "")
            if row.get("already_present"):
                return "Déjà dans la liste : %s" % esc(label)
            return "👀 Ajouté : %s — recherche lancée" % esc(label)
        if not args:
            return "Précisez un titre : /ajouter Dune"
        found = self.app.search_titles(args) or {}
        self._candidates = list(found.get("results") or [])[:6]
        if not self._candidates:
            return "Aucun film ni série trouvé pour « %s »." % esc(args)
        lines = ["Résultats pour « %s » :" % esc(args)]
        for i, c in enumerate(self._candidates, 1):
            lines.append("%d. %s%s — %s%s" % (i, esc(c.get("title")), " (%s)" % c["year"] if c.get("year") else "",
                                              "film" if c.get("type") == "movie" else "série",
                                              " · déjà dans la liste" if c.get("in_watchlist") else ""))
        lines.append("Ajouter : bouton ➕ ou /ajouter <i>n</i>")
        return Answer("\n".join(lines),
                      keyboard(numbered_buttons("ajouter", "➕", len(self._candidates), 3) + [MENU_ROW]))

    def cmd_sync(self, args: str) -> str:
        return ("🔄 Synchronisation de la Watchlist et recherches lancées." if self.app.run_watchlist_now()
                else "Une synchronisation est déjà en cours.")

    def cmd_process(self, args: str) -> str:
        return ("📦 Traitement des téléchargements lancé." if self.app.run_process_now()
                else "Un traitement est déjà en cours.")

    def cmd_rss(self, args: str) -> str:
        try:
            _job, created = self.app.start_command("rss_sync")
        except ValueError as exc:
            return "❌ %s" % esc(exc)
        return "📡 Lecture des flux RSS lancée." if created else "Une lecture des flux RSS est déjà en cours."

    job_timeout = 600.0  # seconds: an update downloads the GitHub archive

    def _run_job(self, command: str) -> Any:
        """Start a web command and wait for its end; returns the Job (or None when one is already running)."""
        job, created = self.app.start_command(command)
        if not created:
            return None
        deadline = time.time() + self.job_timeout
        while job.running and time.time() < deadline and not self._stop.is_set():
            time.sleep(0.2)
        return job

    @staticmethod
    def _job_lines(job: Any, limit: int = 15) -> List[str]:
        out = list(getattr(job, "output", None) or [])[-limit:]
        return ["%s %s" % (LEVEL_ICON.get(level, "•"), esc(text)) for level, text in out]

    def cmd_update(self, args: str) -> str:
        if args.strip().lower() in YES:
            self.reply("⏳ Mise à jour de synoarr en cours…")
            self.app.db.set_kv("telegram_update_pending", "1")
            job = self._run_job("update")
            if job is None:
                self.app.db.set_kv("telegram_update_pending", None)
                return "Une mise à jour est déjà en cours."
            res = job.result if isinstance(job.result, dict) else {}
            lines = self._job_lines(job)
            if res.get("restarting"):
                return "\n".join(["🔄 <b>Mise à jour installée</b>, synoarr redémarre…"] + lines
                                 + ["Un message confirmera le redémarrage."])
            self.app.db.set_kv("telegram_update_pending", None)
            head = "❌ <b>Mise à jour impossible</b>" if (job.status == "error" or res.get("ok") is False) \
                else "✅ <b>synoarr est déjà à jour</b>"
            return "\n".join([head] + lines)
        if args.strip():
            return "Usage : /maj (vérifier) ou /maj oui (installer)"
        job = self._run_job("update_check")
        if job is None:
            return "Une mise à jour est déjà en cours."
        res = job.result if isinstance(job.result, dict) else {}
        lines = self._job_lines(job)
        if job.status == "error" or res.get("ok") is False:
            return "\n".join(["❌ <b>Vérification impossible</b>"] + lines)
        if res.get("available"):
            return Answer("\n".join(["🆕 <b>Mise à jour disponible</b> (version installée : %s)" % esc(__version__)]
                                     + lines + ["Installer et redémarrer : bouton ci-dessous ou /maj oui"]),
                          keyboard([[("⬇️ Installer et redémarrer", "cmd:maj oui")]]))
        return "\n".join(["✅ <b>synoarr est à jour</b> (version %s)" % esc(__version__)] + lines)

    def cmd_space(self, args: str) -> str:
        lines = ["💾 <b>Espace disque</b>"]
        seen = set()
        for label, path in (("Films", self.app.cfg.paths.movies_dir), ("Séries", self.app.cfg.paths.series_dir)):
            p = path
            while p and not os.path.exists(p) and os.path.dirname(p) != p:
                p = os.path.dirname(p)
            try:
                usage = shutil.disk_usage(p)
                dev = os.stat(p).st_dev
            except OSError:
                lines.append("%s : inaccessible" % label)
                continue
            if dev in seen:
                continue
            seen.add(dev)
            pct = 100.0 * usage.free / usage.total if usage.total else 0
            lines.append("%s : %s libres sur %s (%.0f %%)%s" % (
                esc(path), human(usage.free), human(usage.total), pct, " ⚠️" if pct < 10 else ""))
        return "\n".join(lines)


def from_app(app: "App") -> Optional[TelegramBot]:
    tcfg = getattr(app.cfg, "telegram", None)
    if tcfg is None or not getattr(tcfg, "commands", True) or not tcfg.bot_token or not tcfg.chat_id:
        return None
    return TelegramBot(app, tcfg.bot_token, tcfg.chat_id,
                       api_url=getattr(app, "telegram_api_url", None) or "https://api.telegram.org")


__all__ = ["TelegramBot", "from_app", "HELP_TEXT", "ANNOUNCE_ID", "MENU_BUTTONS", "Answer"]
