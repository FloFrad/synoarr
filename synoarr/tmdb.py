"""Optional TMDb (themoviedb.org API v3) lookups used to name downloads that
synoarr did not grab itself. Never raises: returns None / [] on any error.
"""
from __future__ import annotations

import logging
import re
from typing import Tuple, Any, Dict, List, Optional

from .httpclient import HttpClient, HttpError
from .models import EpisodeInfo
from .parser import normalize_title

log = logging.getLogger(__name__)

# TMDb placeholder titles when no translation exists ("Épisode 3")
_GENERIC_EPISODE = re.compile(r"^(episode|épisode|episodio|folge|aflevering|odcinek)\s*\d+$", re.I)


def _year(date: Any) -> Optional[int]:
    if not date:
        return None
    m = re.match(r"^(\d{4})", str(date))
    return int(m.group(1)) if m else None


ANIMATION_GENRE_ID = 16  # TMDb genre id of "Animation" (movies and TV)


class TmdbClient:
    def __init__(self, api_key: str, language: str, http: HttpClient):
        self.api_key = (api_key or "").strip()
        self.language = (language or "").strip() or "fr-FR"
        self.http = http
        self.base_url = "https://api.themoviedb.org/3"
        self.enabled = bool(self.api_key)
        self._warned_auth = False

    # ------------------------------------------------------------ low level
    def _get(self, path: str, params: Optional[Dict[str, Any]] = None,
             language: Optional[str] = None) -> Optional[Dict[str, Any]]:
        if not self.enabled:
            return None
        p: Dict[str, Any] = dict(params or {})
        p["language"] = language or self.language
        headers = {"Accept": "application/json"}
        if self.api_key.startswith("eyJ"):
            headers["Authorization"] = "Bearer %s" % self.api_key
        else:
            p["api_key"] = self.api_key
        try:
            resp = self.http.get(self.base_url.rstrip("/") + path, params=p, headers=headers)
            data = resp.json()
        except HttpError as exc:
            if exc.status == 401:
                if not self._warned_auth:
                    log.warning("TMDb : clé API refusée (HTTP 401), vérifiez [tmdb] api_key")
                    self._warned_auth = True
            elif exc.status == 404:
                log.debug("TMDb : introuvable %s", path)
            else:
                log.warning("TMDb : erreur sur %s : %s", path, exc)
            return None
        return data if isinstance(data, dict) else None

    @staticmethod
    def _pick(results: List[Dict[str, Any]], title: str, year: Optional[int], title_key: str,
              orig_key: str, date_key: str) -> Optional[Dict[str, Any]]:
        if not results:
            return None
        wanted = normalize_title(title)

        def score(r: Dict[str, Any]) -> int:
            s = 0
            names = {normalize_title(str(r.get(title_key) or "")), normalize_title(str(r.get(orig_key) or ""))}
            if wanted and wanted in names:
                s += 10
            ry = _year(r.get(date_key))
            if year and ry:
                if ry == year:
                    s += 5
                elif abs(ry - year) == 1:
                    s += 2
            return s

        # keep TMDb popularity order on ties (sorted is stable)
        return sorted(results[:20], key=score, reverse=True)[0]

    # ------------------------------------------------------------ search
    def search_movie(self, title: str, year: Optional[int]) -> Optional[Dict]:
        if not self.enabled or not title:
            return None
        results: List[Dict[str, Any]] = []
        if year:
            data = self._get("/search/movie", {"query": title, "year": year, "include_adult": "false"})
            results = [r for r in (data or {}).get("results") or [] if isinstance(r, dict)]
        if not results:
            data = self._get("/search/movie", {"query": title, "include_adult": "false"})
            results = [r for r in (data or {}).get("results") or [] if isinstance(r, dict)]
        best = self._pick(results, title, year, "title", "original_title", "release_date")
        if not best or best.get("id") is None:
            return None
        return {"title": best.get("title") or best.get("original_title") or title,
                "year": _year(best.get("release_date")),
                "tmdb": str(best["id"]),
                "original_title": best.get("original_title")}

    def search_show(self, title: str) -> Optional[Dict]:
        if not self.enabled or not title:
            return None
        year = None
        m = re.match(r"^(.*?)[\s.(\[]+((?:19|20)\d\d)[)\]]?$", title.strip())
        query = title
        if m and m.group(1).strip():
            query, year = m.group(1).strip(), int(m.group(2))
        data = self._get("/search/tv", {"query": query, "first_air_date_year": year} if year else {"query": query})
        results = [r for r in (data or {}).get("results") or [] if isinstance(r, dict)]
        if not results and year:
            for q in (query, title):
                data = self._get("/search/tv", {"query": q})
                results = [r for r in (data or {}).get("results") or [] if isinstance(r, dict)]
                if results:
                    break
        best = self._pick(results, query, year, "name", "original_name", "first_air_date")
        if not best or best.get("id") is None:
            return None
        return {"title": best.get("name") or best.get("original_name") or query,
                "year": _year(best.get("first_air_date")),
                "tmdb": str(best["id"]),
                "original_title": best.get("original_name")}

    # ------------------------------------------------------------ episodes
    @staticmethod
    def _genres(data: Dict[str, Any]) -> Tuple[List[str], bool]:
        genres = data.get("genres") or []
        names = [str(g.get("name")) for g in genres if isinstance(g, dict) and g.get("name")]
        ids = {g.get("id") for g in genres if isinstance(g, dict)}
        return names, ANIMATION_GENRE_ID in ids

    def movie_details(self, tmdb_id: str) -> Optional[Dict[str, Any]]:
        """{"collection": name or None, "genres": [...], "is_animation": bool} or None."""
        if not self.enabled or not tmdb_id:
            return None
        data = self._get("/movie/%s" % tmdb_id)
        if not data:
            return None
        names, anim = self._genres(data)
        coll = data.get("belongs_to_collection") or {}
        cname = str(coll.get("name") or "").strip() if isinstance(coll, dict) else ""
        return {"collection": cname or None, "genres": names, "is_animation": anim}

    def show_details(self, tmdb_id: str) -> Optional[Dict[str, Any]]:
        """{"genres": [...], "is_animation": bool} or None."""
        if not self.enabled or not tmdb_id:
            return None
        data = self._get("/tv/%s" % tmdb_id)
        if not data:
            return None
        names, anim = self._genres(data)
        return {"genres": names, "is_animation": anim}

    def find_by_imdb(self, imdb_id: str) -> Optional[Dict[str, str]]:
        """{"movie": tmdb_id} or {"tv": tmdb_id} for an IMDb id, or None."""
        if not self.enabled or not imdb_id:
            return None
        data = self._get("/find/%s" % imdb_id, {"external_source": "imdb_id"})
        for key, kind in (("movie_results", "movie"), ("tv_results", "tv")):
            res = (data or {}).get(key) or []
            if res and isinstance(res[0], dict) and res[0].get("id"):
                return {kind: str(res[0]["id"])}
        return None

    def episode_title(self, tmdb_id: str, season: int, episode: int) -> Optional[str]:
        if not self.enabled or not tmdb_id:
            return None
        path = "/tv/%s/season/%d/episode/%d" % (tmdb_id, int(season), int(episode))
        data = self._get(path)
        name = str((data or {}).get("name") or "").strip()
        if (not name or _GENERIC_EPISODE.match(name)) and not self.language.lower().startswith("en"):
            alt = self._get(path, language="en-US")
            alt_name = str((alt or {}).get("name") or "").strip()
            if alt_name and not _GENERIC_EPISODE.match(alt_name):
                return alt_name
        return name or None

    def show_episodes(self, tmdb_id: str) -> List[EpisodeInfo]:
        if not self.enabled or not tmdb_id:
            return []
        show = self._get("/tv/%s" % tmdb_id)
        if not show:
            return []
        out: Dict[tuple, EpisodeInfo] = {}
        for season in show.get("seasons") or []:
            if not isinstance(season, dict):
                continue
            try:
                num = int(season.get("season_number"))
            except (TypeError, ValueError):
                continue
            if num <= 0:
                continue
            data = self._get("/tv/%s/season/%d" % (tmdb_id, num))
            for ep in (data or {}).get("episodes") or []:
                if not isinstance(ep, dict):
                    continue
                try:
                    e_num = int(ep.get("episode_number"))
                    s_num = int(ep.get("season_number", num))
                except (TypeError, ValueError):
                    continue
                if s_num <= 0:
                    continue
                name = str(ep.get("name") or "").strip() or None
                if name and _GENERIC_EPISODE.match(name):
                    name = None
                air = ep.get("air_date")
                out[(s_num, e_num)] = EpisodeInfo(season=s_num, episode=e_num, title=name,
                                                  air_date=str(air)[:10] if air else None)
        return [out[k] for k in sorted(out)]
