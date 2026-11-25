"""Notifications Telegram (stdlib uniquement).

``TelegramNotifier(bot_token, chat_id, http)`` envoie des messages via l'API Bot de Telegram
(``POST {api_url}/bot<TOKEN>/sendMessage``, JSON ``{chat_id, text, parse_mode: "HTML",
disable_web_page_preview: true}``).

- ``send(text)`` met le message dans une file : un thread d'arrière-plan l'envoie, un job (recherche,
  import) n'attend jamais Telegram. Ne lève jamais.
- Limite de débit : au plus ``max_per_minute`` messages par minute ; les rafales (plusieurs événements en
  quelques secondes, ou en attente d'une place dans la minute) sont regroupées en un seul message.
- ``send_now(text)`` envoie tout de suite (commande « Tester Telegram ») et retourne ``(ok, message)``.
- ``get_chats()`` lit ``getUpdates`` pour trouver le chat_id à mettre dans config.ini.
- ``on_event(kind, message, level)`` est branché sur ``Database.add_listener`` : les événements de
  l'historique (grab, import, download, watchlist, update...) deviennent des notifications selon
  ``[telegram] events``.

Le jeton du bot n'apparaît jamais dans le journal ni dans les messages d'erreur (``redact``).
"""
from __future__ import annotations

import html
import json
import logging
import queue
import re
import threading
import time
from typing import Any, Dict, Iterable, List, Optional, Tuple

from .httpclient import HttpClient, HttpError

log = logging.getLogger(__name__)

API_URL = "https://api.telegram.org"
MAX_LENGTH = 4096  # limite Telegram d'un message (caractères)

# événements configurables ([telegram] events)
EVENTS = ("grab", "import", "error", "watchlist", "upgrade", "update")
DEFAULT_EVENTS = ("grab", "import", "error")

ICONS = {
    "grab": "📥",
    "import": "✅",
    "error": "❌",
    "upgrade": "⬆️",
    "watchlist": "👀",
    "update": "🔄",
}
TITLES = {
    "grab": "Envoyé à Download Station",
    "import": "Importé dans la bibliothèque",
    "error": "Échec",
    "upgrade": "Amélioration de qualité",
    "watchlist": "Ajouté à la Watchlist",
    "update": "Mise à jour de synoarr",
}

_BOT_PATH_RE = re.compile(r"/bot[^/\s?#]+")
_UPGRADE_RE = re.compile(r"am[ée]lioration|upgrade", re.I)


def escape(text: Any) -> str:
    """Texte brut -> HTML Telegram (seuls <, > et & sont à échapper)."""
    return html.escape("" if text is None else str(text), quote=False)


def parse_events(value: Any) -> List[str]:
    """"grab, Import ;error" -> ["grab", "import", "error"] (inconnus ignorés)."""
    if isinstance(value, str):
        items = value.replace(";", ",").split(",")
    else:
        items = list(value or [])
    out = []  # type: List[str]
    for it in items:
        it = str(it).strip().lower()
        if it in EVENTS and it not in out:
            out.append(it)
    return out


def unknown_events(value: Any) -> List[str]:
    items = value.replace(";", ",").split(",") if isinstance(value, str) else list(value or [])
    return [str(it).strip() for it in items if str(it).strip() and str(it).strip().lower() not in EVENTS]


