"""Command line interface: python3 synoarr.py <commande> [options]."""
from __future__ import annotations

import argparse
import errno
import logging
import os
import sqlite3
import sys
import tempfile
import time
from typing import Any, Callable, List, Optional

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


def _in_docker() -> bool:
    return os.path.exists("/.dockerenv")


def _command(rest: str = "") -> str:
    """How the user should run a synoarr command here ("sudo python3 synoarr.py check"...)."""
    rest = rest if rest else " ".join(sys.argv[1:])
    if _in_docker():
        return "docker exec -it synoarr python3 /app/synoarr.py %s" % rest
    return "sudo python3 synoarr.py %s" % rest


def _sudo_hint() -> str:
    if not _not_root() or _in_docker():
        return ""
    return ("      Le dossier de données a sans doute été créé par root (démon lancé avec sudo ou tâche "
            "planifiée DSM en root). Relancez la commande avec sudo : " + _command())


def _chown_to_sudo_user(path: str) -> None:
    """Under sudo, give a created file/folder back to the real user (DSM Text Editor can then save it)."""
    uid, gid = os.environ.get("SUDO_UID"), os.environ.get("SUDO_GID")
    if not uid or not hasattr(os, "chown") or _not_root():
        return
    try:
        os.chown(path, int(uid), int(gid or -1))
    except (OSError, ValueError) as exc:
        log.debug("chown %s impossible : %s", path, exc)


def _share_problem(path: str) -> Optional[str]:
    from .config import missing_share_root
    root = missing_share_root(path)
    if root:
        return ("Dossier partagé %s introuvable (File Station > clic droit > Propriétés > Emplacement) : "
                "corrigez le chemin %s" % (root, path))
    return None


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
def write_initial_config(target: str) -> str:
    """Copy config.example.ini to *target* with a random web UI password. Returns that password."""
    import re as _re
    import secrets
    with open(os.path.join(ROOT_DIR, "config.example.ini"), "r", encoding="utf-8") as fh:
        text = fh.read()
    password = secrets.token_urlsafe(9)
    head, sep, web = text.partition("[web]")
    if sep:
        web = _re.sub(r"(?m)^username\s*=\s*$", "username = admin", web, count=1)
        web = _re.sub(r"(?m)^password\s*=\s*$", "password = " + password, web, count=1)
        text = head + sep + web
    with open(target, "w", encoding="utf-8") as fh:
        fh.write(text)
    return password


def cmd_init(args: argparse.Namespace) -> int:
    target = args.config
    if os.path.exists(target):
        _out(WARN + "%s existe déjà, rien n'a été modifié." % target)
        return 0
    example = os.path.join(ROOT_DIR, "config.example.ini")
    folder = os.path.dirname(os.path.abspath(target))
    created_folder = not os.path.isdir(folder)
    os.makedirs(folder, exist_ok=True)
    if created_folder:
        _chown_to_sudo_user(folder)
    if not os.path.isfile(example):
        _out(KO + "config.example.ini introuvable dans %s" % ROOT_DIR)
        return 2
    password = write_initial_config(target)
    _chown_to_sudo_user(target)
    _out(OK + "Fichier créé : %s" % target)
    _out("      Interface web protégée : utilisateur « admin », mot de passe « %s »" % password)
    _out("      (modifiable dans [web] username / password)")
    _out("      Éditez-le (identifiants Download Station, dossiers), puis lancez : " + _command("check"))
    return 0


def cmd_check(args: argparse.Namespace) -> int:
    try:
        cfg = _load(args)
    except ConfigError as exc:
        _out(KO + str(exc))
        if "introuvable" in str(exc):
            _out("      Créez-le avec : " + _command("init"))
        return 2
    return check_report(cfg, lambda level, msg: _out(_PREFIX[level] + msg))


_PREFIX = {"ok": OK, "err": KO, "warn": WARN, "info": ""}


