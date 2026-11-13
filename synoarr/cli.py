"""Command line interface: python3 synoarr.py <commande> [options]."""
from __future__ import annotations

import argparse
import errno
import logging
import os
import shutil
import sqlite3
import sys
import tempfile
import time
from typing import List, Optional

from . import __version__
from .config import Config, ConfigError, load_config
from .httpclient import HttpError

log = logging.getLogger("synoarr")

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OK, KO, WARN = "[OK]  ", "[ERR] ", "[!]   "


def default_config_path() -> str:
    return os.environ.get("SYNOARR_CONFIG") or os.path.join(ROOT_DIR, "config.ini")


def _out(msg: str = "") -> None:
    try:
        print(msg, flush=True)
    except UnicodeEncodeError:  # terminal without UTF-8
        print(msg.encode("ascii", "replace").decode("ascii"), flush=True)


def _human(size: Optional[int]) -> str:
    if not size:
        return "?"
    s = float(size)
    for unit in ("o", "Ko", "Mo", "Go", "To"):
        if s < 1024 or unit == "To":
            return "%.1f %s" % (s, unit)
        s /= 1024
    return "?"  # pragma: no cover


def _load(args: argparse.Namespace) -> Config:
    cfg = load_config(args.config)
    if getattr(args, "dry_run", False):
        cfg.general.dry_run = True
    for w in getattr(cfg, "warnings", []) or []:
        _out(WARN + w)
    return cfg


def _not_root() -> bool:
    return hasattr(os, "geteuid") and os.geteuid() != 0


def _sudo_hint() -> str:
    if not _not_root():
        return ""
    return ("      Le dossier de données a sans doute été créé par root (démon lancé avec sudo ou tâche "
            "planifiée DSM en root) : relancez la commande avec sudo, ex. : sudo python3 synoarr.py "
            + " ".join(sys.argv[1:]))


def _check_data_dir(cfg: Config) -> Optional[str]:
    """None if the data folder and the database are writable, else a French explanation."""
    d = cfg.data_dir
    try:
        os.makedirs(d, exist_ok=True)
        fd, probe = tempfile.mkstemp(prefix=".synoarr-probe-", dir=d)
        os.close(fd)
        os.remove(probe)
    except OSError as exc:
        return "Dossier de données %s non accessible en écriture (%s)" % (d, exc.strerror or exc)
    for name in ("synoarr.db", "synoarr.db-wal", "synoarr.db-shm"):
        path = os.path.join(d, name)
        if os.path.exists(path) and not os.access(path, os.R_OK | os.W_OK):
            return "Base de données %s non accessible en écriture" % path
    return None


def _app(args: argparse.Namespace, console: bool = True):  # -> App
    from .app import App, setup_logging
    cfg = _load(args)
    if getattr(args, "verbose", False):
        cfg.general.log_level = "DEBUG"
    setup_logging(cfg, console=console)
    problems = cfg.validate()
    if problems:
        for p in problems:
            _out(KO + p)
        raise SystemExit(2)
    return App(cfg)


# ---------------------------------------------------------------- commands
def cmd_init(args: argparse.Namespace) -> int:
    target = args.config
    if os.path.exists(target):
        _out(WARN + "%s existe déjà, rien n'a été modifié." % target)
        return 0
    example = os.path.join(ROOT_DIR, "config.example.ini")
    os.makedirs(os.path.dirname(os.path.abspath(target)), exist_ok=True)
    shutil.copyfile(example, target)
    _out(OK + "Fichier créé : %s" % target)
    _out("      Éditez-le (identifiants Download Station, dossiers), puis lancez : python3 synoarr.py check")
    return 0


