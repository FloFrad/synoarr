# Guide d'installation de synoarr sur un NAS Synology

Durée totale : **20 à 30 minutes**. Aucune connaissance en programmation nécessaire : il suffit de copier-coller
les commandes.

Deux méthodes :

- **Méthode A — native (recommandée, fonctionne sur tous les modèles)** : utilise le Python déjà présent dans DSM 7.
- **Méthode B — Docker / Container Manager** : seulement si votre modèle propose Container Manager.

> Déjà installé ? Allez directement à « Mise à jour » en bas de page.

---

## Étape 1 — Préparer DSM (5 min)

1. **Installer Download Station** : Centre de paquets → rechercher « Download Station » → Installer.
2. **Activer SSH** : Panneau de configuration → Terminal & SNMP → cocher « Activer le service SSH » → Appliquer.
3. **Créer un utilisateur dédié** (conseillé : la validation en 2 étapes bloque l'accès par programme) :
   Panneau de configuration → Utilisateur et groupe → Créer :
   - Nom : `synoarr`, un mot de passe solide.
   - Groupe : `users` suffit.
   - Permissions des dossiers partagés : **Lecture/Écriture** sur `Download` et `video`.
   - Applications : **Autoriser** Download Station.
   - Ne pas activer la validation en 2 étapes pour ce compte.
4. **Noter les chemins exacts de vos dossiers** : File Station → clic droit sur le dossier `Download` →
   Propriétés → « Emplacement » (ex. `/volume1/Download`). Faites de même pour `video`. Attention aux accents
   et aux majuscules : `video`, `Video` et `Vidéos` sont trois dossiers différents.
5. **Créer le dossier d'installation** : si vous n'avez pas de dossier partagé `docker`, créez-le
   (Panneau de configuration → Dossier partagé → Créer → nom `docker`). Il permet aussi d'éditer la configuration
   depuis File Station.
6. **Ouvrir le port de l'interface web** (seulement si le pare-feu DSM est activé) :
   Panneau de configuration → Sécurité → Pare-feu → Modifier les règles → Créer → Ports : Personnalisé → TCP
   `8778` → OK.

## Étape 2 — Télécharger synoarr (2 min)

Connectez-vous en SSH depuis votre ordinateur (Terminal sur Mac/Linux, PowerShell sur Windows) avec **votre compte
administrateur DSM** (celui que vous utilisez pour vous connecter à DSM ; le compte `admin` est désactivé par défaut) :

```bash
ssh VOTRE-ADMIN@IP-DU-NAS
```

Le mot de passe ne s'affiche pas pendant la saisie : c'est normal, tapez-le puis Entrée.

Puis :

```bash
cd /volume1/docker && wget -O synoarr.tar.gz https://github.com/FloFrad/synoarr/archive/refs/heads/main.tar.gz && tar xzf synoarr.tar.gz && mv synoarr-main synoarr && rm synoarr.tar.gz && cd synoarr
```

Vérifiez Python :

```bash
python3 --version
```

Il faut **3.8 ou plus**. Si la commande est introuvable : Centre de paquets → installer « Python 3 » (3.9 ou plus
récent), puis recommencez.

> Toutes les commandes suivantes se lancent depuis `/volume1/docker/synoarr` **avec `sudo`** (le service tourne
> en root ; sans `sudo` la base de données ne serait plus modifiable ensuite). `sudo` redemande votre mot de passe
> administrateur.

## Étape 3 — Configurer (5 min)

```bash
sudo python3 synoarr.py init
```

La commande affiche le **mot de passe de l'interface web** (utilisateur `admin`) : notez-le. Il est aussi dans
`config.ini`, section `[web]`.

Ouvrez ensuite `config.ini` : File Station → `docker/synoarr/config.ini` → clic droit → Ouvrir avec
l'**Éditeur de texte** (paquet « Text Editor » du Centre de paquets), ou en SSH avec `sudo vi config.ini`.

Renseignez au minimum :

```ini
[downloadstation]
username = synoarr
password = le-mot-de-passe
destination = Download

[paths]
download_dir = /volume1/Download
movies_dir = /volume1/video/Films
series_dir = /volume1/video/TvShows
```

Règles importantes :

- `download_dir` est le chemin complet du dossier indiqué dans `destination`
  (`destination = Download` ↔ `download_dir = /volume1/Download`).
- Les dossiers partagés (`Download`, `video`) doivent exister ; les sous-dossiers `Films` et `TvShows` sont créés
  automatiquement.
- Enregistrez le fichier en UTF-8 (c'est le cas par défaut avec l'Éditeur de texte de DSM).

Les autres réglages ont déjà les bonnes valeurs :

- qualité 1080p, puis 720p ;
- films et séries en **anglais** (MULTI accepté, sous-titres préférés), **animation en français** (genre
  « Animation » sur Plex ou TMDb) ;
- noms : `Films/Titre (Année).mkv`, `Films/Nom Collection/Titre (Année).mkv`,
  `TvShows/Série/Saison 01/Série - S01E02 - Titre épisode.mkv`.

**Recommandé — clé TMDb gratuite** (nécessaire pour les dossiers de collections, améliore les titres d'épisodes et
la détection de l'animation) : créez un compte sur <https://www.themoviedb.org>, puis Paramètres → API → Créer →
« Developer », et copiez la « Clé d'API » dans `config.ini` : `[tmdb] api_key = ...`.

Sous-titres anglais : synoarr garde les sous-titres fournis avec la vidéo (`Titre (Année).en.srt`) et préfère les
releases qui en contiennent. Pour les autres, activez la recherche automatique de sous-titres de Plex
(Plex → Paramètres → Langues → « Sélection automatique des sous-titres » et votre langue de sous-titres : anglais).

## Étape 4 — Connecter Plex (2 min)

```bash
sudo python3 synoarr.py plex-login
```

Un code à 4 caractères s'affiche. Ouvrez <https://plex.tv/link> sur votre téléphone ou ordinateur (connecté à
votre compte Plex), saisissez le code : la commande affiche `[OK] Jeton Plex enregistré`.

Facultatif — rafraîchir automatiquement Plex après chaque import : dans `config.ini`,
`[plex] server_url = http://127.0.0.1:32400`.

## Étape 5 — Ajouter des indexers (2 min)

Voir la liste des indexers légaux intégrés :

```bash
sudo python3 synoarr.py indexer catalog
```

Ajouter Internet Archive (films et séries du domaine public) :

```bash
sudo python3 synoarr.py indexer add internet_archive
```

Ajouter n'importe quel indexer par son URL (flux **Torznab** ou **RSS** ; le type est détecté automatiquement) :

```bash
sudo python3 synoarr.py indexer add-url "Mon indexer" "https://exemple.org/api" --api-key VOTRE_CLE
```

**Prowlarr ou Jackett : il faut l'un OU l'autre, pas les deux.** Les deux font la même chose (ils
transforment des sites en flux Torznab). Si les deux étaient synchronisés, chaque site serait interrogé deux fois et
apparaîtrait en double. Gardez celui que vous avez déjà ; si vous n'en avez aucun, Internet Archive suffit pour
commencer.

**Vous utilisez Prowlarr sur le NAS ?** Importez tous ses indexers torrent actifs en une fois (chacun devient
un indexer Torznab « Nom (Prowlarr) », avec sa priorité). La clé API est dans Prowlarr > Settings > General > API Key :

```bash
sudo python3 synoarr.py indexer import-prowlarr http://127.0.0.1:9696 --api-key CLE_PROWLARR
```

Après ce premier import, **synoarr suit Prowlarr automatiquement** (toutes les 6 heures, et à chaque démarrage) :
un indexer ajouté dans Prowlarr apparaît dans synoarr, un indexer supprimé ou désactivé dans Prowlarr est désactivé,
et les priorités suivent celles de Prowlarr. Vous gérez votre annuaire d'indexers dans Prowlarr uniquement ; vous
n'avez jamais à utiliser ses autres fonctions. Fréquence : `[prowlarr] sync_interval_hours` (0 = jamais).
Synchroniser tout de suite : bouton « Synchroniser maintenant » (onglet Indexers) ou
`sudo python3 synoarr.py indexer prowlarr-sync`. Arrêter : bouton « Arrêter la synchro » ou
`indexer prowlarr-forget`.

**Vous utilisez Jackett (à la place de Prowlarr) ?** Même principe : chaque indexer configuré dans Jackett devient un
indexer Torznab « Nom (Jackett) », puis synoarr suit Jackett automatiquement (ajouts, suppressions). Jackett n'a pas
de priorités : elles valent 25 par défaut et se règlent dans synoarr (elles sont conservées à chaque synchro). La clé
API est en haut à droite de la page Jackett (« API Key ») :

```bash
sudo python3 synoarr.py indexer import-jackett http://127.0.0.1:9117 --api-key CLE_JACKETT
```

Depuis l'interface web : onglet Indexers → « Importer depuis Jackett ». Resynchroniser : `indexer jackett-sync` ;
arrêter : `indexer jackett-forget`.

**Priorité des indexers** : chaque indexer a une priorité de 1 (le plus prioritaire) à 50 (25 par défaut). Les
indexers sont interrogés dans cet ordre et, à qualité égale (même résolution, langue et source), la release de
l'indexer le plus prioritaire est choisie, même si une autre a plus de sources. La priorité ne fait jamais préférer
une moins bonne résolution, une autre langue ou une moins bonne source.

```bash
sudo python3 synoarr.py indexer priority "Mon indexer" 5
sudo python3 synoarr.py indexer add-url "Mon indexer" "https://exemple.org/api" --api-key CLE --priority 10
```

Les indexers se gèrent aussi depuis l'interface web (onglet Indexers : ajout, priorité −/+, import Prowlarr).

## Étape 6 — Tout vérifier (3 min)

```bash
sudo python3 synoarr.py check
```

- `[OK]` : bon.
- `[!]` : facultatif ou simple avertissement (par exemple TMDb sans clé).
- `[ERR]` : à corriger avant de continuer (voir « Problèmes fréquents »).

**Important — regardez ce qui sera déplacé au premier démarrage.** synoarr range aussi les vidéos déjà présentes
dans `Download` :

```bash
sudo python3 synoarr.py process --dry-run
```

Si des fichiers personnels apparaissent dans la liste, déplacez-les ailleurs, ou mettez
`only_known_downloads = true` dans `[paths]` (seuls les téléchargements lancés par synoarr seront alors traités).

Test de recherche, sans rien télécharger :

```bash
sudo python3 synoarr.py search --dry-run
```

## Étape 7 — Démarrage automatique (3 min)

Panneau de configuration → **Planificateur de tâches** :

1. Créer → Tâche déclenchée → Script défini par l'utilisateur.
   - Général : nom `synoarr`, utilisateur **root**, événement **Démarrage**.
   - Paramètres de tâche → Exécuter la commande :
     ```
     sh /volume1/docker/synoarr/scripts/start.sh
     ```
2. Créer → Tâche planifiée → Script défini par l'utilisateur (relance synoarr s'il s'est arrêté).
   - Général : nom `synoarr-surveillance`, utilisateur **root**.
   - Planification : tous les jours, de 00:00 à 23:50, toutes les **10 minutes**.
   - Même commande que ci-dessus.
3. Sélectionnez la tâche `synoarr` → **Exécuter** pour la lancer tout de suite.

Ouvrez l'interface : **http://IP-DU-NAS:8778**

Une fois synoarr démarré, **tout se fait depuis cette page**, sans SSH :

| Onglet | Contenu |
|---|---|
| Tableau de bord | état général, téléchargements en cours (progression, vitesses, sources), derniers envois |
| Watchlist | films et séries suivis, épisodes, « Rechercher », « Ignorer », langue par élément (Auto / Anglais / Français), « + Ajouter » (recherche Plex/TMDb ou ajout manuel), « Supprimer », progression de chaque téléchargement (%, taille, vitesses ↓/↑, seeds/pairs, temps restant, état Download Station), « Changer de source » |
| Indexers | ajouter (catalogue, URL, import Prowlarr ou Jackett), priorité (1 = le plus prioritaire), tester, activer, supprimer |
| Historique | envois vers Download Station et événements |
| Commandes | synchroniser la Watchlist, rechercher, traiter les téléchargements, simulation, vérifier la configuration, tester les indexers, recherche manuelle avec envoi, connexion Plex, redémarrer, mettre à jour synoarr (et revenir en arrière) |
| Journal | dernières lignes du journal, filtres, téléchargement |
| Configuration | éditeur de `config.ini` : vérifié avant d'être enregistré, sauvegarde automatique (5 versions), appliqué sans redémarrer |

**Changer de source** : si un téléchargement ne démarre pas (pas de sources, erreur, lien mort), cliquez sur
« Changer de source » sous ce téléchargement dans la Watchlist (ou « Choisir une source » / « Source… » pour un
film ou un épisode encore recherché). Une fenêtre liste tous les résultats des indexers pour exactement ce film,
cette saison ou cet épisode — y compris les résultats rejetés (une confirmation est demandée) — avec score, raison,
taille, seeders, indexer et priorité. Vous pouvez aussi coller un lien magnet ou l'adresse d'un fichier .torrent,
ou envoyer un fichier .torrent depuis votre ordinateur. Par défaut l'ancienne tâche Download Station est supprimée
(case « Supprimer l'ancien téléchargement ») ; cochez « Bloquer l'ancienne release » pour qu'elle ne soit plus jamais
choisie automatiquement. En mode simulation (`dry_run`), rien n'est envoyé ni supprimé.

L'interface demande l'utilisateur `admin` et le mot de passe affiché par `init` (section `[web]` de
`config.ini`). Ne videz pas ces deux valeurs : l'onglet Configuration affiche vos mots de passe. Sans mot de passe,
l'interface n'est accessible que par l'adresse IP ou le nom local du NAS.

Arrêter / redémarrer :

```bash
sudo sh scripts/stop.sh
```

```bash
sudo sh scripts/restart.sh
```

---

## Méthode B — Docker (Container Manager)

1. Faites les étapes 1 et 2 ci-dessus.
2. Préparez le projet :
   ```bash
   cd /volume1/docker/synoarr && cp docker-compose.example.yml docker-compose.yml && mkdir -p config && cp config.example.ini config/config.ini
   ```
3. Éditez `config/config.ini` comme à l'étape 3. Les chemins sont **identiques** en Docker
   (`/volume1/Download`, `/volume1/video/...`) grâce aux montages de `docker-compose.yml`. Si vos dossiers ont un
   autre chemin, modifiez aussi les lignes `source:` et `target:` de `docker-compose.yml`.
4. Container Manager → Projet → Créer : nom `synoarr`, chemin `/docker/synoarr`. DSM détecte le
   `docker-compose.yml` existant : choisissez de l'utiliser → Suivant → Terminé. L'image se construit (1 à 2 min)
   puis démarre.
5. Connexion Plex, indexers et vérification (en SSH) :
   ```bash
   sudo docker exec -it synoarr python3 /app/synoarr.py plex-login
   ```
   ```bash
   sudo docker exec -it synoarr python3 /app/synoarr.py indexer add internet_archive
   ```
   ```bash
   sudo docker exec -it synoarr python3 /app/synoarr.py check
   ```
   ```bash
   sudo docker exec -it synoarr python3 /app/synoarr.py process --dry-run
   ```

En Docker :

- le journal est dans `config/data/synoarr.log` ;
- le mot de passe de l'interface web est généré au premier démarrage : il est dans `config/config.ini`, section
  `[web]` ;
- `Download` et `video` étant deux montages séparés, un « déplacement » est en réalité une copie suivie d'une
  suppression : c'est plus lent pour les grosses saisons, et `action = hardlink` n'y fonctionne pas. Pour des
  déplacements instantanés, préférez la méthode native.

---

## Comment ça marche

1. Toutes les 15 minutes, synoarr lit votre **Watchlist Plex**.
2. Pour chaque film ou épisode manquant (déjà présent dans `video` → ignoré), il interroge vos indexers et choisit
   la meilleure release : 1080p puis 720p, en **anglais** (avec sous-titres si possible) ou en **français pour
   l'animation**, assez de sources, pas de CAM.
3. Le `.torrent` (ou le lien magnet) est envoyé à **Download Station**, dans le dossier `Download`.
4. Toutes les 2 minutes, synoarr regarde le dossier `Download`. Chaque téléchargement terminé est renommé au
   format Plex puis déplacé :
   - `video/Films/Titre (Année).mkv`
   - `video/Films/Nom Collection/Titre (Année).mkv` (films d'une collection, clé TMDb nécessaire)
   - `video/TvShows/Série/Saison 01/Série - S01E02 - Titre épisode.mkv`
   La tâche Download Station est ensuite supprimée, puis Plex est rafraîchi si `server_url` est renseigné.
5. Un téléchargement en erreur est mis sur liste noire et une autre release est recherchée.

Les vidéos que vous déposez vous-même dans `Download` sont aussi renommées et rangées
(désactivable : `[paths] only_known_downloads = true`). Les autres fichiers (documents, photos…) ne sont jamais
supprimés.

## Commandes utiles

| Commande | Effet |
|---|---|
| `sudo python3 synoarr.py check` | vérifie la configuration et toutes les connexions |
| `sudo python3 synoarr.py watchlist` | synchronise et affiche la Watchlist |
| `sudo python3 synoarr.py search` | lance une recherche maintenant |
| `sudo python3 synoarr.py search "Titre 1968"` | teste une recherche et affiche les scores (n'envoie rien) |
| `sudo python3 synoarr.py process` | traite le dossier Download maintenant |
| `sudo python3 synoarr.py status` | derniers envois et événements |
| `sudo python3 synoarr.py indexer list` | liste les indexers (avec leur priorité) |
| `sudo python3 synoarr.py indexer priority <id\|nom> <1-50>` | change la priorité d'un indexer (1 = le plus prioritaire) |
| `sudo python3 synoarr.py indexer import-prowlarr URL --api-key CLE` | importe les indexers torrent de Prowlarr |
| `sudo python3 synoarr.py indexer prowlarr-sync` | resynchronise les indexers avec Prowlarr maintenant |
| `sudo python3 synoarr.py indexer import-jackett URL --api-key CLE` | importe les indexers de Jackett (à la place de Prowlarr) |

Journal : `data/synoarr.log` (consultable dans File Station).

## Problèmes fréquents

| Message | Solution |
|---|---|
| `Identifiants DSM refusés pour l'utilisateur ...` | vérifiez `username` / `password` dans `[downloadstation]` |
| `... validation en 2 étapes (2FA) activée ...` | utilisez un compte sans 2FA (utilisateur dédié, étape 1.3) |
| `API Download Station absente sur le NAS ...` | Download Station n'est pas installé ou pas démarré |
| `... Vérifiez [downloadstation] destination ...` | `destination` doit être un dossier partagé existant, sans `/` au début |
| `Dossier partagé ... introuvable` | corrigez le chemin dans `[paths]` (File Station → Propriétés → Emplacement) |
| `... Relancez la commande avec sudo` | ajoutez `sudo` devant la commande |
| `Plex : aucun jeton` | `sudo python3 synoarr.py plex-login` |
| Rien n'est trouvé | `sudo python3 synoarr.py search "Titre Année"` montre pourquoi chaque résultat est rejeté |
| L'interface web ne s'ouvre pas | port 8778 bloqué par le pare-feu (étape 1.6), ou port déjà utilisé : changez `[web] port` |
| Téléchargements « En attente » sans fin dans Download Station | Commandes → « Diagnostic Download Station » (ou `sudo python3 synoarr.py ds-diag`). Le plus souvent : Download Station > Paramètres > BitTorrent → cocher « Activer le réseau DHT », et autoriser le port BT (16881 par défaut, TCP+UDP) dans le pare-feu DSM / la box. synoarr ajoute des trackers publics aux liens magnet et remplace un téléchargement sans source après 6 h (`[downloadstation] stalled_hours`) |
| synoarr ne démarre pas | lisez `data/console.log` et `data/synoarr.log` |

## Mise à jour

**Le plus simple (installation native) : depuis l'interface web**, onglet Commandes :

1. « Vérifier les mises à jour » : liste les fichiers qui changeraient, sans rien modifier.
2. « Mettre à jour synoarr » : télécharge la dernière version depuis GitHub, vérifie qu'elle démarre avec le
   Python du NAS (sinon rien n'est modifié), sauvegarde les fichiers remplacés, garde `config.ini` et `data/`, puis
   redémarre synoarr ; la page se recharge toute seule.
3. En cas de souci : « Revenir à la version précédente ».

Même chose en SSH : `sudo python3 synoarr.py update` (`--check` pour vérifier, `--rollback` pour revenir en
arrière), puis `sudo sh scripts/restart.sh`.

Ou avec le script :

Installation native :

```bash
cd /volume1/docker/synoarr && sudo sh scripts/update.sh
```

`config.ini` et `data/` sont conservés.

Docker : mettez d'abord les fichiers à jour avec la même commande (elle ne touche ni `config/` ni votre
`docker-compose.yml`), puis Container Manager → Projet → `synoarr` → Arrêter → Action → Construire → Démarrer.

## Désinstallation

Supprimez les deux tâches du Planificateur, lancez `sudo sh scripts/stop.sh`, puis supprimez le dossier
`/volume1/docker/synoarr`.
