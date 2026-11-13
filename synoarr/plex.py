"""Plex client: Watchlist (plex.tv discover provider), metadata (guids, episodes),
PIN login and local server library refresh.

Only stdlib + synoarr.httpclient. Responses are requested as JSON
(``Accept: application/json``) but XML answers are tolerated (Plex sometimes
ignores the Accept header, e.g. behind some proxies or on old servers).

Important: :meth:`PlexClient.watchlist` raises on any error instead of
returning a partial list, because the caller marks every item missing from the
returned list as "removed from the Watchlist".
"""
from __future__ import annotations

import logging
import re
import xml.etree.ElementTree as ET
from typing import Any, Dict, List, Optional, Tuple

from . import __version__
from .httpclient import HttpClient, HttpError, Response, redact
from .models import EpisodeInfo, WatchItem

log = logging.getLogger(__name__)

PRODUCT = "synoarr"
PAGE_SIZE = 100
MAX_PAGES = 200  # safety net against a server that ignores X-Plex-Container-Start

# guid scheme -> our key
_GUID_SCHEMES = {
    "imdb": "imdb",
    "tmdb": "tmdb",
    "themoviedb": "tmdb",
    "tvdb": "tvdb",
    "thetvdb": "tvdb",
}
# XML tags that represent metadata items inside a MediaContainer
_ITEM_TAGS = ("Video", "Directory", "Metadata", "Track", "Photo")


# ---------------------------------------------------------------------------- helpers
def parse_guid(guid: str) -> Optional[Tuple[str, str]]:
    """Parse a Plex guid string into ``(kind, value)``.

    Handles "imdb://tt123", "tmdb://123", "tvdb://456", the legacy agent form
    "com.plexapp.agents.imdb://tt123?lang=en", "com.plexapp.agents.thetvdb://456/1/2?lang=en",
    "com.plexapp.agents.themoviedb://123?lang=fr". Returns None for "plex://..." and unknown schemes.
    """
    if not guid or not isinstance(guid, str) or "://" not in guid:
        return None
    scheme, _, rest = guid.strip().partition("://")
    scheme = scheme.lower()
    if scheme.startswith("com.plexapp.agents."):
        scheme = scheme[len("com.plexapp.agents."):]
    kind = _GUID_SCHEMES.get(scheme)
    if kind is None:
        return None
    value = rest.split("?", 1)[0].split("/", 1)[0].strip()
    if not value:
        return None
    if kind == "imdb":
        if not re.match(r"^tt\d+$", value, re.I):
            if value.isdigit():
                value = "tt" + value
            else:
                return None
        value = value.lower()
    elif not value.isdigit():
        return None
    return kind, value


def extract_guids(item: Dict[str, Any]) -> Dict[str, str]:
    """Collect external ids of a metadata item (``Guid`` list and/or legacy ``guid`` string)."""
    out: Dict[str, str] = {}
    raw = item.get("Guid")
    if isinstance(raw, dict):
        raw = [raw]
    if isinstance(raw, list):
        for g in raw:
            gid = g.get("id") if isinstance(g, dict) else g if isinstance(g, str) else None
            parsed = parse_guid(gid) if gid else None
            if parsed and parsed[0] not in out:
                out[parsed[0]] = parsed[1]
    legacy = parse_guid(item.get("guid") or "")
    if legacy and legacy[0] not in out:
        out[legacy[0]] = legacy[1]
    return out


def extract_genres(item: Dict[str, Any]) -> List[str]:
    """Genre names of a metadata item: "Genre": [{"tag": "Animation"}, ...] (or plain strings)."""
    raw = item.get("Genre") or item.get("genres") or []
    if isinstance(raw, (dict, str)):
        raw = [raw]
    out: List[str] = []
    for g in raw:
        name = g.get("tag") or g.get("title") or g.get("name") if isinstance(g, dict) else g
        if name and str(name) not in out:
            out.append(str(name))
    return out


def _to_int(value: Any) -> Optional[int]:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(str(value).strip()[:4] if isinstance(value, str) and "-" in value else value)
    except (TypeError, ValueError):
        return None


def _rating_key(item: Dict[str, Any]) -> Optional[str]:
    rk = item.get("ratingKey")
    if rk not in (None, ""):
        return str(rk)
    key = str(item.get("key") or "")
    m = re.search(r"/library/metadata/([^/?]+)", key)
    if m:
        return m.group(1)
    guid = str(item.get("guid") or "")
    if guid.startswith("plex://"):
        last = guid.rstrip("/").rsplit("/", 1)[-1]
        if last:
            return last
    return None


def _xml_element_to_dict(el: ET.Element) -> Dict[str, Any]:
    d: Dict[str, Any] = dict(el.attrib)
    for child in el:
        if child.tag in d and not isinstance(d[child.tag], list):
            continue  # attribute with the same name wins
        d.setdefault(child.tag, []).append(_xml_element_to_dict(child))
    return d