def cmd_check(args: argparse.Namespace) -> int:
    try:
        cfg = _load(args)
    except ConfigError as exc:
        _out(KO + str(exc))
        return 2
    errors = 0
    _out("synoarr %s — vérification de %s" % (__version__, cfg.path))
    _out("Python %s" % sys.version.split()[0])
    for p in cfg.validate():
        _out(KO + p)
        errors += 1

    # data folder (database, log): the most common first-run problem is a root-owned folder
    problem = _check_data_dir(cfg)
    if problem:
        _out(KO + problem)
        hint = _sudo_hint()
        if hint:
            _out(hint)
        _out("")
        _out("Impossible de poursuivre la vérification sans base de données.")
        return 1
    _out(OK + "Dossier de données : %s" % cfg.data_dir)

    # folders
    pc = cfg.paths
    if os.path.isdir(pc.download_dir) and os.access(pc.download_dir, os.R_OK | os.W_OK):
        _out(OK + "Dossier des téléchargements : %s" % pc.download_dir)
    else:
        _out(KO + "Dossier des téléchargements introuvable ou non accessible en écriture : %s" % pc.download_dir)
        errors += 1
    dest = cfg.downloadstation.destination
    norm_dl = os.path.normpath(pc.download_dir).replace("\\", "/")
    if dest and not norm_dl.lower().endswith("/" + dest.lower()):
        _out(WARN + "[downloadstation] destination = %r mais [paths] download_dir = %r : download_dir doit être "
             "le chemin du dossier où Download Station enregistre (ex. destination = Download ↔ download_dir = "
             "/volume1/Download)" % (dest, pc.download_dir))
    for label, d in (("Films", pc.movies_dir), ("Séries", pc.series_dir)):
        try:
            os.makedirs(d, exist_ok=True)
            fd, probe = tempfile.mkstemp(prefix=".synoarr-probe-", dir=d)
            os.close(fd)
            os.remove(probe)
            _out(OK + "Dossier %s accessible en écriture : %s" % (label, d))
        except OSError as exc:
            _out(KO + "Dossier %s non accessible en écriture : %s (%s)" % (label, d, exc))
            errors += 1
    if pc.action == "hardlink":
        try:
            if os.stat(pc.download_dir).st_dev != os.stat(pc.movies_dir).st_dev:
                _out(WARN + "action = hardlink mais téléchargements et vidéos sont sur des volumes différents : "
                     "une copie sera faite à la place")
        except OSError:
            pass

    from .app import App, setup_logging
    cfg.general.log_level = "WARNING"
    setup_logging(cfg, console=True)
    app = App(cfg)

    # Download Station
    if app.ds is None:
        _out(KO + "Download Station : [downloadstation] username/password non renseignés")
        errors += 1
    else:
        ok, msg = _safe_test(app.ds.test)
        _out((OK if ok else KO) + "Download Station : " + msg)
        errors += 0 if ok else 1

    # Plex
    if app.plex is None:
        _out(KO + "Plex : aucun jeton. Lancez : python3 synoarr.py plex-login")
        errors += 1
    else:
        ok, msg = _safe_test(app.plex.test)
        _out((OK if ok else KO) + "Plex : " + msg)
        errors += 0 if ok else 1
        if ok:
            try:
                items = app.plex.watchlist()
                _out(OK + "Watchlist Plex : %d élément(s)" % len(items))
            except Exception as exc:
                _out(KO + "Watchlist Plex illisible : %s" % exc)
                errors += 1
    if cfg.plex.server_url:
        _out("      Rafraîchissement Plex après import : %s" % cfg.plex.server_url)

    # TMDb
    _out((OK + "TMDb : clé configurée") if app.tmdb.enabled else
         (WARN + "TMDb : pas de clé (facultatif, améliore le renommage des fichiers inconnus)"))

    # Indexers
    rows = app.indexer_manager.list()
    if not rows:
        _out(KO + "Aucun indexer. Exemple : python3 synoarr.py indexer add internet_archive")
        errors += 1
    for row in rows:
        if not row["enabled"]:
            _out("      Indexer %s : désactivé" % row["name"])
            continue
        ok, msg = app.indexer_manager.test(row["id"])
        _out((OK if ok else KO) + "Indexer %s (%s) : %s" % (row["name"], row["type"], msg))
        errors += 0 if ok else 1

    _out("")
    if errors:
        _out("%d problème(s) à corriger avant de lancer synoarr." % errors)
        return 1
    _out("Tout est prêt. Lancez : python3 synoarr.py run   (ou scripts/start.sh)")
    return 0


def _safe_test(func) -> "tuple":
    try:
        ok, msg = func()
        return bool(ok), str(msg)
    except Exception as exc:  # a test must report, never crash the check
        return False, "erreur : %s" % exc


