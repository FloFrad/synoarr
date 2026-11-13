"""Synology Download Station client (DSM Web API, stdlib only).

Flow: API discovery (SYNO.API.Info) -> login (SYNO.API.Auth, format=sid) ->
calls with ``_sid``. Torrents are added with SYNO.DownloadStation2.Task
(DSM 7 / Download Station 3.9+) with a fallback on the legacy
SYNO.DownloadStation.Task API. Tasks are listed/deleted with the legacy API
(still present on DSM 7), falling back on DownloadStation2 if it is missing.
"""
from __future__ import annotations

import json
import logging
import re
import threading
import urllib.parse
from typing import Any, Dict, List, Mapping, Optional, Tuple

from .bencode import magnet_info
from .httpclient import HttpClient, HttpError, redact
from .models import DSTask

log = logging.getLogger(__name__)

AUTH_API = "SYNO.API.Auth"
TASK_API = "SYNO.DownloadStation.Task"
TASK2_API = "SYNO.DownloadStation2.Task"

# Used when SYNO.API.Info cannot be queried.
DEFAULT_APIS: Dict[str, Dict[str, Any]] = {
    AUTH_API: {"path": "auth.cgi", "minVersion": 1, "maxVersion": 3},
    TASK_API: {"path": "DownloadStation/task.cgi", "minVersion": 1, "maxVersion": 3},
    TASK2_API: {"path": "entry.cgi", "minVersion": 1, "maxVersion": 2},
}

SESSION_CODES = (105, 106, 107, 119)
_PARAM_CODES = (101, 102, 103, 104)

# DS2 integer status (some DSM versions) -> legacy string status
INT_STATUS = {
    1: "waiting",
    2: "downloading",
    3: "paused",
    4: "finishing",
    5: "finished",
    6: "hash_checking",
    7: "seeding",
    8: "filehosting_waiting",
    9: "extracting",
    10: "error",
}

COMMON_ERRORS = {
    100: "erreur inconnue",
    101: "paramètre manquant ou invalide",
    102: "API inexistante",
    103: "méthode inexistante",
    104: "version d'API non supportée",
    105: "permission refusée",
    106: "session expirée",
    107: "session interrompue par une connexion en double",
    119: "session introuvable",
    120: "paramètre invalide",
}

TASK_ERRORS = {
    400: "échec de l'envoi du fichier",
    401: "nombre maximal de tâches atteint",
    402: "accès refusé au dossier de destination",
    403: "le dossier de destination n'existe pas",
    404: "identifiant de tâche invalide",
    405: "action invalide sur la tâche",
    406: "aucun dossier de destination par défaut",
    407: "impossible de définir le dossier de destination",
    408: "fichier introuvable",
}
_DESTINATION_CODES = (120, 402, 403, 406, 407)


class DownloadStationError(Exception):
    def __init__(self, message: str, code: Optional[int] = None, auth: bool = False):
        super().__init__(message)
        self.code = code
        self.auth = auth  # True = login problem (credentials, 2FA...), retrying another API is useless


def _to_int(value: Any, default: int = 0) -> int:
    if value is None or isinstance(value, bool):
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        try:
            return int(float(str(value)))
        except (TypeError, ValueError):
            return default


def _opt_int(value: Any) -> Optional[int]:
    """int, or None when the value is missing / not a number (DSM sometimes sends strings)."""
    if value is None or isinstance(value, bool) or value == "":
        return None
    marker = object()
    n = _to_int(value, marker)  # type: ignore[arg-type]
    return None if n is marker else max(0, n)


def _first(d: Dict[str, Any], *keys: str) -> Any:
    for k in keys:
        if d.get(k) not in (None, ""):
            return d.get(k)
    return None


def normalize_destination(destination: str) -> str:
    """"/volume1/Download/x/" -> "Download/x" (Download Station wants a shared folder path, no leading slash)."""
    d = (destination or "").strip().replace("\\", "/").strip("/")
    m = re.match(r"^volume(?:usb)?\d+/(.+)$", d, re.I)
    if m:
        log.warning("Download Station : destination %r ramenée à %r (le chemin doit commencer par le dossier "
                    "partagé, sans /volumeX/)", destination, m.group(1))
        d = m.group(1)
    return re.sub(r"/{2,}", "/", d)


def map_status(value: Any) -> str:
    if value is None or value == "" or isinstance(value, bool):
        return "unknown"  # treated as active: never import something whose state is unknown
    if isinstance(value, (int, float)) or (isinstance(value, str) and value.strip().isdigit()):
        n = int(value)
        if n in INT_STATUS:
            return INT_STATUS[n]
        if n >= 100:
            return "error"
        return "status_%d" % n
    return str(value).strip().lower()


