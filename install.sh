#!/bin/sh
# Installs meta: copies meta.py to ~/tools and adds a `meta` alias to ~/.zshrc.
# Safe to run again; it updates the copy and leaves an existing alias alone.
set -e

src="$(cd "$(dirname "$0")" && pwd)/meta.py"
dest="$HOME/tools/meta.py"
rc="$HOME/.zshrc"

mkdir -p "$HOME/tools"
cp "$src" "$dest"
echo "Copied meta.py to $dest"

if grep -q "^alias meta=" "$rc" 2>/dev/null; then
  echo "Alias already in $rc"
else
  printf '\n# meta: view, edit and strip image/video metadata\nalias meta="python3 ~/tools/meta.py"\n' >> "$rc"
  echo "Added alias to $rc. Open a new terminal (or run: source ~/.zshrc)."
fi

if ! command -v exiftool >/dev/null 2>&1 && [ ! -x /opt/homebrew/bin/exiftool ]; then
  echo "exiftool is missing. Install it with: brew install exiftool"
fi

# A running meta keeps the old code until it restarts.
if [ "$(python3 "$dest" stop 2>/dev/null)" = "meta stopped." ]; then
  echo "Stopped the running meta; run meta to start the new version."
fi
