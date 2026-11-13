"""Generic RSS / Atom torrent feed.

The feed is fetched as-is (any passkey must be part of the URL) and only items
whose parsed title matches the request are returned; quality.evaluate() does the rest.
"""
from __future__ import annotations

import logging
from typing import List, Tuple

from ..models import Release, SearchRequest
from ..parser import titles_match
from ..quality import parse_for_request
from .base import Indexer, feed_items, is_feed, local_name, parse_feed, parse_xml

log = logging.getLogger(__name__)


class RssIndexer(Indexer):
    type = "rss"

    def _fetch(self) -> List[Release]:
        if not self.url:
            raise ValueError("aucune URL configurée")
        resp = self.http.get(self.url, raise_for_status=False)
        if not resp.ok:
            raise ValueError("HTTP %s" % resp.status)
        root = parse_xml(resp.body)
        if not is_feed(root) and not feed_items(root):
            raise ValueError("ce n'est pas un flux RSS/Atom (élément racine <%s>)" % local_name(root.tag))
        return parse_feed(root, self.name, resp.url or self.url)

    def _search(self, request: SearchRequest) -> List[Release]:
        out = []
        for rel in self._fetch():
            try:
                parsed = parse_for_request(rel.title, request)
            except Exception:  # pragma: no cover - parser is defensive already
                continue
            if titles_match(parsed.title, request.title, request.alt_titles):
                out.append(rel)
        return out

    def _test(self) -> Tuple[bool, str]:
        try:
            releases = self._fetch()
        except Exception as exc:
            return False, "Flux « %s » illisible : %s" % (self.name, self._safe(exc))
        return True, "Flux RSS/Atom valide (%d élément(s) téléchargeable(s))" % len(releases)
