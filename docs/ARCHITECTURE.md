# synoarr – architecture & contrats internes

Objectif : remplacer Prowlarr + Radarr + Sonarr + Filebot sur un NAS Synology, **sans aucune dépendance externe**
(stdlib Python uniquement) et **compatible Python 3.8** (le Python intégré à DSM 7 est `/usr/bin/python3` 3.8).

## Règles de code (obligatoires)

- Stdlib uniquement. Pas de `pip install`. Pas de `requests`, `yaml`, `lxml`…
- Compatible Python 3.8 : `from __future__ import annotations` en tête de chaque module, `typing.List/Dict/Optional`
  (pas de `list[str]` hors annotations), pas de `match`, pas de `str.removeprefix`, pas de `zoneinfo`, pas de `tomllib`.
- Fichiers lus/écrits en `encoding="utf-8"` explicite. Les chemins peuvent contenir des accents (`/volume1/Vidéos`).
- Toute requête réseau passe par `synoarr.httpclient.HttpClient` (injecté dans le constructeur → testable).
- Ne jamais logger de secret : utiliser `httpclient.redact(url)`.
- Logger avec `logging.getLogger(__name__)`. Messages utilisateur en **français**.
- Les erreurs réseau d'un composant ne doivent jamais faire planter le démon : attraper `HttpError` au niveau
  des boucles et logger.
- Tests : `unittest` stdlib, dans `tests/test_<module>.py`, lancés par
  `python3 -m unittest discover -s tests -v` **et** `/root/.local/bin/python3.8 -m unittest discover -s tests -v`.
  Les services externes sont simulés par de vrais serveurs HTTP locaux (`http.server` dans un thread, port 0),
  voir `tests/fakes.py`.

## Modules

| Module | Rôle |
|---|---|
| `models.py` | dataclasses partagées : `Release`, `SearchRequest`, `ParsedName`, `WatchItem`, `EpisodeInfo`, `DSTask` |
| `config.py` | lecture `config.ini` → `Config` (sections dataclass) |
| `httpclient.py` | `HttpClient.get/post` (+ multipart via `files=`), `Response`, `HttpError`, `redact()` |
| `db.py` | `Database` SQLite (indexers, media, episodes, grabs, blocklist, events, kv) |
| `parser.py` | `parse(name) -> ParsedName`, `normalize_title()`, `titles_match(candidate, wanted, alt_titles)` |
| `bencode.py` | décodage .torrent : `torrent_info(data) -> (name, info_hash_hex)` |
| `quality.py` | filtrage + score des `Release` → `pick_best()` |
| `indexers/` | `base.Indexer`, `torznab`, `rss`, `internet_archive`, `catalog.CATALOG`, `create_indexer(row)`, `detect_type()` |
| `plex.py` | Watchlist Plex, métadonnées (guids, épisodes), login PIN, refresh bibliothèque |
| `tmdb.py` | (optionnel) recherche TMDb pour renommage quand le téléchargement n'est pas connu |
| `downloadstation.py` | API DSM Download Station (login, ajout .torrent/magnet/URL, liste, suppression) |
| `organizer.py` | surveille `download_dir`, renomme au format Plex, déplace vers Films/Séries |
| `searcher.py` | orchestration : Watchlist → épisodes voulus → recherche indexers → choix → envoi DS |
| `app.py` | `App` : construit tous les composants depuis `Config`, boucle démon, threads |
| `web.py` | mini interface web (http.server) + API JSON |
| `cli.py` / `__main__.py` | ligne de commande `python3 synoarr.py <commande>` |

## Contrats

