"""Indexers: Torznab, generic RSS/Atom feeds and Internet Archive.

    idx = create_indexer(db_row, http)
    releases = idx.search(SearchRequest(kind="movie", title="Nosferatu", year=1922))
"""
from __future__ import annotations

import logging
import urllib.parse
from typing import Any, Dict, Optional

from ..httpclient import HttpClient, HttpError, redact
from .base import Indexer, feed_items, is_feed, local_name, parse_xml, xml_error
from .catalog import CATALOG, get_catalog_entry
from .internet_archive import InternetArchiveIndexer
from .rss import RssIndexer
from .torznab import TorznabIndexer, endpoint_candidates

log = logging.getLogger(__name__)

__all__ = [
    "Indexer", "TorznabIndexer", "RssIndexer", "InternetArchiveIndexer",
    "CATALOG", "get_catalog_entry", "create_indexer", "detect_type", "INDEXER_TYPES",
]

INDEXER_TYPES = {
    "torznab": TorznabIndexer,
    "rss": RssIndexer,
    "internet_archive": InternetArchiveIndexer,
}


def create_indexer(row: Dict[str, Any], http: HttpClient) -> Indexer:
    """Build the Indexer for a row of the ``indexers`` table. ValueError on unknown type."""
    type_ = str((row or {}).get("type") or "").strip().lower()
    klass = INDEXER_TYPES.get(type_)
    if klass is None:
        raise ValueError("Type d'indexer inconnu : %r (attendu : %s)" % (type_, ", ".join(sorted(INDEXER_TYPES))))
    return klass(row, http)


def _is_archive_org(url: str) -> bool:
    try:
        host = (urllib.parse.urlsplit(url if "://" in url else "https://" + url).hostname or "").lower()
    except ValueError:
        return False
    return host == "archive.org" or host.endswith(".archive.org")


def detect_type(url: str, api_key: str, http: HttpClient) -> str:
    """Guess the indexer type of *url*: "internet_archive", "torznab" or "rss".

    Raises ValueError (French message) when the URL is none of them.
    """
    url = (url or "").strip()
    if not url:
        raise ValueError("Adresse de l'indexer vide.")
    if not url.lower().startswith(("http://", "https://")):
        if _is_archive_org(url):
            return "internet_archive"
        raise ValueError("L'adresse doit commencer par http:// ou https:// (reçu : %s)" % redact(url))
    if _is_archive_org(url):
        return "internet_archive"

    problems = []
    last_error: Optional[str] = None
    # 1. Torznab capabilities
    for endpoint in endpoint_candidates(url):
        params = [("t", "caps")]
        if api_key:
            params.append(("apikey", api_key))
        try:
            resp = http.get(endpoint, params=params, raise_for_status=False)
        except HttpError as exc:
            last_error = str(exc)
            continue
        try:
            root = parse_xml(resp.body)
        except ValueError:
            if not resp.ok:
                problems.append("HTTP %s" % resp.status)
            continue
        if local_name(root.tag) == "caps" and resp.ok:
            return "torznab"
        if xml_error(root) is not None:
            # a Torznab/Newznab API answering with an error (usually a wrong API key)
            return "torznab"

    # 2. plain RSS / Atom feed
    try:
        resp = http.get(url, raise_for_status=False)
        if resp.ok:
            root = parse_xml(resp.body)
            if is_feed(root) or feed_items(root):
                return "rss"
            problems.append("XML sans flux RSS/Atom")
        else:
            problems.append("HTTP %s" % resp.status)
    except HttpError as exc:
        last_error = str(exc)
    except ValueError as exc:
        problems.append(str(exc))

    detail = last_error or ", ".join(dict.fromkeys(problems)) or "réponse non reconnue"
    if api_key:
        detail = detail.replace(api_key, "***")
    raise ValueError("Impossible de reconnaître l'indexer %s : ni API Torznab, ni flux RSS/Atom, "
                     "ni Internet Archive (%s)." % (redact(url), detail))
