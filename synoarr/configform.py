"""Formulaire de configuration de l'interface web (onglet Configuration).

* :func:`form_schema` décrit chaque option des dataclasses de ``config.py`` (type, valeur actuelle lue dans le
  fichier, valeur par défaut, libellé et aide en français tirés des commentaires de ``config.example.ini``) ;
  les secrets (mots de passe, clés, jetons) ne sont jamais renvoyés.
* :func:`apply_changes` réécrit SEULEMENT les clés modifiées dans le texte de ``config.ini`` (commentaires et
  ordre conservés ; clé absente ajoutée à la fin de sa section, section absente ajoutée à la fin du fichier). Le
  texte obtenu est ensuite enregistré par ``App.save_config_text`` (validation, sauvegardes .bak, rechargement).
"""
from __future__ import annotations

import configparser
import dataclasses
import os
import re
from typing import Any, Dict, List, Optional, Tuple

from . import config as _config

SECTION_TITLES = {
    "general": "Général", "plex": "Plex", "downloadstation": "Download Station", "paths": "Dossiers",
    "naming": "Nommage", "quality": "Qualité", "series": "Séries", "tmdb": "TMDb", "prowlarr": "Prowlarr",
    "jackett": "Jackett", "telegram": "Telegram", "subtitles": "Sous-titres", "web": "Interface web",
}
SECTION_ORDER = ["general", "plex", "downloadstation", "paths", "naming", "quality", "series", "tmdb", "prowlarr",
                 "jackett", "telegram", "subtitles", "web"]
