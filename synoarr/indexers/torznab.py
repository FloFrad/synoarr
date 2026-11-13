"""Torznab indexer (Jackett, Prowlarr, any Torznab compatible endpoint).

The URL typed by the user may end with ``/api`` or not: when it does not, the URL
is tried as-is first, then with ``/api`` appended. The working endpoint is remembered.
"""
from __future__ import annotations

import logging
import urllib.parse
import xml.etree.ElementTree as ET
from typing import Any, Dict, List, Optional, Set, Tuple

from ..httpclient import HttpClient, HttpError
from ..models import Release, SearchRequest
from .base import Indexer, categories_for, is_feed, local_name, parse_feed, parse_xml, xml_error

log = logging.getLogger(__name__)

TORZNAB_NS = "http://torznab.com/schemas/2015/feed"

_ERROR_HINTS = {
    "100": "clé API incorrecte",
    "101": "compte suspendu",
    "102": "droits insuffisants",
    "200": "paramètre manquant",
    "201": "paramètre incorrect",
    "202": "fonction non disponible",
    "203": "fonction non disponible",
    "429": "limite de requêtes atteinte",
}


class TorznabError(Exception):
    """The endpoint is a Torznab API but answered with an <error/> element."""

    def __init__(self, code: str, description: str):
        hint = _ERROR_HINTS.get(code, "")
        msg = "erreur Torznab %s%s%s" % (code or "?", " (%s)" % hint if hint else "",
                                         " : %s" % description if description else "")
        super().__init__(msg)
        self.code = code
        self.description = description


def endpoint_candidates(url: str) -> List[str]:
    """URLs to try for a user supplied Torznab URL (as-is, then with '/api' appended)."""
    url = (url or "").strip()
    if not url:
        return []
    try:
        parts = urllib.parse.urlsplit(url)
    except ValueError:
        return [url]
    path = parts.path.rstrip("/")
    if path.lower().endswith("/api"):
        return [url]
    with_api = urllib.parse.urlunsplit((parts.scheme, parts.netloc, path + "/api", parts.query, parts.fragment))
    return [url, with_api]


def parse_caps(root: ET.Element) -> Tuple[Dict[str, Set[str]], int]:
    """Return ({search mode: supported params} for available modes, number of categories)."""
    modes: Dict[str, Set[str]] = {}
    n_categories = 0
    for el in root.iter():
        name = local_name(el.tag)
        if name == "searching":
            for mode in list(el):
                mname = local_name(mode.tag)
                if not mname:
                    continue
                available = (mode.get("available") or "yes").strip().lower() in ("yes", "true", "1")
                if not available:
                    continue
                raw = mode.get("supportedParams") or mode.get("supportedparams") or "q"
                modes[mname] = {p.strip().lower() for p in raw.split(",") if p.strip()}
        elif name == "category":
            n_categories += 1
    return modes, n_categories