def classify(kind: str, message: str, level: str = "INFO") -> List[str]:
    """Événement de l'historique (db.add_event) -> types de notification candidats, du plus précis au plus
    général (le premier activé dans [telegram] events est utilisé). [] = pas de notification."""
    kind = (kind or "").lower()
    level = (level or "INFO").upper()
    msg = message or ""
    is_error = level in ("ERROR", "CRITICAL")
    if kind == "upgrade":
        if is_error:
            return ["error"]
        # searcher : « Amélioration de qualité envoyée à Download Station » remplace l'événement grab -> repli
        # sur grab si upgrade n'est pas activé ; organizer : « Amélioration importée : X remplace Y » s'ajoute à
        # l'événement import (déjà notifié) -> upgrade seulement
        if "download station" in msg.lower():
            return ["upgrade", "grab"]
        return ["upgrade"]
    if kind == "grab":
        if is_error:
            return ["error"]  # « Échec de l'envoi de ... »
        if msg.startswith("[dry-run]"):
            return []  # simulation : rien n'a été envoyé
        if _UPGRADE_RE.search(msg):
            return ["upgrade", "grab"]
        return ["grab"]
    if kind == "import":
        if is_error:
            return ["error"]  # « Import impossible de ... »
        if _UPGRADE_RE.search(msg):
            return ["upgrade", "import"]
        return ["import"]
    if kind == "download":
        # échec / bloqué sans source / abandonné ; WARNING : bloqué depuis des jours, source changée automatiquement
        return ["error"] if is_error or level == "WARNING" else []
    if kind == "watchlist":
        if not is_error and msg.startswith(("Ajouté", "Réactivé")):
            return ["watchlist"]
        return []
    if kind == "update":
        return [] if is_error else ["update"]
    return []


def format_event(event: str, message: str) -> str:
    """Message HTML (français) d'une notification (texte coupé avant échappement : HTML toujours valide)."""
    message = message or ""
    if len(message) > 1000:
        message = message[:999] + "…"
    return "%s <b>%s</b>\n%s" % (ICONS.get(event, "ℹ️"), escape(TITLES.get(event, event)), escape(message))


