#!/bin/bash
# Double-click launcher (macOS). Starts the caption bar with no command line;
# settings are entered in the app. Resolves its own folder so it works wherever
# this folder lives. Quit with the power button (or Esc) in the bar.
DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$DIR" || exit 1
exec "$DIR/.venv/bin/python" "$DIR/duo_web.py" >>"$DIR/duo_web.log" 2>&1
