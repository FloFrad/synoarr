# synoarr

Remplaçant léger de **Prowlarr + Radarr + Sonarr + Filebot**, pensé pour un NAS **Synology** :

- **Indexers** torrent légaux : catalogue intégré (Internet Archive), n'importe quelle URL **Torznab** / **RSS**,
  ou import et synchronisation automatique depuis **Prowlarr** ou **Jackett** (l'un ou l'autre).
- **Watchlist Plex** : chaque film ou série ajouté est recherché automatiquement (épisodes manquants, saisons
  complètes, nouveaux épisodes).
- **Download Station** : le meilleur torrent (1080p → 720p, en anglais, ou en français pour l'animation) est envoyé directement.
- **Renommage façon Filebot** : le dossier `Download` est surveillé, les fichiers terminés sont renommés au format
  Plex puis déplacés dans `video/Films` et `video/TvShows`.
- **Une seule interface web légère** (port 8778) : Watchlist, indexers, toutes les commandes, journal et
  éditeur de configuration. Plus une ligne de commande complète.

Aucune dépendance : **Python 3.8+ seul** (déjà présent dans DSM 7). Docker possible mais pas obligatoire.

```
Watchlist Plex ──► recherche indexers ──► Download Station ──► dossier Download
                                                                     │
                     video/Films, video/TvShows ◄── renommage Plex ◄┘
```

## Installation

Suivez le guide pas à pas : **[docs/INSTALLATION.md](docs/INSTALLATION.md)** (20 à 30 minutes).

Résumé :

```bash
cd /volume1/docker && wget -O synoarr.tar.gz https://github.com/FloFrad/synoarr/archive/refs/heads/main.tar.gz && tar xzf synoarr.tar.gz && mv synoarr-main synoarr && cd synoarr
```

```bash
sudo python3 synoarr.py init
```

Éditez `config.ini` (identifiants Download Station, chemins des dossiers), puis :

```bash
sudo python3 synoarr.py plex-login
```

```bash
sudo python3 synoarr.py indexer add internet_archive
```

```bash
sudo python3 synoarr.py check
```

```bash
sudo sh scripts/start.sh
```

## Format des noms

| Type | Résultat |
|---|---|
| Film | `Films/Night of the Living Dead (1968).mkv` |
| Film d'une collection | `Films/The Matrix Collection/The Matrix Reloaded (2003).mkv` |
| Épisode | `TvShows/The Office/Saison 02/The Office - S02E05 - Halloween.mkv` |
| Sous-titres | `... - S02E05.fr.srt` |

Modifiable dans `config.ini` (`[naming]`), syntaxe proche de Filebot : `{title}`, `{year}`, `{show}`,
`{season:02d}`, `{episode:02d}`, `{episode_title}`…

## Développement

```bash
python3 -m unittest discover -s tests -v
```

Architecture et contrats internes : [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## Avertissement

synoarr est un outil d'automatisation générique. Utilisez-le uniquement avec des sources dont vous avez le droit
de télécharger le contenu (domaine public, licences libres, contenus dont vous détenez les droits).

## Licence

MIT
