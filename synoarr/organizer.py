"""Download organizer (Filebot-like): rename finished downloads to the Plex naming scheme.

    Films/Title (Year)/Title (Year).mkv
    Series/Show/Season 01/Show - S01E02.mkv

Safety rules (the files are the user's real media):

* never touch an entry that Download Station still downloads, that contains partial files
  (``.part``, ``.!ut``...) or that was modified less than ``min_age_minutes`` ago;
* never overwrite a destination: same size (and same head/tail bytes) = already imported,
  different size = keep the biggest one, the replacement is written to a temporary file first;
* copies are written to a temporary file, checked, then renamed: an interrupted copy never
  leaves a truncated video in the library;
* a leftover folder is only removed after a move, when no video remains in it;
* one bad entry or file never stops the cycle: ``run_once`` never raises.
"""
from __future__ import annotations

import errno
import logging
import os
import re
import shutil
import time
import unicodedata
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Optional, Set, Tuple

from .parser import normalize_title, parse, titles_match

from .config import missing_share_root
from .db import release_file_lock, try_file_lock

if TYPE_CHECKING:  # pragma: no cover - imported for type hints only
    from .config import Config
    from .db import Database
    from .downloadstation import DownloadStation
    from .models import DSTask, ParsedName
    from .tmdb import TmdbClient

log = logging.getLogger(__name__)

# Videos smaller than this are ignored (samples, trailers...) unless they are the only video.
MIN_VIDEO_SIZE = 50 * 1024 * 1024
# A remaining "sample" video smaller than this does not prevent the deletion of a leftover folder.
SAMPLE_MAX_SIZE = 500 * 1024 * 1024
# Names of files that are still being written by a download client.
INCOMPLETE_SUFFIXES = (".part", ".!ut", ".!qb", ".crdownload", ".partial", ".aria2", ".downloading",
                       ".synoarr-tmp")
# Top-level / nested names never touched.
IGNORED_NAMES = {"@eadir", "#recycle", "#snapshot", "lost+found", "thumbs.db", "desktop.ini", ".ds_store"}
TMP_PREFIX = ".synoarr-tmp-"
_COMPARE_CHUNK = 1024 * 1024
_MAX_COMPONENT_BYTES = 200

# Files that may be deleted with the leftover folder of one of OUR downloads (anything else keeps the folder).
JUNK_EXTENSIONS = {"nfo", "txt", "jpg", "jpeg", "png", "sfv", "srr", "url", "exe", "md5", "nzb", "torrent"}
# Bonus material: never imported as the movie / episode (like samples).
_EXTRA_DIRS = {"trailer", "trailers", "featurette", "featurettes", "extra", "extras", "bonus", "bonus features",
               "behind the scenes", "deleted scenes", "interviews", "making of", "bandes annonces", "bande annonce"}
_RE_EXTRA = re.compile(r"\b(?:trailers?|teasers?|featurettes?|behind the scenes|deleted scenes?|making of|"
                       r"bande annonce)\b")

_RE_PART = re.compile(r"(?:^|[ ._\-\[(])(?:cd|disc|disk|part|pt)[ ._-]?(\d{1,2})(?=$|[ ._\-\])])", re.I)

_SUB_LANGS = {
    "fr": ("fr", "fre", "fra", "french", "francais", "français", "vf", "vff", "vfq", "vfi", "truefrench", "vostfr",
           "stfr"),
    "en": ("en", "eng", "english", "anglais", "vo"),
    "es": ("es", "spa", "spanish", "espanol", "español", "castellano", "latino"),
    "de": ("de", "ger", "deu", "german", "deutsch", "allemand"),
    "it": ("it", "ita", "italian", "italiano", "italien"),
    "pt": ("pt", "por", "portuguese", "portugues", "português", "brazilian"),
    "nl": ("nl", "dut", "nld", "dutch", "nederlands"),
    "ru": ("ru", "rus", "russian"),
    "ja": ("ja", "jpn", "japanese"),
    "ko": ("ko", "kor", "korean"),
    "zh": ("zh", "chi", "zho", "chinese"),
    "ar": ("ar", "ara", "arabic"),
    "pl": ("pl", "pol", "polish"),
    "sv": ("sv", "swe", "swedish"),
    "da": ("da", "dan", "danish"),
    "no": ("nor", "norwegian"),
    "fi": ("fi", "fin", "finnish"),
    "tr": ("tr", "tur", "turkish"),
    "el": ("el", "gre", "ell", "greek"),
    "he": ("he", "heb", "hebrew"),
    "hu": ("hu", "hun", "hungarian"),
    "cs": ("cs", "cze", "ces", "czech"),
    "ro": ("ro", "rum", "ron", "romanian"),
}
_SUB_LANG_TOKENS = {tok: code for code, toks in _SUB_LANGS.items() for tok in toks}
_SUB_FORCED = {"forced", "force", "forces", "forcés", "foreign"}
_SUB_SDH = {"sdh", "cc"}


class _PlanError(Exception):
    """A file of an entry cannot be renamed (message in French)."""


class _EpisodeList(object):
    """Formats like an int but renders several episodes: '{episode:02d}' -> '01-E02'."""

    def __init__(self, episodes: List[int]):
        self.episodes = episodes

    def __format__(self, spec: str) -> str:
        return "-E".join(format(e, spec) for e in self.episodes)

    def __str__(self) -> str:
        return self.__format__("")


# --------------------------------------------------------------------------- helpers
def _ext(path: str) -> str:
    base = os.path.basename(path)
    if "." not in base:
        return ""
    return base.rsplit(".", 1)[1].lower()


def _stem(path: str) -> str:
    base = os.path.basename(path)
    return base.rsplit(".", 1)[0] if "." in base else base


def _is_ignored_name(name: str) -> bool:
    low = name.lower()
    if not name or name.startswith(".") or name.startswith("@") or low in IGNORED_NAMES:
        return True
    if low.startswith("synoarr-") or low.startswith("_synoarr"):
        return True
    return low.endswith(INCOMPLETE_SUFFIXES)