class TelegramNotifier:
    max_per_minute = 20
    coalesce_seconds = 2.0     # délai de regroupement d'une rafale
    queue_size = 500
    timeout = 20.0

    def __init__(self, bot_token: str, chat_id: str, http: Optional[HttpClient] = None,
                 events: Any = DEFAULT_EVENTS, api_url: Optional[str] = None):
        self.bot_token = (bot_token or "").strip()
        self.chat_id = (chat_id or "").strip()
        self.http = http or HttpClient(timeout=self.timeout, retries=1)
        self.events = parse_events(events)
        self.api_url = (api_url or API_URL).rstrip("/")
        self._queue = queue.Queue(self.queue_size)  # type: queue.Queue
        self._thread = None  # type: Optional[threading.Thread]
        self._lock = threading.Lock()
        self._sent = []  # type: List[float]  # horodatage des derniers envois (limite de débit)
        self._closed = False
        self.sent_count = 0

    # ------------------------------------------------------------- helpers
    @property
    def configured(self) -> bool:
        """Jeton présent (assez pour getUpdates)."""
        return bool(self.bot_token)

    @property
    def enabled(self) -> bool:
        return bool(self.bot_token and self.chat_id)

    def redact(self, text: Any) -> str:
        """Retire le jeton du bot d'un texte (URL, message d'erreur)."""
        s = "" if text is None else str(text)
        if self.bot_token:
            s = s.replace(self.bot_token, "***")
        return _BOT_PATH_RE.sub("/bot***", s)

    def _url(self, method: str) -> str:
        return "%s/bot%s/%s" % (self.api_url, self.bot_token, method)

    def _call(self, method: str, payload: Optional[Dict[str, Any]] = None) -> Tuple[bool, Any, str]:
        """Appel API -> (ok, result, message d'erreur français). Ne lève jamais, jeton masqué."""
        try:
            if payload is None:
                resp = self.http.get(self._url(method), timeout=self.timeout, raise_for_status=False)
            else:
                resp = self.http.post(self._url(method), json_body=payload, timeout=self.timeout,
                                      raise_for_status=False)
        except HttpError as exc:
            return False, None, "Telegram injoignable : %s" % self.redact(exc)
        except Exception as exc:  # pragma: no cover - défensif
            return False, None, "Erreur Telegram : %s" % self.redact(exc)
        try:
            data = json.loads(resp.text())
        except ValueError:
            data = None
        if isinstance(data, dict) and data.get("ok"):
            return True, data.get("result"), ""
        desc = ""
        retry_after = None
        if isinstance(data, dict):
            desc = str(data.get("description") or "")
            params = data.get("parameters") or {}
            if isinstance(params, dict):
                retry_after = params.get("retry_after")
        if not desc:
            desc = "HTTP %s" % resp.status
        if resp.status == 401 or "unauthorized" in desc.lower():
            hint = " (jeton du bot invalide : vérifiez [telegram] bot_token)"
        elif "chat not found" in desc.lower():
            hint = " (chat_id inconnu : envoyez d'abord un message à votre bot, puis vérifiez [telegram] chat_id)"
        else:
            hint = ""
        err = "Telegram a refusé la requête : %s%s" % (self.redact(desc), hint)
        return False, {"retry_after": retry_after, "status": resp.status}, err

    # ------------------------------------------------------------- envoi
    def send_now(self, text: str, escape_html: bool = True) -> Tuple[bool, str]:
        """Envoi synchrone. Retourne (ok, message français). Ne lève jamais."""
        if not self.enabled:
            return False, "Telegram n'est pas configuré ([telegram] bot_token et chat_id)"
        body = _fit(text) if escape_html else str(text)
        if len(body) > MAX_LENGTH:  # HTML déjà formé trop long (ne devrait pas arriver : voir merge)
            body = body[:MAX_LENGTH - 1] + "…"
        payload = {"chat_id": self.chat_id, "text": body, "parse_mode": "HTML",
                   "disable_web_page_preview": True}
        ok, result, err = self._call("sendMessage", payload)
        if not ok and isinstance(result, dict) and result.get("status") == 429:
            try:
                ra = result.get("retry_after")
                wait = min(60.0, max(0.0, float(5 if ra is None else ra)))
            except (TypeError, ValueError):
                wait = 5.0
            if not self._closed:
                time.sleep(wait)
                ok, result, err = self._call("sendMessage", payload)
        with self._lock:
            self._sent.append(time.time())
        if ok:
            self.sent_count += 1
            return True, "Message envoyé"
        return False, err

    def send(self, text: str, escape_html: bool = True) -> None:
        """Envoi asynchrone (file + thread). Ne lève jamais, ne bloque jamais."""
        if not self.enabled or self._closed:
            return
        body = _fit(text) if escape_html else str(text)
        try:
            self._queue.put_nowait(body)
        except queue.Full:
            log.warning("Notification Telegram ignorée : trop de messages en attente")
            return
        self._ensure_thread()

    def notify(self, event: str, message: str) -> None:
        """Notification formatée (icône + titre) d'un type d'événement, si ce type est activé."""
        if event in self.events:
            self.send(format_event(event, message), escape_html=False)

    def on_event(self, kind: str, message: str, level: str = "INFO") -> None:
        """Écouteur de Database.add_event."""
        if not self.enabled:
            return
        try:
            for event in classify(kind, message, level):
                if event in self.events:
                    self.send(format_event(event, message), escape_html=False)
                    return
        except Exception as exc:  # pragma: no cover - ne jamais casser l'historique
            log.debug("Notification Telegram impossible : %s", self.redact(exc))

    # ------------------------------------------------------------- thread
    def _ensure_thread(self) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._thread = threading.Thread(target=self._worker, name="synoarr-telegram", daemon=True)
            self._thread.start()

    def _rate_wait(self) -> float:
        """Secondes avant de pouvoir envoyer un message sans dépasser max_per_minute."""
        now = time.time()
        with self._lock:
            self._sent = [t for t in self._sent if now - t < 60.0]
            if len(self._sent) < max(1, self.max_per_minute):
                return 0.0
            return max(0.0, self._sent[0] + 60.0 - now)

    def _worker(self) -> None:
        while True:
            try:
                first = self._queue.get(timeout=30)
            except queue.Empty:
                if self._closed:
                    return
                continue
            if first is None:
                return
            batch = [first]
            stop = False
            start = time.time()
            while True:
                wait = max(self.coalesce_seconds - (time.time() - start), self._rate_wait())
                if wait <= 0 or self._closed:
                    break
                try:
                    item = self._queue.get(timeout=wait)
                except queue.Empty:
                    continue
                if item is None:
                    stop = True
                    break
                batch.append(item)
            # vide ce qui reste (fermeture ou messages arrivés pendant l'attente)
            while True:
                try:
                    item = self._queue.get_nowait()
                except queue.Empty:
                    break
                if item is None:
                    stop = True
                    continue
                batch.append(item)
            ok, err = self.send_now(merge(batch), escape_html=False)
            if not ok:
                log.warning("Notification Telegram non envoyée : %s", err)
            if stop:
                return

    def close(self, timeout: float = 5.0) -> None:
        """Envoie les notifications en attente puis arrête le thread (au plus *timeout* s)."""
        thread = self._thread
        self._closed = True
        if thread is None or not thread.is_alive():
            return
        try:
            self._queue.put_nowait(None)
        except queue.Full:  # pragma: no cover
            pass
        thread.join(timeout)

    # ------------------------------------------------------------- chat_id
    def get_chats(self) -> Tuple[bool, List[Dict[str, Any]], str]:
        """getUpdates -> (ok, [{"id", "name", "type"}], message d'erreur). Ne lève jamais."""
        if not self.bot_token:
            return False, [], "Renseignez d'abord [telegram] bot_token (bot créé avec @BotFather)"
        ok, result, err = self._call("getUpdates")
        if not ok:
            return False, [], err
        chats = []  # type: List[Dict[str, Any]]
        seen = set()
        for upd in result or []:
            if not isinstance(upd, dict):
                continue
            for key in ("message", "edited_message", "channel_post", "edited_channel_post", "my_chat_member",
                        "chat_member", "callback_query"):
                obj = upd.get(key)
                if not isinstance(obj, dict):
                    continue
                chat = obj.get("chat") or (obj.get("message") or {}).get("chat")
                if not isinstance(chat, dict) or chat.get("id") is None or chat.get("id") in seen:
                    continue
                seen.add(chat.get("id"))
                name = chat.get("title") or " ".join(
                    p for p in (chat.get("first_name"), chat.get("last_name")) if p) or ""
                if chat.get("username"):
                    name = ("%s (@%s)" % (name, chat["username"])) if name else "@" + chat["username"]
                chats.append({"id": chat.get("id"), "name": name or "?", "type": chat.get("type") or ""})
        return True, chats, ""


