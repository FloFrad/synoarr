"""« Test d'envoi Download Station » : sends a legal, heavily seeded torrent (latest Ubuntu Server ISO) to
Download Station the three ways synoarr can (original .torrent file, .torrent link, magnet link), watches each
task for a while, removes it, and reports which ways really start downloading on THIS NAS.

This separates "synoarr sends it wrong" from "Download Station / the network cannot reach peers".
"""
from __future__ import annotations

import logging
import re
import time
import urllib.parse
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Optional, Tuple

from .bencode import bdecode, torrent_info
from .httpclient import HttpClient, HttpError

if TYPE_CHECKING:  # pragma: no cover
    from .downloadstation import DownloadStation

log = logging.getLogger(__name__)

Emit = Callable[[str, str], None]
UBUNTU_RELEASES = "https://releases.ubuntu.com/"
METHODS = ("file", "url", "magnet")
LABELS = {"file": "fichier .torrent envoyé par synoarr", "url": "lien .torrent téléchargé par Download Station",
          "magnet": "lien magnet"}


def _version_key(text: str) -> Tuple[int, ...]:
    """"26.04" / "ubuntu-26.04.1-live-server-amd64.iso.torrent" -> (26, 4) / (26, 4, 1) (release number only)."""
    m = re.search(r"(\d+(?:\.\d+)+)", text)
    return tuple(int(x) for x in m.group(1).split(".")) if m else ()


def find_ubuntu_torrent(http: HttpClient, base: str = UBUNTU_RELEASES) -> str:
    """URL of the newest Ubuntu Server (else Desktop) ISO .torrent published on releases.ubuntu.com."""
    base = base.rstrip("/") + "/"
    listing = http.get(base, timeout=20).text()
    dirs = sorted({d for d in re.findall(r'href="(\d+\.\d+)/"', listing)}, key=_version_key, reverse=True)
    # LTS releases (YY.04 with an even year) first: the most seeded
    lts = [d for d in dirs if d.endswith(".04") and int(d.split(".")[0]) % 2 == 0]
    for d in (lts + [d for d in dirs if d not in lts])[:6]:
        try:
            page = http.get(base + d + "/", timeout=20).text()
        except HttpError:
            continue
        torrents = [t for t in re.findall(r'href="([^"/]+\.iso\.torrent)"', page)
                    if not re.search(r"beta|rc|snapshot|daily", t, re.I)]
        for kind in ("live-server-amd64", "desktop-amd64"):
            cands = sorted((t for t in torrents if kind in t), key=_version_key, reverse=True)
            if cands:
                return base + d + "/" + cands[0]
    raise ValueError("aucun fichier .torrent Ubuntu trouvé sur %s" % base)


def magnet_from_torrent(data: bytes) -> str:
    """Magnet link (hash, name, every tracker) built from a .torrent file."""
    name, info_hash = torrent_info(data)
    meta = bdecode(data)
    trackers: List[str] = []

    def add(t: Any) -> None:
        if isinstance(t, bytes):
            t = t.decode("utf-8", "replace")
        if isinstance(t, str) and t and t not in trackers:
            trackers.append(t)
    add(meta.get(b"announce"))
    for tier in meta.get(b"announce-list") or []:
        for t in tier if isinstance(tier, list) else [tier]:
            add(t)
    parts = ["xt=urn:btih:%s" % info_hash, "dn=%s" % urllib.parse.quote(name, safe="")]
    parts += ["tr=%s" % urllib.parse.quote(t, safe="") for t in trackers]
    return "magnet:?" + "&".join(parts)


def _find_task(ds: "DownloadStation", name: str, info_hash: str, before: set) -> Optional[Any]:
    for t in ds.list_tasks():
        if t.id in before:
            continue
        if (getattr(t, "info_hash", None) or "").lower() == info_hash or t.title == name or info_hash in t.title.lower():
            return t
    return None