def _smart_case(title: str) -> str:
    """Title-case a parsed title only when it is entirely lowercase ("the office" -> "The Office")."""
    if title and title == title.lower() and any(c.isalpha() for c in title):
        return " ".join(w[:1].upper() + w[1:] for w in title.split(" "))
    return title


def _clean_value(value: str) -> str:
    """Sanitize a metadata value before it is inserted in a naming format (no path separators)."""
    value = unicodedata.normalize("NFC", value)
    value = value.replace(":", " - ")
    value = re.sub(r"\s*[/\\]\s*", "-", value)  # "Face/Off" -> "Face-Off"
    return re.sub(r"\s+", " ", value).strip()


def _clean_component(comp: str) -> str:
    """Sanitize one path component: forbidden characters, empty brackets, spaces, leading/trailing dots."""
    s = unicodedata.normalize("NFC", comp)
    s = s.replace(":", " - ")
    s = re.sub(r'[<>"/\\|?*\x00-\x1f\x7f]', "", s)
    prev = None
    while prev != s:  # "Title (  )" / "Title []" left by an empty year or resolution
        prev = s
        s = re.sub(r"\(\s*\)|\[\s*\]|\{\s*\}", "", s)
    s = re.sub(r"\s+", " ", s)
    s = s.strip(" .")
    s = re.sub(r"(?:\s+-)+$", "", s).strip(" .")
    encoded = s.encode("utf-8")
    if len(encoded) > _MAX_COMPONENT_BYTES:
        s = encoded[:_MAX_COMPONENT_BYTES].decode("utf-8", "ignore").rstrip(" .")
    return s


def _subtitle_suffix(text: str, strip_title: bool) -> str:
    """'.fr', '.en.forced', '.en.sdh' or '' from the distinctive part of a subtitle file name."""
    tokens = [t for t in re.split(r"[\W_]+", text.lower()) if t]
    if strip_title:
        title_tokens = [t for t in re.split(r"[\W_]+", parse(text).title.lower()) if t]
        if 0 < len(title_tokens) < len(tokens) and tokens[:len(title_tokens)] == title_tokens:
            tokens = tokens[len(title_tokens):]
    lang = None
    forced = sdh = False
    for tok in tokens:
        if tok in _SUB_FORCED:
            forced = True
        elif tok in _SUB_SDH:
            sdh = True
        elif tok in _SUB_LANG_TOKENS:
            lang = _SUB_LANG_TOKENS[tok]  # last language token wins ("Movie.2010.eng.fr.srt" -> fr)
    suffix = ""
    if lang:
        suffix += "." + lang
    if forced:
        suffix += ".forced"
    if sdh:
        suffix += ".sdh"
    return suffix


def _file_size(path: str) -> int:
    try:
        return os.path.getsize(path)
    except OSError:
        return -1


def _same_content(a: str, b: str) -> bool:
    """Cheap identity check for two files of the same size: compare the first and last MiB."""
    try:
        size = os.path.getsize(a)
        with open(a, "rb") as fa, open(b, "rb") as fb:
            if fa.read(_COMPARE_CHUNK) != fb.read(_COMPARE_CHUNK):
                return False
            if size > _COMPARE_CHUNK:
                offset = max(0, size - _COMPARE_CHUNK)
                fa.seek(offset)
                fb.seek(offset)
                if fa.read(_COMPARE_CHUNK) != fb.read(_COMPARE_CHUNK):
                    return False
        return True
    except OSError:
        return False


def _inside(path: str, root: str) -> bool:
    path = os.path.normcase(os.path.realpath(path))
    root = os.path.normcase(os.path.realpath(root))
    return path != root and path.startswith(root.rstrip(os.sep) + os.sep)


class _Item(object):
    """One planned file operation."""

    def __init__(self, src: str, dst: str, meta: Dict[str, Any]):
        self.src = src
        self.dst = dst
        self.meta = meta


