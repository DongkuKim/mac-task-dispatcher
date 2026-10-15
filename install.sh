#!/bin/bash
# Sets up mac-task-dispatcher: Python venv, queue.json, and the Hammerspoon hook.
set -euo pipefail

REPO="$(cd "$(dirname "$0")" && pwd)"
HS_DIR="$HOME/.hammerspoon"

if ! command -v tmux >/dev/null 2>&1; then
    echo "tmux is required. Install it with: brew install tmux" >&2
    exit 1
fi

echo "Creating Python environment in $REPO/.venv"
python3 -m venv "$REPO/.venv"
"$REPO/.venv/bin/pip" install --quiet --upgrade pip
"$REPO/.venv/bin/pip" install --quiet -r "$REPO/requirements.txt"

if [ ! -f "$REPO/queue.json" ]; then
    # Start empty: the example tasks would run for real as soon as Hammerspoon loads.
    printf '{\n  "settings": {},\n  "tasks": []\n}\n' > "$REPO/queue.json"
    echo "Created an empty queue.json. Add tasks to it (see queue.example.json)."
fi

mkdir -p "$HS_DIR"
ln -sf "$REPO/hammerspoon/mac_task_dispatcher.lua" "$HS_DIR/mac_task_dispatcher.lua"
LINE="require(\"mac_task_dispatcher\").start({ repo = \"$REPO\" })"
if grep -qF 'require("mac_task_dispatcher")' "$HS_DIR/init.lua" 2>/dev/null; then
    echo "~/.hammerspoon/init.lua already loads mac_task_dispatcher; left unchanged."
else
    printf '\n-- mac-task-dispatcher\n%s\n' "$LINE" >> "$HS_DIR/init.lua"
    echo "Added mac_task_dispatcher to ~/.hammerspoon/init.lua"
fi

if [ ! -d "/Applications/Hammerspoon.app" ]; then
    echo
    echo "Hammerspoon isn't installed yet. Install it with: brew install --cask hammerspoon"
fi
echo
echo "Done. Open Hammerspoon (or choose Reload Config from its menu) to start dispatching."
echo "Check tasks any time with: $REPO/.venv/bin/python $REPO/dispatcher.py status"
