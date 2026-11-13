"""Internet Archive (archive.org): public domain films and series, downloaded via the
torrent archive.org generates for every item (served by web seeds, so seeders are unknown).
"""
from __future__ import annotations

import logging
import re
import urllib.parse
from typing import Any, Dict, List, Optional, Tuple

from ..httpclient import HttpClient
from ..models import Release, SearchRequest
from ..parser import titles_match
from ..quality import parse_for_request
from .base import Indexer, to_int

log = logging.getLogger(__name__)

DEFAULT_URL = "https://archive.org"
ROWS = 50

# Lucene special characters that would break the advancedsearch query
_LUCENE_SPECIAL = re.compile(r'[+\-!(){}\[\]^"~*?:\\/&|]')


def _first(value: Any) -> Any:
    if isinstance(value, list):
        return value[0] if value else None
    return value


def lucene_escape(text: str) -> str:
    """Remove characters that have a meaning in Lucene queries; collapse spaces."""
    t = _LUCENE_SPECIAL.sub(" ", text or "")
    t = re.sub(r"\b(AND|OR|NOT|TO)\b", lambda m: m.group(1).lower(), t)
    return re.sub(r"\s+", " ", t).strip()


def _year_of(value: Any) -> Optional[int]:
    v = _first(value)
    if v is None:
        return None
    m = re.search(r"(1[89]\d\d|20\d\d)", str(v))
    return int(m.group(1)) if m else None


class InternetArchiveIndexer(Indexer):
    type = "internet_archive"

    def __init__(self, row: Dict[str, Any], http: HttpClient):
        super().__init__(row, http)
        base = self.url or DEFAULT_URL
        if not base.lower().startswith(("http://", "https://")):
            base = "https://" + base
        parts = urllib.parse.urlsplit(base)
        self.base_url = urllib.parse.urlunsplit((parts.scheme, parts.netloc, parts.path.rstrip("/"), "", ""))

    # --------------------------------------------------------------- urls
    def search_url(self) -> str:
        return self.base_url + "/advancedsearch.php"

    def torrent_url(self, identifier: str) -> str:
        ident = urllib.parse.quote(identifier, safe="")
        return "%s/download/%s/%s_archive.torrent" % (self.base_url, ident, ident)

    def details_url(self, identifier: str) -> str:
        return "%s/details/%s" % (self.base_url, urllib.parse.quote(identifier, safe=""))

    def build_query(self, request: SearchRequest) -> str:
        title = lucene_escape(request.title)
        return "title:(%s) AND mediatype:(movies)" % title

    def _params(self, query: str, rows: int = ROWS) -> List[Tuple[str, Any]]:
        return [
            ("q", query),
            ("fl[]", "identifier"),
            ("fl[]", "title"),
            ("fl[]", "year"),
            ("fl[]", "item_size"),
            ("fl[]", "downloads"),
            ("sort[]", "downloads desc"),
            ("rows", rows),
            ("page", 1),
            ("output", "json"),
        ]

    def _query(self, query: str, rows: int = ROWS) -> List[Dict[str, Any]]:
        resp = self.http.get(self.search_url(), params=self._params(query, rows),
                             headers={"Accept": "application/json"}, raise_for_status=False)
        if not resp.ok:
            raise ValueError("HTTP %s" % resp.status)
        data = resp.json()
        if not isinstance(data, dict) or not isinstance(data.get("response"), dict):
            raise ValueError("réponse inattendue de l'API Internet Archive")
        docs = data["response"].get("docs") or []
        return [d for d in docs if isinstance(d, dict)]

    # ------------------------------------------------------------- search
    def doc_to_release(self, doc: Dict[str, Any]) -> Optional[Release]:
        identifier = _first(doc.get("identifier"))
        title = _first(doc.get("title"))
        if not identifier or not title:
            return None
        identifier = str(identifier).strip()
        title = re.sub(r"\s+", " ", str(title)).strip()
        if not identifier or not title:
            return None
        year = _year_of(doc.get("year"))
        name = title
        if year and str(year) not in title:
            name = "%s (%s)" % (title, year)
        size = to_int(_first(doc.get("item_size"))) or 0
        return Release(
            title=name,
            indexer=self.name,
            download_url=self.torrent_url(identifier),
            magnet=None,
            size=max(0, size),
            seeders=None,
            leechers=None,
            info_hash=None,
            info_url=self.details_url(identifier),
            categories=[2000],
        )

    def _search(self, request: SearchRequest) -> List[Release]:
        if not lucene_escape(request.title):
            return []
        out: List[Release] = []
        for doc in self._query(self.build_query(request)):
            rel = self.doc_to_release(doc)
            if rel is None:
                continue
            if titles_match(parse_for_request(rel.title, request).title, request.title, request.alt_titles):
                out.append(rel)
        return out

    def _test(self) -> Tuple[bool, str]:
        try:
            docs = self._query("mediatype:(movies)", rows=1)
        except Exception as exc:
            return False, "Internet Archive ne répond pas : %s" % self._safe(exc)
        if not docs:
            return False, "Internet Archive a répondu mais sans aucun résultat"
        return True, "Connexion à Internet Archive réussie"
