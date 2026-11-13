"""Indexer base class and shared feed (RSS / Atom / Torznab) parsing helpers."""
from __future__ import annotations

import logging
import re
import urllib.parse
import xml.etree.ElementTree as ET
from typing import Any, Dict, List, Optional, Tuple

from ..bencode import magnet_info
from ..httpclient import HttpClient, redact
from ..models import Release, SearchRequest

log = logging.getLogger(__name__)

MOVIE_CATEGORIES = [2000]
TV_CATEGORIES = [5000]


# ------------------------------------------------------------------ helpers
def local_name(tag: Any) -> str:
    """'{http://ns}attr' -> 'attr' (lower-cased). Comments / PIs return ''."""
    if not isinstance(tag, str):
        return ""
    return tag.rsplit("}", 1)[-1].split(":")[-1].lower()


def to_int(value: Any) -> Optional[int]:
    if value is None:
        return None
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    s = str(value).strip().replace(",", "").replace(" ", "")
    if not s:
        return None
    try:
        return int(s)
    except ValueError:
        try:
            return int(float(s))
        except ValueError:
            return None


_SIZE_UNITS = {
    "b": 1, "o": 1, "": 1,
    "kb": 1000, "ko": 1000, "kib": 1024, "k": 1024,
    "mb": 1000 ** 2, "mo": 1000 ** 2, "mib": 1024 ** 2, "m": 1024 ** 2,
    "gb": 1000 ** 3, "go": 1000 ** 3, "gib": 1024 ** 3, "g": 1024 ** 3,
    "tb": 1000 ** 4, "to": 1000 ** 4, "tib": 1024 ** 4, "t": 1024 ** 4,
}


def parse_size(value: Any) -> int:
    """'1234' -> 1234, '1.5 GiB' -> 1610612736, '700 MB' -> 700000000. Unknown -> 0."""
    if value is None:
        return 0
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return max(0, int(value))
    s = str(value).strip().lower()
    if not s:
        return 0
    m = re.match(r"^([0-9]+(?:[.,][0-9]+)?)\s*([a-z]*)$", s)
    if not m:
        return 0
    num = float(m.group(1).replace(",", "."))
    unit = _SIZE_UNITS.get(m.group(2))
    if unit is None:
        return 0
    return max(0, int(num * unit))


def parse_categories(value: Any) -> List[int]:
    """'2000, 2040;5000' -> [2000, 2040, 5000]."""
    if not value:
        return []
    if isinstance(value, (list, tuple)):
        items = [str(v) for v in value]
    else:
        items = re.split(r"[,;\s]+", str(value))
    out: List[int] = []
    for it in items:
        n = to_int(it)
        if n is not None and n > 0 and n not in out:
            out.append(n)
    return out


def categories_for(request: SearchRequest, configured: List[int]) -> List[int]:
    """Categories to send: configured ones matching the request kind (movies 2xxx, TV 5xxx), else defaults."""
    is_movie = request.kind == "movie"
    if configured:
        lo, hi = (2000, 2999) if is_movie else (5000, 5999)
        matching = [c for c in configured if lo <= c <= hi]
        if matching:
            return matching
        # custom (e.g. >= 100000 tracker specific) categories: send them all
        if all(c >= 100000 for c in configured):
            return list(configured)
    return list(MOVIE_CATEGORIES if is_movie else TV_CATEGORIES)


def parse_xml(body: bytes) -> ET.Element:
    """Parse an XML document defensively; ValueError (French message) on HTML pages / garbage."""
    if body is None:
        raise ValueError("réponse vide")
    data = bytes(body)
    if data.startswith(b"\xef\xbb\xbf"):
        data = data[3:]
    data = data.lstrip()
    if not data:
        raise ValueError("réponse vide")
    head = data[:1024].lower()
    if not data.startswith(b"<"):
        if head.startswith(b"{") or head.startswith(b"["):
            raise ValueError("réponse JSON reçue au lieu d'un flux XML")
        raise ValueError("réponse non XML (début : %r)" % data[:80].decode("utf-8", "replace"))
    if head.startswith(b"<!doctype html") or b"<html" in head:
        raise ValueError("page HTML reçue au lieu d'un flux XML (mauvaise adresse ou page de connexion ?)")
    if b"<!entity" in data[:8192].lower():
        raise ValueError("document XML refusé (déclarations d'entités)")
    try:
        return ET.fromstring(data)
    except ET.ParseError as exc:
        raise ValueError("XML invalide : %s" % exc)
    except Exception as exc:  # pragma: no cover - defensive (expat oddities)
        raise ValueError("XML illisible : %s" % exc)


