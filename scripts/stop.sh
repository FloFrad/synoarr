#!/bin/sh
# Arrête synoarr proprement.
DIR="$(cd "$(dirname "$0")/.." && pwd)"
PIDFILE="$DIR/synoarr.pid"
is_synoarr() {
  [ -n "$1" ] && kill -0 "$1" 2>/dev/null && grep -q "synoarr.py" "/proc/$1/cmdline" 2>/dev/null
}
if [ ! -f "$PIDFILE" ] || ! is_synoarr "$(cat "$PIDFILE")"; then
  echo "synoarr ne tourne pas"
  rm -f "$PIDFILE"
  exit 0
fi
PID="$(cat "$PIDFILE")"
kill "$PID"
i=0
while kill -0 "$PID" 2>/dev/null && [ $i -lt 30 ]; do sleep 1; i=$((i+1)); done
if kill -0 "$PID" 2>/dev/null; then kill -9 "$PID"; fi
rm -f "$PIDFILE"
echo "synoarr arrêté"