def check_report(cfg: Config, emit: Callable[[str, str], None], app: Any = None,
                 hint: Optional[Callable[[str], str]] = None) -> int:
    """Vérification complète (commande ``check`` et interface web).

    *emit(level, message)* reçoit chaque ligne, level ∈ "ok" | "err" | "warn" | "info".
    *app* : App déjà construite (interface web, démon) ; None = en créer une (CLI).
    *hint(commande)* : comment l'utilisateur lance une commande (CLI : « sudo python3 synoarr.py … »).
    Retourne le code de sortie de la commande (0 = tout est prêt, 1 = problème(s))."""
    hint = hint or _cli_hint
    errors = 0
    emit("info", "synoarr %s — vérification de %s" % (__version__, cfg.path))
    emit("info", "Python %s" % sys.version.split()[0])
    for p in cfg.validate():
        emit("err", p)
        errors += 1

    # data folder (database, log): the most common first-run problem is a root-owned folder
    problem = _check_data_dir(cfg)
    if problem:
        emit("err", problem)
        sudo = _sudo_hint() if app is None else ""
        if sudo:
            emit("info", sudo)
        emit("info", "")
        emit("info", "Impossible de poursuivre la vérification sans base de données.")
        return 1
    emit("ok", "Dossier de données : %s" % cfg.data_dir)

    # folders
    pc = cfg.paths
    if os.path.isdir(pc.download_dir) and os.access(pc.download_dir, os.R_OK | os.W_OK):
        emit("ok", "Dossier des téléchargements : %s" % pc.download_dir)
    else:
        emit("err", "Dossier des téléchargements introuvable ou non accessible en écriture : %s" % pc.download_dir)
        errors += 1
    dest = cfg.downloadstation.destination
    norm_dl = os.path.normpath(pc.download_dir).replace("\\", "/")
    if dest and not norm_dl.lower().endswith("/" + dest.lower()):
        emit("warn", "[downloadstation] destination = %r mais [paths] download_dir = %r : download_dir doit être "
             "le chemin du dossier où Download Station enregistre (ex. destination = Download ↔ download_dir = "
             "/volume1/Download)" % (dest, pc.download_dir))
    for label, d in (("Films", pc.movies_dir), ("Séries", pc.series_dir)):
        share = _share_problem(d)
        if share:  # never create a share root: a typo ("Videos" for "Vidéos") would create a hidden folder
            emit("err", share)
            errors += 1
            continue
        try:
            os.makedirs(d, exist_ok=True)
            fd, probe = tempfile.mkstemp(prefix=".synoarr-probe-", dir=d)
            os.close(fd)
            os.remove(probe)
            emit("ok", "Dossier %s accessible en écriture : %s" % (label, d))
        except OSError as exc:
            emit("err", "Dossier %s non accessible en écriture : %s (%s)" % (label, d, exc))
            errors += 1
    if pc.action == "hardlink":
        try:
            if os.stat(pc.download_dir).st_dev != os.stat(pc.movies_dir).st_dev:
                emit("warn", "action = hardlink mais téléchargements et vidéos sont sur des volumes différents : "
                     "une copie sera faite à la place")
        except OSError:
            pass

    if app is None:
        from .app import App, setup_logging
        cfg.general.log_level = "WARNING"
        setup_logging(cfg, console=True)
        app = App(cfg)

    # Download Station
    if app.ds is None:
        emit("err", "Download Station : [downloadstation] username/password non renseignés")
        errors += 1
    elif not cfg.downloadstation.password:
        emit("err", "Download Station : [downloadstation] password est vide")
        errors += 1
    else:
        ok, msg = _safe_test(app.ds.test)
        emit("ok" if ok else "err", "Download Station : " + msg)
        errors += 0 if ok else 1

    # Plex
    if app.plex is None:
        emit("err", "Plex : aucun jeton. Lancez : " + hint("plex-login"))
        errors += 1
    else:
        ok, msg = _safe_test(app.plex.test)
        emit("ok" if ok else "err", "Plex : " + msg)
        errors += 0 if ok else 1
        if ok:
            try:
                items = app.plex.watchlist()
                emit("ok", "Watchlist Plex : %d élément(s)" % len(items))
            except Exception as exc:
                emit("err", "Watchlist Plex illisible : %s" % exc)
                errors += 1
    if cfg.plex.server_url:
        emit("info", "      Rafraîchissement Plex après import : %s" % cfg.plex.server_url)

    # TMDb
    if app.tmdb.enabled:
        emit("ok", "TMDb : clé configurée")
    else:
        emit("warn", "TMDb : pas de clé (facultatif, améliore le renommage des fichiers inconnus)")

    # Indexers
    rows = app.indexer_manager.list()
    if not rows:
        emit("err", "Aucun indexer. Exemple : " + hint("indexer add internet_archive"))
        errors += 1
    for row in rows:
        if not row["enabled"]:
            emit("info", "      Indexer %s : désactivé" % row["name"])
            continue
        ok, msg = app.indexer_manager.test(row["id"])
        emit("ok" if ok else "err", "Indexer %s (%s) : %s" % (row["name"], row["type"], msg))
        errors += 0 if ok else 1

    emit("info", "")
    if errors:
        emit("info", "%d problème(s) à corriger avant de lancer synoarr." % errors)
        return 1
    emit("info", "Tout est prêt. Lancez : " + hint("run"))
    return 0


