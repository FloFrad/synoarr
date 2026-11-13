"""Release / file name parser (a tiny, dependency-free "guessit").

    >>> p = parse("The.Last.of.Us.S01E03.1080p.WEB-DL.MULTI.x264-GRP.mkv")
    >>> p.title, p.seasons, p.episodes, p.resolution, p.languages
    ('The Last of Us', [1], [3], '1080p', ['multi'])
"""
from __future__ import annotations

import re
import unicodedata
from typing import List, Optional, Tuple

from .models import ParsedName

KNOWN_EXTENSIONS = {
    "mkv", "mp4", "avi", "m4v", "mov", "wmv", "ts", "m2ts", "mpg", "mpeg", "webm", "ogv",
    "srt", "ass", "ssa", "sub", "idx", "vtt", "nfo", "txt", "torrent", "jpg", "png",
}

# --- marker regexes (applied on the "spaced" name) -------------------------------------------
_RE_SXXEYY = re.compile(r"\bS(\d{1,2})[ ._-]?E(\d{1,3})((?:[ ._-]?-?[ ._-]?E\d{1,3})*)", re.I)
_RE_NXNN = re.compile(r"\b(\d{1,2})x(\d{2,3})\b", re.I)
_RE_SEASON_RANGE = re.compile(r"\bS(\d{1,2})[ ._-]?-[ ._-]?S?(\d{1,2})\b", re.I)
_RE_SEASON_ONLY = re.compile(r"\bS(\d{1,2})\b(?![ ._-]?E\d)", re.I)
_RE_SEASON_WORD = re.compile(r"\b(?:season|saison|series|temporada|stagione)[ ._-]?(\d{1,2})\b", re.I)
_RE_EPISODE_WORD = re.compile(r"\b(?:episode|ep|e)[ ._-]?(\d{1,3})\b", re.I)
_RE_COMPLETE = re.compile(r"\b(?:complete|integrale|intégrale|integral|l[' ]?int[eé]grale)\b", re.I)
_RE_YEAR = re.compile(r"(?<![0-9])(19[2-9]\d|20\d\d)(?![0-9p])")
_RE_RESOLUTION = re.compile(r"\b(2160p|4k|uhd|1080[pi]|720p|576p|480p|360p)\b", re.I)
_RE_SAMPLE = re.compile(r"\bsample\b", re.I)
_RE_BRACKET_PREFIX = re.compile(r"^\s*(\[[^\]]*\]\s*)+")
_RE_SITE_PREFIX = re.compile(r"^\s*(?:www\.)?[a-z0-9-]+\.(?:com|net|org|to|io|cc|me|tv|info)\s*[-–]\s*", re.I)

_SOURCES = [
    ("cam", r"cam|camrip|hdcam"),
    ("telesync", r"ts|telesync|hdts|pdvd"),
    ("telecine", r"tc|telecine"),
    ("screener", r"scr|screener|dvdscr|bdscr"),
    ("workprint", r"workprint|wp"),
    ("bluray", r"blu[ .-]?ray|bdrip|brrip|bdremux|bd25|bd50|remux"),
    ("webdl", r"web[ .-]?dl|webdl|amzn|nf|dsnp|hmax|atvp"),
    ("webrip", r"web[ .-]?rip|webrip"),
    ("web", r"web"),
    ("hdtv", r"hdtv|pdtv|dsr|tvrip|satrip"),
    ("dvdrip", r"dvdrip|dvd[ .-]?r|dvd|dvd5|dvd9"),
]
_RE_SOURCES = [(name, re.compile(r"\b(?:%s)\b" % pat, re.I)) for name, pat in _SOURCES]

