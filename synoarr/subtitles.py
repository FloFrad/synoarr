"""Sous-titres automatiques (à la Bazarr) via l'API REST OpenSubtitles.com v1.

Flux (https://api.opensubtitles.com/api/v1, en-têtes ``Api-Key`` + ``User-Agent: synoarr v<version>``) :

* ``POST /login`` ``{"username", "password"}`` -> ``token`` (``Authorization: Bearer``), facultatif mais
  nécessaire pour télécharger avec le quota d'un compte gratuit ; fait une seule fois, jeton gardé en mémoire ;
* ``GET /subtitles`` (``imdb_id``/``tmdb_id`` pour un film, ``parent_imdb_id``/``parent_tmdb_id`` +
  ``season_number``/``episode_number`` pour un épisode, sinon ``query``) + ``languages`` + ``moviehash``
  (empreinte OpenSubtitles du fichier vidéo, voir :func:`opensubtitles_hash`) ;
* ``POST /download`` ``{"file_id"}`` -> ``{"link", "remaining", "reset_time_utc"...}`` puis ``GET link``.

Choix du meilleur sous-titre : empreinte identique d'abord, puis préférence malentendants, puis ressemblance
du nom de la release avec le nom du fichier vidéo, puis nombre de téléchargements.
Enregistrement : ``<nom de la vidéo>.<langue>.srt`` à côté de la vidéo (nommage Plex), jamais d'écrasement.

Quotas : ``remaining`` à 0 ou HTTP 406 -> plus aucun téléchargement jusqu'à ``reset_time_utc`` (ou minuit
UTC) ; HTTP 429 (trop de requêtes) -> pause courte. Chaque vidéo cherchée est mémorisée (table
``subtitle_searches``) : une vidéo sans résultat n'est recherchée à nouveau qu'après quelques jours.
Rien ne lève vers le démon : toutes les erreurs sont journalisées.
"""
from __future__ import annotations

import calendar
import logging
import os
import queue
import re
import struct
import threading
import time
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Set, Tuple

from . import __version__
from .httpclient import HttpError, Response
from .organizer import (MIN_VIDEO_SIZE, TMP_PREFIX, _EXTRA_DIRS, _SUB_FORCED, _SUB_LANG_TOKENS,
                        _is_ignored_name)
from .parser import normalize_title, parse, titles_match

if TYPE_CHECKING:  # pragma: no cover
    from .config import Config
    from .db import Database
    from .httpclient import HttpClient
    from .tmdb import TmdbClient

log = logging.getLogger(__name__)

API_URL = "https://api.opensubtitles.com/api/v1"
HASH_CHUNK = 65536
_MASK64 = 0xFFFFFFFFFFFFFFFF

_TABLE = """
CREATE TABLE IF NOT EXISTS subtitle_searches (
    path TEXT PRIMARY KEY,
    languages TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT '',     -- found | partial | none | error
    attempts INTEGER NOT NULL DEFAULT 0,
    last_at REAL NOT NULL DEFAULT 0,
    message TEXT
)
"""
_KV_QUOTA = "subtitles_quota_until"  # epoch (secondes) : plus de téléchargement avant cette date