def _cli_hint(command: str) -> str:
    if command == "run":
        return "docker restart synoarr" if _in_docker() else "scripts/start.sh (ou " + _command("run") + ")"
    return _command(command)


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
                _out("Aucun indexer. Voir : " + _command("indexer catalog"))
            else:
                _out("%3s  %-25s %-17s %-8s %-5s %s" % ("id", "nom", "type", "état", "prio", "url"))
            from .httpclient import redact
            for r in rows:
                _out("%3d  %-25s %-17s %-8s %-5s %s" % (r["id"], r["name"], r["type"],
                                                         "actif" if r["enabled"] else "inactif",
                                                         r.get("priority") if r.get("priority") is not None else 25,
                                                         redact(r["url"])))
            if rows:
                _out("Priorité : 1 = le plus prioritaire, 50 = le moins (modifier : %s)"
                     % _command("indexer priority <id|nom> <1-50>"))
        elif action == "catalog":
            _out("Indexers légaux disponibles (ajout : %s) :" % _command("indexer add <id>"))
            for e in mgr.catalog():
                _out("  %-22s %-30s %s%s" % (e["id"], e["name"], e.get("description", ""),
                                             "  [installé]" if e["installed"] else ""))
            _out("Vous pouvez aussi ajouter n'importe quel flux Torznab ou RSS :")
            _out("  " + _command("indexer add-url \"Mon indexer\" https://exemple.org/api --api-key XXXX"))
        elif action == "add":
            if not args.target:
                _out(KO + "Précisez l'identifiant du catalogue, ex. : indexer add internet_archive")
                return 2
            row = mgr.add_from_catalog(args.target)
            _out(OK + "Ajouté : %s" % row.get("name"))
            ok, msg = mgr.test(row["id"])
            _out((OK if ok else KO) + "Test : " + msg)
            if not ok:
                return 1
        elif action == "add-url":
            if not args.target or not args.url:
                _out(KO + "Usage : indexer add-url \"Nom\" URL [--api-key CLE] [--categories 2000,5000]")
                return 2
            row = mgr.add_url(args.target, args.url, args.api_key or "", args.categories or "",
                              type_=args.type, priority=args.priority)
            _out(OK + "Ajouté : %s (type %s, priorité %s)" % (row.get("name"), row.get("type"), row.get("priority")))
            ok, msg = mgr.test(row["id"])
            _out((OK if ok else KO) + "Test : " + msg)
            if not ok:
                return 1
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
        elif action == "import-prowlarr":
            if not args.target:
                _out(KO + "Usage : indexer import-prowlarr http://127.0.0.1:9696 --api-key CLE")
                return 2
            res = mgr.import_prowlarr(args.target, args.api_key or "")
            _out(OK + res["message"])
            for label, names in (("ajouté", res["added"]), ("mis à jour", res["updated"])):
                for n in names:
                    _out("      %s : %s" % (label, n))
            for n in res["skipped"]:
                _out(WARN + "ignoré : " + n)
            for n in res.get("disabled") or []:
                _out(WARN + "désactivé (absent ou désactivé dans Prowlarr) : " + n)
            _out("      Synchronisation automatique activée (toutes les %s h)." % app.cfg.prowlarr.sync_interval_hours)
        elif action == "prowlarr-sync":
            if not mgr.prowlarr_settings(app.cfg)["configured"]:
                _out(KO + "Prowlarr n'est pas configuré : lancez d'abord indexer import-prowlarr")
                return 1
            res = mgr.sync_prowlarr(app.cfg) or {}
            _out((KO if res.get("ok") is False else OK) + str(res.get("message")))
            return 1 if res.get("ok") is False else 0
        elif action == "import-jackett":
            if not args.target:
                _out(KO + "Usage : indexer import-jackett http://127.0.0.1:9117 --api-key CLE")
                return 2
            res = mgr.import_jackett(args.target, args.api_key or "")
            _out(OK + res["message"])
            for label, names in (("ajouté", res["added"]), ("mis à jour", res["updated"])):
                for n in names:
                    _out("      %s : %s" % (label, n))
            for n in res["skipped"]:
                _out(WARN + "ignoré : " + n)
            for n in res.get("disabled") or []:
                _out(WARN + "désactivé (absent de Jackett) : " + n)
            _out("      Synchronisation automatique activée (toutes les %s h)." % app.cfg.prowlarr.sync_interval_hours)
        elif action == "jackett-sync":
            if not mgr.jackett_settings(app.cfg)["configured"]:
                _out(KO + "Jackett n'est pas configuré : lancez d'abord indexer import-jackett")
                return 1
            res = mgr.sync_jackett(app.cfg) or {}
            _out((KO if res.get("ok") is False else OK) + str(res.get("message")))
            return 1 if res.get("ok") is False else 0
        elif action == "jackett-forget":
            mgr.forget_jackett()
            _out(OK + "Synchronisation automatique avec Jackett arrêtée (indexers déjà importés gardés)")
        elif action == "prowlarr-forget":
            mgr.forget_prowlarr()
            _out(OK + "Synchronisation automatique avec Prowlarr arrêtée (indexers déjà importés gardés)")
        elif action == "priority":
            if not args.target or args.url is None:
                _out(KO + "Usage : indexer priority <id|nom> <1-50>  (1 = le plus prioritaire)")
                return 2
            row = mgr.set_priority(args.target, args.url)
            _out(OK + "Priorité de l'indexer %s : %s" % (row.get("name"), row.get("priority")))
    except ValueError as exc:
        _out(KO + str(exc))
        return 1
    return 0