def _fit(text: Any) -> str:
    """Texte brut -> HTML échappé d'au plus MAX_LENGTH caractères (coupé avant échappement)."""
    raw = "" if text is None else str(text)
    body = escape(raw)
    n = len(raw)
    while len(body) > MAX_LENGTH and n > 0:
        n = max(0, min(n - 1, int(n * (MAX_LENGTH - 1) / len(body))))
        body = escape(raw[:n]) + "…"
    return body


def merge(messages: Iterable[str]) -> str:
    """Regroupe plusieurs messages HTML en un seul (≤ MAX_LENGTH caractères)."""
    msgs = [m for m in messages if m]
    if len(msgs) == 1:
        return msgs[0][:MAX_LENGTH]
    out = []  # type: List[str]
    length = 0
    for i, m in enumerate(msgs):
        rest = len(msgs) - i
        tail = "\n\n… et %d autre(s) notification(s)" % rest
        if length + len(m) + 2 + len(tail) > MAX_LENGTH:
            out.append(tail.strip())
            break
        out.append(m)
        length += len(m) + 2
    return "\n\n".join(out)


def from_config(tcfg: Any, http: Optional[HttpClient] = None, api_url: Optional[str] = None) -> TelegramNotifier:
    """TelegramNotifier depuis la section [telegram] (désactivé si bot_token ou chat_id est vide)."""
    return TelegramNotifier(getattr(tcfg, "bot_token", "") or "", getattr(tcfg, "chat_id", "") or "", http,
                            events=getattr(tcfg, "events", DEFAULT_EVENTS), api_url=api_url)
