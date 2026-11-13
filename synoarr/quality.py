"""Release filtering and scoring.

``evaluate()`` decides whether a release fits a request and how good it is;
``pick_best()`` returns the best acceptable release of a search.

Score layout (higher is better, each level dominates the following ones):
    resolution rank   x 100000   (first entry of cfg.resolutions = best)
    language          0 .. 48000 (order of cfg.languages, MULTI bonus)
    source            0 .. 6000  (bluray > webdl > webrip > web > hdtv > dvdrip)
    prefer_words      1500 each  (capped at 4500)
    seeders           0 .. 800   (log scale)
    size              0 .. 200   (reasonable size)
"""
from __future__ import annotations

import logging
import math
import re
import unicodedata
from typing import Callable, List, Optional, Set, Tuple

from .config import QualityConfig
from .models import ParsedName, Release, SearchRequest
from .parser import normalize_title, parse, titles_match

log = logging.getLogger(__name__)

__all__ = ["evaluate", "pick_best", "rank_releases", "parse_for_request"]

MB = 1024 * 1024
GB = 1024 * MB

RESOLUTION_WEIGHT = 100000
LANGUAGE_MAX = 40000
MULTI_BONUS = 8000
SOURCE_WEIGHT = 1000
PREFER_WORD_BONUS = 1500
PREFER_WORD_MAX = 4500
SEEDERS_MAX = 800
UNKNOWN_SEEDERS_POINTS = 150  # e.g. Internet Archive webseeds: reliable but unknown count

_SOURCE_POINTS = {"bluray": 6, "webdl": 5, "webrip": 4, "web": 3, "hdtv": 2, "dvdrip": 1}
_BAD_SOURCES = ("cam", "telesync", "telecine", "screener", "workprint")
_SOURCE_LABELS = {
    "cam": "CAM", "telesync": "TELESYNC", "telecine": "TELECINE", "screener": "SCREENER", "workprint": "WORKPRINT",
}

# Language codes each detected tag satisfies.
_LANG_SATISFIES = {
    "multi": ("multi", "fr", "en"),
    "vostfr": ("vostfr", "en"),  # original version (usually English) with French subtitles
}


def _has_word(normalized: str, word: str) -> bool:
    w = normalize_title(word)
    if not w:
        return False
    return re.search(r"(?<![a-z0-9])%s(?![a-z0-9])" % re.escape(w), normalized) is not None


def _tail_after_title(release_title: str, parsed_title: str) -> str:
    """Normalized release name without the leading title (avoid rejecting a movie called "Cam")."""
    norm = normalize_title(release_title)
    pt = normalize_title(parsed_title)
    if pt and norm.startswith(pt):
        return norm[len(pt):].strip()
    return norm


_PLACEHOLDER = "Zqxw"
_RE_LEADING_TAGS = re.compile(r"^\s*(?:\[[^\]]*\]\s*)+")


def _strip_title_prefix(release_title: str, wanted: str) -> Optional[str]:
    """If *release_title* starts with the *wanted* title (ignoring case, accents, punctuation), return
    the remainder, starting at the separator that follows the title. None otherwise."""
    wn = normalize_title(wanted)
    if not wn:
        return None
    raw = _RE_LEADING_TAGS.sub("", release_title)
    for i in range(1, len(raw) + 1):
        if i < len(raw):
            ch = raw[i]
            # the title must end on a separator (apostrophes belong to words: "Grey's")
            if ch.isalnum() or ch in "'’`" or unicodedata.combining(ch):
                continue
        prefix = normalize_title(raw[:i])
        if prefix == wn:
            return raw[i:]
        if len(prefix) > len(wn):
            return None
    return None


def parse_for_request(title: str, request: SearchRequest) -> ParsedName:
    """parse() the release title, knowing which title we look for.

    The generic parser cuts titles on words such as "French" ("The French Dispatch") and takes a
    number in the title for a year ("Blade Runner 2049", "1917"). When the release name starts with the
    wanted title (or an alternative title), the rest of the name is parsed on its own instead.
    """
    for wanted in [request.title] + list(request.alt_titles or []):
        rest = _strip_title_prefix(title, wanted or "")
        if rest is None:
            continue
        q = parse(_PLACEHOLDER + rest)
        if normalize_title(q.title) != _PLACEHOLDER.lower():
            continue  # something else follows the wanted title ("Dune Part Two", "The Office UK")
        q.raw = title
        q.title = wanted
        return q
    return parse(title)