def cmd_watchlist(args: argparse.Namespace) -> int:
    action = getattr(args, "action", None) or "list"
    if action in ("add", "remove"):
        return _watchlist_edit(args, action)
    app = _app(args)
    if not args.no_sync:
        _sync(app)
    medias = app.db.list_media()
    if not medias:
        _out("Watchlist vide (ou pas encore synchronisée).")
    for m in medias:
        extra = ""
        if m["type"] == "show":
            eps = app.db.list_episodes(m["id"])
            done = sum(1 for e in eps if e["status"] == "done")
            grabbed = sum(1 for e in eps if e["status"] == "grabbed")
            wanted = sum(1 for e in eps if e["status"] == "wanted")
            extra = "  épisodes : %d importés, %d en cours, %d recherchés / %d" % (done, grabbed, wanted, len(eps))
        _out("%3d  %-6s %-40s %-6s %-10s%s%s" % (
            m["id"], "film" if m["type"] == "movie" else "série", m["title"][:40], m["year"] or "",
            m["status"], " (local)" if str(m["plex_key"]).startswith("local:") else
            "" if m["on_watchlist"] else " (retiré)", extra))
    return 0


# ---- watchlist add/delete
def _watchlist_edit(args: argparse.Namespace, action: str) -> int:
    app = _app(args)
    _quiet_console(args)
    try:
        if action == "remove":
            if not args.target or not str(args.target).isdigit():
                _out(KO + "Usage : watchlist remove <id> [--keep-plex] [--delete-downloads] (id : voir « watchlist »)")
                return 2
            media = app.db.get_media(int(args.target))
            res = app.remove_from_watchlist(int(args.target), remove_from_plex=not args.keep_plex,
                                            delete_downloads=args.delete_downloads)
            title = media["title"] if media else args.target
            if res.get("plex_removed"):
                _out(OK + "« %s » retiré de synoarr et de la Watchlist Plex" % title)
            else:
                _out(OK + "« %s » retiré de la liste de synoarr" % title)
            if args.delete_downloads:
                _out(OK + "%d téléchargement(s) supprimé(s) de Download Station" % res.get("downloads_deleted", 0))
            if res.get("warning"):
                _out(WARN + res["warning"])
            return 0
        # add
        if not args.target:
            _out(KO + "Usage : watchlist add \"Titre\" [--year 1999] [--type movie|show] [--local] [--pick N]")
            return 2
        if args.manual:
            if not args.type:
                _out(KO + "--manual nécessite --type movie ou --type show")
                return 2
            row = app.add_manual(args.type, args.target, args.year, imdb=args.imdb, tmdb=args.tmdb)
        else:
            found = app.search_titles(args.target, args.type, args.year)
            for w in found["warnings"]:
                _out(WARN + w)
            results = found["results"]
            if not results:
                _out(KO + "Aucun résultat pour « %s ». Ajout sans recherche : watchlist add \"%s\" --manual "
                     "--type movie|show [--year AAAA] [--imdb tt...]" % (args.target, args.target))
                return 1
            for i, c in enumerate(results, 1):
                ids = ", ".join("%s %s" % (k, c[k]) for k in ("imdb", "tmdb", "tvdb") if c.get(k))
                _out("%3d  %-6s %-45s %-6s %-5s%s%s" % (
                    i, "film" if c["type"] == "movie" else "série", c["title"][:45], c.get("year") or "",
                    c["source"], "  [déjà suivi]" if c.get("in_watchlist") else "", ("  (" + ids + ")") if ids else ""))
            pick = args.pick
            if pick is None:
                if not sys.stdin.isatty():
                    _out(KO + "Précisez le numéro à ajouter avec --pick N")
                    return 2
                try:
                    answer = input("Numéro à ajouter (Entrée = annuler) : ").strip()
                except EOFError:
                    answer = ""
                if not answer:
                    _out("Annulé.")
                    return 0
                if not answer.isdigit():
                    _out(KO + "Numéro invalide : %s" % answer)
                    return 2
                pick = int(answer)
            if not 1 <= pick <= len(results):
                _out(KO + "Numéro invalide : %s (1 à %d)" % (pick, len(results)))
                return 2
            row = app.add_to_watchlist(results[pick - 1], add_to_plex=not args.local)
        local = str(row["plex_key"]).startswith("local:")
        what = "entrée locale" if local else "Watchlist Plex"
        verb = ("ajouté" if not row.get("already_present") else
                "déjà connu, réactivé" if row.get("reactivated") else "déjà dans la liste")
        _out(OK + "« %s »%s %s (%s, id %d)" % (row["title"], " (%s)" % row["year"] if row.get("year") else "",
                                              verb, what, row["id"]))
        if row.get("warning"):
            _out(WARN + row["warning"])
        if args.search:
            stats = app.searcher.run_once(media_id=row["id"], force=True)
            _out("%d recherche(s), %d envoi(s) vers Download Station" % (stats["searched"], stats["grabbed"]))
        else:
            _out("      Rechercher maintenant : " + _command("search --media %d" % row["id"]))
        return 0
    except ValueError as exc:
        _out(KO + str(exc))
        return 1
