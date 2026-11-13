# Guide d'installation de synoarr sur un NAS Synology

Durée totale : **20 à 30 minutes**. Aucune connaissance en programmation nécessaire : il suffit de copier-coller
les commandes.

Deux méthodes :

- **Méthode A — native (recommandée, marche sur tous les modèles)** : utilise le Python déjà présent dans DSM 7.
- **Méthode B — Docker / Container Manager** : seulement si votre modèle propose Container Manager.

Dans les commandes, remplacez `VOTRE-COMPTE` par votre nom d'utilisateur GitHub.

---

## Étape 1 — Préparer DSM (5 min)

1. **Installer Download Station** : Centre de paquets → rechercher « Download Station » → Installer.
2. **Activer SSH** : Panneau de configuration → Terminal & SNMP → cocher « Activer le service SSH » → Appliquer.
3. **Créer un utilisateur dédié** (conseillé, car la double authentification bloque l'API) :
   Panneau de configuration → Utilisateur et groupe → Créer :
   - Nom : `synoarr`, un mot de passe solide (évitez les espaces suivis de `;` ou `#`).
   - Groupe : `users` suffit.
   - Permissions des dossiers partagés : **Lecture/Écriture** sur `Download` et `Vidéos`.
   - Applications : **Autoriser** Download Station.
   - Ne pas activer la double authentification pour ce compte.
4. **Noter les chemins exacts de vos dossiers** : File Station → clic droit sur le dossier `Download` →
   Propriétés → « Emplacement » (ex. `/volume1/Download`). Faites de même pour `Vidéos`.
5. **Créer le dossier d'installation** : si vous n'avez pas de dossier partagé `docker`, créez-le
   (Panneau de configuration → Dossier partagé → Créer → nom `docker`). Il servira aussi à éditer la config
   depuis File Station.

## Étape 2 — Télécharger synoarr (2 min)

Connectez-vous en SSH depuis votre ordinateur (Terminal sur Mac/Linux, PowerShell sur Windows) avec votre compte
administrateur DSM :

```bash
ssh admin@IP-DU-NAS
```

Puis :

```bash
cd /volume1/docker && wget -O synoarr.tar.gz https://github.com/VOTRE-COMPTE/synoarr/archive/refs/heads/main.tar.gz && tar xzf synoarr.tar.gz && mv synoarr-main synoarr && rm synoarr.tar.gz && cd synoarr
```

Vérifiez Python :

```bash
python3 --version
```

Il faut **3.8 ou plus**. Si la commande est introuvable : Centre de paquets → installer « Python 3.9 » (ou plus
récent), puis recommencez.

> Toutes les commandes suivantes se lancent depuis `/volume1/docker/synoarr` **avec `sudo`** (le service tourne
> en root ; sans `sudo` la base de données ne serait plus modifiable ensuite).

## Étape 3 — Configurer (5 min)

```bash
sudo python3 synoarr.py init
```

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
movies_dir = /volume1/Vidéos/Films
series_dir = /volume1/Vidéos/Séries
```

Règle importante : `download_dir` doit être le chemin complet du dossier indiqué dans `destination`
(`destination = Download` ↔ `download_dir = /volume1/Download`).

Les autres réglages (qualité 1080p puis 720p, langues anglais/français, format des noms façon Plex) ont déjà les
bonnes valeurs par défaut.

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

Ajouter n'importe quel indexer par son URL (flux **Torznab** ou **RSS**, le type est détecté tout seul) :

```bash
sudo python3 synoarr.py indexer add-url "Mon indexer" "https://exemple.org/api" --api-key VOTRE_CLE
```

Les indexers se gèrent aussi depuis l'interface web (onglet Indexers).

## Étape 6 — Tout vérifier (1 min)

```bash
sudo python3 synoarr.py check
```

Chaque ligne doit commencer par `[OK]`. Sinon, le message indique quoi corriger (voir « Problèmes fréquents »).

Test à blanc, sans rien télécharger ni déplacer :

```bash
sudo python3 synoarr.py search --dry-run
```

```bash
sudo python3 synoarr.py process --dry-run
```

## Étape 7 — Démarrage automatique (3 min)

Panneau de configuration → **Planificateur de tâches** :

1. Créer → Tâche déclenchée → Script défini par l'utilisateur.
   - Général : nom `synoarr`, utilisateur **root**, événement **Démarrage**.
   - Paramètres de tâche → Exécuter la commande :
     ```
     sh /volume1/docker/synoarr/scripts/start.sh
     ```
2. Créer → Tâche planifiée → Script défini par l'utilisateur (redémarre synoarr s'il s'est arrêté).
   - Général : nom `synoarr-surveillance`, utilisateur **root**.
   - Planification : tous les jours, toutes les **10 minutes**.
   - Même commande que ci-dessus.
3. Sélectionnez la tâche `synoarr` → **Exécuter** pour la lancer tout de suite.

Ouvrez l'interface : **http://IP-DU-NAS:8778**

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
2. Créez le dossier `config` et la configuration :
   ```bash
   cd /volume1/docker/synoarr && mkdir -p config && cp config.example.ini config/config.ini
   ```
3. Éditez `config/config.ini` comme à l'étape 3. Les chemins sont **identiques** en Docker
   (`/volume1/Download`, `/volume1/Vidéos/...`), grâce aux montages du fichier `docker-compose.yml`. Si vos dossiers
   ont un autre chemin, modifiez aussi les lignes `volumes:` de `docker-compose.yml`.
4. Container Manager → Projet → Créer : nom `synoarr`, chemin `/docker/synoarr`, source « Utiliser le
   docker-compose.yml existant » → Suivant → Terminé. L'image se construit (1 à 2 min) puis démarre.
5. Connexion Plex et indexers (en SSH) :
   ```bash
   sudo docker exec -it synoarr python3 /app/synoarr.py plex-login
   ```
   ```bash
   sudo docker exec -it synoarr python3 /app/synoarr.py indexer add internet_archive
   ```
   ```bash
   sudo docker exec -it synoarr python3 /app/synoarr.py check
   ```

---

## Comment ça marche

1. Toutes les 15 minutes, synoarr lit votre **Watchlist Plex**.
2. Pour chaque film ou épisode manquant (déjà présent dans `Vidéos` → ignoré), il interroge vos indexers et choisit
   la meilleure release : 1080p puis 720p, anglais ou français (MULTI accepté), assez de sources, pas de CAM.
3. Le `.torrent` (ou magnet) est envoyé à **Download Station**, dans le dossier `Download`.
4. Toutes les 2 minutes, synoarr regarde le dossier `Download` : chaque téléchargement terminé est renommé au
   format Plex puis déplacé :
   - `Vidéos/Films/Titre (Année)/Titre (Année).mkv`
   - `Vidéos/Séries/Série/Season 01/Série - S01E02.mkv`
   La tâche Download Station est ensuite supprimée et Plex rafraîchi (si `server_url` est renseigné).
5. Un téléchargement en erreur est mis sur liste noire et une autre release est recherchée.

Les fichiers que vous déposez vous-même dans `Download` sont aussi renommés et rangés
(désactivable : `[paths] only_known_downloads = true`).

## Commandes utiles

| Commande | Effet |
|---|---|
| `sudo python3 synoarr.py check` | vérifie la configuration et toutes les connexions |
| `sudo python3 synoarr.py watchlist` | synchronise et affiche la Watchlist |
| `sudo python3 synoarr.py search` | lance une recherche maintenant |
| `sudo python3 synoarr.py search "Titre 1968"` | teste une recherche et affiche les scores (n'envoie rien) |
| `sudo python3 synoarr.py process` | traite le dossier Download maintenant |
| `sudo python3 synoarr.py status` | derniers envois et événements |
| `sudo python3 synoarr.py indexer list` | liste les indexers |

Journal : `data/synoarr.log` (consultable dans File Station).

## Problèmes fréquents

| Message | Solution |
|---|---|
| `Download Station : identifiants refusés` | vérifiez `username` / `password` dans `[downloadstation]` |
| `... double authentification ...` | utilisez un compte sans 2FA (utilisateur dédié, étape 1.3) |
| `... API Download Station introuvable` | Download Station n'est pas installé ou pas démarré |
| `... dossier de destination ...` | `destination` doit être un dossier partagé existant, sans `/` au début |
| `Dossier ... non accessible en écriture` | lancez la commande avec `sudo`, vérifiez le chemin dans File Station |
| `Plex : aucun jeton` | `sudo python3 synoarr.py plex-login` |
| Rien n'est trouvé | `sudo python3 synoarr.py search "Titre Année"` montre pourquoi chaque résultat est rejeté |
| L'interface web ne s'ouvre pas | port 8778 bloqué par le pare-feu DSM, ou changez `[web] port` |

## Mise à jour

```bash
cd /volume1/docker/synoarr && sudo sh scripts/stop.sh && wget -O /tmp/s.tar.gz https://github.com/VOTRE-COMPTE/synoarr/archive/refs/heads/main.tar.gz && tar xzf /tmp/s.tar.gz -C /tmp && cp -r /tmp/synoarr-main/. . && rm -rf /tmp/s.tar.gz /tmp/synoarr-main && sudo sh scripts/start.sh
```

`config.ini` et `data/` ne sont pas écrasés (ils ne font pas partie de l'archive).

## Désinstallation

Supprimez les deux tâches du Planificateur, lancez `sudo sh scripts/stop.sh`, puis supprimez le dossier
`/volume1/docker/synoarr`.