CHOICES = {
    ("paths", "action"): [("move", "move — déplacer"), ("copy", "copy — copier"),
                          ("hardlink", "hardlink — lien dur (même volume)")],
    ("series", "monitor"): [("all", "all — tous les épisodes"), ("future", "future — épisodes à venir"),
                            ("latest_season", "latest_season — dernière saison")],
    ("downloadstation", "send_mode"): [("file", "file — envoyer le fichier .torrent"),
                                       ("url", "url — Download Station télécharge le lien")],
    ("downloadstation", "prefer"): [("torrent", "torrent — fichier .torrent d'abord"),
                                    ("magnet", "magnet — lien magnet d'abord")],
    ("general", "log_level"): [("DEBUG", "DEBUG"), ("INFO", "INFO"), ("WARNING", "WARNING"), ("ERROR", "ERROR")],
}
LABELS = {
    "general": {"data_dir": "Dossier des données", "log_level": "Niveau du journal",
                "watchlist_interval_minutes": "Lecture de la Watchlist (minutes)",
                "process_interval_minutes": "Surveillance des téléchargements (minutes)",
                "research_interval_hours": "Nouvelle recherche d'un contenu introuvable (heures)",
                "rss_interval_minutes": "Synchronisation RSS (minutes, 0 = désactivée)",
                "dry_run": "Mode simulation", "update_repo": "Dépôt GitHub des mises à jour"},
    "plex": {"token": "Jeton Plex (X-Plex-Token)", "server_url": "Serveur Plex local",
             "server_token": "Jeton du serveur local"},
    "downloadstation": {"url": "Adresse de DSM", "username": "Compte DSM", "password": "Mot de passe DSM",
                        "destination": "Dossier de destination", "verify_ssl": "Vérifier le certificat HTTPS",
                        "send_mode": "Envoi des .torrent", "prefer": "Préférence torrent / magnet",
                        "extra_trackers": "Trackers ajoutés aux liens magnet",
                        "stalled_hours": "Abandon sans source après (heures)",
                        "test_torrent_url": "Torrent du test d'envoi"},
    "paths": {"download_dir": "Dossier des téléchargements", "movies_dir": "Dossier des films",
              "series_dir": "Dossier des séries", "action": "Action de rangement",
              "min_age_minutes": "Âge minimal d'un téléchargement (minutes)",
              "remove_task_after_import": "Supprimer la tâche après l'import",
              "delete_leftovers": "Supprimer les fichiers restants",
              "only_known_downloads": "Seulement les téléchargements de synoarr"},
    "naming": {"movie_format": "Nom des films", "movie_collection_format": "Nom des films d'une collection",
               "series_format": "Nom des épisodes", "subtitle_extensions": "Extensions de sous-titres",
               "video_extensions": "Extensions vidéo"},
    "quality": {"resolutions": "Résolutions acceptées", "allow_other_resolutions": "Accepter les autres résolutions",
                "languages": "Langues audio", "languages_animation": "Langues audio de l'animation",
                "upgrade": "Améliorations de qualité", "upgrade_until": "Améliorer jusqu'à",
                "min_seeders": "Seeders minimum", "min_size_mb": "Taille minimale (Mo)",
                "max_size_gb_movie": "Taille maximale d'un film (Go)",
                "max_size_gb_episode": "Taille maximale d'un épisode (Go)",
                "max_size_gb_season": "Taille maximale d'une saison (Go)", "reject_words": "Mots rejetés",
                "prefer_words": "Mots favorisés"},
    "series": {"monitor": "Épisodes suivis", "season_packs": "Saisons complètes d'abord"},
    "tmdb": {"api_key": "Clé API TMDb", "language": "Langue TMDb"},
    "prowlarr": {"url": "Adresse de Prowlarr", "api_key": "Clé API Prowlarr",
                 "sync_interval_hours": "Synchronisation (heures, 0 = jamais)"},
    "jackett": {"url": "Adresse de Jackett", "api_key": "Clé API Jackett"},
    "telegram": {"bot_token": "Jeton du bot", "chat_id": "Identifiant de conversation (chat_id)",
                 "events": "Événements notifiés"},
    "subtitles": {"enabled": "Sous-titres automatiques", "api_key": "Clé API OpenSubtitles",
                  "username": "Compte OpenSubtitles", "password": "Mot de passe OpenSubtitles",
                  "languages": "Langues des sous-titres", "hearing_impaired": "Sous-titres pour malentendants",
                  "scan_interval_hours": "Recherche périodique (heures, 0 = après import)",
                  "scan_limit": "Vidéos par passage"},
    "web": {"enabled": "Interface web activée", "host": "Adresse d'écoute", "port": "Port",
            "username": "Identifiant", "password": "Mot de passe"},
}
SECRET_KEYS = ("password", "api_key", "token", "bot_token", "server_token", "passkey")
_KEY_LINE = re.compile(r"^\s*([A-Za-z0-9_.\-]+)\s*[=:]")
_COMMENTED_KEY = re.compile(r"^\s*[;#]\s*([a-z0-9_]+)\s*=")
_SECTION_LINE = re.compile(r"^\s*\[([^\]]+)\]\s*$")


def is_secret(key: str) -> bool:
    return key in SECRET_KEYS or key.endswith(("_password", "_token", "_api_key"))


def example_path() -> str:
    return os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config.example.ini")


def parse_help(text: str) -> Dict[Tuple[str, str], Tuple[str, bool]]:
    """{(section, key): (help, inherited)} from the comment lines just above each key of config.example.ini.
    A key without its own comment right after another key shares the previous key's help (inherited=True).
    A long comment (3 lines or more) above the first key of a section describes the section: (section, "")."""
    out = {}  # type: Dict[Tuple[str, str], Tuple[str, bool]]
    known = {name: {f.name for f in dataclasses.fields(k)} for name, k in _sections()}
    section = ""
    comments = []  # type: List[str]
    last = None  # type: Optional[Tuple[str, bool]]
    first = in_step = False
    for line in (text or "").splitlines():
        m = _SECTION_LINE.match(line)
        if m:
            section, comments, last, first = m.group(1).strip().lower(), [], None, True
            continue
        stripped = line.strip()
        ck = _COMMENTED_KEY.match(line)
        km = _KEY_LINE.match(line) if not stripped.startswith((";", "#")) else None
        key = (km.group(1).lower() if km else ck.group(1) if ck else None)
        if key and ck and not km and key not in known.get(section, ()):
            key = None  # « ; true = simulation… » is prose, not a commented-out option
        if key and section:
            if first and len(comments) >= 3:
                out[(section, "")] = (" ".join(comments).replace(" \n", "\n"), False)
                comments = []
            first = False
            if comments:
                last = (" ".join(comments).replace(" \n", "\n"), False)
            elif last is not None:
                last = (last[0], True)
            if last is not None:
                out.setdefault((section, key), last)
            comments = []
            continue
        if stripped.startswith((";", "#")):
            raw = stripped.lstrip(";#")
            body = raw.strip()
            if body and not set(body) <= set("=-_ "):
                # numbered steps and indented lines start a new line (shown with white-space: pre-line)
                numbered = bool(re.match(r"^\d+\.\s", body))
                indented = raw.startswith("  ")
                if comments and (numbered or (indented and not in_step)):
                    body = "\n" + body
                in_step = numbered or (in_step and indented)
                comments.append(body)
        elif not stripped:
            comments, last = [], None
    return out