class DownloadStation:
    def __init__(self, url: str, username: str, password: str, destination: str, http: HttpClient):
        self.url = (url or "").strip().rstrip("/")
        if self.url and not re.match(r"^https?://", self.url, re.I):
            # "192.168.1.10:5000" would make urllib raise ValueError (unknown url type)
            self.url = "http://" + self.url
        if self.url.lower().endswith("/webapi"):
            self.url = self.url[: -len("/webapi")]
        self.username = username or ""
        self.password = password or ""
        self.destination = normalize_destination(destination)
        self.http = http
        self._sid: Optional[str] = None
        self._apis: Optional[Dict[str, Dict[str, Any]]] = None
        self._discovered = False  # True when SYNO.API.Info answered
        self._lock = threading.RLock()

    def quick_client(self, timeout: float = 6.0) -> "DownloadStation":
        """Copie (mêmes adresse et identifiants, session et API déjà découvertes reprises) avec un délai
        court et sans nouvelle tentative : pour les lectures fréquentes (progression) qui ne doivent jamais
        bloquer longtemps quand DSM accepte la connexion mais ne répond pas."""
        http = HttpClient(timeout=timeout, retries=0, verify_ssl=getattr(self.http, "verify_ssl", True),
                          use_proxy=getattr(self.http, "use_proxy", True))
        other = DownloadStation(self.url, self.username, self.password, "", http)
        other.destination = self.destination
        with self._lock:
            other._sid = self._sid
            other._apis = dict(self._apis) if self._apis is not None else None
            other._discovered = self._discovered
        return other

    # ================================================================ low level
    def _raw(self, path: str, params: Mapping[str, Any], method: str = "GET",
             data: Optional[Mapping[str, Any]] = None,
             files: Optional[Mapping[str, Tuple[str, bytes, str]]] = None) -> Dict[str, Any]:
        url = "%s/webapi/%s" % (self.url, path.lstrip("/"))
        try:
            if method == "GET":
                resp = self.http.get(url, params=params)
            else:
                resp = self.http.post(url, params=params, data=data, files=files)
        except HttpError as exc:
            if exc.status is None:
                raise DownloadStationError(
                    "Impossible de joindre DSM (%s) : %s. Vérifiez [downloadstation] url "
                    "(ex. http://127.0.0.1:5000 ou https://127.0.0.1:5001)." % (self.url, exc))
            raise DownloadStationError("DSM a répondu HTTP %s pour %s. Vérifiez [downloadstation] url."
                                       % (exc.status, redact(exc.url or url)))
        self._follow_redirect(resp.url)
        try:
            payload = resp.json()
        except HttpError:
            raise DownloadStationError("Réponse inattendue de DSM pour %s (pas du JSON) : %r. Vérifiez "
                                       "[downloadstation] url." % (redact(url), resp.text()[:120]))
        if not isinstance(payload, dict):
            raise DownloadStationError("Réponse inattendue de DSM pour %s : %r" % (redact(url), payload))
        return payload

    def _follow_redirect(self, final_url: str) -> None:
        """If DSM redirected us (e.g. http:5000 -> https:5001), use the new base from now on:
        a redirected POST loses its body."""
        if not final_url or final_url.startswith(self.url + "/"):
            return
        parts = urllib.parse.urlsplit(final_url)
        idx = parts.path.find("/webapi/")
        if not parts.scheme or idx < 0:
            return
        new_base = "%s://%s%s" % (parts.scheme, parts.netloc, parts.path[:idx])
        if new_base != self.url:
            log.info("Download Station : redirection de %s vers %s, adresse mise à jour", self.url, new_base)
            self.url = new_base

    @staticmethod
    def _error_code(payload: Dict[str, Any]) -> int:
        err = payload.get("error")
        if isinstance(err, dict):
            return _to_int(err.get("code"), 100)
        return _to_int(err, 100)

    # ================================================================ discovery
    def _discover(self) -> None:
        params = {"api": "SYNO.API.Info", "version": 1, "method": "query", "query": "ALL"}
        try:
            payload = self._raw("query.cgi", params)
        except DownloadStationError as exc:
            if "Impossible de joindre" in str(exc):
                raise
            log.warning("Download Station : découverte des API impossible (%s), chemins par défaut utilisés", exc)
            payload = {}
        apis = payload.get("data") if payload.get("success") else None
        if isinstance(apis, dict) and apis:
            self._apis = {k: v for k, v in apis.items() if isinstance(v, dict)}
            self._discovered = True
        else:
            if payload:
                log.warning("Download Station : SYNO.API.Info a répondu %r, chemins par défaut utilisés", payload)
            self._apis = {k: dict(v) for k, v in DEFAULT_APIS.items()}
            self._discovered = False

    def _ensure_discovered(self) -> Dict[str, Dict[str, Any]]:
        if self._apis is None:
            self._discover()
        assert self._apis is not None
        return self._apis

    def has_api(self, name: str) -> bool:
        apis = self._ensure_discovered()
        return name in apis or (not self._discovered and name in DEFAULT_APIS)

    def _not_installed_error(self) -> DownloadStationError:
        return DownloadStationError(
            "API Download Station absente sur le NAS : le paquet Download Station n'est pas installé ou n'est "
            "pas démarré. Installez-le / lancez-le depuis le Centre de paquets de DSM.", 102)

    def _api_info(self, name: str) -> Dict[str, Any]:
        apis = self._ensure_discovered()
        info = apis.get(name)
        if info is None:
            if name in (TASK_API, TASK2_API) and self._discovered:
                raise self._not_installed_error()
            info = DEFAULT_APIS.get(name)
            if info is None:
                raise DownloadStationError("API %s inconnue" % name, 102)
        return info

    def _version(self, name: str, wanted: int) -> int:
        info = self._api_info(name)
        lo = _to_int(info.get("minVersion"), 1) or 1
        hi = _to_int(info.get("maxVersion"), wanted) or wanted
        return max(lo, min(wanted, hi))

    # ================================================================ auth
    def login(self) -> None:
        with self._lock:
            if not self.username:
                raise DownloadStationError("Identifiants Download Station manquants : renseignez [downloadstation] "
                                           "username et password.", auth=True)
            self._ensure_discovered()
            info = self._api_info(AUTH_API)
            version = self._version(AUTH_API, 6)
            params = {"api": AUTH_API, "version": version, "method": "login", "account": self.username,
                      "passwd": self.password, "session": "DownloadStation", "format": "sid"}
            # POST keeps the password out of URLs / proxy logs; some setups only accept GET.
            try:
                payload = self._raw(info["path"], {}, method="POST", data=params)
            except DownloadStationError as exc:
                if "Impossible de joindre" in str(exc):
                    raise
                log.debug("Download Station : login POST en échec (%s), essai en GET", exc)
                payload = {"success": False, "error": {"code": 101}}
            if not payload.get("success") and self._error_code(payload) in _PARAM_CODES:
                log.debug("Download Station : login POST refusé (%s), essai en GET", self._error_code(payload))
                payload = self._raw(info["path"], params)
            if not payload.get("success"):
                code = self._error_code(payload)
                raise DownloadStationError(self._login_message(code), code, auth=True)
            data = payload.get("data") or {}
            sid = data.get("sid") if isinstance(data, dict) else None
            if not sid:
                raise DownloadStationError("Connexion DSM réussie mais aucun identifiant de session (sid) reçu.",
                                           auth=True)
            self._sid = str(sid)
            log.debug("Download Station : connecté en tant que %s (Auth v%s)", self.username, version)

    def _login_message(self, code: int) -> str:
        user = self.username
        if code == 400:
            return ("Identifiants DSM refusés pour l'utilisateur « %s » : nom d'utilisateur ou mot de passe "
                    "incorrect (vérifiez [downloadstation] username / password)." % user)
        if code == 401:
            return "Le compte DSM « %s » est désactivé." % user
        if code == 402:
            return ("Permission refusée pour le compte DSM « %s » : autorisez-lui l'application Download Station "
                    "(Panneau de configuration > Privilèges d'application)." % user)
        if code in (403, 404, 406):
            return ("Le compte DSM « %s » a la validation en 2 étapes (2FA) activée%s : synoarr ne peut pas saisir "
                    "de code. Créez un utilisateur DSM dédié sans 2FA, avec accès à Download Station et au dossier "
                    "de destination, et utilisez-le dans [downloadstation]."
                    % (user, " (imposée par l'administrateur)" if code == 406 else ""))
        if code == 407:
            return ("L'adresse IP de synoarr est bloquée par DSM (blocage automatique après trop d'échecs) : "
                    "débloquez-la dans Panneau de configuration > Sécurité > Protection.")
        if code in (408, 409, 410):
            return ("Le mot de passe du compte DSM « %s » a expiré ou doit être changé : connectez-vous une fois "
                    "à DSM avec ce compte pour le modifier." % user)
        if code == 102:
            return "API d'authentification DSM introuvable : vérifiez [downloadstation] url."
        return "Connexion à DSM impossible pour « %s » (code d'erreur %s : %s)." % (
            user, code, COMMON_ERRORS.get(code, "inconnu"))

    # ================================================================ generic call
    def _call(self, api: str, method: str, params: Optional[Mapping[str, Any]] = None, version: int = 1,
              http_method: str = "GET", files: Optional[Mapping[str, Tuple[str, bytes, str]]] = None,
              sid_in_query: bool = True) -> Any:
        """Call *api*/*method*; re-login once on session errors. Returns ``data`` (may be None)."""
        for attempt in (1, 2):
            if not self._sid:
                self.login()
            info = self._api_info(api)
            fields: Dict[str, Any] = {"api": api, "version": self._version(api, version), "method": method}
            fields.update(params or {})
            sid = self._sid
            if http_method == "GET":
                fields["_sid"] = sid
                payload = self._raw(info["path"], fields)
            else:
                query: Dict[str, Any] = {}
                # Same as Sonarr's DownloadStation proxies: SYNO.DownloadStation2.Task wants _sid in the
                # query string (api/version/method + params in the body), the legacy APIs in the body.
                if sid_in_query:
                    query["_sid"] = sid
                else:
                    fields["_sid"] = sid
                payload = self._raw(info["path"], query, method="POST", data=fields, files=files)
            if payload.get("success"):
                return payload.get("data")
            code = self._error_code(payload)
            if code in SESSION_CODES and attempt == 1:
                log.debug("Download Station : session invalide (code %s), reconnexion", code)
                with self._lock:
                    if self._sid == sid:
                        self._sid = None
                continue
            raise DownloadStationError(self._call_message(api, method, code), code)
        raise DownloadStationError("Download Station : échec après reconnexion")  # pragma: no cover

    def _call_message(self, api: str, method: str, code: int) -> str:
        if code == 102:
            return self._not_installed_error().args[0]
        if code == 105:
            return ("Permission refusée : l'utilisateur DSM « %s » n'a pas le droit d'utiliser Download Station "
                    "(Panneau de configuration > Privilèges d'application > Download Station)." % self.username)
        if code in SESSION_CODES:
            return "Session DSM invalide (code %s : %s) malgré une reconnexion." % (code, COMMON_ERRORS[code])
        label = TASK_ERRORS.get(code) or COMMON_ERRORS.get(code) or "erreur inconnue"
        msg = "Download Station (%s %s) : %s (code %s)." % (api.rsplit(".", 2)[-2], method, label, code)
        if method == "create" and code in _DESTINATION_CODES:
            msg += (" Vérifiez [downloadstation] destination (actuellement « %s ») : ce doit être un dossier "
                    "partagé existant (éventuellement suivi d'un sous-dossier), sans « / » initial ni /volume1, "
                    "par ex. « Download » ou « Download/synoarr », et l'utilisateur « %s » doit pouvoir y écrire."
                    % (self.destination or "(vide)", self.username))
        return msg

    # ================================================================ add
    def _create_with_fallback(self, ds2: Dict[str, Any], legacy: Dict[str, Any],
                              ds2_files: Optional[Mapping[str, Tuple[str, bytes, str]]],
                              legacy_files: Optional[Mapping[str, Tuple[str, bytes, str]]]) -> str:
        """Create a task; returns the API used: "DownloadStation2" or "ancienne API"."""
        ds2_error: Optional[DownloadStationError] = None
        if self.has_api(TASK2_API):
            try:
                data = self._call(TASK2_API, "create", ds2, version=2, http_method="POST", files=ds2_files,
                                  sid_in_query=True)
                log.debug("Download Station : création via DownloadStation2 OK (réponse %s)", str(data)[:200])
                return "DownloadStation2"
            except DownloadStationError as exc:
                if exc.auth:
                    raise
                ds2_error = exc
                log.warning("Download Station : échec de l'ajout via DownloadStation2 (%s), essai avec l'ancienne "
                            "API", exc)
        if not self.has_api(TASK_API):
            if ds2_error is not None:
                raise ds2_error
            raise self._not_installed_error()
        try:
            self._call(TASK_API, "create", legacy, version=3, http_method="POST", files=legacy_files,
                       sid_in_query=False)
        except DownloadStationError as exc:
            if ds2_error is not None and (exc.code in _PARAM_CODES or exc.auth):
                raise ds2_error
            raise
        if ds2_error is not None:
            log.warning("Download Station : tâche créée avec l'ancienne API (DownloadStation2 a refusé : %s)", ds2_error)
        return "ancienne API"

    def add_torrent_file(self, data: bytes, filename: str) -> str:
        if not data:
            raise DownloadStationError("Fichier .torrent vide")
        name = re.sub(r'[\\/:*?"<>|\r\n]+', "_", (filename or "").strip()) or "synoarr"
        if not name.lower().endswith(".torrent"):
            name += ".torrent"
        ctype = "application/x-bittorrent"
        ds2: Dict[str, Any] = {"type": '"file"', "file": '["torrent"]', "create_list": "false"}
        legacy: Dict[str, Any] = {}
        if self.destination:
            ds2["destination"] = json.dumps(self.destination, ensure_ascii=False)
            legacy["destination"] = self.destination
        api = self._create_with_fallback(ds2, legacy, {"torrent": (name, data, ctype)},
                                         {"file": (name, data, ctype)})
        log.info("Download Station : torrent « %s » ajouté%s (%s, %d octets envoyés tels quels)", name,
                 " dans %s" % self.destination if self.destination else "", api, len(data))
        return api

    def add_url(self, url: str) -> str:
        url = (url or "").strip()
        if not url:
            raise DownloadStationError("URL vide")
        ds2: Dict[str, Any] = {"type": '"url"', "url": json.dumps([url], ensure_ascii=False),
                               "create_list": "false"}
        legacy: Dict[str, Any] = {"uri": url}
        if self.destination:
            ds2["destination"] = json.dumps(self.destination, ensure_ascii=False)
            legacy["destination"] = self.destination
        api = self._create_with_fallback(ds2, legacy, None, None)
        log.info("Download Station : lien ajouté via %s (%s)", api,
                 redact(url)[:120] if not url.startswith("magnet:") else url[:300])
        return api

    # ================================================================ list / delete
    def list_tasks(self) -> List[DSTask]:
        tasks: List[Any] = []
        if self.has_api(TASK_API):
            try:
                data = self._call(TASK_API, "list", {"additional": "detail,transfer"}, version=1)
                tasks = (data or {}).get("tasks") or [] if isinstance(data, dict) else []
                return [t for t in (self._to_task(x) for x in tasks) if t is not None]
            except DownloadStationError as exc:
                if exc.auth or exc.code not in _PARAM_CODES or not self.has_api(TASK2_API):
                    raise
                log.debug("Download Station : liste via l'ancienne API impossible (%s), essai DownloadStation2", exc)
        if not self.has_api(TASK2_API):
            raise self._not_installed_error()
        data = self._call(TASK2_API, "list", {"additional": json.dumps(["detail", "transfer"]),
                                              "offset": 0, "limit": -1}, version=2)
        if isinstance(data, dict):
            tasks = data.get("task") or data.get("tasks") or []
        elif isinstance(data, list):
            tasks = data
        return [t for t in (self._to_task(x) for x in tasks) if t is not None]

    def diagnostics(self) -> Dict[str, Any]:
        """Raw facts for troubleshooting (tasks with their raw status/status_extra, DS settings, schedule).
        Never raises: each part reports its own error. URIs are never returned (they can hold a passkey)."""
        out: Dict[str, Any] = {"tasks": [], "config": None, "schedule": None, "errors": []}
        try:
            data = self._call(TASK_API, "list", {"additional": "detail,transfer"}, version=1)
            for t in ((data or {}).get("tasks") or []) if isinstance(data, dict) else []:
                if not isinstance(t, dict):
                    continue
                add = t.get("additional") if isinstance(t.get("additional"), dict) else {}
                detail = add.get("detail") if isinstance(add.get("detail"), dict) else {}
                transfer = add.get("transfer") if isinstance(add.get("transfer"), dict) else {}
                out["tasks"].append({
                    "id": t.get("id"), "title": t.get("title"), "type": t.get("type"),
                    "status": t.get("status"), "status_extra": t.get("status_extra"),
                    "size": t.get("size"), "destination": detail.get("destination"),
                    "create_time": detail.get("create_time"), "started_time": detail.get("started_time"),
                    "total_peers": detail.get("total_peers"), "connected_seeders": detail.get("connected_seeders"),
                    "connected_leechers": detail.get("connected_leechers"),
                    "downloaded": transfer.get("size_downloaded"), "speed_download": transfer.get("speed_download"),
                    "is_magnet": str(detail.get("uri") or "").lower().startswith("magnet:"),
                })
        except Exception as exc:
            out["errors"].append("liste des tâches : %s" % exc)
        for key, api, method in (("config", "SYNO.DownloadStation.Info", "getconfig"),
                                 ("schedule", "SYNO.DownloadStation.Schedule", "getconfig")):
            try:
                if self.has_api(api):
                    out[key] = self._call(api, method, {}, version=1)
            except Exception as exc:
                out["errors"].append("%s : %s" % (api, exc))
        return out

    @staticmethod
    def _to_task(t: Any) -> Optional[DSTask]:
        if not isinstance(t, dict):
            return None
        task_id = t.get("id", t.get("task_id"))
        if task_id in (None, ""):
            return None
        add = t.get("additional") if isinstance(t.get("additional"), dict) else {}
        detail = add.get("detail") if isinstance(add.get("detail"), dict) else {}
        transfer = add.get("transfer") if isinstance(add.get("transfer"), dict) else {}
        status = map_status(t.get("status"))
        error = None
        extra = t.get("status_extra")
        if isinstance(extra, dict) and extra.get("error_detail"):
            error = str(extra.get("error_detail"))
        elif status == "error" and isinstance(t.get("status"), (int, str)) and str(t.get("status")).isdigit() \
                and int(t.get("status")) >= 100:
            error = "erreur %s" % t.get("status")
        dest = detail.get("destination")
        downloaded = _first(transfer, "size_downloaded", "downloaded_size")
        uri = detail.get("uri")
        uri = str(uri) if isinstance(uri, str) and uri else None
        info_hash = None
        if uri and uri.lower().startswith("magnet:"):
            try:
                info_hash = magnet_info(uri)[1]
            except Exception:  # never fail a listing on a strange URI
                info_hash = None
        return DSTask(id=str(task_id), title=str(t.get("title") or t.get("name") or ""), status=status,
                      size=_to_int(t.get("size")), downloaded=_to_int(downloaded),
                      destination=str(dest) if dest else None,
                      type=str(t["type"]) if t.get("type") is not None else None, error=error,
                      speed_down=_opt_int(_first(transfer, "speed_download", "download_speed")),
                      speed_up=_opt_int(_first(transfer, "speed_upload", "upload_speed")),
                      seeders=_opt_int(_first(detail, "connected_seeders", "seeders")),
                      leechers=_opt_int(_first(detail, "connected_leechers", "leechers")),
                      peers=_opt_int(_first(detail, "total_peers", "connected_peers", "peers")),
                      uploaded=_opt_int(_first(transfer, "size_uploaded", "uploaded_size")),
                      created=_opt_int(_first(detail, "create_time", "created_time")) or None,
                      info_hash=info_hash)

    def delete_task(self, task_id: str) -> None:
        """Delete the task (never the downloaded files)."""
        if self.has_api(TASK_API):
            try:
                data = self._call(TASK_API, "delete", {"id": task_id, "force_complete": "false"}, version=1)
            except DownloadStationError as exc:
                if exc.auth or exc.code not in _PARAM_CODES or not self.has_api(TASK2_API):
                    raise
                data = None
            else:
                for res in data if isinstance(data, list) else []:
                    if not isinstance(res, dict):
                        continue
                    code = _to_int(res.get("error"), 0)
                    if code == 404:
                        log.info("Download Station : tâche %s déjà supprimée", task_id)
                    elif code:
                        raise DownloadStationError("Suppression de la tâche %s impossible : %s (code %s)" % (
                            task_id, TASK_ERRORS.get(code) or COMMON_ERRORS.get(code, "erreur"), code), code)
                return
        if not self.has_api(TASK2_API):
            raise self._not_installed_error()
        self._call(TASK2_API, "delete", {"id": json.dumps([task_id]), "force_complete": "false"}, version=2)

    # ================================================================ test
    def test(self) -> Tuple[bool, str]:
        try:
            self.login()
            if self._discovered and not (self.has_api(TASK_API) or self.has_api(TASK2_API)):
                return False, str(self._not_installed_error())
            tasks = self.list_tasks()
        except DownloadStationError as exc:
            return False, str(exc)
        msg = "Connexion à Download Station OK (%d tâche(s))." % len(tasks)
        if self.destination:
            msg += " Destination : %s." % self.destination
        else:
            msg += " Aucune destination configurée : le dossier par défaut de Download Station sera utilisé."
        return True, msg
