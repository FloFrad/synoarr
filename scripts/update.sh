#!/bin/sh
# Met à jour synoarr (installation native) depuis GitHub puis redémarre.
# Usage : sudo sh scripts/update.sh VOTRE-COMPTE
# config.ini et data/ sont conservés.
DIR="$(cd "$(dirname "$0")/.." && pwd)"
OWNER="${1:-}"
cd "$DIR" || exit 1
WAS_RUNNING=0
if [ -f "$DIR/synoarr.pid" ] && grep -q "synoarr.py" "/proc/$(cat "$DIR/synoarr.pid")/cmdline" 2>/dev/null; then
  WAS_RUNNING=1
fi
if [ -d .git ] && command -v git >/dev/null 2>&1; then
  git pull --ff-only || exit 1
else
  if [ -z "$OWNER" ]; then
    echo "Usage : sudo sh scripts/update.sh VOTRE-COMPTE-GITHUB" >&2
    exit 2
  fi
  TMP="$(mktemp -d)"
  wget -q -O "$TMP/s.tar.gz" "https://github.com/$OWNER/synoarr/archive/refs/heads/main.tar.gz" || { echo "Téléchargement impossible" >&2; rm -rf "$TMP"; exit 1; }
  tar xzf "$TMP/s.tar.gz" -C "$TMP" || { rm -rf "$TMP"; exit 1; }
  cp -r "$TMP/synoarr-main/." "$DIR/"
  rm -rf "$TMP"
fi
echo "Fichiers mis à jour."
if [ "$WAS_RUNNING" = 1 ]; then
  sh "$DIR/scripts/restart.sh"
else
  echo "Docker : Container Manager > Projet > synoarr > Arrêter > Action > Construire > Démarrer."
  echo "Natif  : sudo sh scripts/start.sh"
fi
