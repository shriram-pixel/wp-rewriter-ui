#!/usr/bin/env bash
# macOS double-click launcher: updates, runs the app, opens the browser.
set -e

# When double-clicked, run relative to this file's own folder
cd "$(dirname "$0")"

echo "=================================================="
echo "  WordPress Agent - updating and launching (macOS)"
echo "=================================================="
echo

if command -v git >/dev/null 2>&1; then
  echo "Getting the latest updates from the repo..."
  git pull --autostash || echo "[WARN] git pull failed - launching current files."
else
  echo "[WARN] Git not found - skipping update, launching current files."
fi

if   [ -x ".venv/bin/python" ];         then PY=".venv/bin/python"
elif command -v python3 >/dev/null 2>&1; then PY="python3"
elif command -v python  >/dev/null 2>&1; then PY="python"
else
  echo "[ERROR] Python not found. Please run install.command first."
  echo; read -p "Press Return to close..." _; exit 1
fi

if [ -f requirements.txt ]; then
  echo "Checking dependencies..."
  "$PY" -m pip install -q -r requirements.txt || true
fi

# open the default browser a few seconds after the server starts
( sleep 3; open "http://127.0.0.1:5000/" >/dev/null 2>&1 ) &

echo
echo "Server starting at  http://127.0.0.1:5000/"
echo "Leave this window open while you use the app. Press Ctrl+C here to stop it."
echo

set +e
"$PY" app.py

echo
echo "The server has stopped."
read -p "Press Return to close this window..." _
