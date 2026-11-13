"""Self-update from GitHub (used by the web UI "Mettre à jour" command and `synoarr.py update`).

Two modes:
- git checkout (a .git folder and the git command): ``git pull --ff-only``;
- archive install (the guide's wget/tar method): download
  https://github.com/<repo>/archive/refs/heads/main.tar.gz, extract it safely, check that the new code
  imports with THIS Python, back up the files about to change, then copy the new files over.

config.ini, data/, config/ (Docker) and docker-compose.yml are never touched.
In Docker the code lives in the image: the update must be done by rebuilding the project.
"""
from __future__ import annotations

import filecmp
import logging
import os
import shutil
import subprocess
import sys
import tarfile
import time
from typing import Any, Callable, Dict, List, Optional

from .httpclient import HttpClient, HttpError

log = logging.getLogger(__name__)

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_REPO = "FloFrad/synoarr"
# never replaced by an update (user files)
PROTECTED = {"config.ini", "data", "config", "docker-compose.yml", "synoarr.pid", ".git"}
MAX_ARCHIVE = 20 * 1024 * 1024
KEEP_BACKUPS = 3

Emit = Callable[[str, str], None]


class UpdateError(Exception):
    """French message for the user."""


def in_docker() -> bool:
    return os.path.exists("/.dockerenv") or os.environ.get("SYNOARR_IN_DOCKER") == "1"


def _is_git_checkout(root: str) -> bool:
    return os.path.isdir(os.path.join(root, ".git")) and shutil.which("git") is not None