# ---- end watchlist add/delete


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


def _quiet_console(args: argparse.Namespace) -> None:
    """The command prints its own report: console log lines would only duplicate it."""
    if getattr(args, "verbose", False):
        return
    for h in logging.getLogger().handlers:
        if type(h) is logging.StreamHandler:
            h.setLevel(logging.WARNING)


def cmd_process(args: argparse.Namespace) -> int:
    app = _app(args)
    _quiet_console(args)
    actions = app.organizer.run_once(dry_run=args.dry_run or app.cfg.general.dry_run)
    if not actions:
        _out("Rien à traiter dans %s" % app.cfg.paths.download_dir)
    for a in actions:
        _out("%-9s %s\n          -> %s %s" % (a.get("status"), a.get("src"), a.get("dst") or "",
                                             ("(" + a["message"] + ")") if a.get("message") else ""))
    return 0


def cmd_update(args: argparse.Namespace) -> int:
    """Self-update from GitHub (same code as the web UI). Restart the daemon afterwards (scripts/restart.sh)."""
    from .updater import UpdateError, make_updater
    cfg = _load(args)
    upd = make_updater(cfg)
    emit = lambda level, msg: _out(_PREFIX.get(level, "") + msg)  # noqa: E731
    try:
        if args.check:
            res = upd.check(emit)
        elif args.rollback:
            res = upd.rollback(emit)
            res["updated"] = True
        else:
            res = upd.apply(emit)
    except UpdateError as exc:
        _out(KO + str(exc))
        return 1
    if res.get("updated"):
        _out("      Redémarrez synoarr : sudo sh scripts/restart.sh (ou bouton « Redémarrer » de l'interface web)")
    return 0