def feed_items(root: ET.Element) -> List[ET.Element]:
    """All RSS <item> / Atom <entry> elements of a parsed feed."""
    return [el for el in root.iter() if local_name(el.tag) in ("item", "entry")]


def is_feed(root: ET.Element) -> bool:
    name = local_name(root.tag)
    if name in ("rss", "feed", "rdf"):
        return True
    return any(local_name(el.tag) == "channel" for el in root)


def xml_error(root: ET.Element) -> Optional[Tuple[str, str]]:
    """Torznab/Newznab ``<error code=".." description=".."/>`` -> (code, description)."""
    if local_name(root.tag) == "error":
        return (root.get("code") or "").strip(), (root.get("description") or root.text or "").strip()
    return None


def _text(el: ET.Element) -> str:
    return "".join(el.itertext()).strip()


def _is_http(url: Optional[str]) -> bool:
    return bool(url) and str(url).lower().startswith(("http://", "https://"))


def _is_magnet(url: Optional[str]) -> bool:
    return bool(url) and str(url).lower().startswith("magnet:")


def item_to_release(item: ET.Element, indexer_name: str, base_url: str = "") -> Optional[Release]:
    """Convert an RSS item / Atom entry (with optional torznab/newznab/ezrss/nyaa extensions) to a Release."""
    title = ""
    link = None  # RSS <link>text
    atom_links: List[Tuple[str, str, str, str]] = []  # (href, rel, type, length)
    enclosure_url = None
    enclosure_len = None
    size = 0
    seeders = None
    leechers = None
    peers = None
    info_hash = None
    magnet = None
    pub_date = None
    comments = None
    guid = None
    categories: List[int] = []

    for child in list(item):
        name = local_name(child.tag)
        if not name:
            continue
        if name == "title":
            title = _text(child)
        elif name == "link":
            href = child.get("href")
            if href:
                atom_links.append((href.strip(), (child.get("rel") or "alternate").lower(),
                                   (child.get("type") or "").lower(), child.get("length") or ""))
            else:
                link = _text(child) or None
        elif name == "enclosure":
            enclosure_url = (child.get("url") or "").strip() or enclosure_url
            enclosure_len = child.get("length") or enclosure_len
        elif name == "attr":
            aname = (child.get("name") or "").strip().lower()
            aval = (child.get("value") or "").strip()
            if aname == "size":
                size = parse_size(aval) or size
            elif aname in ("seeders", "seeds"):
                seeders = to_int(aval)
            elif aname == "leechers":
                leechers = to_int(aval)
            elif aname == "peers":
                peers = to_int(aval)
            elif aname == "infohash":
                info_hash = aval.lower() or info_hash
            elif aname in ("magneturl", "magneturi"):
                magnet = aval or magnet
            elif aname == "category":
                c = to_int(aval)
                if c is not None and c not in categories:
                    categories.append(c)
        elif name in ("size", "contentlength"):
            size = parse_size(_text(child)) or size
        elif name in ("seeders", "seeds"):
            seeders = to_int(_text(child))
        elif name == "leechers":
            leechers = to_int(_text(child))
        elif name == "peers":
            peers = to_int(_text(child))
        elif name == "infohash":
            info_hash = _text(child).lower() or info_hash
        elif name in ("magneturi", "magneturl", "magnet"):
            magnet = _text(child) or magnet
        elif name in ("pubdate", "published", "updated", "date"):
            pub_date = pub_date or (_text(child) or None)
        elif name == "comments":
            comments = _text(child) or None
        elif name in ("guid", "id"):
            guid = _text(child) or None
        elif name == "category":
            c = to_int(_text(child) or child.get("term"))
            if c is not None and c not in categories:
                categories.append(c)

    title = re.sub(r"\s+", " ", title).strip()
    if not title:
        return None

    def absolute(url: Optional[str]) -> Optional[str]:
        if not url:
            return None
        url = url.strip()
        if _is_magnet(url) or _is_http(url):
            return url
        if base_url and (url.startswith("/") or not re.match(r"^[a-z][a-z0-9+.-]*:", url, re.I)):
            try:
                return urllib.parse.urljoin(base_url, url)
            except ValueError:
                return None
        return None

    # candidate download links, best first
    candidates: List[Optional[str]] = [enclosure_url]
    for href, rel, typ, length in atom_links:
        if rel == "enclosure" or "bittorrent" in typ or href.lower().startswith("magnet:"):
            candidates.append(href)
            if length and not enclosure_len:
                enclosure_len = length
    candidates.append(link)
    for href, rel, typ, _length in atom_links:
        if rel not in ("enclosure",):
            candidates.append(href)
    if guid and (_is_magnet(guid) or (_is_http(guid) and guid.lower().split("?")[0].endswith(".torrent"))):
        candidates.append(guid)

    download_url = None
    for cand in candidates:
        url = absolute(cand)
        if not url:
            continue
        if _is_magnet(url):
            magnet = magnet or url
        elif download_url is None:
            download_url = url
    if magnet and not _is_magnet(magnet):
        magnet = None
    if not download_url and not magnet:
        return None

    if not size:
        size = parse_size(enclosure_len)
    if info_hash and not re.match(r"^[0-9a-f]{40}$", info_hash):
        info_hash = None
    if not info_hash and magnet:
        info_hash = magnet_info(magnet)[1]
    if leechers is None and peers is not None and seeders is not None:
        leechers = max(0, peers - seeders)

    info_url = comments if _is_http(comments) else None
    if info_url is None:
        alternates = [href for href, rel, _t, _l in atom_links if rel == "alternate"]
        for cand in [link] + alternates + [guid]:
            if _is_http(cand) and cand != download_url:
                info_url = cand
                break

    return Release(
        title=title,
        indexer=indexer_name,
        download_url=download_url,
        magnet=magnet,
        size=size,
        seeders=seeders,
        leechers=leechers,
        info_hash=info_hash,
        pub_date=pub_date,
        info_url=info_url,
        categories=categories,
    )


