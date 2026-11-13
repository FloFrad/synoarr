#!/bin/sh
# Démarre synoarr en arrière-plan (ne fait rien s'il tourne déjà).
# Utilisable tel quel dans le Planificateur de tâches DSM (utilisateur root).
DIR="$(cd "$(dirname "$0")/.." && pwd)"
PIDFILE="$DIR/synoarr.pid"
LOG="$DIR/data/console.log"
export LANG=C.UTF-8 LC_ALL=C.UTF-8 PYTHONUTF8=1 PYTHONIOENCODING=utf-8 PYTHONDONTWRITEBYTECODE=1

# vrai si le PID existe ET correspond bien à synoarr (évite un PID réutilisé après un redémarrage)
is_synoarr() {
  [ -n "$1" ] && kill -0 "$1" 2>/dev/null && grep -q "synoarr.py" "/proc/$1/cmdline" 2>/dev/null
}

PY=""
for c in python3 /usr/local/bin/python3 /usr/bin/python3 /bin/python3 python3.13 python3.12 python3.11 python3.10 python3.9 python3.8; do
  if command -v "$c" >/dev/null 2>&1 && "$c" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 8) else 1)' 2>/dev/null; then
    PY="$c"; break
  fi
done
if [ -z "$PY" ]; then
  echo "Python 3.8+ introuvable. Installez « Python 3 » depuis le Centre de paquets." >&2
  exit 1
fi

if [ -f "$PIDFILE" ] && is_synoarr "$(cat "$PIDFILE")"; then
  echo "synoarr tourne déjà (PID $(cat "$PIDFILE"))"
  exit 0
fi
rm -f "$PIDFILE"

mkdir -p "$DIR/data"
# console.log ne garde que le dernier démarrage (le journal complet est data/synoarr.log)
: > "$LOG"
cd "$DIR" || exit 1
nohup "$PY" "$DIR/synoarr.py" run "$@" >> "$LOG" 2>&1 &
echo $! > "$PIDFILE"
sleep 3
if is_synoarr "$(cat "$PIDFILE")"; then
  echo "synoarr démarré (PID $(cat "$PIDFILE"), $("$PY" --version 2>&1)) — journal : $DIR/data/synoarr.log"
else
  echo "synoarr s'est arrêté immédiatement. Sortie :" >&2
  tail -n 30 "$LOG" >&2
  rm -f "$PIDFILE"
  exit 1
fi
