#!/bin/sh
# Démarre synoarr en arrière-plan (ne fait rien s'il tourne déjà).
# Utilisable tel quel dans le Planificateur de tâches DSM (utilisateur root).
DIR="$(cd "$(dirname "$0")/.." && pwd)"
PIDFILE="$DIR/synoarr.pid"
export LANG=C.UTF-8 LC_ALL=C.UTF-8 PYTHONUTF8=1 PYTHONIOENCODING=utf-8 PYTHONDONTWRITEBYTECODE=1

PY=""
for c in /usr/local/bin/python3 /usr/bin/python3 /bin/python3 python3; do
  if command -v "$c" >/dev/null 2>&1 && "$c" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 8) else 1)' 2>/dev/null; then
    PY="$c"; break
  fi
done
if [ -z "$PY" ]; then
  echo "Python 3.8+ introuvable. Installez « Python 3 » depuis le Centre de paquets." >&2
  exit 1
fi

if [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
  echo "synoarr tourne déjà (PID $(cat "$PIDFILE"))"
  exit 0
fi

mkdir -p "$DIR/data"
cd "$DIR" || exit 1
nohup "$PY" "$DIR/synoarr.py" run "$@" >> "$DIR/data/console.log" 2>&1 &
echo $! > "$PIDFILE"
sleep 2
if kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
  echo "synoarr démarré (PID $(cat "$PIDFILE")) — journal : $DIR/data/synoarr.log"
else
  echo "synoarr s'est arrêté immédiatement. Dernières lignes de $DIR/data/console.log :" >&2
  tail -n 20 "$DIR/data/console.log" >&2
  rm -f "$PIDFILE"
  exit 1
fi