_LANGS = [
    ("multi", r"multi|multi[ .-]?vf2?|multi[ .-]?truefrench|dual"),
    ("vostfr", r"vostfr|subfrench|vost|stfr"),
    ("fr", r"french|truefrench|vff|vfq|vfi|vf2?|fr|fre|fra|francais|français|qc"),
    ("en", r"english|eng|vo"),
    ("it", r"italian|ita"),
    ("de", r"german|ger|deutsch"),
    ("es", r"spanish|spa|esp|castellano|latino"),
    ("ru", r"russian|rus"),
    ("pt", r"portuguese|por|dublado"),
    ("nl", r"dutch|nl"),
    ("pl", r"polish|pol|pl"),
    ("hi", r"hindi|hin"),
    ("ja", r"japanese|jap|jpn"),
    ("ko", r"korean|kor"),
    ("zh", r"chinese|chi|mandarin|cantonese"),
    ("tr", r"turkish|tur"),
]
_RE_LANGS = [(code, re.compile(r"(?<![a-z0-9])(?:%s)(?![a-z0-9])" % pat, re.I)) for code, pat in _LANGS]

# words that signal the end of the title even if no other marker was found
_RE_TECH = re.compile(
    r"\b(?:x26[45]|h[ .]?26[45]|hevc|avc|xvid|divx|aac|ac3|eac3|dts|ddp?5[ .]?1|atmos|truehd|10bit|hdr|hdr10|dv|"
    r"proper|repack|internal|limited|extended|unrated|remastered|directors[ .]?cut|imax|"
    r"multi|truefrench|french|vff|vostfr|bluray|bdrip|brrip|web[ .-]?dl|webrip|hdtv|dvdrip|remux)\b",
    re.I,
)

_RE_ARTICLES = re.compile(r"^(?:the|a|an|le|la|les|l|un|une|des)\s+")


def _strip_extension(name: str) -> Tuple[str, Optional[str]]:
    base = name.strip()
    if "." in base:
        stem, ext = base.rsplit(".", 1)
        if ext.lower() in KNOWN_EXTENSIONS:
            return stem, ext.lower()
    return base, None


def _spaced(name: str) -> str:
    s = name.replace("_", " ")
    # dots used as separators (keep dots inside acronyms like "S.H.I.E.L.D" mostly intact by just spacing)
    s = re.sub(r"(?<!\d)\.(?!\d)|(?<=\d)\.(?!\d)|(?<!\d)\.(?=\d)", " ", s)
    s = re.sub(r"\s+", " ", s)
    return s.strip()


def _clean_title(raw: str) -> str:
    t = raw
    t = re.sub(r"[\[\(\{][^\]\)\}]*$", "", t)  # unterminated bracket at the end
    t = re.sub(r"[\[\(\{]\s*[\]\)\}]", "", t)
    t = t.replace("(", " ").replace(")", " ").replace("[", " ").replace("]", " ")
    t = re.sub(r"\s+", " ", t).strip(" -–_.,:")
    return t


