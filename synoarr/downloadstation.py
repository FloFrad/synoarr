"""Synology Download Station client (DSM Web API, stdlib only).

Flow: API discovery (SYNO.API.Info) -> login (SYNO.API.Auth, format=sid) ->
calls with ``_sid``. Torrents are added with SYNO.DownloadStation2.Task
(DSM 7 / Download Station 3.9+) with exactly the requests of Prowlarr/Sonarr's
DownloadStationTaskProxyV2 (.torrent: POST multipart "fileData"; link: GET
type=url&url=...), with a fallback on the legacy SYNO.DownloadStation.Task API.
Like Prowlarr, an empty destination is replaced by Download Station's default
folder (SYNO.DownloadStation.Info getconfig). Tasks are listed/deleted with the
legacy API (still present on DSM 7), falling back on DownloadStation2 if it is missing.
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
INFO_API = "SYNO.DownloadStation.Info"
LOCATION_API = "SYNO.DownloadStation2.Settings.Location"
TASK2_LIST_API = "SYNO.DownloadStation2.Task.List"
TASK2_LIST_POLLING_API = "SYNO.DownloadStation2.Task.List.Polling"
# Prowlarr/Sonarr (DownloadStationTaskProxyV2.AddTaskFromData): multipart field "fileData", part type
# application/octet-stream (HttpRequestBuilder.AddFormUpload default).
UPLOAD_FIELD = "fileData"
UPLOAD_CTYPE = "application/octet-stream"
# Above this URL length the DS2 "create" of a link is POSTed instead of sent in the query string (like Prowlarr).
MAX_GET_URL = 6000

# Used when SYNO.API.Info cannot be queried.
DEFAULT_APIS: Dict[str, Dict[str, Any]] = {
    AUTH_API: {"path": "auth.cgi", "minVersion": 1, "maxVersion": 3},
    TASK_API: {"path": "DownloadStation/task.cgi", "minVersion": 1, "maxVersion": 3},
    TASK2_API: {"path": "entry.cgi", "minVersion": 1, "maxVersion": 2},
    INFO_API: {"path": "DownloadStation/info.cgi", "minVersion": 1, "maxVersion": 2},
    LOCATION_API: {"path": "entry.cgi", "minVersion": 1, "maxVersion": 1},
    TASK2_LIST_API: {"path": "entry.cgi", "minVersion": 1, "maxVersion": 2},
    TASK2_LIST_POLLING_API: {"path": "entry.cgi", "minVersion": 1, "maxVersion": 2},
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


def _ids(value: Any) -> List[str]:
    """DSM "list_id" / "task_id": list, single value or nothing -> list of non-empty strings."""
    if isinstance(value, (list, tuple)):
        return [str(v) for v in value if v not in (None, "") and not isinstance(v, bool)]
    if value in (None, "") or isinstance(value, bool):
        return []
    return [str(value)]


def _plain_param(value: str) -> str:
    """Value sent as is (like Prowlarr's AddQueryParam), unless DSM would read it as JSON (a destination
    "2024", "true"...): entry.cgi decodes each parameter as JSON first, then falls back on the raw string."""
    try:
        json.loads(value)
    except ValueError:
        return value
    return json.dumps(value, ensure_ascii=False)


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
        self._default_destination: Optional[str] = None  # Download Station's default folder (cache)
        self.last_create: Optional[Dict[str, Any]] = None  # API, HTTP method and "data" of the last create
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
            other._default_destination = self._default_destination
        return other

    # ================================================================ low level
    def _raw(self, path: str, params: Mapping[str, Any], method: str = "GET",
             data: Optional[Mapping[str, Any]] = None,
             files: Optional[Mapping[str, Tuple[str, bytes, str]]] = None) -> Dict[str, Any]:
        url = "%s/webapi/%s" % (self.url, path.lstrip("/"))
        try:
            if method == "GET":
                # %20 / %2B like .NET's Uri.EscapeDataString (Prowlarr), never "+" for a space
                items = [(k, v) for k, v in params.items() if v is not None]
                query = urllib.parse.urlencode(items, quote_via=urllib.parse.quote, safe="")
                resp = self.http.get(url + ("?" + query if query else ""))
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
                    "de code. Utilisez un compte sans 2FA disposant des mêmes droits Download Station que le vôtre "
                    "(un compte trop limité laisse les tâches « En attente »), puis vérifiez avec Commandes > « Test "
                    "d'envoi Download Station »."
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
                ordered: Dict[str, Any] = {"_sid": sid}  # first, like Prowlarr's DiskStationProxyBase.BuildRequest
                ordered.update(fields)
                payload = self._raw(info["path"], ordered)
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
    def _effective_destination(self) -> str:
        """Configured destination or, when empty, Download Station's default folder, read and passed
        explicitly like Prowlarr/Sonarr (TorrentDownloadStation.GetDownloadDirectory -> GetDefaultDir:
        SYNO.DownloadStation.Info getconfig "default_destination"). "" if it cannot be read."""
        if self.destination:
            return self.destination
        if self._default_destination:
            return self._default_destination
        found = ""
        for api, method, version in ((INFO_API, "getconfig", 1), (LOCATION_API, "get", 1)):
            if not self.has_api(api):
                continue
            try:
                data = self._call(api, method, {}, version=version)
            except DownloadStationError as exc:
                if exc.auth:
                    raise
                log.debug("Download Station : dossier par défaut illisible via %s (%s)", api, exc)
                continue
            value = data.get("default_destination") if isinstance(data, dict) else None
            if isinstance(value, str) and value.strip():
                found = normalize_destination(value)
                break
        if found:
            self._default_destination = found
            log.info("Download Station : aucune destination configurée, dossier par défaut de Download Station "
                     "« %s » transmis explicitement (comme Prowlarr)", found)
        else:
            log.info("Download Station : aucune destination configurée et dossier par défaut illisible : "
                     "la tâche est créée sans destination")
        return found

    def _create_with_fallback(self, ds2: Dict[str, Any], legacy: Dict[str, Any],
                              ds2_files: Optional[Mapping[str, Tuple[str, bytes, str]]],
                              legacy_files: Optional[Mapping[str, Tuple[str, bytes, str]]],
                              destination: str = "", ds2_http_method: str = "POST") -> str:
        """Create a task; returns the API used: "DownloadStation2" or "ancienne API"."""
        ds2_error: Optional[DownloadStationError] = None
        if self.has_api(TASK2_API):
            how = "multipart" if ds2_files else ("GET" if ds2_http_method == "GET" else "POST")
            try:
                data = self._call(TASK2_API, "create", ds2, version=2, http_method=ds2_http_method,
                                  files=ds2_files, sid_in_query=True)
                self.last_create = {"api": TASK2_API, "http": how, "data": data}
                log.info("Download Station : création via %s create v%s (%s %s) -> réponse %s", TASK2_API,
                         self._version(TASK2_API, 2), how, self._api_info(TASK2_API).get("path"),
                         json.dumps(data, ensure_ascii=False)[:300])
                self._confirm_file_lists(data, destination)
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
            data = self._call(TASK_API, "create", legacy, version=3, http_method="POST", files=legacy_files,
                              sid_in_query=False)
        except DownloadStationError as exc:
            if ds2_error is not None and (exc.code in _PARAM_CODES or exc.auth):
                raise ds2_error
            raise
        self.last_create = {"api": TASK_API, "http": "multipart" if legacy_files else "POST", "data": data}
        log.info("Download Station : création via %s create v%s (%s %s) -> réponse %s", TASK_API,
                 self._version(TASK_API, 3), self.last_create["http"], self._api_info(TASK_API).get("path"),
                 json.dumps(data, ensure_ascii=False)[:300])
        if ds2_error is not None:
            log.warning("Download Station : tâche créée avec l'ancienne API (DownloadStation2 a refusé : %s)", ds2_error)
        return "ancienne API"

    def _confirm_file_lists(self, data: Any, destination: str) -> None:
        """DownloadStation2 answers {"list_id": [...], "task_id": []} when it prepared a file-selection list
        instead of a task (create_list not taken as false): nothing downloads until the list is confirmed
        (what the DS web UI does after its "select files" dialog). Confirm it with every file selected.
        Never raises."""
        if not isinstance(data, dict):
            return
        list_ids = _ids(data.get("list_id"))
        if not list_ids:
            return
        log.warning("Download Station a préparé une liste de sélection de fichiers (list_id %s) au lieu de lancer "
                    "la tâche : confirmation automatique avec tous les fichiers", ", ".join(list_ids))
        for list_id in list_ids:
            try:
                self._confirm_file_list(list_id, destination)
            except Exception as exc:  # never fail an add that DSM accepted
                log.warning("Download Station : confirmation de la liste %s impossible (%s) : validez la tâche "
                            "à la main dans Download Station", list_id, exc)

    def _confirm_file_list(self, list_id: str, destination: str) -> None:
        selected: Optional[List[int]] = None
        if self.has_api(TASK2_LIST_API):
            info: Any = None
            for key in ("list_id", "id"):
                try:
                    info = self._call(TASK2_LIST_API, "get", {key: json.dumps(list_id)}, version=2)
                    break
                except DownloadStationError as exc:
                    if exc.auth:
                        raise
                    log.debug("Download Station : lecture de la liste %s (%s=) impossible : %s", list_id, key, exc)
            files = info.get("files") if isinstance(info, dict) else None
            if isinstance(files, list) and files:
                selected = [_to_int(f.get("index"), i) if isinstance(f, dict) else i for i, f in enumerate(files)]
        api = TASK2_LIST_POLLING_API if self.has_api(TASK2_LIST_POLLING_API) else TASK2_LIST_API
        params: Dict[str, Any] = {"list_id": json.dumps(list_id), "create_subfolder": "true"}
        if destination:
            params["destination"] = json.dumps(destination, ensure_ascii=False)
        if selected is not None:
            params["selected"] = json.dumps(selected, separators=(",", ":"))
        data = self._call(api, "download", params, version=2, http_method="POST", sid_in_query=True)
        log.info("Download Station : liste %s confirmée via %s download (%s fichier(s)) -> réponse %s", list_id, api,
                 len(selected) if selected is not None else "tous les", json.dumps(data, ensure_ascii=False)[:300])

    def add_torrent_file(self, data: bytes, filename: str) -> str:
        if not data:
            raise DownloadStationError("Fichier .torrent vide")
        name = re.sub(r'[\\/:*?"<>|\r\n]+', "_", (filename or "").strip()) or "synoarr"
        if not name.lower().endswith(".torrent"):
            name += ".torrent"
        # Exactly Prowlarr's DownloadStationTaskProxyV2.AddTaskFromData: POST multipart, _sid in the query,
        # api/version/method, type="file", file=["fileData"], create_list=false, destination="<dir>", fileData.
        ds2: Dict[str, Any] = {"type": '"file"', "file": json.dumps([UPLOAD_FIELD]), "create_list": "false"}
        legacy: Dict[str, Any] = {}
        dest = self._effective_destination()
        if dest:
            ds2["destination"] = json.dumps(dest, ensure_ascii=False)
            legacy["destination"] = dest
        api = self._create_with_fallback(ds2, legacy, {UPLOAD_FIELD: (name, data, UPLOAD_CTYPE)},
                                         {"file": (name, data, UPLOAD_CTYPE)}, destination=dest)
        log.info("Download Station : torrent « %s » ajouté%s (%s, %d octets envoyés tels quels)", name,
                 " dans %s" % dest if dest else "", api, len(data))
        return api

    def add_url(self, url: str) -> str:
        url = (url or "").strip()
        if not url:
            raise DownloadStationError("URL vide")
        # Exactly Prowlarr's DownloadStationTaskProxyV2.AddTaskFromUrl: GET, query string
        # _sid, api, version, method, type=url, url=<link as is>, create_list=false, destination=<dir>.
        ds2: Dict[str, Any] = {"type": "url", "url": _plain_param(url), "create_list": "false"}
        legacy: Dict[str, Any] = {"uri": url}
        dest = self._effective_destination()
        if dest:
            ds2["destination"] = _plain_param(dest)
            legacy["destination"] = dest
        method = "GET"
        if len(urllib.parse.urlencode(ds2)) + len(self.url) + 200 > MAX_GET_URL:
            method = "POST"  # very long magnet (many trackers): too long for a request line
        api = self._create_with_fallback(ds2, legacy, None, None, destination=dest, ds2_http_method=method)
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