def _git(root: str, *args: str, timeout: int = 120) -> str:
    try:
        out = subprocess.run(["git", "-C", root] + list(args), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                             timeout=timeout, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        raise UpdateError("git a échoué : %s" % exc)
    text = out.stdout.decode("utf-8", "replace").strip()
    if out.returncode != 0:
        raise UpdateError("git %s a échoué : %s" % (" ".join(args), text[-500:]))
    return text


def _safe_extract(archive: str, dest: str) -> str:
    """Extract a GitHub tarball, refusing absolute paths, '..', links and devices. Returns the top folder."""
    with tarfile.open(archive, "r:gz") as tar:
        members = tar.getmembers()
        top = None
        for m in members:
            name = m.name.replace("\\", "/")
            parts = [p for p in name.split("/") if p not in ("", ".")]
            if name.startswith("/") or ".." in parts:
                raise UpdateError("archive refusée : chemin suspect %r" % m.name)
            if not (m.isfile() or m.isdir()):
                if m.issym() or m.islnk() or m.isdev() or m.isfifo():
                    raise UpdateError("archive refusée : lien ou fichier spécial %r" % m.name)
            if parts:
                top = top or parts[0]
                if parts[0] != top:
                    raise UpdateError("archive inattendue (plusieurs dossiers racine)")
        for m in members:
            if m.isfile() or m.isdir():
                tar.extract(m, dest)
    if not top:
        raise UpdateError("archive vide")
    return os.path.join(dest, top)


def _iter_files(base: str) -> List[str]:
    out = []
    for dirpath, dirs, files in os.walk(base):
        rel_dir = os.path.relpath(dirpath, base)
        first = rel_dir.split(os.sep)[0] if rel_dir != "." else ""
        if first in PROTECTED or first == "__pycache__":
            dirs[:] = []
            continue
        dirs[:] = [d for d in dirs if d not in PROTECTED and d != "__pycache__"]
        for f in files:
            rel = os.path.normpath(os.path.join(rel_dir, f)) if rel_dir != "." else f
            if rel in PROTECTED or f.endswith(".pyc"):
                continue
            out.append(rel)
    return sorted(out)


class Updater(object):
    def __init__(self, data_dir: str, http: HttpClient, repo: str = DEFAULT_REPO, root: str = ROOT_DIR,
                 branch: str = "main"):
        self.data_dir = data_dir
        self.http = http
        self.repo = (repo or DEFAULT_REPO).strip().strip("/")
        self.root = root
        self.branch = branch
        self.archive_url = "https://github.com/%s/archive/refs/heads/%s.tar.gz" % (self.repo, self.branch)

    # ------------------------------------------------------------------ public
    def check(self, emit: Emit) -> Dict[str, Any]:
        """Compare the installed files with GitHub. Nothing is modified."""
        if in_docker():
            return self._docker_info(emit)
        if _is_git_checkout(self.root):
            _git(self.root, "fetch", "--quiet", "origin", self.branch)
            behind = int(_git(self.root, "rev-list", "--count", "HEAD..origin/%s" % self.branch) or 0)
            emit("ok" if behind == 0 else "info",
                 "À jour" if behind == 0 else "%d nouveau(x) commit(s) disponible(s)" % behind)
            return {"mode": "git", "available": behind > 0, "changed": behind}
        work = self._download_and_extract(emit)
        try:
            diff = self._diff(work)
        finally:
            shutil.rmtree(os.path.dirname(work), ignore_errors=True)
        self._report(diff, emit)
        return {"mode": "archive", "available": bool(diff["changed"] or diff["added"]),
                "changed": diff["changed"], "added": diff["added"]}

    def apply(self, emit: Emit) -> Dict[str, Any]:
        """Install the latest version. Returns {"updated": bool, ...}. The caller restarts synoarr."""
        if in_docker():
            return self._docker_info(emit)
        if _is_git_checkout(self.root):
            emit("info", "Installation git : git pull")
            before = _git(self.root, "rev-parse", "HEAD")
            out = _git(self.root, "pull", "--ff-only", "origin", self.branch, timeout=300)
            after = _git(self.root, "rev-parse", "HEAD")
            for line in out.splitlines()[-10:]:
                emit("info", line)
            self._check_imports(self.root, emit)
            updated = before != after
            emit("ok", "Mise à jour installée" if updated else "Déjà à jour")
            return {"mode": "git", "updated": updated}
        work = self._download_and_extract(emit)
        try:
            diff = self._diff(work)
            self._report(diff, emit)
            todo = diff["changed"] + diff["added"]
            if not todo:
                emit("ok", "Déjà à jour")
                return {"mode": "archive", "updated": False}
            self._check_imports(work, emit)
            backup = self._backup(diff["changed"], emit)
            for rel in todo:
                src, dst = os.path.join(work, rel), os.path.join(self.root, rel)
                os.makedirs(os.path.dirname(dst) or self.root, exist_ok=True)
                tmp = dst + ".synoarr-new"
                shutil.copy2(src, tmp)
                os.replace(tmp, dst)
            for rel in todo:  # scripts must stay executable
                if rel.endswith(".sh") or rel == "synoarr.py":
                    try:
                        os.chmod(os.path.join(self.root, rel), 0o755)
                    except OSError:
                        pass
            emit("ok", "%d fichier(s) mis à jour (sauvegarde : %s)" % (len(todo), backup))
            return {"mode": "archive", "updated": True, "files": todo, "backup": backup}
        finally:
            shutil.rmtree(os.path.dirname(work), ignore_errors=True)

    def rollback(self, emit: Emit) -> Dict[str, Any]:
        """Restore the files saved by the last update."""
        base = os.path.join(self.data_dir, "update-backups")
        backups = sorted(os.listdir(base)) if os.path.isdir(base) else []
        if not backups:
            raise UpdateError("Aucune sauvegarde de mise à jour à restaurer")
        last = os.path.join(base, backups[-1])
        files = _iter_files(last)
        for rel in files:
            dst = os.path.join(self.root, rel)
            os.makedirs(os.path.dirname(dst) or self.root, exist_ok=True)
            shutil.copy2(os.path.join(last, rel), dst)
        shutil.rmtree(last, ignore_errors=True)
        emit("ok", "Version précédente restaurée (%d fichier(s))" % len(files))
        return {"restored": len(files)}

    # ----------------------------------------------------------------- private
    def _docker_info(self, emit: Emit) -> Dict[str, Any]:
        emit("warn", "synoarr tourne dans Docker : le programme fait partie de l'image.")
        emit("info", "Pour mettre à jour : en SSH, cd /volume1/docker/synoarr && sudo sh scripts/update.sh, puis "
                     "Container Manager > Projet > synoarr > Arrêter > Action > Construire > Démarrer.")
        return {"mode": "docker", "available": None, "updated": False}

    def _download_and_extract(self, emit: Emit) -> str:
        emit("info", "Téléchargement de %s" % self.archive_url)
        try:
            resp = self.http.get(self.archive_url, timeout=120)
        except HttpError as exc:
            raise UpdateError("Téléchargement impossible (%s). Le dépôt %s est-il public ?" % (exc, self.repo))
        if len(resp.body) > MAX_ARCHIVE:
            raise UpdateError("archive trop volumineuse")
        work_base = os.path.join(self.data_dir, "update-tmp-%d" % int(time.time() * 1000))
        os.makedirs(work_base, exist_ok=True)
        archive = os.path.join(work_base, "synoarr.tar.gz")
        with open(archive, "wb") as fh:
            fh.write(resp.body)
        try:
            top = _safe_extract(archive, work_base)
        except (tarfile.TarError, OSError, EOFError) as exc:
            shutil.rmtree(work_base, ignore_errors=True)
            raise UpdateError("archive illisible : %s" % exc)
        except UpdateError:
            shutil.rmtree(work_base, ignore_errors=True)
            raise
        os.remove(archive)
        for required in ("synoarr.py", os.path.join("synoarr", "__init__.py"), os.path.join("synoarr", "cli.py")):
            if not os.path.isfile(os.path.join(top, required)):
                shutil.rmtree(work_base, ignore_errors=True)
                raise UpdateError("l'archive ne contient pas synoarr (fichier %s absent)" % required)
        return top

    def _diff(self, work: str) -> Dict[str, List[str]]:
        changed, added = [], []  # type: List[str], List[str]
        for rel in _iter_files(work):
            cur = os.path.join(self.root, rel)
            if not os.path.exists(cur):
                added.append(rel)
            elif not filecmp.cmp(cur, os.path.join(work, rel), shallow=False):
                changed.append(rel)
        return {"changed": changed, "added": added}

    @staticmethod
    def _report(diff: Dict[str, List[str]], emit: Emit) -> None:
        if not diff["changed"] and not diff["added"]:
            emit("ok", "synoarr est à jour")
            return
        emit("info", "Mise à jour disponible : %d fichier(s) modifié(s), %d nouveau(x)"
             % (len(diff["changed"]), len(diff["added"])))
        for rel in (diff["changed"] + diff["added"])[:30]:
            emit("info", "  " + rel)

    @staticmethod
    def _check_imports(code_dir: str, emit: Emit) -> None:
        """The new code must load with the Python running synoarr (e.g. DSM's Python 3.8)."""
        snippet = ("import sys; sys.path.insert(0, %r); import synoarr.cli, synoarr.app, synoarr.web, "
                   "synoarr.organizer, synoarr.searcher" % code_dir)
        try:
            out = subprocess.run([sys.executable, "-c", snippet], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                 timeout=60, check=False, cwd=code_dir)
        except (OSError, subprocess.SubprocessError) as exc:
            raise UpdateError("vérification de la nouvelle version impossible : %s" % exc)
        if out.returncode != 0:
            tail = out.stdout.decode("utf-8", "replace").strip().splitlines()[-3:]
            raise UpdateError("la nouvelle version ne démarre pas avec Python %s : %s — rien n'a été modifié"
                              % (sys.version.split()[0], " / ".join(tail)))
        emit("ok", "Nouvelle version vérifiée avec Python %s" % sys.version.split()[0])

    def _backup(self, files: List[str], emit: Emit) -> str:
        base = os.path.join(self.data_dir, "update-backups")
        dest = os.path.join(base, time.strftime("%Y%m%d-%H%M%S"))
        os.makedirs(dest, exist_ok=True)
        for rel in files:
            src = os.path.join(self.root, rel)
            if os.path.isfile(src):
                os.makedirs(os.path.dirname(os.path.join(dest, rel)) or dest, exist_ok=True)
                shutil.copy2(src, os.path.join(dest, rel))
        for old in sorted(os.listdir(base))[:-KEEP_BACKUPS]:
            shutil.rmtree(os.path.join(base, old), ignore_errors=True)
        return dest


def make_updater(cfg: Any, http: Optional[HttpClient] = None) -> Updater:
    repo = getattr(cfg.general, "update_repo", "") or DEFAULT_REPO
    return Updater(cfg.data_dir, http or HttpClient(timeout=120), repo=repo)