def parse_container(resp: Response) -> Dict[str, Any]:
    """Return the ``MediaContainer`` dict of a Plex response (JSON or XML).

    Items are always exposed under the ``Metadata`` key as a list of dicts.
    Raises HttpError if the body is neither JSON nor XML.
    """
    text = resp.text().strip()
    if not text:
        return {"Metadata": []}
    if text[:1] in "{[":
        data = resp.json()
        if isinstance(data, dict):
            mc = data.get("MediaContainer", data)
        else:
            mc = {"Metadata": data}
        if not isinstance(mc, dict):
            return {"Metadata": []}
        mc = dict(mc)
        items = mc.get("Metadata")
        if items is None:
            items = []
            for tag in _ITEM_TAGS:
                if isinstance(mc.get(tag), list):
                    items.extend(mc[tag])
        if isinstance(items, dict):
            items = [items]
        mc["Metadata"] = [i for i in items if isinstance(i, dict)]
        return mc
    if text.startswith("<"):
        try:
            root = ET.fromstring(text.encode("utf-8"))
        except ET.ParseError as exc:
            raise HttpError("Réponse XML Plex invalide de %s: %s" % (redact(resp.url), exc),
                            resp.status, resp.body, resp.url)
        mc = dict(root.attrib)
        items = []
        for child in root:
            if child.tag in _ITEM_TAGS:
                items.append(_xml_element_to_dict(child))
            else:
                mc.setdefault(child.tag, []).append(_xml_element_to_dict(child))
        mc["Metadata"] = items
        mc["_root_tag"] = root.tag
        return mc
    raise HttpError("Réponse Plex inattendue de %s: %r" % (redact(resp.url), text[:200]),
                    resp.status, resp.body, resp.url)


def _parse_object(resp: Response) -> Dict[str, Any]:
    """Parse a plex.tv v2 object answer (pin, user) given as JSON or XML."""
    text = resp.text().strip()
    if text.startswith("<"):
        try:
            root = ET.fromstring(text.encode("utf-8"))
        except ET.ParseError as exc:
            raise HttpError("Réponse XML Plex invalide de %s: %s" % (redact(resp.url), exc),
                            resp.status, resp.body, resp.url)
        return dict(root.attrib)
    data = resp.json()
    if isinstance(data, dict) and isinstance(data.get("user"), dict) and "username" not in data:
        return data["user"]
    return data if isinstance(data, dict) else {}


