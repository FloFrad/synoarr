"""Minimal bencode encoder/decoder (BitTorrent metainfo files) + magnet parsing.

    >>> name, info_hash = torrent_info(open("file.torrent", "rb").read())
    >>> magnet_info("magnet:?xt=urn:btih:...&dn=Name")
    ('Name', '...')

Byte strings are returned as ``bytes`` by :func:`bdecode` (keys included) because
torrent files mix text and binary data (``pieces``).
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import re
import urllib.parse
from typing import Any, Dict, List, Optional, Tuple

__all__ = ["bdecode", "bencode", "torrent_info", "magnet_info", "BencodeError"]

_MAX_DEPTH = 200


class BencodeError(ValueError):
    """Raised when data is not valid bencode."""


# ------------------------------------------------------------------ decoding
def _decode(data: bytes, i: int, depth: int) -> Tuple[Any, int]:
    """Decode the value starting at offset *i*; return (value, offset after the value)."""
    if depth > _MAX_DEPTH:
        raise BencodeError("bencode: imbrication trop profonde")
    if i >= len(data):
        raise BencodeError("bencode: fin de données inattendue (offset %d)" % i)
    c = data[i:i + 1]
    if c == b"i":
        end = data.find(b"e", i + 1)
        if end == -1:
            raise BencodeError("bencode: entier non terminé (offset %d)" % i)
        raw = data[i + 1:end]
        if not re.fullmatch(rb"-?\d+", raw):
            raise BencodeError("bencode: entier invalide %r (offset %d)" % (raw[:20], i))
        return int(raw), end + 1
    if c == b"l":
        i += 1
        out: List[Any] = []
        while True:
            if i >= len(data):
                raise BencodeError("bencode: liste non terminée")
            if data[i:i + 1] == b"e":
                return out, i + 1
            val, i = _decode(data, i, depth + 1)
            out.append(val)
    if c == b"d":
        i += 1
        d: Dict[bytes, Any] = {}
        while True:
            if i >= len(data):
                raise BencodeError("bencode: dictionnaire non terminé")
            if data[i:i + 1] == b"e":
                return d, i + 1
            key, i = _decode(data, i, depth + 1)
            if not isinstance(key, bytes):
                raise BencodeError("bencode: clé de dictionnaire non textuelle (offset %d)" % i)
            val, i = _decode(data, i, depth + 1)
            d[key] = val
    if c.isdigit():
        colon = data.find(b":", i)
        if colon == -1 or colon - i > 20:
            raise BencodeError("bencode: longueur de chaîne invalide (offset %d)" % i)
        raw_len = data[i:colon]
        if not raw_len.isdigit():
            raise BencodeError("bencode: longueur de chaîne invalide %r (offset %d)" % (raw_len, i))
        length = int(raw_len)
        start = colon + 1
        end = start + length
        if end > len(data):
            raise BencodeError("bencode: chaîne tronquée (offset %d)" % i)
        return data[start:end], end
    raise BencodeError("bencode: octet inattendu %r (offset %d)" % (c, i))


def _to_bytes(data: Any) -> bytes:
    if isinstance(data, (bytes, bytearray, memoryview)):
        return bytes(data)
    if isinstance(data, str):
        return data.encode("utf-8")
    raise BencodeError("bencode: type de données non supporté %s" % type(data).__name__)


def bdecode(data: bytes) -> Any:
    """Decode a complete bencoded value. Trailing whitespace is tolerated, other trailing data is not."""
    data = _to_bytes(data)
    try:
        value, end = _decode(data, 0, 0)
    except RecursionError:
        raise BencodeError("bencode: imbrication trop profonde")
    if data[end:].strip():
        raise BencodeError("bencode: données en trop après la valeur (offset %d)" % end)
    return value


# ------------------------------------------------------------------ encoding
def bencode(value: Any) -> bytes:
    """Encode *value* (int, bytes, str, list/tuple, dict with bytes/str keys). Dict keys are sorted."""
    out: List[bytes] = []
    _encode(value, out, 0)
    return b"".join(out)


def _encode(value: Any, out: List[bytes], depth: int) -> None:
    if depth > _MAX_DEPTH:
        raise BencodeError("bencode: imbrication trop profonde")
    if isinstance(value, bool):
        out.append(b"i%de" % int(value))
    elif isinstance(value, int):
        out.append(b"i%de" % value)
    elif isinstance(value, (bytes, bytearray, memoryview)):
        b = bytes(value)
        out.append(b"%d:" % len(b))
        out.append(b)
    elif isinstance(value, str):
        b = value.encode("utf-8")
        out.append(b"%d:" % len(b))
        out.append(b)
    elif isinstance(value, (list, tuple)):
        out.append(b"l")
        for item in value:
            _encode(item, out, depth + 1)
        out.append(b"e")
    elif isinstance(value, dict):
        items = []
        for k, v in value.items():
            kb = k.encode("utf-8") if isinstance(k, str) else k
            if not isinstance(kb, (bytes, bytearray)):
                raise BencodeError("bencode: clé de dictionnaire invalide %r" % (k,))
            items.append((bytes(kb), v))
        items.sort(key=lambda kv: kv[0])
        out.append(b"d")
        for kb, v in items:
            out.append(b"%d:" % len(kb))
            out.append(kb)
            _encode(v, out, depth + 1)
        out.append(b"e")
    else:
        raise BencodeError("bencode: type non encodable %s" % type(value).__name__)


# ------------------------------------------------------------------ torrents
def _decode_text(raw: Any) -> str:
    if isinstance(raw, bytes):
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError:
            return raw.decode("latin-1")
    if isinstance(raw, str):
        return raw
    return ""


def torrent_info(data: bytes) -> Tuple[str, str]:
    """Return ``(name, info_hash_hex)`` of a .torrent file.

    The info hash is the SHA-1 of the *original* bytes of the ``info`` dictionary
    (not of a re-encoding, which could differ for non canonical files).
    Raises ValueError when *data* is not a valid torrent (e.g. an HTML error page).
    """
    try:
        data = _to_bytes(data)
    except BencodeError:
        raise ValueError("Données de torrent invalides (type inattendu)")
    if not data or data[:1] != b"d":
        snippet = data[:60].decode("utf-8", "replace") if data else ""
        raise ValueError("Ce n'est pas un fichier .torrent valide (début: %r)" % snippet)
    try:
        i = 1
        info_span = None
        info_value = None
        while True:
            if i >= len(data):
                raise BencodeError("bencode: dictionnaire non terminé")
            if data[i:i + 1] == b"e":
                break
            key, i = _decode(data, i, 1)
            if not isinstance(key, bytes):
                raise BencodeError("bencode: clé non textuelle")
            start = i
            val, i = _decode(data, i, 1)
            if key == b"info":
                info_span = (start, i)
                info_value = val
    except (BencodeError, RecursionError) as exc:
        raise ValueError("Fichier .torrent invalide : %s" % exc)
    if info_span is None or not isinstance(info_value, dict):
        raise ValueError("Fichier .torrent invalide : dictionnaire 'info' absent")
    info_hash = hashlib.sha1(data[info_span[0]:info_span[1]]).hexdigest()
    raw_name = info_value.get(b"name.utf-8", info_value.get(b"name", b""))
    return _decode_text(raw_name), info_hash


# ------------------------------------------------------------------ magnets
def _normalize_btih(value: str) -> Optional[str]:
    v = value.strip()
    if re.match(r"^[0-9a-fA-F]{40}$", v):
        return v.lower()
    if re.match(r"^[A-Za-z2-7]{32}$", v):
        try:
            return binascii.hexlify(base64.b32decode(v.upper())).decode("ascii")
        except (binascii.Error, ValueError):
            return None
    return None


def magnet_info(magnet: str) -> Tuple[Optional[str], Optional[str]]:
    """Return ``(display_name, info_hash_hex_lowercase)`` from a magnet URI; (None, None) if not a magnet."""
    if not isinstance(magnet, str):
        return None, None
    m = magnet.strip()
    if not m.lower().startswith("magnet:"):
        return None, None
    query = m.split("?", 1)[1] if "?" in m else ""
    try:
        pairs = urllib.parse.parse_qsl(query, keep_blank_values=True)
    except ValueError:
        return None, None
    name: Optional[str] = None
    info_hash: Optional[str] = None
    for k, v in pairs:
        key = k.lower()
        if key == "dn" and name is None and v.strip():
            name = v.strip()
        elif (key == "xt" or key.startswith("xt.")) and info_hash is None:
            if v.lower().startswith("urn:btih:"):
                info_hash = _normalize_btih(v[9:])
    return name, info_hash


def add_trackers(magnet: str, trackers: Any, minimum: int = 2) -> str:
    """Append *trackers* to a magnet link that has fewer than *minimum* trackers (no duplicates).
    A tracker-less magnet can only be resolved through DHT."""
    import urllib.parse
    if not magnet or not magnet.lower().startswith("magnet:?") or not trackers:
        return magnet
    query = magnet.split("?", 1)[1]
    existing = []
    for part in query.split("&"):
        k, _, v = part.partition("=")
        if k.lower() == "tr" and v:
            existing.append(urllib.parse.unquote_plus(v).strip().lower())
    if len(existing) >= minimum:
        return magnet
    extra = []
    for t in trackers:
        t = str(t).strip()
        if t and t.lower() not in existing:
            existing.append(t.lower())
            extra.append("tr=" + urllib.parse.quote(t, safe=""))
    if not extra:
        return magnet
    return magnet + ("&" if not magnet.endswith("&") else "") + "&".join(extra)


def normalize_magnet(magnet: str) -> Tuple[str, Optional[str]]:
    """Repair a magnet whose encoding was damaged on the way (HTML entities "&amp;", double percent-encoding
    "%2520" / "udp%253A%252F%252F"...). Returns (magnet, reason) - reason None when nothing had to be fixed.
    The content (hash, name, trackers) is never changed, only its encoding."""
    import html
    import urllib.parse
    if not isinstance(magnet, str) or not magnet.lower().startswith("magnet:?"):
        return magnet, None
    original = magnet.strip()
    reasons = []
    m = original
    if "&amp;" in m.lower():
        m = html.unescape(m)
        reasons.append("entités HTML (&amp;)")
    query = m.split("?", 1)[1]
    pairs = []
    for part in query.split("&"):
        if not part:
            continue
        k, _, v = part.partition("=")
        val = urllib.parse.unquote_plus(v) if k.lower() != "xt" else urllib.parse.unquote(v)
        # double encoding: after one decoding the value still looks percent-encoded (udp%3A%2F%2F..., %20...)
        rounds = 0
        while re.search(r"%[0-9A-Fa-f]{2}", val) and rounds < 3 and (
                k.lower() == "tr" and re.match(r"^[a-z]+%3a%2f%2f", val, re.I) or
                k.lower() == "dn" and re.search(r"%(20|28|29|5b|5d|2c|27|26)", val, re.I)):
            val = urllib.parse.unquote(val)
            rounds += 1
        if rounds:
            reasons.append("double encodage (%s)" % k)
        pairs.append((k, val))
    if not reasons:
        return original, None
    out = []
    for k, v in pairs:
        if k.lower() == "xt":
            out.append("%s=%s" % (k, v))
        else:
            out.append("%s=%s" % (k, urllib.parse.quote(v, safe="")))
    fixed = "magnet:?" + "&".join(out)
    return fixed, ", ".join(sorted(set(reasons)))
