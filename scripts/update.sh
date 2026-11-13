#!/bin/sh
# Met à jour synoarr depuis GitHub (si installé avec git) puis redémarre.
DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$DIR" || exit 1
if [ -d .git ]; then
  git pull --ff-only || exit 1
else
  echo "Pas un dépôt git : téléchargez la nouvelle archive et remplacez les fichiers (gardez config.ini et data/)."
  exit 1
fi
"$DIR/scripts/restart.sh"