# --------------------------------------------------------------------------- organizer
class Organizer:
    min_video_size = MIN_VIDEO_SIZE
    sample_max_size = SAMPLE_MAX_SIZE

    def __init__(self, cfg: "Config", db: "Database", ds: Optional["DownloadStation"],
                 tmdb: Optional["TmdbClient"], on_imported: Optional[Callable[[], None]] = None):
        self.cfg = cfg
        self.db = db
        self.ds = ds
        self.tmdb = tmdb
        self.on_imported = on_imported
        self._warned = set()  # type: Set[str]
        self._tmp_counter = 0
        # entries already fully present in the library (copy/hardlink keeps them for seeding):
        # path -> newest mtime, so they are not re-checked every cycle while unchanged
        self._settled = {}  # type: Dict[str, float]

    # ================================================================== public API
    def run_once(self, dry_run: bool = False) -> List[Dict]:
        """Process every top-level entry of the download folder. Never raises."""
        dry = bool(dry_run or self.cfg.general.dry_run)
        lock_fd = None
        try:
            if not dry:
                lock_fd = self._lock()
                if lock_fd is False:
                    log.info("Un autre traitement des téléchargements est en cours (démon ou commande) : "
                             "cycle ignoré")
                    return []
            return self._run(dry)
        except Exception as exc:  # last resort: the daemon must survive
            log.exception("Erreur inattendue de l'organiseur : %s", exc)
            return []
        finally:
            release_file_lock(lock_fd)

    def _lock(self) -> Any:
        """Inter-process lock in data_dir. Returns an fd, None (no locking possible) or False (busy)."""
        try:
            data_dir = self.cfg.data_dir
        except (TypeError, AttributeError):  # pragma: no cover
            return None
        return try_file_lock(data_dir, "organizer.lock")

    def plan(self, entry_path: str) -> List[Tuple[str, str, Dict]]:
        """Planned (src, dst, meta) for one entry, without touching anything."""
        try:
            name = os.path.basename(entry_path.rstrip("/\\"))
            grab = self._find_grab(name, os.path.isdir(entry_path))
            items, _problems = self._plan_entry(entry_path, name, grab)
            return [(it.src, it.dst, it.meta) for it in items]
        except Exception as exc:
            log.exception("Impossible de planifier %s : %s", entry_path, exc)
            return []

    # ================================================================== cycle
    def _run(self, dry_run: bool) -> List[Dict]:
        root = self.cfg.paths.download_dir
        if not os.path.isdir(root):
            self._warn_once("nodir:" + root, "Dossier de téléchargement introuvable : %s" % root)
            return []
        try:
            names = sorted(os.listdir(root))
        except OSError as exc:
            log.error("Lecture du dossier %s impossible : %s", root, exc)
            return []
        tasks, ds_ok = self._list_tasks()
        min_age = max(0.0, float(self.cfg.paths.min_age_minutes or 0))
        if not ds_ok:
            min_age *= 2
        actions = []  # type: List[Dict]
        imported = 0
        for name in names:
            entry_path = os.path.join(root, name)
            try:
                entry_actions, count = self._process_entry(entry_path, name, tasks, ds_ok, min_age, dry_run)
            except Exception as exc:
                log.exception("Erreur inattendue pendant le traitement de %s : %s", entry_path, exc)
                entry_actions, count = [self._action(entry_path, "", "error", "erreur inattendue : %s" % exc)], 0
            actions.extend(entry_actions)
            imported += count
        if imported and not dry_run:
            log.info("%d fichier(s) vidéo importé(s)", imported)
            if self.on_imported is not None:
                try:
                    self.on_imported()
                except Exception as exc:
                    log.warning("Action après import en erreur : %s", exc)
        return actions

    def _list_tasks(self) -> Tuple[List["DSTask"], bool]:
        if self.ds is None:
            return [], True
        try:
            tasks = list(self.ds.list_tasks() or [])
            self._warned.discard("ds")
            return tasks, True
        except Exception as exc:
            self._warn_once("ds", "Download Station injoignable (%s) : traitement poursuivi avec un délai minimum "
                                  "doublé par sécurité" % exc)
            return [], False

    def _process_entry(self, entry_path: str, name: str, tasks: List["DSTask"], ds_ok: bool, min_age: float,
                       dry_run: bool) -> Tuple[List[Dict], int]:
        if _is_ignored_name(name) or os.path.islink(entry_path):
            return [], 0
        is_dir = os.path.isdir(entry_path)
        if not is_dir and not os.path.isfile(entry_path):
            return [], 0
        if self._is_protected(entry_path):
            return [], 0
        if not is_dir and _ext(name) not in self._video_exts():
            return [], 0  # stray top-level file (subtitles are handled with their video)

        grab = self._find_grab(name, is_dir)
        matching = self._matching_tasks(name, is_dir, grab, tasks)
        busy = [t for t in matching if not t.is_complete]
        if busy:
            return [self._skip(entry_path, "téléchargement non terminé dans Download Station (%s)" % busy[0].status)], 0
        if grab and grab.get("status") == "failed":
            # the download failed (task in error, release blocklisted): what is on disk is probably partial
            return [self._skip(entry_path, "téléchargement en échec dans Download Station : fichiers ignorés "
                                           "(supprimez-les ou importez-les à la main)")], 0
        if grab and grab.get("status") == "sent" and self.ds is not None and not ds_ok:
            # one of our downloads, but its state cannot be checked: a stalled download looks "old" too
            return [self._skip(entry_path, "Download Station injoignable : état du téléchargement inconnu")], 0
        if is_dir:
            partial = self._find_incomplete(entry_path)
            if partial:
                return [self._skip(entry_path, "fichier en cours d'écriture : %s" % partial)], 0
        newest = self._newest_mtime(entry_path)
        if self._settled.get(entry_path) == newest:
            return [], 0
        age_min = (time.time() - newest) / 60.0
        if age_min < -24 * 60:
            age_min = min_age  # mtime more than a day in the future (wrong clock): do not wait forever
        if age_min < min_age:
            return [self._skip(entry_path, "modifié il y a %.1f min (attente de %.0f min)" % (max(age_min, 0), min_age))], 0
        if self.cfg.paths.only_known_downloads and not grab:
            return [self._skip(entry_path, "téléchargement inconnu (only_known_downloads)")], 0

        items, problems = self._plan_entry(entry_path, name, grab)
        problem_actions = [self._skip(src, msg) for src, msg in problems]
        if not items:
            if not problem_actions:
                problem_actions = [self._skip(entry_path, "aucune vidéo trouvée")]
            return problem_actions, 0

        verb = {"move": "déplacement", "copy": "copie", "hardlink": "lien physique"}.get(self.cfg.paths.action,
                                                                                        "déplacement")
        if dry_run:
            actions = []
            for it in items:
                msg = verb
                if os.path.lexists(it.dst):
                    msg += " (la destination existe déjà)"
                actions.append(self._action(it.src, it.dst, "planned", msg))
            return actions + problem_actions, 0

        actions, imported, settled = self._execute(entry_path, name, is_dir, grab, matching, ds_ok, items,
                                                   problem_actions)
        if settled and os.path.exists(entry_path):
            self._settled[entry_path] = self._newest_mtime(entry_path)
        return actions, imported

    def _execute(self, entry_path: str, name: str, is_dir: bool, grab: Optional[Dict[str, Any]],
                 matching: List["DSTask"], ds_ok: bool, items: List[_Item],
                 problem_actions: List[Dict]) -> Tuple[List[Dict], int, bool]:
        actions = []  # type: List[Dict]
        present = {}  # type: Dict[str, bool]
        imported = 0
        videos = [it for it in items if it.meta["kind"] != "subtitle"]
        subs = [it for it in items if it.meta["kind"] == "subtitle"]
        for it in videos:
            status, msg, ok = self._transfer(it.src, it.dst)
            present[it.src] = ok
            actions.append(self._action(it.src, it.dst, status, msg))
            if status == "imported":
                log.info("Importé : %s -> %s (%s)", it.src, it.dst, msg)
                if msg != "déjà présent":
                    imported += 1
            elif status == "error":
                self._warn_once("err:" + it.src, "Import impossible de %s : %s" % (it.src, msg), event=True)
            else:
                self._warn_once("skip:" + it.src, "%s ignoré : %s" % (it.src, msg))
            if ok:
                self._mark_done(it.meta)
        for it in subs:
            if not present.get(it.meta.get("video", ""), False):
                actions.append(self._skip(it.src, "vidéo associée non importée"))
                continue
            status, msg, _ok = self._transfer(it.src, it.dst)
            actions.append(self._action(it.src, it.dst, status, msg))
            if status == "error":
                log.warning("Sous-titre %s non importé : %s", it.src, msg)

        settled = bool(videos) and all(present.get(it.src) for it in videos)
        if settled:
            self._entry_done(entry_path, name, is_dir, grab, matching, ds_ok, videos, imported > 0)
        return actions + problem_actions, imported, settled

    def _entry_done(self, entry_path: str, name: str, is_dir: bool, grab: Optional[Dict[str, Any]],
                    matching: List["DSTask"], ds_ok: bool, videos: List[_Item], new_files: bool) -> None:
        paths = self.cfg.paths
        if grab and grab.get("status") != "imported":
            try:
                self.db.set_grab_status(int(grab["id"]), "imported", "importé dans la bibliothèque")
                # episodes the download did not contain (incomplete season pack...) are searched again
                if self.db.reset_grab_episodes(grab):
                    log.info("%s ne contenait pas tous les épisodes attendus : les manquants seront recherchés",
                             name)
            except Exception as exc:
                log.warning("Mise à jour du grab %s impossible : %s", grab.get("id"), exc)
        if paths.remove_task_after_import and matching:
            if self.ds is None or not ds_ok:
                log.warning("Tâche Download Station de %s non supprimée : Download Station injoignable", name)
            else:
                for task in matching:
                    try:
                        self.ds.delete_task(task.id)
                        log.info("Tâche Download Station supprimée : %s", task.title)
                    except Exception as exc:
                        log.warning("Suppression de la tâche Download Station %s impossible : %s", task.title, exc)
        if is_dir and paths.action == "move" and paths.delete_leftovers and grab:
            # only for downloads synoarr sent itself: a folder dropped by the user is never deleted
            self._delete_leftovers(entry_path)
        if not new_files:
            return
        try:
            dsts = ", ".join(os.path.basename(it.dst) for it in videos)
            self.db.add_event("import", "Importé : %s -> %s" % (name, dsts))
        except Exception as exc:
            log.warning("Écriture de l'historique impossible : %s", exc)

    def _mark_done(self, meta: Dict[str, Any]) -> None:
        media_id = meta.get("media_id")
        if not media_id:
            return
        try:
            if meta["kind"] == "movie":
                self.db.set_media_status(int(media_id), "done")
            elif meta["kind"] == "episode":
                for ep in meta.get("episodes") or []:
                    self.db.set_episode_status(int(media_id), int(meta["season"]), int(ep), "done")
        except Exception as exc:
            log.warning("Mise à jour du statut en base impossible : %s", exc)

    # ================================================================== guards
    def _video_exts(self) -> Set[str]:
        return set(e.lower().lstrip(".") for e in self.cfg.naming.video_extensions)

    def _sub_exts(self) -> Set[str]:
        return set(e.lower().lstrip(".") for e in self.cfg.naming.subtitle_extensions)

    def _is_protected(self, entry_path: str) -> bool:
        """Never process the library or synoarr's own data folder if they live inside download_dir."""
        try:
            rp = os.path.realpath(entry_path)
            for d in (self.cfg.paths.movies_dir, self.cfg.paths.series_dir, self.cfg.data_dir):
                if not d:
                    continue
                rd = os.path.realpath(d)
                if rp == rd or _inside(rd, rp):
                    return True
        except Exception:  # pragma: no cover - defensive
            return True
        return False

    def _find_grab(self, name: str, is_dir: bool) -> Optional[Dict[str, Any]]:
        try:
            grab = self.db.find_grab_by_name(name)
            if not grab and not is_dir:
                grab = self.db.find_grab_by_name(_stem(name))
            if not grab:
                grab = self._guess_grab(name, is_dir)
            return grab
        except Exception as exc:
            log.warning("Recherche du grab pour %s impossible : %s", name, exc)
            return None

    def _strip_video_ext(self, name: str) -> str:
        return _stem(name) if _ext(name) in self._video_exts() else name

    def _guess_grab(self, name: str, is_dir: bool) -> Optional[Dict[str, Any]]:
        """Pending grab whose name differs from the entry: magnet without "dn" (the release title of the
        indexer is not the torrent name), punctuation changed... Only downloads still 'sent' are considered."""
        sent = self.db.list_grabs(status="sent", limit=500)
        if not sent:
            return None
        key = normalize_title(self._strip_video_ext(name))
        if key:
            for g in sent:
                for k in ("torrent_name", "release_title"):
                    v = g.get(k) or ""
                    if v and normalize_title(self._strip_video_ext(v)) == key:
                        return g
        p = parse(name)
        if not p.title:
            return None
        medias = {}  # type: Dict[int, Optional[Dict[str, Any]]]
        for g in sent:  # newest first
            mid = g.get("media_id")
            if not mid:
                continue
            if mid not in medias:
                medias[mid] = self.db.get_media(int(mid))
            media = medias[mid]
            if not media:
                continue
            alts = [media["original_title"]] if media.get("original_title") else []
            if not titles_match(p.title, media.get("title") or "", alts):
                continue
            if g.get("season") is None:
                if media.get("type") != "movie" or p.seasons or p.episodes:
                    continue
                if p.year and media.get("year") and abs(p.year - int(media["year"])) > 1:
                    continue
                return g
            if p.season is None or int(g["season"]) not in p.seasons:
                continue
            if g.get("episode") is None:
                if not p.episodes:  # the season pack itself, not one of its episodes
                    return g
            elif int(g["episode"]) in p.episodes:
                return g
        return None

    def _guess_media(self, titles: List[str], year: Optional[int], is_show: bool) -> Optional[Dict[str, Any]]:
        """Watchlist item matching a download that synoarr did not send (same naming + status update).
        Only an unambiguous match is used."""
        titles = [t for t in titles if t]
        if not titles:
            return None
        try:
            rows = self.db.list_media()
        except Exception as exc:
            log.debug("Lecture de la Watchlist impossible : %s", exc)
            return None
        found = []
        for m in rows:
            if (m.get("type") == "show") != is_show:
                continue
            alts = [m["original_title"]] if m.get("original_title") else []
            if not any(titles_match(t, m.get("title") or "", alts) for t in titles):
                continue
            if not is_show and year and m.get("year") and abs(int(m["year"]) - year) > 1:
                continue
            found.append(m)
        return found[0] if len(found) == 1 else None

    @staticmethod
    def _matching_tasks(name: str, is_dir: bool, grab: Optional[Dict[str, Any]],
                        tasks: List["DSTask"]) -> List["DSTask"]:
        keys = {name.strip().lower()}
        if not is_dir:
            keys.add(_stem(name).strip().lower())
        if grab:
            for k in ("torrent_name", "release_title"):
                if grab.get(k):
                    keys.add(str(grab[k]).strip().lower())
        norm_keys = set(normalize_title(k) for k in keys)
        norm_keys.discard("")
        out = []
        for t in tasks:
            title = (t.title or "").strip()
            if title.lower() in keys:
                out.append(t)
            elif not t.is_complete and normalize_title(title) in norm_keys:
                out.append(t)  # extra safety: an active task whose title differs only by punctuation
        return out

    def _find_incomplete(self, entry_path: str) -> Optional[str]:
        for dirpath, _dirs, files in os.walk(entry_path):
            for f in files:
                if f.lower().endswith(INCOMPLETE_SUFFIXES):
                    return os.path.relpath(os.path.join(dirpath, f), entry_path)
        return None

    @staticmethod
    def _newest_mtime(entry_path: str) -> float:
        try:
            newest = os.lstat(entry_path).st_mtime
        except OSError:
            return time.time()
        if os.path.isdir(entry_path):
            for dirpath, dirs, files in os.walk(entry_path):
                for n in dirs + files:
                    try:
                        m = os.lstat(os.path.join(dirpath, n)).st_mtime
                    except OSError:
                        continue
                    if m > newest:
                        newest = m
        return newest

    def _warn_once(self, key: str, message: str, event: bool = False) -> None:
        if key in self._warned:
            log.debug(message)
            return
        self._warned.add(key)
        log.warning(message)
        if event:
            try:
                self.db.add_event("import", message, "ERROR")
            except Exception:
                pass

    @staticmethod
    def _action(src: str, dst: str, status: str, message: str) -> Dict[str, Any]:
        return {"src": src, "dst": dst, "status": status, "message": message}

    def _skip(self, src: str, message: str) -> Dict[str, Any]:
        log.debug("Ignoré %s : %s", src, message)
        return self._action(src, "", "skipped", message)

    # ================================================================== planning
    def _scan(self, entry_path: str) -> List[str]:
        out = []
        for dirpath, dirs, files in os.walk(entry_path):
            dirs[:] = sorted(d for d in dirs if not _is_ignored_name(d)
                             and not os.path.islink(os.path.join(dirpath, d)))
            for f in sorted(files):
                full = os.path.join(dirpath, f)
                if _is_ignored_name(f) or os.path.islink(full):
                    continue
                out.append(full)
        return out

    def _is_sample(self, entry_path: str, video: str, entry_parsed: "ParsedName") -> bool:
        rel = os.path.relpath(video, entry_path) if os.path.isdir(entry_path) else os.path.basename(video)
        for part in rel.replace("\\", "/").split("/")[:-1]:
            if part.lower() in ("sample", "samples", "échantillon"):
                return True
        return parse(os.path.basename(video)).is_sample and not entry_parsed.is_sample

    @staticmethod
    def _is_extra(entry_path: str, video: str) -> bool:
        """Bonus material (trailer, featurette, extras folder...): never imported as the movie/episode."""
        rel = os.path.relpath(video, entry_path) if os.path.isdir(entry_path) else os.path.basename(video)
        for part in rel.replace("\\", "/").split("/")[:-1]:
            if normalize_title(part) in _EXTRA_DIRS:
                return True
        # "Big.Movie.2020.Trailer.1080p.mkv" (the word after the title), not "Trailer.Park.Boys.S01E01.mkv"
        p = parse(os.path.basename(video))
        norm, title = normalize_title(_stem(video)), normalize_title(p.title)
        tail = norm[len(title):] if title and norm.startswith(title) else norm
        return bool(_RE_EXTRA.search(tail))

    @staticmethod
    def _is_junk(entry_path: str, path: str) -> bool:
        """Leftover file that may be deleted: nfo/txt/images/checksums..., or anything named / filed as sample."""
        name = os.path.basename(path)
        if _ext(name) in JUNK_EXTENSIONS or _is_ignored_name(name):
            return True
        rel = os.path.relpath(path, entry_path).replace("\\", "/").lower()
        return "sample" in rel

    def _is_junk_sample(self, entry_path: str, path: str) -> bool:
        """A small sample video that may be deleted with the leftover folder."""
        sample = self._is_sample(entry_path, path, parse(os.path.basename(entry_path)))
        return sample and 0 <= _file_size(path) < self.sample_max_size

    def _select_videos(self, entry_path: str, videos: List[str],
                       entry_parsed: "ParsedName") -> Tuple[List[str], List[Tuple[str, str]]]:
        infos = []
        for v in videos:
            if self._is_extra(entry_path, v):
                log.debug("Bonus ignoré : %s", v)
                continue
            size = _file_size(v)
            if size < 0:
                continue
            infos.append((v, size, self._is_sample(entry_path, v, entry_parsed)))
        non_sample = [i for i in infos if not i[2]]
        big = [i for i in non_sample if i[1] >= self.min_video_size]
        if big:
            chosen = big
        elif len(non_sample) == 1:
            chosen = non_sample
        elif len(infos) == 1:
            chosen = infos
        else:
            chosen = []
        chosen_paths = [i[0] for i in chosen]
        problems = []
        for v, _size, sample in infos:
            if v not in chosen_paths and not sample:
                problems.append((v, "vidéo ignorée (moins de %d Mo)" % (self.min_video_size // (1024 * 1024))))
        return chosen_paths, problems

    def _plan_entry(self, entry_path: str, name: str,
                    grab: Optional[Dict[str, Any]]) -> Tuple[List[_Item], List[Tuple[str, str]]]:
        is_dir = os.path.isdir(entry_path)
        vexts, sexts = self._video_exts(), self._sub_exts()
        if is_dir:
            files = self._scan(entry_path)
            subs = [f for f in files if _ext(f) in sexts]
        else:
            files = [entry_path]
            subs = self._sibling_subtitles(entry_path, sexts)
        videos = [f for f in files if _ext(f) in vexts]
        entry_parsed = parse(name)
        chosen, problems = self._select_videos(entry_path, videos, entry_parsed)
        if not chosen:
            if videos and not problems:
                problems.append((entry_path, "seuls des échantillons (sample) ont été trouvés"))
            return [], problems

        media = None
        if grab and grab.get("media_id"):
            try:
                media = self.db.get_media(int(grab["media_id"]))
            except Exception as exc:
                log.warning("Lecture du média %s impossible : %s", grab.get("media_id"), exc)
        parsed = dict((v, parse(os.path.basename(v))) for v in chosen)
        if media is None and not grab:
            is_show = any(p.is_episode for p in parsed.values()) or entry_parsed.is_episode or \
                bool(entry_parsed.seasons)
            first = parsed[chosen[0]]
            media = self._guess_media([entry_parsed.title, first.title], entry_parsed.year or first.year, is_show)
            if media:
                log.debug("%s associé à « %s » de la Watchlist", name, media.get("title"))

        if media:
            kind = "movie" if media.get("type") == "movie" else "show"
        elif grab and grab.get("season") is not None:
            kind = "show"
        elif any(p.is_episode for p in parsed.values()) or entry_parsed.is_episode or entry_parsed.seasons:
            kind = "show"
        else:
            kind = "movie"

        if kind == "movie":
            items, more = self._plan_movie(entry_path, chosen, parsed, entry_parsed, media)
        else:
            items, more = self._plan_series(entry_path, chosen, parsed, entry_parsed, media, grab)
        problems.extend(more)
        items, more = self._dedupe(items)
        problems.extend(more)
        if grab:
            for it in items:
                it.meta["grab_id"] = grab.get("id")
        items.extend(self._plan_subtitles(entry_path, items, subs, kind))
        return items, problems

    def _sibling_subtitles(self, video: str, sexts: Set[str]) -> List[str]:
        """Subtitles lying next to a single top-level video file ("Movie.mkv" + "Movie.fr.srt")."""
        folder = os.path.dirname(video)
        stem = _stem(video).lower()
        out = []
        try:
            names = sorted(os.listdir(folder))
        except OSError:
            return out
        for n in names:
            full = os.path.join(folder, n)
            if _ext(n) not in sexts or _is_ignored_name(n) or not os.path.isfile(full) or os.path.islink(full):
                continue
            low = _stem(n).lower()
            if low == stem or (low.startswith(stem) and low[len(stem):len(stem) + 1] in (".", " ", "_", "-")):
                out.append(full)
        return out

    # ---------------------------------------------------------------- movies
    def _plan_movie(self, entry_path: str, chosen: List[str], parsed: Dict[str, "ParsedName"],
                    entry_parsed: "ParsedName",
                    media: Optional[Dict[str, Any]]) -> Tuple[List[_Item], List[Tuple[str, str]]]:
        problems = []  # type: List[Tuple[str, str]]
        main = max(chosen, key=_file_size)
        pmain = parsed[main]
        ids = {"imdb": "", "tmdb": "", "tvdb": ""}
        if media:
            title, year = media.get("title") or "", media.get("year")
            for k in ids:
                ids[k] = str(media.get(k) or "")
        else:
            title, year = entry_parsed.title, entry_parsed.year
            if not title or (not year and pmain.year and pmain.title):
                title, year = pmain.title or title, pmain.year or year
            title = _smart_case(title)
            found = self._tmdb_call("search_movie", title, year) if title else None
            if found and found.get("title"):
                title = found["title"]
                year = found.get("year") or year
                ids["tmdb"] = str(found.get("tmdb") or "")
        if not title:
            return [], [(main, "titre du film introuvable")]

        # several big videos: multi-part movie (CD1/CD2) or a movie + extras (extras are left in place)
        parts = {}  # type: Dict[str, int]
        if len(chosen) > 1:
            for v in chosen:
                m = _RE_PART.search(_stem(v))
                if m:
                    parts[v] = int(m.group(1))
            if len(parts) != len(chosen) or len(set(parts.values())) != len(parts):
                parts = {}
                for v in chosen:
                    if v != main:
                        problems.append((v, "vidéo supplémentaire ignorée (film déjà choisi : %s)"
                                         % os.path.basename(main)))
                chosen = [main]

        items = []
        for v in chosen:
            p = parsed[v]
            kw = {"title": title, "year": year if year else "", "show": title, "season": 0, "episode": 0,
                  "episode_title": "", "ext": _ext(v), "resolution": p.resolution or entry_parsed.resolution or ""}
            kw.update(ids)
            try:
                stem = self._render(self.cfg.paths.movies_dir, self.cfg.naming.movie_format, kw)
            except _PlanError as exc:
                problems.append((v, str(exc)))
                continue
            if v in parts:
                stem += " - pt%d" % parts[v]
            items.append(_Item(v, stem + "." + _ext(v), {
                "kind": "movie", "media_id": media.get("id") if media else None, "title": title, "year": year,
                "season": None, "episodes": [], "size": _file_size(v)}))
        return items, problems

    # ---------------------------------------------------------------- series
    def _plan_series(self, entry_path: str, chosen: List[str], parsed: Dict[str, "ParsedName"],
                     entry_parsed: "ParsedName", media: Optional[Dict[str, Any]],
                     grab: Optional[Dict[str, Any]]) -> Tuple[List[_Item], List[Tuple[str, str]]]:
        problems = []  # type: List[Tuple[str, str]]
        ids = {"imdb": "", "tmdb": "", "tvdb": ""}
        if media:
            show, year = media.get("title") or "", media.get("year")
            for k in ids:
                ids[k] = str(media.get(k) or "")
        else:
            year = entry_parsed.year
            show = ""
            if entry_parsed.title and (entry_parsed.seasons or entry_parsed.episodes):
                show = entry_parsed.title
            else:
                counts = {}  # type: Dict[str, int]
                for p in parsed.values():
                    if p.is_episode and p.title:
                        counts[p.title] = counts.get(p.title, 0) + 1
                if counts:
                    show = max(sorted(counts), key=lambda t: counts[t])
                else:
                    show = entry_parsed.title
            show = _smart_case(show)
            found = self._tmdb_call("search_show", show) if show else None
            if found and found.get("title"):
                show = found["title"]
                year = found.get("year") or year
                ids["tmdb"] = str(found.get("tmdb") or "")
        if not show:
            return [], [(v, "nom de la série introuvable") for v in chosen]

        items = []
        for v in chosen:
            p = parsed[v]
            season, episodes = p.season, list(p.episodes)
            if not episodes and len(chosen) == 1:
                if entry_parsed.is_episode:
                    season, episodes = entry_parsed.season, list(entry_parsed.episodes)
                elif grab and grab.get("episode") is not None and grab.get("season") is not None:
                    season, episodes = int(grab["season"]), [int(grab["episode"])]
            if episodes and season is None:
                season = self._season_from_folders(entry_path, v)
                if season is None:
                    season = entry_parsed.season
                if season is None and grab and grab.get("season") is not None:
                    season = int(grab["season"])
            if not episodes or season is None:
                problems.append((v, "saison/épisode non reconnu dans le nom du fichier"))
                continue
            ep_title = ""
            if "episode_title" in self.cfg.naming.series_format:
                ep_title = self._episode_title(media, ids["tmdb"], season, episodes)
            kw = {"show": show, "title": show, "year": year if year else "", "season": season,
                  "episode": _EpisodeList(episodes) if len(episodes) > 1 else episodes[0],
                  "episode_title": ep_title, "ext": _ext(v),
                  "resolution": p.resolution or entry_parsed.resolution or ""}
            kw.update(ids)
            try:
                stem = self._render(self.cfg.paths.series_dir, self.cfg.naming.series_format, kw)
            except _PlanError as exc:
                problems.append((v, str(exc)))
                continue
            items.append(_Item(v, stem + "." + _ext(v), {
                "kind": "episode", "media_id": media.get("id") if media else None, "title": show, "year": year,
                "season": season, "episodes": episodes, "size": _file_size(v)}))
        return items, problems

    def _season_from_folders(self, entry_path: str, video: str) -> Optional[int]:
        if not os.path.isdir(entry_path):
            return None
        rel_dirs = os.path.relpath(os.path.dirname(video), entry_path).replace("\\", "/").split("/")
        for d in reversed(rel_dirs):
            if d and d != ".":
                p = parse(d)
                if p.seasons:
                    return p.seasons[0]
        return None

    def _episode_title(self, media: Optional[Dict[str, Any]], tmdb_id: str, season: int,
                       episodes: List[int]) -> str:
        titles = []
        for ep in episodes:
            t = None
            if media:
                try:
                    row = self.db.get_episode(int(media["id"]), season, ep)
                    t = row.get("title") if row else None
                except Exception:
                    t = None
            if not t and tmdb_id:
                t = self._tmdb_call("episode_title", tmdb_id, season, ep)
            if t and t not in titles:
                titles.append(str(t))
        return " & ".join(titles)

    def _tmdb_call(self, method: str, *args: Any) -> Any:
        if self.tmdb is None or not getattr(self.tmdb, "enabled", False):
            return None
        try:
            return getattr(self.tmdb, method)(*args)
        except Exception as exc:
            log.warning("TMDb indisponible (%s) : %s", method, exc)
            return None

    # ---------------------------------------------------------------- naming
    def _render(self, base_dir: str, fmt: str, kw: Dict[str, Any]) -> str:
        """Destination path WITHOUT extension."""
        values = dict((k, _clean_value(v) if isinstance(v, str) else v) for k, v in kw.items())
        try:
            rel = fmt.format(**values)
        except (KeyError, IndexError, ValueError, TypeError, AttributeError) as exc:
            raise _PlanError("format de nommage invalide (%s) : %s" % (fmt, exc))
        parts = [_clean_component(p) for p in re.split(r"[/\\]", rel)]
        parts = [p for p in parts if p]
        if not parts:
            raise _PlanError("nom de destination vide (format %s)" % fmt)
        dst = os.path.join(base_dir, *parts)
        if not _inside(dst, base_dir):
            raise _PlanError("destination hors de %s : %s" % (base_dir, dst))
        return dst

    @staticmethod
    def _dedupe(items: List[_Item]) -> Tuple[List[_Item], List[Tuple[str, str]]]:
        best = {}  # type: Dict[str, _Item]
        order = []  # type: List[str]
        problems = []
        for it in items:
            key = it.dst.lower()
            other = best.get(key)
            if other is None:
                best[key] = it
                order.append(key)
                continue
            keep, drop = (it, other) if it.meta.get("size", 0) > other.meta.get("size", 0) else (other, it)
            best[key] = keep
            problems.append((drop.src, "même destination que %s (le plus gros fichier est gardé)"
                             % os.path.basename(keep.src)))
        return [best[k] for k in order], problems

    def _plan_subtitles(self, entry_path: str, videos: List[_Item], subs: List[str], kind: str) -> List[_Item]:
        out = []  # type: List[_Item]
        if not videos or not subs:
            return out
        used = set(it.dst.lower() for it in videos)
        for s in subs:
            s_dir, s_stem = os.path.dirname(s), _stem(s)
            owner, rest = None, None
            for it in videos:  # 1. "<video stem>.fr.srt" next to the video
                v_stem = _stem(it.src)
                if os.path.dirname(it.src) == s_dir and s_stem.lower().startswith(v_stem.lower()):
                    owner, rest = it, s_stem[len(v_stem):]
                    break
            if owner is None and kind == "show":  # 2. same season/episode numbers
                ps = parse(os.path.basename(s))
                if ps.episodes:
                    for it in videos:
                        if it.meta.get("season") == ps.season and it.meta.get("episodes") == ps.episodes:
                            owner = it
                            break
            if owner is None and kind == "movie" and len(videos) == 1:  # 3. subtitles of a movie folder
                owner = videos[0]
            if owner is None:
                log.debug("Sous-titre sans vidéo associée : %s", s)
                continue
            if rest is not None:
                suffix = _subtitle_suffix(rest, strip_title=False)
            else:
                suffix = _subtitle_suffix(s_stem, strip_title=True)
                if not suffix and s_dir != os.path.dirname(owner.src):  # "Subs/French/xx.srt"
                    suffix = _subtitle_suffix(os.path.basename(s_dir), strip_title=False)
            base = owner.dst.rsplit(".", 1)[0]
            ext = _ext(s)
            dst = "%s%s.%s" % (base, suffix, ext)
            n = 2
            while dst.lower() in used:
                dst = "%s%s.%d.%s" % (base, suffix, n, ext)
                n += 1
            used.add(dst.lower())
            out.append(_Item(s, dst, {"kind": "subtitle", "video": owner.src, "media_id": None,
                                      "size": _file_size(s)}))
        return out

    # ================================================================== file operations
    def _transfer(self, src: str, dst: str) -> Tuple[str, str, bool]:
        """Import one file. Returns (status, message, destination_holds_this_media)."""
        action = self.cfg.paths.action
        try:
            if os.path.lexists(dst):
                if os.path.isdir(dst):
                    return "error", "la destination est un dossier", False
                if os.path.samefile(src, dst):
                    if action == "move":
                        os.remove(src)  # dst is a hard link of src: the data stays in the library
                    return "imported", "déjà présent", True
                ssize, dsize = os.path.getsize(src), os.path.getsize(dst)
                if ssize == dsize:
                    if not _same_content(src, dst):
                        return ("skipped", "un fichier différent de même taille existe déjà, rien n'est modifié",
                                False)
                    if action == "move":
                        os.remove(src)
                    return "imported", "déjà présent", True
                if ssize < dsize:
                    return ("skipped", "la destination existante est plus grosse (%d > %d octets), conservée"
                            % (dsize, ssize), True)
                self._place(src, dst, replace=True)
                return "imported", "remplace une version plus petite (%d < %d octets)" % (dsize, ssize), True
            parent = os.path.dirname(dst)
            missing = missing_share_root(dst)
            if missing:  # never create a shared folder (typo in movies_dir/series_dir)
                return ("error", "dossier partagé %s introuvable : vérifiez movies_dir / series_dir (File Station > "
                        "clic droit > Propriétés > Emplacement)" % missing, False)
            if parent:
                os.makedirs(parent, exist_ok=True)
            how = self._place(src, dst, replace=False)
            return "imported", how, True
        except OSError as exc:
            return "error", "%s" % exc, False

    def _tmp_path(self, dst: str) -> str:
        self._tmp_counter += 1
        return os.path.join(os.path.dirname(dst), "%s%d-%d%s" % (TMP_PREFIX, os.getpid(), self._tmp_counter,
                                                                 ".synoarr-tmp"))

    def _place(self, src: str, dst: str, replace: bool) -> str:
        action = self.cfg.paths.action
        if not replace and os.path.lexists(dst):
            raise OSError(errno.EEXIST, "la destination existe déjà", dst)
        if action == "hardlink":
            tmp = self._tmp_path(dst) if replace else None
            try:
                if tmp:
                    os.link(src, tmp)
                    os.replace(tmp, dst)
                else:
                    os.link(src, dst)
                return "lien physique"
            except OSError as exc:
                if tmp and os.path.lexists(tmp):
                    try:
                        os.remove(tmp)
                    except OSError:
                        pass
                log.info("Lien physique impossible pour %s (%s) : copie à la place", src, exc)
            self._copy_into(src, dst)
            return "copie (lien physique impossible)"
        if action == "copy":
            self._copy_into(src, dst)
            return "copie"
        # move: same volume = atomic rename; otherwise verified copy then removal of the source
        try:
            os.rename(src, dst)
            return "déplacement"
        except OSError as exc:
            if exc.errno not in (errno.EXDEV, errno.EPERM, errno.EACCES, errno.ENOTSUP, errno.EINVAL,
                                 errno.EOPNOTSUPP):
                raise
            log.debug("Renommage impossible (%s), copie puis suppression de la source", exc)
        self._copy_into(src, dst)
        os.remove(src)
        return "déplacement (copie entre volumes)"

    def _copy_into(self, src: str, dst: str) -> None:
        """Copy src to a temporary file next to dst, check its size, then rename it to dst."""
        tmp = self._tmp_path(dst)
        try:
            shutil.copyfile(src, tmp)
            try:
                shutil.copystat(src, tmp)
            except OSError:
                pass  # ACL/permission quirks of Synology shares: data matters, not metadata
            if os.path.getsize(tmp) != os.path.getsize(src):
                raise OSError(errno.EIO, "copie incomplète", dst)
            os.replace(tmp, dst)
        except BaseException:
            try:
                if os.path.lexists(tmp):
                    os.remove(tmp)
            except OSError:
                pass
            raise

    def _delete_leftovers(self, entry_path: str) -> bool:
        """Remove a download folder after a move, only if no real video remains inside."""
        root = self.cfg.paths.download_dir
        if os.path.islink(entry_path) or not os.path.isdir(entry_path) or not _inside(entry_path, root):
            return False
        for dirpath, _dirs, files in os.walk(entry_path):
            for f in files:
                full = os.path.join(dirpath, f)
                if f.lower().endswith(INCOMPLETE_SUFFIXES):
                    log.info("Dossier %s conservé : fichier en cours d'écriture %s", entry_path, f)
                    return False
                if not self._is_junk(entry_path, full):
                    log.info("Dossier %s conservé : il contient encore %s", entry_path,
                             os.path.relpath(full, entry_path))
                    return False
        try:
            shutil.rmtree(entry_path)
            log.info("Dossier de téléchargement supprimé : %s", entry_path)
            return True
        except OSError as exc:
            log.warning("Suppression du dossier %s impossible : %s", entry_path, exc)
            return False