def run(ds: "DownloadStation", http: HttpClient, emit: Emit, torrent_url: str = "", wait: float = 90.0,
        methods: Tuple[str, ...] = METHODS, poll: float = 5.0, sleep: Callable[[float], None] = time.sleep,
        ) -> Dict[str, Any]:
    """Run the test. Never leaves a test task behind (each one is deleted after its check)."""
    url = torrent_url.strip()
    if not url:
        emit("info", "Recherche du dernier fichier .torrent officiel d'Ubuntu (contenu libre, très partagé)…")
        url = find_ubuntu_torrent(http)
    emit("info", "Torrent de test : %s" % url)
    data = http.get(url, timeout=60).body
    name, info_hash = torrent_info(data)
    magnet = magnet_from_torrent(data)
    emit("info", "Nom : %s — empreinte : %s" % (name, info_hash))
    results = {}  # type: Dict[str, Dict[str, Any]]
    for method in methods:
        emit("info", "— Essai : %s" % LABELS[method])
        res = {"started": False, "api": None, "status": None, "peers": None, "error": None}
        results[method] = res
        try:
            before = {t.id for t in ds.list_tasks()}
            if method == "file":
                res["api"] = ds.add_torrent_file(data, name + ".torrent")
            elif method == "url":
                res["api"] = ds.add_url(url)
            else:
                res["api"] = ds.add_url(magnet)
        except Exception as exc:
            res["error"] = str(exc)
            emit("err", "Envoi refusé par Download Station : %s" % exc)
            continue
        emit("info", "Envoyé via %s, observation pendant %d s…" % (res["api"], wait))
        task = None
        deadline = time.time() + wait
        while time.time() < deadline:
            sleep(poll)
            try:
                task = _find_task(ds, name, info_hash, before) or task
            except Exception as exc:
                emit("warn", "Liste des tâches illisible : %s" % exc)
                continue
            if task is None:
                continue
            res["status"], res["peers"] = task.status, task.peers
            moving = (task.downloaded or 0) > 0 or (task.speed_down or 0) > 0 or (task.peers or 0) > 0 or \
                (task.seeders or 0) > 0
            if task.status in ("finished", "seeding") or (task.status == "downloading" and moving):
                res["started"] = True
                break
            if task.status == "error":
                res["error"] = task.error or "erreur"
                break
        if task is None:
            emit("err", "La tâche n'apparaît pas dans Download Station")
        else:
            emit("ok" if res["started"] else "err",
                 "%s : %s — statut %s, pairs %s, reçu %s octets" % (
                     LABELS[method], "DÉMARRE" if res["started"] else "NE DÉMARRE PAS", task.status,
                     task.peers if task.peers is not None else "?", task.downloaded or 0))
            try:
                ds.delete_task(task.id)
                emit("info", "Tâche de test supprimée")
            except Exception as exc:
                emit("warn", "Supprimez la tâche de test à la main dans Download Station (%s)" % exc)
    ok = [m for m in methods if results.get(m, {}).get("started")]
    if len(ok) == len(methods):
        emit("ok", "Tout fonctionne : synoarr envoie correctement les fichiers .torrent, les liens et les magnets. "
                   "Un téléchargement qui reste « En attente » n'a simplement pas de source disponible.")
    elif not ok:
        emit("err", "Aucune méthode ne démarre, même avec un torrent très partagé.")
        emit("info", "Ajoutez le même torrent à la main dans Download Station (ou envoyez une release via Prowlarr) :")
        emit("info", "• s'il démarre : c'est le COMPTE DSM de synoarr qui n'a pas assez de droits (cause la plus "
                     "fréquente). Mettez dans [downloadstation] username/password le même compte que vous ou que "
                     "Prowlarr (sans validation en 2 étapes), puis relancez ce test.")
        emit("info", "• s'il reste aussi en attente : Download Station n'arrive pas à joindre le réseau BitTorrent "
                     "(port BT bloqué par le pare-feu DSM ou la box, VPN, limite de vitesse à 0, planification).")
    else:
        bad = [LABELS[m] for m in methods if m not in ok]
        emit("warn", "Fonctionne : %s. Ne fonctionne pas : %s." % (", ".join(LABELS[m] for m in ok), ", ".join(bad)))
        if "file" in ok and "magnet" not in ok:
            emit("info", "Les magnets ne démarrent pas : vérifiez « Activer le réseau DHT » dans Download Station > "
                         "Paramètres > BitTorrent, et le port BT (UDP) dans le pare-feu.")
    return {"torrent": url, "name": name, "info_hash": info_hash, "results": results, "working": ok}