def cmd_ds_diag(args: argparse.Namespace) -> int:
    """Why do Download Station tasks stay waiting? (same as the web UI command)."""
    app = _app(args)

    class _Out(object):
        private = {}  # type: dict

        @staticmethod
        def emit(level: str, msg: str) -> None:
            _out(_PREFIX.get(level, "") + msg)
    res = app._cmd_ds_diag(_Out())
    return 0 if res.get("ok", True) else 1


def cmd_once(args: argparse.Namespace) -> int:
    app = _app(args)
    app.run_once()
    return 0


def _wait_for_docker_config(args: argparse.Namespace) -> bool:
    """Docker first run: create config.ini from the example and wait until the user edits it.
    Returns True if it waited (the configuration then exists)."""
    target = args.config
    folder = os.path.dirname(os.path.abspath(target))
    if os.path.exists(target) or not _in_docker() or not os.path.isdir(folder):
        return False
    password = write_initial_config(target)
    mtime = os.path.getmtime(target)
    _out(OK + "Interface web protégée : utilisateur « admin », mot de passe « %s » ([web] dans config.ini)"
         % password)
    msg = ("Configuration créée : éditez %s (config/config.ini sur le NAS) ; synoarr démarrera dès qu'il sera "
           "enregistré (ou redémarrez le conteneur)." % target)
    _out(WARN + msg)
    last_msg = time.time()
    while True:
        time.sleep(_DOCKER_POLL)
        try:
            if os.path.getmtime(target) != mtime:
                _out(OK + "Configuration modifiée : démarrage")
                time.sleep(min(2, _DOCKER_POLL))  # let the editor finish writing
                return True
        except OSError:
            pass
        if time.time() - last_msg >= 600:
            _out(WARN + msg)
            last_msg = time.time()


_DOCKER_POLL = 10.0


