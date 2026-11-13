"""Release / file name parser (a tiny, dependency-free "guessit").

    >>> p = parse("The.Last.of.Us.S01E03.1080p.WEB-DL.MULTI.x264-GRP.mkv")
    >>> p.title, p.seasons, p.episodes, p.resolution, p.languages
    ('The Last of Us', [1], [3], '1080p', ['multi'])
"""
from __future__ import annotations

import datetime as _dt
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
    r"bluray|bdrip|brrip|web[ .-]?dl|webrip|hdtv|dvdrip|remux)\b",
    re.I,
)
# language words also end the title, but they are real words too ("The French Dispatch", "Multi Love"):
# they only count when followed by nothing or by another technical token (see _language_cut)
_RE_TECH_LANG = re.compile(r"\b(?:multi|truefrench|french|vff|vfq|vostfr|subfrench)\b", re.I)
_RE_COUNTRY_SUFFIX = re.compile(r"\s(?:us|uk|au|nz|ca)$")

_RE_ARTICLES = re.compile(r"^(?:the|a|an|le|la|les|l|un|une|des)\s+")


def _strip_extension(name: str) -> Tuple[str, Optional[str]]:
    base = name.strip()
    if "." in base:
        stem, ext = base.rsplit(".", 1)
        if ext.lower() in KNOWN_EXTENSIONS:
            # ".TS" is also the telesync tag ("Dune.2021.720p.TS"): only an extension after a group tag
            # ("...x264-GRP.ts") or when the name has no resolution marker at all
            if ext.lower() == "ts" and _RE_RESOLUTION.search(stem.replace(".", " ")) and \
                    not re.search(r"-[A-Za-z0-9]+$", stem):
                return base, None
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


def _is_tech_token(rest: str) -> bool:
    """True if *rest* (text after a language word) starts with a technical token."""
    rest = rest.lstrip(" -")
    if not rest:
        return True
    for rx in (_RE_TECH, _RE_TECH_LANG, _RE_RESOLUTION, _RE_SXXEYY, _RE_SEASON_ONLY, _RE_SEASON_WORD,
               _RE_COMPLETE):
        if rx.match(rest):
            return True
    if re.match(r"(?:19|20)\d\d\b", rest):
        return True
    for _code, rx in _RE_LANGS:
        if rx.match(rest):
            return True
    for _src, rx in _RE_SOURCES:
        if rx.match(rest):
            return True
    return False


def _language_cut(s: str, other_markers: bool) -> Optional[int]:
    """Position of the first language word that ends the title, if any."""
    found = [m for m in _RE_TECH_LANG.finditer(s) if m.start() > 0]
    for m in found:
        if _is_tech_token(s[m.end():]):
            return m.start()
    if found and not other_markers:
        return found[0].start()  # nothing else to cut on: "Kaamelott FRENCH HDLight"
    return None


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
    max_year = _dt.date.today().year + 2  # "Blade Runner 2049", "Cyberpunk 2077": part of the title
    years = [y for y in years if int(y.group(1)) <= max_year]
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
    lang_cut = _language_cut(s, bool(marker_positions))
    if lang_cut is not None:
        marker_positions.append(lang_cut)
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
    m = re.search(r"\s+((?:19|20)\d\d)$", t)  # trailing year ("Doctor Who 2005"), not "Blade Runner 2049"
    if m and int(m.group(1)) <= _dt.date.today().year + 2:
        t = t[:m.start()]
    t = _RE_ARTICLES.sub("", t)
    return t.replace(" and ", " ")


def _cores(title: str) -> List[str]:
    """Comparable forms of a title. An apostrophe is dropped ("Grey's" -> "greys") but may also separate an
    elided French article: "L'Arme fatale" must match both "L.Arme.Fatale" and "LArme.Fatale"."""
    out = [_core(title)]
    if re.search(r"['’`]", title or ""):
        alt = _core(re.sub(r"['’`]", " ", title))
        if alt not in out:
            out.append(alt)
    return [c for c in out if c]


def _same_core(c: str, w: str) -> bool:
    return c == w or c.replace(" ", "") == w.replace(" ", "")


def titles_match(candidate: str, wanted: str, alt_titles: Optional[List[str]] = None) -> bool:
    """True if the parsed *candidate* title designates the same work as *wanted* (or one of alt_titles).

    A trailing country code on one side only is ignored ("The Office US" ~ "The Office"), never when both
    sides have a different one ("The Office UK" != "The Office US")."""
    cands = _cores(candidate)
    if not cands:
        return False
    for w in [wanted] + list(alt_titles or []):
        for wc in _cores(w):
            for c in cands:
                if _same_core(c, wc):
                    return True
                c_cc, w_cc = _RE_COUNTRY_SUFFIX.search(c), _RE_COUNTRY_SUFFIX.search(wc)
                if c_cc and not w_cc and c[:c_cc.start()].strip() and _same_core(c[:c_cc.start()], wc):
                    return True
                if w_cc and not c_cc and wc[:w_cc.start()].strip() and _same_core(c, wc[:w_cc.start()]):
                    return True
    return False