def parse(name: str) -> ParsedName:
    """Parse a release title, folder name or file name."""
    original = name
    # keep only the last path component
    name = re.split(r"[\\/]", name.strip().rstrip("/\\"))[-1]
    stem, ext = _strip_extension(name)
    stem = _RE_BRACKET_PREFIX.sub("", stem)
    stem = _RE_SITE_PREFIX.sub("", stem)
    s = _spaced(stem)
    result = ParsedName(raw=original, extension=ext)

    marker_positions: List[int] = []

    # --- seasons / episodes
    m = _RE_SXXEYY.search(s)
    if m:
        result.seasons = [int(m.group(1))]
        eps = [int(m.group(2))]
        extra = re.findall(r"E(\d{1,3})", m.group(3) or "", re.I)
        eps.extend(int(e) for e in extra)
        if len(eps) == 2 and "-" in (m.group(3) or "") and eps[1] > eps[0]:
            eps = list(range(eps[0], eps[1] + 1))
        result.episodes = sorted(set(eps))
        marker_positions.append(m.start())
    else:
        m = _RE_NXNN.search(s)
        if m and not _RE_RESOLUTION.search(m.group(0)):
            result.seasons = [int(m.group(1))]
            result.episodes = [int(m.group(2))]
            marker_positions.append(m.start())
        else:
            m = _RE_SEASON_RANGE.search(s)
            if m:
                a, b = int(m.group(1)), int(m.group(2))
                if b >= a:
                    result.seasons = list(range(a, b + 1))
                    result.is_season_pack = True
                    marker_positions.append(m.start())
            if not result.seasons:
                m = _RE_SEASON_ONLY.search(s) or _RE_SEASON_WORD.search(s)
                if m:
                    result.seasons = [int(m.group(1))]
                    marker_positions.append(m.start())
                    # "Season 2 Episode 5"
                    me = _RE_EPISODE_WORD.search(s, m.end())
                    if me and me.start() - m.end() <= 3:
                        result.episodes = [int(me.group(1))]
                    else:
                        result.is_season_pack = True

    mc = _RE_COMPLETE.search(s)
    if mc:
        result.is_complete = True
        marker_positions.append(mc.start())
        if not result.episodes:
            result.is_season_pack = bool(result.seasons) or result.is_season_pack

    # --- resolution
    mr = _RE_RESOLUTION.search(s)
    if mr:
        val = mr.group(1).lower()
        result.resolution = "2160p" if val in ("4k", "uhd", "2160p") else val.replace("i", "p")
        marker_positions.append(mr.start())

    # --- year: prefer the last year located before other markers (handles "2001 A Space Odyssey 1968")
    first_marker = min(marker_positions) if marker_positions else len(s)
    years = [y for y in _RE_YEAR.finditer(s)]
    year_match = None
    candidates = [y for y in years if y.start() <= first_marker and y.start() > 0]
    if candidates:
        year_match = candidates[-1]
    elif years and years[0].start() > 0:
        year_match = years[0]
    if year_match:
        result.year = int(year_match.group(1))
        marker_positions.append(year_match.start())

    # --- source
    for src, rx in _RE_SOURCES:
        ms = rx.search(s)
        if ms and (ms.start() > 0):
            # only accept short ambiguous tokens (ts, tc, wp, nf...) after the title area
            if src in ("telesync", "telecine", "workprint", "web", "webdl") and ms.start() < first_marker \
                    and not marker_positions:
                continue
            result.source = src
            break

    # --- title: everything before the first marker
    tech = _RE_TECH.search(s)
    if tech and tech.start() > 0:
        marker_positions.append(tech.start())
    cut = min(marker_positions) if marker_positions else len(s)
    title_part = s[:cut]
    result.title = _clean_title(title_part)
    if not result.title and year_match is not None and year_match.start() == 0:
        result.title = year_match.group(1)

    # --- languages: only look after the title to avoid "Le Fabuleux Destin d'Amélie" false positives
    tail = s[cut:] if cut < len(s) else ""
    langs: List[str] = []
    for code, rx in _RE_LANGS:
        if rx.search(tail):
            langs.append(code)
    if "multi" in langs:
        langs = ["multi"] + [lg for lg in langs if lg not in ("multi", "fr", "en")]
    if "vostfr" in langs and "fr" in langs:
        langs.remove("fr")
    result.languages = langs

    result.is_sample = bool(_RE_SAMPLE.search(s))
    return result


# ------------------------------------------------------------------- title helpers
def normalize_title(title: str) -> str:
    """Lowercase, strip accents and punctuation: "L'Étrange Noël & Co." -> "l etrange noel and co"."""
    t = unicodedata.normalize("NFKD", title or "")
    t = "".join(c for c in t if not unicodedata.combining(c))
    t = t.lower().replace("&", " and ")
    t = re.sub(r"['’`]", "", t)
    t = re.sub(r"[^a-z0-9]+", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def _core(title: str) -> str:
    t = normalize_title(title)
    t = re.sub(r"\s+(19|20)\d\d$", "", t)  # trailing year
    t = _RE_ARTICLES.sub("", t)
    return t.replace(" and ", " ")


def titles_match(candidate: str, wanted: str, alt_titles: Optional[List[str]] = None) -> bool:
    """True if the parsed *candidate* title designates the same work as *wanted* (or one of alt_titles)."""
    c = _core(candidate)
    if not c:
        return False
    for w in [wanted] + list(alt_titles or []):
        wc = _core(w)
        if not wc:
            continue
        if c == wc or c.replace(" ", "") == wc.replace(" ", ""):
            return True
    return False