def cmd_plex_login(args: argparse.Namespace) -> int:
    app = _app(args)
    from .plex import PlexClient
    if app.cfg.plex.token:
        _out(WARN + "[plex] token est renseigné dans config.ini : il reste prioritaire sur le jeton obtenu ici. "
             "Videz cette ligne pour utiliser le nouveau jeton.")
    client = PlexClient("", app.plex_client_id, app.http)
    try:
        pin_id, code = client.start_pin_login()
    except HttpError as exc:
        _out(KO + "plex.tv injoignable : %s" % exc)
        _out("      Vérifiez la connexion Internet du NAS puis relancez la commande.")
        return 1
    _out("")
    _out("  1. Ouvrez https://plex.tv/link dans un navigateur (connecté à votre compte Plex)")
    _out("  2. Saisissez le code :  %s" % code)
    _out("")
    _out("En attente de la validation (5 minutes max)...")
    deadline = time.time() + 300
    while time.time() < deadline:
        try:
            token = client.check_pin(pin_id)
        except HttpError as exc:  # transient network error: keep polling until the deadline
            log.debug("Vérification du code Plex : %s", exc)
            token = None
        if token:
            app.save_plex_token(token)
            ok, msg = app.plex.test()
            _out((OK if ok else KO) + "Jeton Plex enregistré. " + msg)
            return 0 if ok else 1
        time.sleep(3)
    _out(KO + "Code non validé à temps. Relancez la commande.")
    return 1


def cmd_indexer(args: argparse.Namespace) -> int:
    app = _app(args)
    mgr = app.indexer_manager
    action = args.action
    try:
        if action == "list":
            rows = mgr.list()
            if not rows:
                _out("Aucun indexer. Voir : python3 synoarr.py indexer catalog")
            for r in rows:
                _out("%3d  %-25s %-17s %-8s %s" % (r["id"], r["name"], r["type"],
                                                    "actif" if r["enabled"] else "inactif", r["url"]))
        elif action == "catalog":
            _out("Indexers légaux disponibles (ajout : python3 synoarr.py indexer add <id>) :")
            for e in mgr.catalog():
                _out("  %-22s %-30s %s%s" % (e["id"], e["name"], e.get("description", ""),
                                             "  [installé]" if e["installed"] else ""))
            _out("Vous pouvez aussi ajouter n'importe quel flux Torznab ou RSS :")
            _out("  python3 synoarr.py indexer add-url \"Mon indexer\" https://exemple.org/api --api-key XXXX")
        elif action == "add":
            if not args.target:
                _out(KO + "Précisez l'identifiant du catalogue, ex. : indexer add internet_archive")
                return 2
            row = mgr.add_from_catalog(args.target)
            _out(OK + "Ajouté : %s" % row.get("name"))
            ok, msg = mgr.test(row["id"])
            _out((OK if ok else KO) + "Test : " + msg)
        elif action == "add-url":
            if not args.target or not args.url:
                _out(KO + "Usage : indexer add-url \"Nom\" URL [--api-key CLE] [--categories 2000,5000]")
                return 2
            row = mgr.add_url(args.target, args.url, args.api_key or "", args.categories or "",
                              type_=args.type)
            _out(OK + "Ajouté : %s (type %s)" % (row.get("name"), row.get("type")))
            ok, msg = mgr.test(row["id"])
            _out((OK if ok else KO) + "Test : " + msg)
        elif action == "test":
            rows = [mgr._row(args.target)] if args.target else mgr.list()
            for r in rows:
                ok, msg = mgr.test(r["id"])
                _out((OK if ok else KO) + "%s : %s" % (r["name"], msg))
        elif action in ("enable", "disable"):
            mgr.set_enabled(args.target, action == "enable")
            _out(OK + "Indexer %s %s" % (args.target, "activé" if action == "enable" else "désactivé"))
        elif action == "remove":
            mgr.delete(args.target)
            _out(OK + "Indexer %s supprimé" % args.target)
    except ValueError as exc:
        _out(KO + str(exc))
        return 1
    return 0


def cmd_watchlist(args: argparse.Namespace) -> int:
    app = _app(args)
    if not args.no_sync:
        _sync(app)
    for m in app.db.list_media():
        extra = ""
        if m["type"] == "show":
            eps = app.db.list_episodes(m["id"])
            done = sum(1 for e in eps if e["status"] == "done")
            grabbed = sum(1 for e in eps if e["status"] == "grabbed")
            wanted = sum(1 for e in eps if e["status"] == "wanted")
            extra = "  épisodes : %d importés, %d en cours, %d recherchés / %d" % (done, grabbed, wanted, len(eps))
        _out("%3d  %-6s %-40s %-6s %-10s%s%s" % (
            m["id"], "film" if m["type"] == "movie" else "série", m["title"][:40], m["year"] or "",
            m["status"], "" if m["on_watchlist"] else " (retiré)", extra))
    return 0