### quality.py
```python
def evaluate(release: Release, request: SearchRequest, cfg: QualityConfig) -> Tuple[Optional[int], str]
    # (score, raison). score None = rejeté, raison en français ("résolution 480p non acceptée"...)
def pick_best(releases: List[Release], request: SearchRequest, cfg: QualityConfig,
              is_blocked: Callable[[str], bool] = lambda t: False) -> Optional[Release]
```
Règles : le titre parsé doit `titles_match` la demande ; film → année ±1 si les deux sont connues, pas de marqueur
saison/épisode ; épisode → même saison et l'épisode dans `episodes` (pack refusé) ; saison → pack de cette saison
(`is_season_pack` et `request.season in seasons`, épisodes vides). Rejets : `reject_words` (mots entiers sur le titre
normalisé, ou `source` in cam/telesync/telecine/screener/workprint), résolution hors liste (sauf
`allow_other_resolutions`; résolution inconnue = acceptée avec un score faible), langue (voir ci-dessous), seeders
connus < `min_seeders`, taille connue < `min_size_mb` ou > max selon le type, sample, release bloquée.
Langues : `cfg.languages` liste de codes ("en","fr"). Une release sans tag de langue = anglais/VO → acceptée si "en"
est demandé. "multi" acceptée si "fr" ou "en" demandés. "vostfr" acceptée si "en" demandé (VO). Toute release dont
toutes les langues détectées sont hors liste (ex. ["it"]) est rejetée. Score : rang de résolution (index dans
`cfg.resolutions`, premier = meilleur) prime, puis langue préférée (ordre de `cfg.languages`, multi = compatible fr+en
→ bonus), puis source (bluray > webdl > webrip > web > hdtv > dvdrip), `prefer_words`, puis seeders (log), puis
taille raisonnable. Égalité → plus de seeders.

### bencode.py
```python
def bdecode(data: bytes) -> Any
def torrent_info(data: bytes) -> Tuple[str, str]   # (info["name"] décodé utf-8, sha1(bencode(info)).hexdigest())
def magnet_info(magnet: str) -> Tuple[Optional[str], Optional[str]]  # (dn, info_hash hex minuscule; base32 converti)
```
`torrent_info` lève `ValueError` si les données ne sont pas un torrent valide (ex. page HTML).

### indexers/
```python
class Indexer:
    def __init__(self, row: Dict[str, Any], http: HttpClient)  # row = ligne table indexers
    name: str
    def search(self, request: SearchRequest) -> List[Release]   # ne lève pas : log + [] sur erreur
    def test(self) -> Tuple[bool, str]                           # message français
def create_indexer(row, http) -> Indexer
def detect_type(url: str, api_key: str, http: HttpClient) -> str   # "torznab" | "rss" | "internet_archive"
CATALOG: List[Dict]  # {"id","name","type","url","description","categories"} – UNIQUEMENT des sources légales
```
- **torznab** : `GET {url}?t=caps` pour test ; recherche `t=movie` (`imdbid` sans "tt" si connu, sinon `q`),
  `t=tvsearch` (`q`, `season`, `ep`; `tvdbid` si connu), repli `t=search&q=`. `apikey`, `cat` (catégories de la ligne
  ou 2000 films / 5000 séries). L'URL saisie peut se terminer par `/api` ou non : si elle ne se termine pas par
  `/api`, essayer telle quelle puis avec `/api` ajouté. Parse XML RSS : `title`, `enclosure@url` / `link`,
  `size`, `torznab:attr` (seeders, peers, infohash, magneturl, size). Namespace `http://torznab.com/schemas/2015/feed`.
- **rss** : flux RSS/Atom générique ; filtre les items dont le titre parsé `titles_match` la demande ; lien
  `enclosure`/`link` (magnet ou .torrent), taille si présente.
- **internet_archive** : API `https://archive.org/advancedsearch.php` (`q=title:(...) AND mediatype:(movies)`,
  `fl[]=identifier,title,year,item_size,downloads`, `rows=50`, `output=json`) ; torrent
  `https://archive.org/download/{id}/{id}_archive.torrent` ; `seeders=None` (webseeds). Titre de la Release =
  `"{title} ({year})"` pour que le parser trouve l'année.