def opensubtitles_hash(path: str) -> Optional[str]:
    """Empreinte OpenSubtitles : taille du fichier + somme (modulo 2^64) des entiers 64 bits little-endian
    des premiers et derniers 64 Kio, en 16 caractères hexadécimaux. None si le fichier fait moins de 128 Kio
    ou est illisible."""
    try:
        size = os.path.getsize(path)
        if size < 2 * HASH_CHUNK:
            return None
        with open(path, "rb") as fh:
            head = fh.read(HASH_CHUNK)
            fh.seek(size - HASH_CHUNK)
            tail = fh.read(HASH_CHUNK)
    except OSError:
        return None
    if len(head) != HASH_CHUNK or len(tail) != HASH_CHUNK:
        return None
    fmt = "<%dQ" % (HASH_CHUNK // 8)
    total = size + sum(struct.unpack(fmt, head)) + sum(struct.unpack(fmt, tail))
    return "%016x" % (total & _MASK64)


def normalize_language(code: str) -> str:
    """'EN' -> 'en', 'fre'/'french' -> 'fr', 'pt-BR' -> 'pt-br' (codes OpenSubtitles)."""
    c = (code or "").strip().lower().replace("_", "-")
    if not c:
        return ""
    if c in _SUB_LANG_TOKENS and len(c) != 2:
        return _SUB_LANG_TOKENS[c]
    return c


def _base_lang(code: str) -> str:
    return normalize_language(code).split("-", 1)[0]


def existing_subtitle_languages(video_path: str, sub_exts: List[str]) -> Set[str]:
    """Langues (codes de base 'en', 'fr'...) des sous-titres complets déjà présents à côté de la vidéo
    (« Film (2010).fr.srt », « Film (2010).en.sdh.srt »). Les sous-titres forcés ne comptent pas."""
    folder, base = os.path.split(video_path)
    stem = base.rsplit(".", 1)[0] if "." in base else base
    exts = set(e.lower().lstrip(".") for e in sub_exts)
    found = set()  # type: Set[str]
    try:
        names = os.listdir(folder or ".")
    except OSError:
        return found
    prefix = stem.lower() + "."
    for name in names:
        low = name.lower()
        if not low.startswith(prefix):
            continue
        after = low[len(prefix):]
        rest, ext = after.rsplit(".", 1) if "." in after else ("", after)
        if ext not in exts:
            continue
        tokens = [t for t in re.split(r"[.\s_\-\[\]()]+", rest) if t]
        if any(t in _SUB_FORCED for t in tokens):
            continue
        for tok in tokens:
            if tok in _SUB_LANG_TOKENS:
                found.add(_SUB_LANG_TOKENS[tok])
    return found


def _tokens(text: str) -> Set[str]:
    return set(t for t in re.split(r"[^a-z0-9]+", (text or "").lower()) if t)


def name_similarity(a: str, b: str) -> float:
    """Ressemblance (0..1) de deux noms de release : indice de Jaccard des mots."""
    ta, tb = _tokens(a), _tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / float(len(ta | tb))


def _imdb_digits(imdb: Any) -> Optional[str]:
    digits = re.sub(r"\D", "", str(imdb or ""))
    return str(int(digits)) if digits and int(digits) > 0 else None


def _tmdb_digits(tmdb: Any) -> Optional[str]:
    digits = re.sub(r"\D", "", str(tmdb or ""))
    return str(int(digits)) if digits and int(digits) > 0 else None


def _next_utc_midnight(now: float) -> float:
    return (int(now) // 86400 + 1) * 86400.0


def _parse_reset(value: Any, now: float) -> float:
    """reset_time_utc '2022-04-08T13:03:16.000Z' -> epoch ; repli : minuit UTC suivant."""
    if isinstance(value, str) and len(value) >= 19:
        try:
            ts = calendar.timegm(time.strptime(value[:19], "%Y-%m-%dT%H:%M:%S"))
            if now < ts < now + 3 * 86400:
                return float(ts)
        except ValueError:
            pass
    return _next_utc_midnight(now)


class QuotaExceeded(Exception):
    """Quota de téléchargements OpenSubtitles atteint (ou trop de requêtes)."""


class SubtitleService:
    base_url = API_URL
    min_video_size = MIN_VIDEO_SIZE
    request_pause = 1.0  # secondes entre deux vidéos d'un scan (limite de l'API : quelques requêtes/s)
    rate_limit_pause = 120.0  # pause après un HTTP 429 persistant
    idle_timeout = 30.0  # le thread de la file s'arrête après ce délai sans travail

    def __init__(self, cfg: "Config", db: "Database", http: "HttpClient", tmdb: Optional["TmdbClient"] = None):
        self.cfg = cfg
        self.db = db
        self.http = http
        self.tmdb = tmdb if (tmdb is not None and getattr(tmdb, "enabled", False)) else None
        self.stop_event = None  # type: Optional[threading.Event]
        self._token = None  # type: Optional[str]
        self._login_failed = False
        self._api = None  # type: Optional[str]  # base_url renvoyée par /login (ex. vip-api)
        self._rate_until = 0.0
        self._lock = threading.RLock()
        self._queue = queue.Queue()  # type: queue.Queue
        self._worker = None  # type: Optional[threading.Thread]
        self._worker_lock = threading.Lock()
        self.scanning = False
        try:
            self.db.execute(_TABLE)
        except Exception as exc:  # pragma: no cover - base en lecture seule
            log.warning("Table des recherches de sous-titres indisponible : %s", exc)

    # ------------------------------------------------------------ settings
    @property
    def scfg(self) -> Any:
        return getattr(self.cfg, "subtitles", None)

    @property
    def configured(self) -> bool:
        s = self.scfg
        return bool(s is not None and (s.api_key or "").strip())

    @property
    def enabled(self) -> bool:
        s = self.scfg
        return bool(s is not None and s.enabled and self.configured)

    def languages(self) -> List[str]:
        out = []  # type: List[str]
        for code in (getattr(self.scfg, "languages", None) or ["en"]):
            c = normalize_language(code)
            if c and c not in out:
                out.append(c)
        return out

    def quota_until(self) -> float:
        try:
            return float(self.db.get_kv(_KV_QUOTA) or 0)
        except (TypeError, ValueError):
            return 0.0

    def quota_reached(self) -> bool:
        return time.time() < self.quota_until() or time.time() < self._rate_until

    def _set_quota(self, until: float, why: str) -> None:
        try:
            self.db.set_kv(_KV_QUOTA, "%.0f" % until)
        except Exception:  # pragma: no cover
            pass
        log.warning("OpenSubtitles : %s ; prochains téléchargements de sous-titres après %s", why,
                    time.strftime("%Y-%m-%d %H:%M", time.localtime(until)))

    def _stopped(self) -> bool:
        return bool(self.stop_event is not None and self.stop_event.is_set())

    # ------------------------------------------------------------- HTTP
    def _url(self, path: str) -> str:
        return (self._api or self.base_url).rstrip("/") + path

    def _headers(self, auth: bool = False) -> Dict[str, str]:
        h = {"Api-Key": (self.scfg.api_key or "").strip(), "User-Agent": "synoarr v%s" % __version__,
             "Content-Type": "application/json", "Accept": "application/json"}
        if auth and self._token:
            h["Authorization"] = "Bearer %s" % self._token
        return h

    def _check(self, resp: Response, what: str) -> None:
        if resp.status == 429:
            self._rate_until = time.time() + self.rate_limit_pause
            raise QuotaExceeded("trop de requêtes (HTTP 429)")
        if resp.status == 406:
            self._set_quota(_parse_reset(self._json(resp).get("reset_time_utc"), time.time()),
                            "quota de téléchargements atteint")
            raise QuotaExceeded("quota de téléchargements atteint")
        if resp.status == 401:
            raise HttpError("%s : clé API ou identifiants OpenSubtitles refusés (HTTP 401)" % what, 401)
        if not resp.ok:
            msg = self._json(resp).get("message") or self._json(resp).get("errors") or ""
            raise HttpError("%s : HTTP %s %s" % (what, resp.status, msg), resp.status)

    @staticmethod
    def _json(resp: Response) -> Dict[str, Any]:
        try:
            data = resp.json()
        except Exception:
            return {}
        return data if isinstance(data, dict) else {}

    def login(self) -> Optional[str]:
        """Jeton de session (une seule connexion par processus). None sans identifiants ou en cas d'échec."""
        s = self.scfg
        if self._token:
            return self._token
        if self._login_failed or not (s.username or "").strip() or not s.password:
            return None
        resp = self.http.post(self._url("/login"), json_body={"username": s.username.strip(), "password": s.password},
                              headers=self._headers(), raise_for_status=False)
        if resp.status in (400, 401, 403):
            self._login_failed = True
            log.error("Connexion OpenSubtitles refusée pour %s : vérifiez [subtitles] username/password "
                      "(téléchargements avec le quota anonyme)", s.username.strip())
            return None
        self._check(resp, "Connexion OpenSubtitles")
        data = self._json(resp)
        token = data.get("token")
        if not token:
            raise HttpError("Connexion OpenSubtitles : réponse sans jeton")
        self._token = str(token)
        base = str(data.get("base_url") or "").strip().strip("/")
        if base and self.base_url == API_URL and re.match(r"^[a-z0-9.-]+\.opensubtitles\.com$", base):
            self._api = "https://%s/api/v1" % base
        return self._token

    def search(self, params: Dict[str, Any]) -> List[Dict[str, Any]]:
        """GET /subtitles ; paramètres triés et en minuscules (recommandation de l'API, évite les
        redirections). Renvoie la liste data[]."""
        items = sorted((k, str(v).lower()) for k, v in params.items() if v is not None and str(v) != "")
        resp = self.http.get(self._url("/subtitles"), params=items, headers=self._headers(auth=True),
                             raise_for_status=False)
        self._check(resp, "Recherche OpenSubtitles")
        data = self._json(resp).get("data")
        return [d for d in data if isinstance(d, dict)] if isinstance(data, list) else []

    def download(self, file_id: int) -> bytes:
        """POST /download puis GET du lien. Lève QuotaExceeded / HttpError."""
        if time.time() < self.quota_until():
            raise QuotaExceeded("quota de téléchargements atteint")
        try:
            self.login()
        except QuotaExceeded:
            raise
        except HttpError as exc:
            log.warning("Connexion OpenSubtitles impossible (%s) : téléchargement avec le quota anonyme", exc)
        body = {"file_id": int(file_id), "sub_format": "srt"}
        resp = self.http.post(self._url("/download"), json_body=body, headers=self._headers(auth=True),
                              raise_for_status=False)
        if resp.status == 401 and self._token:  # jeton expiré : une reconnexion
            self._token = None
            self.login()
            resp = self.http.post(self._url("/download"), json_body=body, headers=self._headers(auth=True),
                                  raise_for_status=False)
        self._check(resp, "Téléchargement OpenSubtitles")
        data = self._json(resp)
        link = data.get("link")
        if not link:
            raise HttpError("Téléchargement OpenSubtitles : pas de lien (%s)" % (data.get("message") or "?"))
        remaining = data.get("remaining")
        content = self.http.get(str(link), headers={"User-Agent": "synoarr v%s" % __version__}).body
        try:
            if remaining is not None and int(remaining) <= 0:
                self._set_quota(_parse_reset(data.get("reset_time_utc"), time.time()),
                                "quota quotidien de téléchargements épuisé")
        except (TypeError, ValueError):
            pass
        return content

    # -------------------------------------------------------- selection
    def choose(self, candidates: List[Dict[str, Any]], language: str, release_name: str) -> Optional[Dict[str, Any]]:
        """Meilleur sous-titre d'une langue : {"file_id", "file_name", "release", "score"} ou None."""
        hi_wanted = bool(getattr(self.scfg, "hearing_impaired", False))
        best = None  # type: Optional[Tuple[Any, Dict[str, Any]]]
        for c in candidates:
            a = c.get("attributes") if isinstance(c.get("attributes"), dict) else {}
            if normalize_language(str(a.get("language") or "")) != language:
                continue
            if a.get("foreign_parts_only"):
                continue
            files = [f for f in (a.get("files") or []) if isinstance(f, dict) and f.get("file_id")]
            if len(files) != 1:  # sous-titres en plusieurs CD : inutilisables pour un seul fichier vidéo
                continue
            release = str(a.get("release") or files[0].get("file_name") or "")
            machine = bool(a.get("machine_translated") or a.get("ai_translated"))
            try:
                downloads = int(a.get("download_count") or 0)
            except (TypeError, ValueError):
                downloads = 0
            key = (1 if a.get("moviehash_match") else 0,
                   1 if bool(a.get("hearing_impaired")) == hi_wanted else 0,
                   0 if machine else 1,
                   round(name_similarity(release, release_name), 2),
                   1 if a.get("from_trusted") else 0,
                   downloads)
            if best is None or key > best[0]:
                best = (key, {"file_id": files[0]["file_id"], "file_name": files[0].get("file_name") or "",
                              "release": release, "score": key})
        return best[1] if best else None

    # ---------------------------------------------------- identification
    def identify(self, video_path: str, kind: Optional[str] = None) -> Dict[str, Any]:
        """Type, titre, année, saison/épisode et identifiants d'une vidéo de la bibliothèque (base synoarr,
        puis TMDb, sinon recherche par titre)."""
        info = {"kind": kind, "title": "", "year": None, "season": None, "episode": None,
                "imdb": None, "tmdb": None}  # type: Dict[str, Any]
        p = parse(os.path.basename(video_path))
        series_dir = self.cfg.paths.series_dir
        rel_show = None
        if series_dir and _inside(video_path, series_dir):
            info["kind"] = info["kind"] or "episode"
            rel = os.path.relpath(video_path, series_dir).split(os.sep)
            if len(rel) > 1:
                rel_show = rel[0]
        elif self.cfg.paths.movies_dir and _inside(video_path, self.cfg.paths.movies_dir):
            info["kind"] = info["kind"] or "movie"
        if not info["kind"]:
            info["kind"] = "episode" if p.is_episode else "movie"
        if info["kind"] == "episode":
            info["season"], info["episode"] = p.season, p.episode
            sp = parse(rel_show) if rel_show else None
            info["title"] = (sp.title if sp and sp.title else "") or p.title
            info["year"] = (sp.year if sp else None) or p.year
            mtype = "show"
        else:
            info["title"], info["year"] = p.title, p.year
            mtype = "movie"
        if not info["title"]:
            return info
        row = self._find_media(mtype, info["title"], info["year"])
        if row:
            info["imdb"], info["tmdb"] = row.get("imdb"), row.get("tmdb")
            info["year"] = info["year"] or row.get("year")
        elif self.tmdb is not None:
            try:
                found = (self.tmdb.search_movie(info["title"], info["year"]) if mtype == "movie"
                         else self.tmdb.search_show(info["title"]))
            except Exception as exc:  # pragma: no cover - TmdbClient ne lève pas
                log.debug("TMDb : %s", exc)
                found = None
            if found and found.get("tmdb"):
                info["tmdb"] = str(found["tmdb"])
        return info

    def _find_media(self, mtype: str, title: str, year: Optional[int]) -> Optional[Dict[str, Any]]:
        try:
            rows = self.db.query("SELECT * FROM media WHERE type = ?", (mtype,))
        except Exception:
            return None
        for row in rows:
            if row.get("year") and year and abs(int(row["year"]) - int(year)) > 1:
                continue
            alts = [t for t in (row.get("original_title"), row.get("title_fr")) if t]
            if titles_match(title, row.get("title") or "", alts):
                return row
        return None

    # ------------------------------------------------------------ fetch
    def wanted_missing(self, video_path: str, languages: Optional[List[str]] = None) -> List[str]:
        wanted = [normalize_language(l) for l in (languages or self.languages())]
        have = existing_subtitle_languages(video_path, self.cfg.naming.subtitle_extensions)
        return [l for l in wanted if l and _base_lang(l) not in have and
                not os.path.exists(self.target_path(video_path, l))]

    @staticmethod
    def target_path(video_path: str, language: str) -> str:
        base, _ext = os.path.splitext(video_path)
        return "%s.%s.srt" % (base, language)

    def fetch_for(self, video_path: str, kind: Optional[str] = None, imdb: Any = None, tmdb: Any = None,
                  season: Optional[int] = None, episode: Optional[int] = None,
                  languages: Optional[List[str]] = None, release_name: str = "", title: str = "",
                  year: Optional[int] = None) -> Dict[str, Any]:
        """Cherche et enregistre les sous-titres manquants d'une vidéo. Ne lève jamais.
        Retour : {"video", "status" (found|partial|none|present|quota|error|disabled), "downloaded": [chemins],
        "missing": [langues], "message"}."""
        res = {"video": video_path, "status": "none", "downloaded": [], "missing": [], "message": ""}  # type: Dict[str, Any]
        if not self.configured:
            res.update(status="disabled", message="[subtitles] api_key non renseignée")
            return res
        with self._lock:
            try:
                return self._fetch(res, video_path, kind, imdb, tmdb, season, episode, languages,
                                   release_name, title, year)
            except QuotaExceeded as exc:
                res.update(status="quota", message=str(exc))
            except HttpError as exc:
                log.warning("Sous-titres de %s : %s", os.path.basename(video_path), exc)
                res.update(status="error", message=str(exc))
            except Exception as exc:  # le démon ne doit jamais tomber
                log.exception("Sous-titres de %s : erreur inattendue : %s", video_path, exc)
                res.update(status="error", message=str(exc))
            if res["status"] == "error":
                self._remember(video_path, languages, "error", res["message"])
            return res

    def _fetch(self, res: Dict[str, Any], video_path: str, kind: Optional[str], imdb: Any, tmdb: Any,
               season: Optional[int], episode: Optional[int], languages: Optional[List[str]],
               release_name: str, title: str, year: Optional[int]) -> Dict[str, Any]:
        if not os.path.isfile(video_path):
            res.update(status="error", message="fichier introuvable")
            return res
        missing = self.wanted_missing(video_path, languages)
        if not missing:
            res.update(status="present", message="sous-titres déjà présents")
            return res
        res["missing"] = list(missing)
        if self.quota_reached():
            raise QuotaExceeded("quota OpenSubtitles atteint : nouvel essai plus tard")
        if not (imdb or tmdb or title):
            info = self.identify(video_path, kind)
            kind = kind or info["kind"]
            imdb, tmdb = info["imdb"], info["tmdb"]
            title, year = info["title"], info["year"]
            season = season if season is not None else info["season"]
            episode = episode if episode is not None else info["episode"]
        if not kind:
            kind = "episode" if parse(os.path.basename(video_path)).is_episode else "movie"
        if kind == "episode" and (season is None or episode is None):
            p = parse(os.path.basename(video_path))
            season = season if season is not None else p.season
            episode = episode if episode is not None else p.episode

        params = {"languages": ",".join(sorted(missing))}  # type: Dict[str, Any]
        imdb_d, tmdb_d = _imdb_digits(imdb), _tmdb_digits(tmdb)
        if kind == "episode":
            params["type"] = "episode"
            if imdb_d:
                params["parent_imdb_id"] = imdb_d
            elif tmdb_d:
                params["parent_tmdb_id"] = tmdb_d
            if season is not None:
                params["season_number"] = int(season)
            if episode is not None:
                params["episode_number"] = int(episode)
        else:
            params["type"] = "movie"
            if imdb_d:
                params["imdb_id"] = imdb_d
            elif tmdb_d:
                params["tmdb_id"] = tmdb_d
        if not (imdb_d or tmdb_d):
            q = title or parse(os.path.basename(video_path)).title
            if q:
                params["query"] = normalize_title(q)
            if kind == "movie" and year:
                params["year"] = int(year)
        movie_hash = opensubtitles_hash(video_path)
        candidates = []  # type: List[Dict[str, Any]]
        if movie_hash:
            candidates = self.search(dict(params, moviehash=movie_hash))
        if not candidates and (imdb_d or tmdb_d or params.get("query")):
            candidates = self.search(params)

        name = release_name or os.path.splitext(os.path.basename(video_path))[0]
        downloaded = []  # type: List[str]
        for lang in missing:
            if self._stopped():
                break
            pick = self.choose(candidates, lang, name)
            if pick is None:
                continue
            content = self.download(pick["file_id"])
            path = self._save(video_path, lang, content)
            if path:
                downloaded.append(path)
                log.info("Sous-titre %s téléchargé : %s (release %s)", lang, os.path.basename(path),
                         pick["release"] or pick["file_name"])
        still = [l for l in missing if self.target_path(video_path, l) not in downloaded]
        res["downloaded"], res["missing"] = downloaded, still
        if downloaded:
            res["status"] = "partial" if still else "found"
            try:
                self.db.add_event("subtitles", "Sous-titres (%s) : %s" % (
                    ", ".join(l for l in missing if l not in still), os.path.basename(video_path)))
            except Exception:  # pragma: no cover
                pass
        else:
            res["status"] = "none"
            res["message"] = "aucun sous-titre trouvé (%s)" % ", ".join(missing)
            log.info("Aucun sous-titre %s trouvé pour %s", ", ".join(missing), os.path.basename(video_path))
        self._remember(video_path, languages, res["status"], res["message"])
        return res

    def _save(self, video_path: str, lang: str, content: bytes) -> Optional[str]:
        """Écrit <vidéo>.<langue>.srt sans jamais écraser un fichier existant."""
        if not content or not content.strip():
            log.warning("Sous-titre %s vide pour %s : ignoré", lang, os.path.basename(video_path))
            return None
        target = self.target_path(video_path, lang)
        if os.path.lexists(target):
            return None
        folder = os.path.dirname(target)
        tmp = os.path.join(folder, "%ssub-%d-%s" % (TMP_PREFIX, os.getpid(), threading.get_ident()))
        try:
            with open(tmp, "wb") as fh:
                fh.write(content)
            try:
                os.link(tmp, target)  # échoue si la cible existe : jamais d'écrasement
            except FileExistsError:
                return None
            except OSError:  # système de fichiers sans liens physiques
                if os.path.lexists(target):
                    return None
                os.rename(tmp, target)
            return target
        finally:
            try:
                if os.path.exists(tmp):
                    os.remove(tmp)
            except OSError:  # pragma: no cover
                pass

    # ------------------------------------------------------------ cache
    def _remember(self, video_path: str, languages: Optional[List[str]], status: str, message: str) -> None:
        try:
            row = self.db.query_one("SELECT attempts FROM subtitle_searches WHERE path = ?", (video_path,))
            attempts = (int(row["attempts"]) if row else 0) + 1
            if status == "found":
                attempts = 0
            self.db.execute("INSERT OR REPLACE INTO subtitle_searches(path, languages, status, attempts, last_at, "
                            "message) VALUES(?, ?, ?, ?, ?, ?)",
                            (video_path, ",".join(languages or self.languages()), status, attempts, time.time(),
                             message or ""))
        except Exception as exc:  # pragma: no cover
            log.debug("Mémorisation de la recherche de sous-titres impossible : %s", exc)

    def _recently_tried(self, video_path: str, now: float) -> bool:
        try:
            row = self.db.query_one("SELECT * FROM subtitle_searches WHERE path = ?", (video_path,))
        except Exception:
            return False
        if not row or row.get("languages") != ",".join(self.languages()):
            return False
        last = float(row.get("last_at") or 0)
        status = row.get("status")
        if status == "error":
            delay = 6 * 3600.0
        elif status in ("none", "partial"):
            delay = min(30, 2 ** max(1, int(row.get("attempts") or 1))) * 86400.0
        else:
            return False
        return now - last < delay

    # ------------------------------------------------------------- scan
    def library_videos(self) -> List[str]:
        exts = set(e.lower().lstrip(".") for e in self.cfg.naming.video_extensions)
        out = []  # type: List[str]
        roots = []  # type: List[str]
        for root in (self.cfg.paths.movies_dir, self.cfg.paths.series_dir):
            if root and os.path.isdir(root) and root not in roots:
                roots.append(root)
        for root in roots:
            for dirpath, dirnames, filenames in os.walk(root):
                dirnames[:] = [d for d in dirnames if not _is_ignored_name(d) and d.lower() not in _EXTRA_DIRS]
                for fn in filenames:
                    if _is_ignored_name(fn) or fn.startswith(TMP_PREFIX) or "." not in fn:
                        continue
                    if fn.rsplit(".", 1)[1].lower() not in exts or re.search(r"\bsample\b", fn.lower()):
                        continue
                    path = os.path.join(dirpath, fn)
                    try:
                        if os.path.islink(path) or os.path.getsize(path) < self.min_video_size:
                            continue
                    except OSError:
                        continue
                    out.append(path)
        return out

    def scan_library(self, limit: Optional[int] = None, force: bool = False) -> Dict[str, Any]:
        """Cherche les sous-titres manquants des vidéos de la bibliothèque, les plus anciennes d'abord, par
        petits lots (*limit* vidéos, défaut [subtitles] scan_limit). Ne lève jamais."""
        stats = {"videos": 0, "missing": 0, "searched": 0, "downloaded": 0, "errors": 0,
                 "quota_reached": False}  # type: Dict[str, Any]
        if not self.configured:
            log.warning("Sous-titres : [subtitles] api_key non renseignée")
            return stats
        if limit is None:
            limit = int(getattr(self.scfg, "scan_limit", 20) or 20)
        self.scanning = True
        try:
            if self.quota_reached():
                stats["quota_reached"] = True
                log.info("Quota OpenSubtitles atteint : recherche de sous-titres reportée")
                return stats
            now = time.time()
            todo = []  # type: List[Tuple[float, str]]
            for path in self.library_videos():
                stats["videos"] += 1
                if not self.wanted_missing(path):
                    continue
                stats["missing"] += 1
                if not force and self._recently_tried(path, now):
                    continue
                try:
                    mtime = os.path.getmtime(path)
                except OSError:
                    continue
                todo.append((mtime, path))
            todo.sort()
            for i, (_mtime, path) in enumerate(todo[:max(0, int(limit))]):
                if self._stopped():
                    break
                if i and self.request_pause > 0 and self.stop_event is not None:
                    if self.stop_event.wait(self.request_pause):
                        break
                elif i and self.request_pause > 0:
                    time.sleep(self.request_pause)
                res = self.fetch_for(path)
                stats["searched"] += 1
                stats["downloaded"] += len(res["downloaded"])
                if res["status"] == "error":
                    stats["errors"] += 1
                if res["status"] == "quota":
                    stats["quota_reached"] = True
                    break
            log.info("Sous-titres : %d vidéo(s), %d sans sous-titre voulu, %d recherchée(s), %d téléchargé(s)%s",
                     stats["videos"], stats["missing"], stats["searched"], stats["downloaded"],
                     " (quota atteint)" if stats["quota_reached"] else "")
            return stats
        except Exception as exc:  # pragma: no cover - jamais vers le démon
            log.exception("Recherche des sous-titres manquants en erreur : %s", exc)
            stats["errors"] += 1
            return stats
        finally:
            self.scanning = False

    # ------------------------------------------------------ import hook
    def enqueue(self, video_path: str, meta: Optional[Dict[str, Any]] = None) -> None:
        """Appelé par l'organiseur après chaque vidéo importée : recherche en arrière-plan."""
        if not self.enabled:
            return
        self._queue.put((video_path, dict(meta or {})))
        with self._worker_lock:
            if self._worker is None or not self._worker.is_alive():
                self._worker = threading.Thread(target=self._work, name="synoarr-subtitles", daemon=True)
                self._worker.start()

    def _work(self) -> None:
        while not self._stopped():
            try:
                item = self._queue.get(timeout=self.idle_timeout)
            except queue.Empty:
                return
            try:
                self.process_item(*item)
            finally:
                self._queue.task_done()

    def drain(self) -> int:
        """Traite la file dans le thread courant (tests, CLI). Renvoie le nombre de vidéos traitées."""
        n = 0
        while True:
            try:
                item = self._queue.get_nowait()
            except queue.Empty:
                return n
            try:
                self.process_item(*item)
                n += 1
            finally:
                self._queue.task_done()

    def wait_idle(self, timeout: float = 10.0) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self._queue.unfinished_tasks == 0:
                return True
            time.sleep(0.02)
        return False

    def process_item(self, video_path: str, meta: Dict[str, Any]) -> Dict[str, Any]:
        try:
            kind = meta.get("kind") if meta.get("kind") in ("movie", "episode") else None
            imdb = tmdb = None
            title, year = "", meta.get("year")
            media_id = meta.get("media_id")
            if media_id:
                row = self.db.get_media(int(media_id))
                if row:
                    imdb, tmdb = row.get("imdb"), row.get("tmdb")
                    title, year = row.get("title") or "", row.get("year") or year
            episodes = meta.get("episodes") or []
            season = meta.get("season")
            if not (imdb or tmdb):
                return self.fetch_for(video_path, kind=kind, season=season,
                                      episode=episodes[0] if episodes else None,
                                      release_name=meta.get("source_name") or "")
            return self.fetch_for(video_path, kind=kind, imdb=imdb, tmdb=tmdb, season=season,
                                  episode=episodes[0] if episodes else None,
                                  release_name=meta.get("source_name") or "", title=title, year=year)
        except Exception as exc:  # pragma: no cover
            log.exception("Sous-titres de %s : erreur inattendue : %s", video_path, exc)
            return {"video": video_path, "status": "error", "downloaded": [], "missing": [], "message": str(exc)}


def _inside(path: str, root: str) -> bool:
    try:
        return os.path.commonpath([os.path.abspath(path), os.path.abspath(root)]) == os.path.abspath(root)
    except ValueError:
        return False