def _sync(app) -> None:
    try:
        app.searcher.sync_watchlist()
    except HttpError as exc:
        _out(WARN + "Watchlist Plex illisible (%s) : affichage des données déjà connues" % exc)


def cmd_search(args: argparse.Namespace) -> int:
    app = _app(args)
    if args.query:
        from .models import SearchRequest
        from .parser import parse
        p = parse(args.query)
        if p.episodes:
            req = SearchRequest("episode", p.title, season=p.season, episode=p.episode)
        elif p.seasons:
            req = SearchRequest("season", p.title, season=p.season)
        else:
            req = SearchRequest("movie", p.title, year=p.year)
        _out("Recherche : %s (%s)" % (req.describe(), req.kind))
        report = app.searcher.search_report(req)
        if not report:
            _out("Aucun résultat.")
        for rel, score, reason in report[:args.limit]:
            _out("%s %-70s %-10s S:%-4s %-16s %s" % (
                ("%5d" % score) if score is not None else "  rej", rel.title[:70], _human(rel.size),
                "?" if rel.seeders is None else rel.seeders, rel.indexer[:16], reason))
        return 0
    if not args.no_sync:
        _sync(app)
    stats = app.searcher.run_once(media_id=args.media, force=args.force)
    _out("%d recherche(s), %d envoi(s) vers Download Station" % (stats["searched"], stats["grabbed"]))
    return 0


def cmd_process(args: argparse.Namespace) -> int:
    app = _app(args)
    actions = app.organizer.run_once(dry_run=args.dry_run or app.cfg.general.dry_run)
    if not actions:
        _out("Rien à traiter dans %s" % app.cfg.paths.download_dir)
    for a in actions:
        _out("%-9s %s\n          -> %s %s" % (a.get("status"), a.get("src"), a.get("dst") or "",
                                             ("(" + a["message"] + ")") if a.get("message") else ""))
    return 0


def cmd_once(args: argparse.Namespace) -> int:
    app = _app(args)
    app.run_once()
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    app = _app(args)
    app.run_forever()
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    app = _app(args, console=False)
    for g in app.db.list_grabs(limit=args.limit):
        _out("%s  %-9s %s" % (g["created_at"], g["status"], g["release_title"]))
    _out("")
    for e in reversed(app.db.recent_events(args.limit)):
        _out("%s  %-7s %-10s %s" % (e["ts"], e["level"], e["kind"], e["message"]))
    return 0


