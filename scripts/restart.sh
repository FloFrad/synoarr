#!/bin/sh
DIR="$(cd "$(dirname "$0")" && pwd)"
"$DIR/stop.sh"
"$DIR/start.sh" "$@"
