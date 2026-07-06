#!/usr/bin/env bash
# Everyday launcher for macOS and Linux: updates, then runs the app and opens the browser.
# Run with:  ./setup.sh    (or: bash setup.sh)
set -e

# Always run from this script's own folder (the project folder)
cd "$(dirname "$0")"

echo "=================================================="
echo "  WordPress Agent - updating and launching"
echo "=================================================="
echo

# ---- pull the latest files from the repo ----
if command -v git >/dev/null 2>&1; then
  echo "Getting the latest updates from the repo..."
  git pull --autostash || echo "[WARN] git pull failed - launching current files."
else
  echo "[WARN] Git not found - skipping update, launching current files."
fi

# ---- choose Python: the project's own .venv if present, else system ----
if   [ -x ".venv/bin/python" ];        then PY=".venv/bin/python"
elif command -v python3 >/dev/null 2>&1; then PY="python3"
elif command -v python  >/dev/null 2>&1; then PY="python"
else
  echo "[ERROR] Python not found. Please run install.sh first."
  exit 1
fi

# ---- install any newly added dependencies (quick if nothing changed) ----
if [ -f requirements.txt ]; then
  echo "Checking dependencies..."
  "$PY" -m pip install -q -r requirements.txt || true
fi

# ---- pick the right "open a browser" command for this OS ----
case "$(uname -s)" in
  Darwin) OPEN_CMD="open" ;;      # macOS
  *)      OPEN_CMD="xdg-open" ;;  # Linux
esac

# ---- open the browser a few seconds after the server starts ----
( sleep 3; "$OPEN_CMD" "http://127.0.0.1:5000/" >/dev/null 2>&1 ) &

echo
echo "Server starting at  http://127.0.0.1:5000/"
echo "Leave this terminal open while you use the app. Press Ctrl+C here to stop it."
echo

# ---- run the app (stays running until you stop it) ----
exec "$PY" app.py