# ------------------------------------------------------------------ parser
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="synoarr", description="Watchlist Plex → indexers → Download Station → "
                                "renommage Plex. Toutes les commandes acceptent --config.")
    p.add_argument("--version", action="version", version="synoarr " + __version__)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--config", "-c", default=default_config_path(),
                        help="chemin du config.ini (défaut : %(default)s)")
    common.add_argument("--dry-run", action="store_true", help="simulation : rien n'est envoyé ni déplacé")
    common.add_argument("--verbose", "-v", action="store_true", help="journal détaillé (DEBUG)")
    sub = p.add_subparsers(dest="command")

    sub.add_parser("init", parents=[common], help="créer config.ini à partir de config.example.ini") \
        .set_defaults(func=cmd_init)
    sub.add_parser("check", parents=[common], help="vérifier la configuration et toutes les connexions") \
        .set_defaults(func=cmd_check)
    sub.add_parser("plex-login", parents=[common], help="connecter votre compte Plex (code sur plex.tv/link)") \
        .set_defaults(func=cmd_plex_login)

    pi = sub.add_parser("indexer", parents=[common], help="gérer les indexers")
    pi.add_argument("action", choices=["list", "catalog", "add", "add-url", "test", "enable", "disable", "remove"])
    pi.add_argument("target", nargs="?", help="id du catalogue, nom ou numéro de l'indexer")
    pi.add_argument("url", nargs="?", help="URL (pour add-url)")
    pi.add_argument("--api-key", default="")
    pi.add_argument("--categories", default="", help="catégories Torznab, ex. 2000,5000")
    pi.add_argument("--type", choices=["torznab", "rss", "internet_archive"], default=None,
                    help="forcer le type (détecté automatiquement sinon)")
    pi.set_defaults(func=cmd_indexer)

    pw = sub.add_parser("watchlist", parents=[common], help="synchroniser et afficher la Watchlist Plex")
    pw.add_argument("--no-sync", action="store_true")
    pw.set_defaults(func=cmd_watchlist)

    ps = sub.add_parser("search", parents=[common],
                        help="rechercher maintenant (tout, --media ID, ou une requête libre pour tester)")
    ps.add_argument("query", nargs="?", help="requête libre, ex. \"Night of the Living Dead 1968\" (n'envoie rien)")
    ps.add_argument("--media", type=int, help="id d'un élément (voir watchlist)")
    ps.add_argument("--force", action="store_true", help="ignorer le délai entre deux recherches")
    ps.add_argument("--no-sync", action="store_true")
    ps.add_argument("--limit", type=int, default=30)
    ps.set_defaults(func=cmd_search)

    sub.add_parser("process", parents=[common], help="renommer/déplacer les téléchargements terminés") \
        .set_defaults(func=cmd_process)
    sub.add_parser("once", parents=[common], help="un cycle complet puis quitter (mode tâche planifiée)") \
        .set_defaults(func=cmd_once)
    sub.add_parser("run", parents=[common], help="lancer le démon (+ interface web)").set_defaults(func=cmd_run)
    pst = sub.add_parser("status", parents=[common], help="derniers envois et événements")
    pst.add_argument("--limit", type=int, default=20)
    pst.set_defaults(func=cmd_status)
    return p


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "command", None):
        parser.print_help()
        return 0
    try:
        return int(args.func(args) or 0)
    except ConfigError as exc:
        _out(KO + str(exc))
        if "introuvable" in str(exc):
            _out("      Créez-le avec : python3 synoarr.py init")
        return 2
    except KeyboardInterrupt:
        return 130
    except SystemExit:
        raise
    except Exception as exc:  # never a traceback for the user: a French explanation + hint
        return _report_error(exc, args)


def _report_error(exc: BaseException, args: argparse.Namespace) -> int:
    from .downloadstation import DownloadStationError
    log.debug("Détail de l'erreur", exc_info=True)
    text = str(exc)
    low = text.lower()
    if isinstance(exc, sqlite3.Error):
        if any(k in low for k in ("readonly", "read-only", "unable to open", "permission", "disk i/o")):
            _out(KO + "Base de données de synoarr non accessible en écriture (%s)." % text)
            hint = _sudo_hint()
            _out(hint or "      Vérifiez les droits du dossier de données ([general] data_dir).")
        elif "locked" in low:
            _out(KO + "Base de données occupée par un autre processus synoarr (%s). Réessayez dans un instant."
                 % text)
        elif "malformed" in low or "not a database" in low:
            _out(KO + "Base de données endommagée (%s). Arrêtez synoarr et renommez data/synoarr.db pour "
                 "repartir de zéro (les indexers et la Watchlist seront à recréer)." % text)
        else:
            _out(KO + "Erreur de base de données : %s" % text)
        return 1
    if isinstance(exc, PermissionError) or (isinstance(exc, OSError) and exc.errno in (errno.EACCES, errno.EPERM,
                                                                                          errno.EROFS)):
        _out(KO + "Permission refusée : %s" % exc)
        _out(_sudo_hint() or "      Vérifiez les droits des dossiers configurés.")
        return 1
    if isinstance(exc, DownloadStationError):
        _out(KO + "Download Station : %s" % text)
        return 1
    if isinstance(exc, HttpError):
        _out(KO + "Erreur réseau : %s" % text)
        _out("      Vérifiez la connexion du NAS et les adresses dans config.ini (python3 synoarr.py check).")
        return 1
    if isinstance(exc, OSError):
        _out(KO + "Erreur système : %s" % exc)
        return 1
    _out(KO + "Erreur inattendue : %s: %s" % (type(exc).__name__, text))
    if getattr(args, "verbose", False):
        import traceback
        traceback.print_exc()
    else:
        _out("      Relancez avec --verbose pour le détail technique (à joindre à un rapport de bug).")
    return 1