def cmd_run(args: argparse.Namespace) -> int:
    _wait_for_docker_config(args)
    from .app import App, setup_logging
    cfg = _load(args)
    if getattr(args, "verbose", False):
        cfg.general.log_level = "DEBUG"
    # started in the background (start.sh redirects stdout to a file): the log file is enough
    setup_logging(cfg, console=sys.stderr.isatty() or _in_docker())
    problems = cfg.validate()
    if problems:
        for p in problems:
            _out(KO + p)
        return 2
    App(cfg).run_forever()
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
    # accepted before ("synoarr --config x check") or after the command: the sub-command only overrides
    # the value when the option is given there (SUPPRESS), defaults live on the main parser
    p.add_argument("--config", "-c", default=default_config_path(),
                   help="chemin du config.ini (défaut : %(default)s)")
    p.add_argument("--dry-run", action="store_true", help="simulation : rien n'est envoyé ni déplacé")
    p.add_argument("--verbose", "-v", action="store_true", help="journal détaillé (DEBUG)")
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--config", "-c", default=argparse.SUPPRESS,
                        help="chemin du config.ini (défaut : %s)" % default_config_path())
    common.add_argument("--dry-run", action="store_true", default=argparse.SUPPRESS,
                        help="simulation : rien n'est envoyé ni déplacé")
    common.add_argument("--verbose", "-v", action="store_true", default=argparse.SUPPRESS,
                        help="journal détaillé (DEBUG)")
    sub = p.add_subparsers(dest="command")

    sub.add_parser("init", parents=[common], help="créer config.ini à partir de config.example.ini") \
        .set_defaults(func=cmd_init)
    sub.add_parser("check", parents=[common], help="vérifier la configuration et toutes les connexions") \
        .set_defaults(func=cmd_check)
    sub.add_parser("plex-login", parents=[common], help="connecter votre compte Plex (code sur plex.tv/link)") \
        .set_defaults(func=cmd_plex_login)

    pi = sub.add_parser("indexer", parents=[common], help="gérer les indexers")
    pi.add_argument("action", choices=["list", "catalog", "add", "add-url", "test", "enable", "disable", "remove",
                                       "priority", "import-prowlarr", "prowlarr-sync", "prowlarr-forget", "import-jackett", "jackett-sync", "jackett-forget"])
    pi.add_argument("target", nargs="?", help="id du catalogue, nom ou numéro de l'indexer (import-prowlarr : "
                                              "adresse de Prowlarr, ex. http://127.0.0.1:9696)")
    pi.add_argument("url", nargs="?", help="URL (pour add-url) ou priorité 1-50 (pour priority)")
    pi.add_argument("--priority", default=None,
                    help="priorité pour add-url : 1 (le plus prioritaire) à 50, défaut 25")
    pi.add_argument("--api-key", default="")
    pi.add_argument("--categories", default="", help="catégories Torznab, ex. 2000,5000")
    pi.add_argument("--type", choices=["torznab", "rss", "internet_archive"], default=None,
                    help="forcer le type (détecté automatiquement sinon)")
    pi.set_defaults(func=cmd_indexer)

    pw = sub.add_parser("watchlist", parents=[common],
                        help="afficher la Watchlist (list), ajouter (add \"Titre\") ou retirer (remove ID)")
    pw.add_argument("action", nargs="?", choices=["list", "add", "remove"], default="list")
    pw.add_argument("target", nargs="?", help="titre à rechercher (add) ou id de l'élément (remove)")
    pw.add_argument("--no-sync", action="store_true", help="list : ne pas relire la Watchlist Plex")
    pw.add_argument("--year", type=int, default=None, help="add : année")
    pw.add_argument("--type", choices=["movie", "show"], default=None, help="add : film (movie) ou série (show)")
    pw.add_argument("--local", action="store_true",
                    help="add : entrée locale à synoarr, sans l'ajouter à la Watchlist Plex")
    pw.add_argument("--pick", type=int, default=None, help="add : numéro du résultat à ajouter (sans question)")
    pw.add_argument("--manual", action="store_true", help="add : ajout sans recherche (entrée locale, --type requis)")
    pw.add_argument("--imdb", default=None, help="add --manual : identifiant IMDb (tt...)")
    pw.add_argument("--tmdb", default=None, help="add --manual : identifiant TMDb")
    pw.add_argument("--search", action="store_true", help="add : lancer la recherche aussitôt après l'ajout")
    pw.add_argument("--keep-plex", action="store_true",
                    help="remove : laisser l'élément dans la Watchlist Plex (il est alors masqué)")
    pw.add_argument("--delete-downloads", action="store_true",
                    help="remove : supprimer aussi ses téléchargements en cours dans Download Station")
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
    sub.add_parser("ds-diag", parents=[common], help="diagnostic des téléchargements Download Station en attente") \
        .set_defaults(func=cmd_ds_diag)
    pu = sub.add_parser("update", parents=[common], help="mettre à jour synoarr depuis GitHub")
    pu.add_argument("--check", action="store_true", help="seulement vérifier s'il existe une mise à jour")
    pu.add_argument("--rollback", action="store_true", help="revenir à la version précédente")
    pu.set_defaults(func=cmd_update)
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
            _out("      Créez-le avec : " + _command("init"))
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
        _out("      Vérifiez la connexion du NAS et les adresses dans config.ini (%s)." % _command("check"))
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