class TorznabIndexer(Indexer):
    type = "torznab"

    def __init__(self, row: Dict[str, Any], http: HttpClient):
        super().__init__(row, http)
        self._endpoint: Optional[str] = None
        self._caps: Optional[Dict[str, Set[str]]] = None
        self._n_categories = 0

    # ------------------------------------------------------------ transport
    def endpoints(self) -> List[str]:
        return [self._endpoint] if self._endpoint else endpoint_candidates(self.url)

    def _get_xml(self, params: List[Tuple[str, Any]], expect: str) -> ET.Element:
        """GET the API with *params*; return the XML root. expect: 'caps' or 'feed'."""
        full = list(params)
        if self.api_key:
            full.append(("apikey", self.api_key))
        last_exc: Optional[Exception] = None
        for endpoint in self.endpoints():
            try:
                resp = self.http.get(endpoint, params=full, raise_for_status=False)
            except HttpError as exc:
                last_exc = exc
                continue
            if not resp.ok:
                hint = " (clé API incorrecte ?)" if resp.status in (401, 403) else ""
                # some servers send a Torznab <error/> with a 4xx status
                try:
                    err = xml_error(parse_xml(resp.body))
                except ValueError:
                    err = None
                if err:
                    last_exc = TorznabError(*err)
                else:
                    last_exc = ValueError("HTTP %s%s" % (resp.status, hint))
                continue
            try:
                root = parse_xml(resp.body)
            except ValueError as exc:
                last_exc = exc
                continue
            err = xml_error(root)
            if err:
                # this endpoint *is* a Torznab API: remember it and report the error
                self._endpoint = endpoint
                raise TorznabError(*err)
            ok = local_name(root.tag) == "caps" if expect == "caps" else is_feed(root)
            if ok:
                self._endpoint = endpoint
                return root
            last_exc = ValueError("réponse inattendue (élément racine <%s>)" % local_name(root.tag))
        if last_exc is None:
            raise ValueError("aucune URL configurée")
        raise last_exc

    def caps(self) -> Dict[str, Set[str]]:
        """Available search modes and their parameters (cached after the first success)."""
        if self._caps is None:
            root = self._get_xml([("t", "caps")], expect="caps")
            self._caps, self._n_categories = parse_caps(root)
        return self._caps

    # --------------------------------------------------------------- search
    def _attempts(self, request: SearchRequest, caps: Optional[Dict[str, Set[str]]]) -> List[List[Tuple[str, Any]]]:
        attempts: List[List[Tuple[str, Any]]] = []
        title = request.title
        if request.kind == "movie":
            mode = caps.get("movie-search") if caps is not None else {"q"}
            if mode is not None:
                imdb = (request.imdb or "").strip().lower()
                if caps is not None and imdb and "imdbid" in mode:
                    digits = imdb[2:] if imdb.startswith("tt") else imdb
                    if digits.isdigit():
                        attempts.append([("t", "movie"), ("imdbid", digits)])
                if "q" in mode:
                    attempts.append([("t", "movie"), ("q", request.query_string())])
        else:
            mode = caps.get("tv-search") if caps is not None else {"q", "season", "ep"}
            if mode is not None and request.season is None:
                # discovery of every release of a show (e.g. all season packs): q only
                if "q" in mode:
                    attempts.append([("t", "tvsearch"), ("q", title)])
            elif mode is not None:
                ep_params: List[Tuple[str, Any]] = []
                if "season" in mode:
                    ep_params.append(("season", request.season))
                    if request.kind == "episode" and request.episode is not None and "ep" in mode:
                        ep_params.append(("ep", request.episode))
                tvdb = (request.tvdb or "").strip()
                if caps is not None and tvdb.isdigit() and "tvdbid" in mode and ep_params:
                    attempts.append([("t", "tvsearch"), ("tvdbid", tvdb)] + ep_params)
                if "q" in mode:
                    if ep_params and len(ep_params) == (2 if request.kind == "episode" else 1):
                        attempts.append([("t", "tvsearch"), ("q", title)] + ep_params)
                    else:
                        attempts.append([("t", "tvsearch"), ("q", request.query_string())])
        if caps is None or "search" in caps or not attempts:
            attempts.append([("t", "search"), ("q", request.query_string())])
        return attempts

    def _search(self, request: SearchRequest) -> List[Release]:
        caps: Optional[Dict[str, Set[str]]]
        try:
            caps = self.caps()
        except TorznabError as exc:
            if exc.code in ("100", "101", "102"):
                raise
            caps = None
        except Exception as exc:
            log.debug("Torznab %s : capacités indisponibles (%s), recherche simple", self.name, self._safe(exc))
            caps = None
        cats = categories_for(request, self.categories)
        last_exc: Optional[Exception] = None
        failures = 0
        attempts = self._attempts(request, caps)
        for params in attempts:
            full = params + [("cat", ",".join(str(c) for c in cats)), ("extended", "1"), ("limit", "100")]
            try:
                root = self._get_xml(full, expect="feed")
            except TorznabError as exc:
                if exc.code in ("100", "101", "102"):
                    raise
                last_exc = exc
                failures += 1
                continue
            except (HttpError, ValueError) as exc:
                last_exc = exc
                failures += 1
                continue
            base = self._endpoint or self.url
            releases = parse_feed(root, self.name, base)
            if releases:
                log.info("Indexer « %s » : %d résultat(s) pour « %s »", self.name, len(releases), request.describe())
                return releases
        if failures == len(attempts) and last_exc is not None:
            raise last_exc
        return []

    # ----------------------------------------------------------------- test
    def _test(self) -> Tuple[bool, str]:
        self._caps = None
        self._endpoint = None
        try:
            modes = self.caps()
        except TorznabError as exc:
            return False, "« %s » : %s" % (self.name, self._safe(exc))
        except Exception as exc:
            return False, "« %s » ne répond pas comme une API Torznab : %s" % (self.name, self._safe(exc))
        features = []
        if "movie-search" in modes:
            features.append("films")
        if "tv-search" in modes:
            features.append("séries")
        if "search" in modes:
            features.append("recherche libre")
        return True, "Connexion Torznab réussie (%d catégories ; recherches : %s)" % (
            self._n_categories, ", ".join(features) or "aucune annoncée")