def _language_points(langs: List[str], wanted: List[str]) -> Tuple[Optional[int], str]:
    """Return (points, reason). points None = rejected."""
    wanted = [w.lower() for w in wanted if w]
    if not wanted:
        return 0, ""
    detected = [lg.lower() for lg in langs] or ["en"]  # no tag = English / original version
    satisfied: Set[str] = set()
    for lg in detected:
        for code in _LANG_SATISFIES.get(lg, (lg,)):
            if code in wanted:
                satisfied.add(code)
    if not satisfied:
        if langs:
            return None, "langue %s non acceptée (souhaitées : %s)" % (
                "/".join(langs), ", ".join(wanted))
        return None, "release sans tag de langue (anglais/VO) mais l'anglais n'est pas demandé"
    n = len(wanted)
    best_idx = min(wanted.index(c) for c in satisfied)
    points = int(LANGUAGE_MAX * (n - best_idx) / n)
    if "multi" in detected:
        points += MULTI_BONUS
    return points, ""


def _seeders_points(seeders: Optional[int]) -> int:
    if seeders is None:
        return UNKNOWN_SEEDERS_POINTS
    if seeders <= 0:
        return 0
    return int(min(math.log10(seeders + 1), 4.0) / 4.0 * SEEDERS_MAX)


def _max_size_bytes(kind: str, cfg: QualityConfig) -> float:
    if kind == "movie":
        gb = cfg.max_size_gb_movie
    elif kind == "season":
        gb = cfg.max_size_gb_season
    else:
        gb = cfg.max_size_gb_episode
    return gb * GB if gb and gb > 0 else 0.0


def _size_points(size: int, max_bytes: float) -> int:
    """Small bonus for a known, reasonable size (not tiny, not close to the maximum)."""
    if not size or size <= 0:
        return 0
    if not max_bytes:
        return 100
    ratio = size / max_bytes
    if 0.08 <= ratio <= 0.7:
        return 200
    return 100


def _check_structure(p: ParsedName, request: SearchRequest) -> str:
    """Return a rejection reason, or "" if the release structure fits the request."""
    kind = request.kind
    if kind == "movie":
        if p.seasons or p.episodes:
            return "contient un marqueur saison/épisode alors qu'un film est recherché"
        if request.year and p.year and abs(int(request.year) - int(p.year)) > 1:
            return "année %s différente de %s" % (p.year, request.year)
        return ""
    if kind == "episode":
        if request.season is None or request.episode is None:
            return "demande d'épisode incomplète"
        if not p.episodes:
            if p.is_season_pack or p.seasons or p.is_complete:
                return "pack de saison refusé pour un épisode"
            return "aucun numéro d'épisode"
        if request.season not in p.seasons:
            return "saison %s au lieu de %s" % ("/".join(str(s) for s in p.seasons) or "?", request.season)
        if request.episode not in p.episodes:
            return "épisode %s au lieu de %s" % ("/".join(str(e) for e in p.episodes), request.episode)
        return ""
    if kind == "season":
        if p.episodes:
            return "épisode isolé, pack de saison attendu"
        if request.season is None:
            # discovery mode: any season pack of the show
            return "" if (p.is_season_pack and p.seasons) else "pas un pack de saison"
        if not p.is_season_pack or request.season not in p.seasons:
            if p.seasons:
                return "saison %s au lieu de %s" % ("/".join(str(s) for s in p.seasons), request.season)
            return "pas un pack de la saison %s" % request.season
        return ""
    return "type de recherche inconnu: %s" % kind