### plex.py
```python
class PlexClient:
    def __init__(self, token: str, client_id: str, http: HttpClient)
    def watchlist(self) -> List[WatchItem]          # paginé, films + séries, guids remplis si possible
    def get_guids(self, rating_key: str) -> Dict[str, str]
    def show_episodes(self, rating_key: str) -> List[EpisodeInfo]
    def test(self) -> Tuple[bool, str]
    def start_pin_login(self) -> Tuple[int, str]      # (pin_id, code à saisir sur https://plex.tv/link)
    def check_pin(self, pin_id: int) -> Optional[str]  # authToken ou None
def refresh_library(server_url: str, token: str, http: HttpClient) -> bool
```
Endpoints (JSON via header `Accept: application/json`, en-têtes `X-Plex-Token`, `X-Plex-Client-Identifier`,
`X-Plex-Product: synoarr`) : watchlist `https://discover.provider.plex.tv/library/sections/watchlist/all`
(`X-Plex-Container-Start`, `X-Plex-Container-Size=100`, `includeGuids=1`), métadonnées
`https://discover.provider.plex.tv/library/metadata/{ratingKey}` (`Guid: [{"id":"imdb://tt.."}, ...]`), saisons
`/library/metadata/{key}/children` (type season, `index`), épisodes `/library/metadata/{seasonKey}/children`
(`index`, `parentIndex`, `title`, `originallyAvailableAt`). Repli sur `https://metadata.provider.plex.tv` si discover
répond 404. PIN : `POST https://plex.tv/api/v2/pins` (`strong=false`) → `id`,`code` ; `GET /api/v2/pins/{id}` →
`authToken`. Refresh : `GET {server_url}/library/sections/all/refresh`.
Les URLs de base sont des attributs (`discover_url`, `metadata_url`, `plextv_url`) pour pointer les tests vers un faux serveur.

### tmdb.py
```python
class TmdbClient:
    def __init__(self, api_key: str, language: str, http: HttpClient)  # api_key vide → enabled False
    enabled: bool
    def search_movie(self, title: str, year: Optional[int]) -> Optional[Dict]  # {"title","year","tmdb"}
    def search_show(self, title: str) -> Optional[Dict]                      # {"title","year","tmdb"}
    def episode_title(self, tmdb_id: str, season: int, episode: int) -> Optional[str]
    def show_episodes(self, tmdb_id: str) -> List[EpisodeInfo]
```
Clé v3 en paramètre `api_key` ; si la clé ressemble à un jeton v4 (commence par "eyJ") → header `Authorization: Bearer`.
`base_url` attribut. Ne lève pas : None/[] sur erreur.

### downloadstation.py
```python
class DownloadStationError(Exception)
class DownloadStation:
    def __init__(self, url, username, password, destination, http: HttpClient)
    def login(self) -> None                         # lève DownloadStationError (message clair : 2FA, identifiants...)
    def add_torrent_file(self, data: bytes, filename: str) -> None
    def add_url(self, url: str) -> None             # magnet ou http
    def list_tasks(self) -> List[DSTask]
    def delete_task(self, task_id: str) -> None      # supprime la tâche, PAS les fichiers
    def test(self) -> Tuple[bool, str]
```
Découverte : `GET {url}/webapi/query.cgi?api=SYNO.API.Info&version=1&method=query&query=ALL` (chemins + versions
max). Login `SYNO.API.Auth` (version min(6,max), `method=login`, `account`, `passwd`, `session=DownloadStation`,
`format=sid`) → `data.sid`, ensuite `_sid` sur chaque appel. Ajout fichier : `SYNO.DownloadStation2.Task`
version 2 `method=create` POST multipart sur `entry.cgi`, champs `api`,`version`,`method`,`type="\"file\""`,
`file="[\"torrent\"]"`, `destination="\"<dest>\""`, `create_list=false`, `_sid`, fichier dans le champ `torrent`.
Ajout URL : `SYNO.DownloadStation2.Task` `type="\"url\""`, `url=json.dumps([url])`, `destination`, `create_list=false`.
Repli si DownloadStation2 absent/erreur : `SYNO.DownloadStation.Task` v1-3 `method=create` (`uri=` pour URL,
multipart champ `file` pour fichier, `destination=<dest>` sans guillemets). Liste : `SYNO.DownloadStation.Task`
`method=list&additional=detail,transfer` → `data.tasks[]` (`id`,`title`,`status`,`size`,
`additional.detail.destination`, `additional.transfer.size_downloaded`, `status_extra.error_detail`).
Suppression : `SYNO.DownloadStation.Task` `method=delete&id=..&force_complete=false`.
Erreurs `success:false` : codes session 105/106/107/119 → re-login une fois puis rejouer ; 400 identifiants,
401 compte désactivé, 402 permission refusée, 403/404/406 2FA → message français explicite.