def parse_feed(root: ET.Element, indexer_name: str, base_url: str = "") -> List[Release]:
    out: List[Release] = []
    for item in feed_items(root):
        try:
            rel = item_to_release(item, indexer_name, base_url)
        except Exception as exc:  # one broken item must not lose the others
            log.debug("Élément de flux ignoré (%s): %s", indexer_name, exc)
            continue
        if rel is not None:
            out.append(rel)
    return out


# ------------------------------------------------------------------ base class
class Indexer:
    """Base class. Subclasses implement ``_search`` and ``_test``."""

    type = "base"

    def __init__(self, row: Dict[str, Any], http: HttpClient):
        row = dict(row or {})
        self.row = row
        self.http = http
        self.id = row.get("id")
        self.url = str(row.get("url") or "").strip()
        self.name = str(row.get("name") or "").strip() or self.type
        self.api_key = str(row.get("api_key") or "").strip()
        self.categories = parse_categories(row.get("categories"))
        self.priority = to_int(row.get("priority"))
        if self.priority is None:
            self.priority = 25
        enabled = row.get("enabled", 1)
        self.enabled = bool(to_int(enabled)) if not isinstance(enabled, bool) else enabled

    def __repr__(self) -> str:
        return "<%s %r %s>" % (type(self).__name__, self.name, redact(self.url))

    def _safe(self, message: Any) -> str:
        """Message without secrets (api key, redacted URLs)."""
        text = str(message)
        if self.api_key and len(self.api_key) >= 4:
            text = text.replace(self.api_key, "***")
        return text

    # -------------------------------------------------------------- public
    def search(self, request: SearchRequest) -> List[Release]:
        """Search releases. Never raises: logs a warning and returns [] on any error."""
        try:
            results = self._search(request)
            return [r for r in (results or []) if isinstance(r, Release)]
        except Exception as exc:
            log.warning("Indexer « %s » (%s) : recherche « %s » impossible : %s", self.name,
                        redact(self.url), request.describe(), self._safe(exc))
            return []

    def test(self) -> Tuple[bool, str]:
        """Check that the indexer answers. Returns (ok, message in French). Never raises."""
        try:
            return self._test()
        except Exception as exc:
            return False, "Échec du test de « %s » : %s" % (self.name, self._safe(exc))

    # --------------------------------------------------------- subclasses
    def _search(self, request: SearchRequest) -> List[Release]:  # pragma: no cover - abstract
        raise NotImplementedError

    def _test(self) -> Tuple[bool, str]:  # pragma: no cover - abstract
        raise NotImplementedError
