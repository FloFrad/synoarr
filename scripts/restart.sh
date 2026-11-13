#!/bin/sh
DIR="$(cd "$(dirname "$0")" && pwd)"
sh "$DIR/stop.sh"
sh "$DIR/start.sh" "$@"