### organizer.py
```python
class Organizer:
    def __init__(self, cfg: Config, db: Database, ds: Optional[DownloadStation], tmdb: Optional[TmdbClient],
                 on_imported: Optional[Callable[[], None]] = None)
    def run_once(self, dry_run: bool = False) -> List[Dict]   # liste d'actions {"src","dst","status","message"}
    def plan(self, entry_path: str) -> List[Tuple[str, str, Dict]]  # (src, dst, meta) sans rien toucher
```
Algorithme : pour chaque entrée de premier niveau de `paths.download_dir` (fichier ou dossier, ignorer cachés,
`@eaDir`, `#recycle`, `.part`, `.!ut`, dossiers synoarr internes) : ignorer si une tâche DS **active** a ce titre
(comparaison insensible à la casse), si modifiée depuis < `min_age_minutes` (mtime max récursif), ou si
`only_known_downloads` et pas de grab. Retrouver les métadonnées : `db.find_grab_by_name(entry_name)` → media
(titre, année, type) ; sinon `parse()` + TMDb si activé ; sinon parse seul. Pour chaque vidéo (extensions config,
hors samples et fichiers < 50 Mo sauf si c'est la seule vidéo) : film → `naming.movie_format`, épisode →
`naming.series_format` (`show`, `season`, `episode`, `episode_title` = titre BD/TMDb ou "", `title`, `year`, `ext`,
`resolution`, `imdb`, `tmdb`, `tvdb`). Multi-épisodes : `S01E01-E02`. Sous-titres : même nom que la vidéo +
suffixe langue détecté (`.fr.srt`, `.en.srt`, `.forced`) ; un sous-titre unique dans un dossier film va avec le film.
Nettoyage du nom (caractères interdits `<>:"/\|?*` → `:` devient ` - `, autres supprimés, espaces multiples, points
finaux). Ne jamais écraser : si destination existe avec même taille → considérer importé (supprimer source si move) ;
si taille différente → garder la plus grosse (remplacer si nouvelle plus grosse, sinon ignorer et logger).
Action move (os.rename puis repli shutil.move entre volumes), copy (shutil.copy2), hardlink (os.link, repli copy).
Après import réussi d'une entrée : statut grab `imported`, épisodes/film `done`, si `remove_task_after_import`
supprimer la tâche DS correspondante, si `delete_leftovers` et action move supprimer le dossier source restant
(seulement s'il ne reste ni vidéo ni sous-dossier non vide de vidéos), `db.add_event`, puis `on_imported()`
(refresh Plex) une fois par cycle s'il y a eu au moins un import. Dossiers créés avec `os.makedirs(exist_ok=True)`.
Mode `dry_run` : ne rien modifier, renvoyer les actions prévues.

### web.py
```python
class WebServer:
    def __init__(self, app: "App", host: str, port: int, username: str = "", password: str = "")
    def start(self) -> None      # thread daemon, ThreadingHTTPServer
    def stop(self) -> None
```
`App` expose (voir app.py) : `cfg`, `db`, `searcher.run_once()`, `organizer.run_once(dry_run=False)`,
`run_watchlist_now()`, `run_process_now()` (déclenchent en arrière-plan), `indexer_manager` avec
`list()`, `add_from_catalog(catalog_id)`, `add_url(name, url, api_key, categories)`, `test(id)`, `set_enabled(id, bool)`,
`delete(id)`, `status() -> dict` (dernières exécutions, erreurs, version).
Pages : une seule page HTML (vanilla JS, CSS inline, aucun CDN) avec onglets : Tableau de bord (statut, boutons
« Synchroniser la Watchlist », « Traiter les téléchargements »), Watchlist (films/séries + statut, épisodes),
Indexers (liste, test, activer/désactiver, supprimer, ajouter depuis le catalogue ou par URL), Historique (grabs +
événements). API JSON sous `/api/...`. Auth Basic si username/password configurés.