def evaluate(release: Release, request: SearchRequest, cfg: QualityConfig) -> Tuple[Optional[int], str]:
    """Return (score, reason). score None = rejected, reason in French."""
    title = (release.title or "").strip()
    if not title:
        return None, "titre vide"
    p = parse_for_request(title, request)

    if p.is_sample:
        return None, "échantillon (sample)"

    if not titles_match(p.title, request.title, request.alt_titles):
        return None, "titre « %s » ne correspond pas à « %s »" % (p.title, request.title)

    reason = _check_structure(p, request)
    if reason:
        return None, reason

    # --- reject words / bad sources
    if p.source in _BAD_SOURCES:
        return None, "source %s refusée" % _SOURCE_LABELS.get(p.source, p.source)
    tail = _tail_after_title(title, p.title)
    wanted_norm = " %s " % normalize_title(request.title)
    for word in cfg.reject_words:
        nw = normalize_title(word)
        if not nw or (" %s " % nw) in wanted_norm:
            continue  # never reject on a word that belongs to the wanted title
        if _has_word(tail, nw):
            return None, "mot refusé « %s »" % word

    # --- resolution
    resolutions = [r.lower() for r in cfg.resolutions if r]
    score = 0
    if p.resolution:
        if p.resolution in resolutions:
            score += (len(resolutions) - resolutions.index(p.resolution)) * RESOLUTION_WEIGHT
        elif not cfg.allow_other_resolutions:
            return None, "résolution %s non acceptée (acceptées : %s)" % (p.resolution, ", ".join(resolutions))
        # other resolution allowed: no resolution points (ranked below listed ones)
    # unknown resolution: accepted, no resolution points (low score)

    # --- language
    lang_points, reason = _language_points(p.languages, cfg.languages)
    if lang_points is None:
        return None, reason
    score += lang_points

    # --- seeders
    if release.seeders is not None and release.seeders < cfg.min_seeders:
        return None, "pas assez de sources (%d seeders, minimum %d)" % (release.seeders, cfg.min_seeders)

    # --- size
    size = release.size or 0
    max_bytes = _max_size_bytes(request.kind, cfg)
    if size > 0:
        if cfg.min_size_mb and size < cfg.min_size_mb * MB:
            return None, "taille trop petite (%.0f Mo, minimum %.0f Mo)" % (size / MB, cfg.min_size_mb)
        if max_bytes and size > max_bytes:
            return None, "taille trop grande (%.1f Go, maximum %.1f Go)" % (size / GB, max_bytes / GB)

    # --- bonuses
    score += _SOURCE_POINTS.get(p.source or "", 0) * SOURCE_WEIGHT
    prefer = 0
    for word in cfg.prefer_words:
        if _has_word(tail, word):
            prefer += PREFER_WORD_BONUS
    score += min(prefer, PREFER_WORD_MAX)
    score += _seeders_points(release.seeders)
    score += _size_points(size, max_bytes)

    parts = [p.resolution or "résolution inconnue"]
    if p.source:
        parts.append(p.source)
    parts.append("/".join(p.languages) if p.languages else "VO")
    if release.seeders is not None:
        parts.append("%d seeders" % release.seeders)
    return score, "acceptée (%s)" % ", ".join(parts)


def rank_releases(releases: List[Release], request: SearchRequest, cfg: QualityConfig,
                  is_blocked: Callable[[str], bool] = lambda t: False) -> List[Tuple[int, Release]]:
    """Return accepted releases as (score, release), best first."""
    accepted: List[Tuple[int, int, int, Release]] = []
    seen = set()
    for idx, rel in enumerate(releases or []):
        try:
            key = (rel.info_hash or "").lower() or None
            if key and key in seen:
                continue
            try:
                blocked = bool(is_blocked(rel.title))
            except Exception as exc:  # a broken blocklist must not stop the search
                log.warning("Vérification de la liste de blocage impossible pour %s : %s", rel.title, exc)
                blocked = False
            if blocked:
                log.debug("Refusée « %s » : release bloquée", rel.title)
                continue
            score, reason = evaluate(rel, request, cfg)
        except Exception as exc:  # never let a weird release crash the search
            log.warning("Évaluation impossible de « %s » : %s", getattr(rel, "title", "?"), exc)
            continue
        if score is None:
            log.debug("Refusée « %s » : %s", rel.title, reason)
            continue
        if key:
            seen.add(key)
        seeders = rel.seeders if rel.seeders is not None else -1
        accepted.append((score, seeders, -idx, rel))
    accepted.sort(key=lambda t: (t[0], t[1], t[2]), reverse=True)
    return [(score, rel) for score, _s, _i, rel in accepted]


def pick_best(releases: List[Release], request: SearchRequest, cfg: QualityConfig,
              is_blocked: Callable[[str], bool] = lambda t: False) -> Optional[Release]:
    """Best acceptable release, or None. Equal scores: more seeders wins, then indexer order."""
    ranked = rank_releases(releases, request, cfg, is_blocked)
    return ranked[0][1] if ranked else None