def _label(key: str, help_text: str, inherited: bool) -> str:
    if help_text and not inherited:
        first = re.split(r"(?<=[.!?])\s|\s[:;]\s|\s\(", help_text, 1)[0].strip().rstrip(".:;")
        if 3 <= len(first) <= 70:
            return first[0].upper() + first[1:]
    return key.replace("_", " ").capitalize()


def _kind(default: Any) -> str:
    if isinstance(default, bool):
        return "bool"
    if isinstance(default, int):
        return "int"
    if isinstance(default, float):
        return "float"
    if isinstance(default, list):
        return "list"
    return "str"


def format_value(value: Any) -> str:
    """Python value -> text written in config.ini."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float):
        return ("%.6f" % value).rstrip("0").rstrip(".") or "0"
    if isinstance(value, (list, tuple)):
        return ", ".join(str(v) for v in value)
    return "" if value is None else str(value)


def _sections() -> List[Tuple[str, Any]]:
    known = dict(getattr(_config, "_SECTIONS", {}))
    names = [n for n in SECTION_ORDER if n in known] + sorted(n for n in known if n not in SECTION_ORDER)
    return [(n, known[n]) for n in names]


def _parser(text: str) -> configparser.ConfigParser:
    parser = configparser.ConfigParser(interpolation=None)
    try:
        parser.read_string(text or "")
    except configparser.Error:
        pass  # a broken file still shows the form with the defaults (the raw editor shows the error)
    return parser


def form_schema(text: str, example_text: Optional[str] = None) -> Dict[str, Any]:
    """Sections and fields of the form, values read from *text* (the current config.ini)."""
    if example_text is None:
        try:
            with open(example_path(), encoding="utf-8") as fh:
                example_text = fh.read()
        except OSError:
            example_text = ""
    helps = parse_help(example_text)
    parser = _parser(text)
    sections = []
    for name, klass in _sections():
        defaults = klass()
        fields = []
        for f in dataclasses.fields(klass):
            default = getattr(defaults, f.name)
            kind = _kind(default)
            value = default
            present = parser.has_option(name, f.name)
            if present:
                try:
                    value = _config._convert("%s.%s" % (name, f.name), parser.get(name, f.name), default)
                except _config.ConfigError:
                    value = parser.get(name, f.name)  # shown as typed: the user fixes it
            help_text, inherited = helps.get((name, f.name), ("", False))
            label = LABELS.get(name, {}).get(f.name) or _label(f.name, help_text, inherited)
            entry = {"key": f.name, "type": kind, "label": label, "help": "" if inherited else help_text,
                     "default": default, "in_file": present, "secret": is_secret(f.name)}
            if (name, f.name) in CHOICES:
                entry["choices"] = [list(c) for c in CHOICES[(name, f.name)]]
            if entry["secret"]:
                entry["value"] = None
                entry["default"] = None
                entry["has_value"] = bool(str(value or "").strip())
            else:
                entry["value"] = value
            fields.append(entry)
        sections.append({"name": name, "title": SECTION_TITLES.get(name, name), "fields": fields,
                         "help": helps.get((name, ""), ("", False))[0]})
    return {"sections": sections}


def _field_types() -> Dict[str, Dict[str, Any]]:
    return {name: {f.name: getattr(klass(), f.name) for f in dataclasses.fields(klass)} for name, klass in _sections()}


def normalize_changes(text: str, changes: Any) -> Tuple[Dict[str, Dict[str, str]], List[str]]:
    """Validate *changes* ({section: {key: value}}) against the dataclasses: (text values to write, problems).
    Values equal to the current ones are dropped (only real changes are written)."""
    if not isinstance(changes, dict):
        return {}, ["Objet JSON {section: {clé: valeur}} attendu"]
    types = _field_types()
    parser = _parser(text)
    out = {}  # type: Dict[str, Dict[str, str]]
    problems = []  # type: List[str]
    for section, values in changes.items():
        if section not in types:
            problems.append("Section inconnue : [%s]" % section)
            continue
        if not isinstance(values, dict):
            problems.append("[%s] : objet {clé: valeur} attendu" % section)
            continue
        for key, raw in values.items():
            if key not in types[section]:
                problems.append("Option inconnue « %s » dans [%s]" % (key, section))
                continue
            default = types[section][key]
            name = "%s.%s" % (section, key)
            if isinstance(raw, bool):
                raw_text = "true" if raw else "false"
            elif isinstance(raw, (int, float)):
                raw_text = format_value(raw)
            elif isinstance(raw, list):
                raw_text = ", ".join(str(v) for v in raw)
            elif isinstance(raw, str):
                raw_text = raw.strip()
            elif raw is None:
                raw_text = ""
            else:
                problems.append("[%s] %s : valeur invalide" % (section, key))
                continue
            if "\n" in raw_text or "\r" in raw_text:
                problems.append("[%s] %s : une seule ligne attendue" % (section, key))
                continue
            try:
                new = _config._convert(name, raw_text, default)
            except _config.ConfigError as exc:
                problems.append("[%s] %s" % (section, exc))
                continue
            if (section, key) == ("web", "port") and not 1 <= new <= 65535:
                problems.append("[web] port : nombre de 1 à 65535 attendu")
                continue
            if isinstance(new, (int, float)) and not isinstance(new, bool) and new < 0:
                problems.append("[%s] %s : nombre positif ou nul attendu" % (section, key))
                continue
            if parser.has_option(section, key):
                try:
                    cur = _config._convert(name, parser.get(section, key), default)
                except _config.ConfigError:
                    cur = object()
            else:
                cur = default
            if new == cur:
                continue
            out.setdefault(section, {})[key] = format_value(new) if not isinstance(new, str) else raw_text
    return out, problems


def apply_changes(text: str, changes: Dict[str, Dict[str, str]]) -> str:
    """Rewrite only the given keys in *text*; comments, order and other keys are kept as they are."""
    lines = (text or "").split("\n")
    for section, values in changes.items():
        for key, value in values.items():
            new_line = ("%s = %s" % (key, value)).rstrip()
            start = end = None
            for i, line in enumerate(lines):
                m = _SECTION_LINE.match(line)
                if m:
                    if start is not None:
                        end = i
                        break
                    if m.group(1).strip() == section:
                        start = i
            if start is None:  # missing section: added at the end of the file
                while lines and not lines[-1].strip():
                    lines.pop()
                lines += ["", "[%s]" % section, new_line, ""]
                continue
            if end is None:
                end = len(lines)
            idx = None
            for i in range(start + 1, end):
                m = _KEY_LINE.match(lines[i])
                if m and not lines[i].lstrip().startswith((";", "#")) and m.group(1).lower() == key \
                        and not lines[i][:1].isspace():
                    idx = i
                    break
            if idx is not None:
                j = idx + 1  # continuation lines of a multi-line value go away with it
                while j < end and lines[j][:1].isspace() and lines[j].strip() \
                        and not lines[j].strip().startswith((";", "#")):
                    j += 1
                lines[idx:j] = [new_line]
            else:  # missing key: added after the last non-blank line of its section
                k = end
                while k > start + 1 and not lines[k - 1].strip():
                    k -= 1
                lines.insert(k, new_line)
    out = "\n".join(lines)
    return out if out.endswith("\n") else out + "\n"