# ----------------------------------------------------------------------------- client
class PlexClient:
    def __init__(self, token: str, client_id: str, http: HttpClient):
        self.token = (token or "").strip()
        self.client_id = (client_id or "").strip() or "synoarr"
        self.http = http
        self.discover_url = "https://discover.provider.plex.tv"
        self.metadata_url = "https://metadata.provider.plex.tv"
        self.plextv_url = "https://plex.tv"

    # ------------------------------------------------------------ low level
    def _headers(self, with_token: bool = True) -> Dict[str, str]:
        h = {
            "Accept": "application/json",
            "X-Plex-Product": PRODUCT,
            "X-Plex-Version": __version__,
            "X-Plex-Client-Identifier": self.client_id,
            "X-Plex-Platform": "Python",
            "X-Plex-Device-Name": "synoarr (Synology)",
        }
        if with_token and self.token:
            h["X-Plex-Token"] = self.token
        return h

    def _provider_get(self, path: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """GET *path* on the discover provider, falling back to the metadata provider on 404."""
        bases = [self.discover_url.rstrip("/"), self.metadata_url.rstrip("/")]
        last_exc: Optional[HttpError] = None
        for i, base in enumerate(bases):
            try:
                resp = self.http.get(base + path, params=params, headers=self._headers())
                return parse_container(resp)
            except HttpError as exc:
                last_exc = exc
                if exc.status == 404 and i == 0:
                    log.debug("Plex: 404 sur %s, essai sur %s", redact(base + path), bases[1])
                    continue
                raise self._friendly(exc)
        assert last_exc is not None
        raise self._friendly(last_exc)

    @staticmethod
    def _friendly(exc: HttpError) -> HttpError:
        if exc.status == 401:
            return HttpError("Jeton Plex refusé (HTTP 401) : jeton invalide ou expiré. Refaites la connexion Plex "
                             "(python3 synoarr.py plex-login) ou corrigez [plex] token.", 401, exc.body, exc.url)
        return exc

    def _paginated(self, path: str, extra: Optional[Dict[str, Any]] = None,
                   max_items: Optional[int] = None) -> Tuple[List[Dict[str, Any]], Optional[int]]:
        """Fetch every page of a container. Returns (items, totalSize)."""
        items: List[Dict[str, Any]] = []
        start = 0
        total: Optional[int] = None
        size = PAGE_SIZE if max_items is None else max(1, min(PAGE_SIZE, max_items))
        for _ in range(MAX_PAGES):
            params: Dict[str, Any] = dict(extra or {})
            params["X-Plex-Container-Start"] = start
            params["X-Plex-Container-Size"] = size
            mc = self._provider_get(path, params)
            page = mc.get("Metadata") or []
            t = _to_int(mc.get("totalSize"))
            if t is not None:
                total = t
            items.extend(page)
            start += len(page)
            if not page:
                break
            if max_items is not None and len(items) >= max_items:
                break
            if total is not None:
                if start >= total:
                    break
            elif len(page) < size:
                break
        return items, total

    # ------------------------------------------------------------ watchlist
    def watchlist(self) -> List[WatchItem]:
        """All Watchlist items (movies and shows). Raises HttpError on failure (never a partial list)."""
        raw, _total = self._paginated("/library/sections/watchlist/all",
                                      {"includeGuids": 1, "includeCollections": 0, "includeExternalMedia": 1})
        out: List[WatchItem] = []
        seen = set()
        for item in raw:
            wi = self._to_watch_item(item)
            if wi is None or wi.rating_key in seen:
                continue
            seen.add(wi.rating_key)
            if not wi.guids:
                try:
                    wi.guids = self.get_guids(wi.rating_key)
                except HttpError as exc:
                    log.warning("Plex : identifiants externes indisponibles pour « %s » : %s", wi.title, exc)
            out.append(wi)
        return out

    def _to_watch_item(self, item: Dict[str, Any]) -> Optional[WatchItem]:
        itype = str(item.get("type") or "").lower()
        if itype not in ("movie", "show"):
            log.debug("Plex : élément de Watchlist ignoré (type %r) : %s", itype, item.get("title"))
            return None
        rk = _rating_key(item)
        title = item.get("title")
        if not rk or not title:
            log.debug("Plex : élément de Watchlist incomplet ignoré : %r", item)
            return None
        year = _to_int(item.get("year"))
        if year is None and item.get("originallyAvailableAt"):
            year = _to_int(str(item.get("originallyAvailableAt"))[:4])
        orig = item.get("originalTitle") or None
        return WatchItem(rating_key=rk, type=itype, title=str(title), year=year, guids=extract_guids(item),
                         original_title=str(orig) if orig and orig != title else None,
                         genres=extract_genres(item))

    # ------------------------------------------------------------ metadata
    def get_guids(self, rating_key: str) -> Dict[str, str]:
        mc = self._provider_get("/library/metadata/%s" % rating_key, {"includeGuids": 1})
        guids: Dict[str, str] = {}
        for item in mc.get("Metadata") or []:
            for k, v in extract_guids(item).items():
                guids.setdefault(k, v)
            if guids:
                break
        return guids

    def get_details(self, rating_key: str) -> Dict[str, Any]:
        """{"guids": {...}, "genres": [...]} from the item's metadata page. Raises HttpError."""
        mc = self._provider_get("/library/metadata/%s" % rating_key, {"includeGuids": 1})
        guids: Dict[str, str] = {}
        genres: List[str] = []
        for item in mc.get("Metadata") or []:
            for k, v in extract_guids(item).items():
                guids.setdefault(k, v)
            for g in extract_genres(item):
                if g not in genres:
                    genres.append(g)
            if guids or genres:
                break
        return {"guids": guids, "genres": genres}

    def show_episodes(self, rating_key: str) -> List[EpisodeInfo]:
        """Episodes of a show (season 0 / specials excluded), sorted."""
        seasons, _ = self._paginated("/library/metadata/%s/children" % rating_key)
        episodes: Dict[Tuple[int, int], EpisodeInfo] = {}
        season_items = [s for s in seasons if str(s.get("type") or "season").lower() == "season"]
        for season in season_items:
            sidx = _to_int(season.get("index"))
            if sidx is None or sidx <= 0:
                continue  # specials / unknown
            skey = _rating_key(season)
            if not skey:
                continue
            try:
                eps, _ = self._paginated("/library/metadata/%s/children" % skey)
            except HttpError as exc:
                # never return a partial list: missing old episodes would later be added as "wanted"
                raise HttpError("Plex : épisodes de la saison %s indisponibles : %s" % (sidx, exc),
                                exc.status, exc.body, exc.url)
            for ep in eps:
                self._add_episode(episodes, ep, sidx)
        if not season_items:
            # some providers answer with episodes directly, or need allLeaves/grandchildren
            for s in seasons:
                if str(s.get("type") or "").lower() == "episode":
                    self._add_episode(episodes, s, None)
            if not episodes:
                for suffix in ("allLeaves", "grandchildren"):
                    try:
                        eps, _ = self._paginated("/library/metadata/%s/%s" % (rating_key, suffix))
                    except HttpError as exc:
                        log.debug("Plex : %s indisponible : %s", suffix, exc)
                        continue
                    for ep in eps:
                        self._add_episode(episodes, ep, None)
                    if episodes:
                        break
        return [episodes[k] for k in sorted(episodes)]

    @staticmethod
    def _add_episode(out: Dict[Tuple[int, int], EpisodeInfo], ep: Dict[str, Any], season: Optional[int]) -> None:
        if str(ep.get("type") or "episode").lower() != "episode":
            return
        eidx = _to_int(ep.get("index"))
        sidx = _to_int(ep.get("parentIndex"))
        if sidx is None:
            sidx = season
        if eidx is None or sidx is None or sidx <= 0 or eidx < 0:
            return
        air = ep.get("originallyAvailableAt")
        air = str(air)[:10] if air else None
        title = ep.get("title")
        out[(sidx, eidx)] = EpisodeInfo(season=sidx, episode=eidx, title=str(title) if title else None, air_date=air)

    # ------------------------------------------------------------ account
    def get_username(self) -> Optional[str]:
        resp = self.http.get(self.plextv_url.rstrip("/") + "/api/v2/user", headers=self._headers())
        data = _parse_object(resp)
        for key in ("username", "title", "friendlyName", "email"):
            if data.get(key):
                return str(data[key])
        return None

    def test(self) -> Tuple[bool, str]:
        if not self.token:
            return False, ("Aucun jeton Plex configuré : lancez « python3 synoarr.py plex-login » "
                           "ou renseignez [plex] token.")
        username = None
        try:
            username = self.get_username()
        except HttpError as exc:
            exc = self._friendly(exc)
            if exc.status == 401:
                return False, str(exc)
            log.debug("Plex : /api/v2/user indisponible : %s", exc)
        try:
            raw, total = self._paginated("/library/sections/watchlist/all", {"includeGuids": 1}, max_items=1)
        except HttpError as exc:
            return False, "Erreur d'accès à la Watchlist Plex : %s" % exc
        count = total if total is not None else len(raw)
        who = " en tant que %s" % username if username else ""
        return True, "Connexion Plex OK%s : %d élément(s) dans la Watchlist." % (who, count)

    # ------------------------------------------------------------ PIN login
    def start_pin_login(self) -> Tuple[int, str]:
        """Create a (non strong) PIN. Returns (pin_id, code to enter on https://plex.tv/link)."""
        resp = self.http.post(self.plextv_url.rstrip("/") + "/api/v2/pins",
                              data={"strong": "false", "X-Plex-Product": PRODUCT,
                                    "X-Plex-Client-Identifier": self.client_id},
                              headers=self._headers(with_token=False))
        data = _parse_object(resp)
        pin_id = _to_int(data.get("id"))
        code = data.get("code")
        if pin_id is None or not code:
            raise HttpError("Réponse inattendue de plex.tv lors de la création du code PIN", resp.status,
                            resp.body, resp.url)
        return pin_id, str(code)

    def check_pin(self, pin_id: int) -> Optional[str]:
        """Return the auth token once the PIN has been linked, None while pending.

        Raises HttpError if the PIN expired / does not exist (HTTP 404).
        """
        url = self.plextv_url.rstrip("/") + "/api/v2/pins/%s" % pin_id
        try:
            resp = self.http.get(url, headers=self._headers(with_token=False))
        except HttpError as exc:
            if exc.status == 404:
                raise HttpError("Code PIN Plex expiré ou inconnu : recommencez la connexion.", 404, exc.body, exc.url)
            if exc.status is None or exc.status >= 500 or exc.status == 429:
                log.warning("Plex : vérification du code PIN impossible pour l'instant : %s", exc)
                return None
            raise
        data = _parse_object(resp)
        token = data.get("authToken") or data.get("auth_token")
        return str(token) if token else None

    # ------------------------------------------------------------ server
    def refresh_library(self, server_url: str, token: Optional[str] = None) -> bool:
        return refresh_library(server_url, token or self.token, self.http)


def refresh_library(server_url: str, token: str, http: HttpClient) -> bool:
    """Ask a local Plex Media Server to scan all its libraries. Never raises."""
    if not server_url:
        return False
    url = server_url.rstrip("/") + "/library/sections/all/refresh"
    headers = {"Accept": "application/json", "X-Plex-Product": PRODUCT}
    if token:
        headers["X-Plex-Token"] = token
    try:
        http.get(url, headers=headers)
        log.info("Plex : actualisation des bibliothèques demandée")
        return True
    except HttpError as exc:
        if exc.status == 401:
            log.warning("Plex : actualisation refusée (HTTP 401), vérifiez [plex] server_token")
        else:
            log.warning("Plex : actualisation des bibliothèques impossible : %s", exc)
        return False
