#!/usr/bin/env bash
# uruchom_jellyfin.sh — przygotowuje środowisko i uruchamia program.
# Domyślnie PODGLĄD. Aby wykonać zmiany:   ./uruchom_jellyfin.sh --apply
# Argumenty są przekazywane dalej (np. --cleanup-empty, --language pl-PL).

set -euo pipefail
cd "$(dirname "$0")"

if [[ ! -d .venv ]]; then
  echo "Tworzę lokalne środowisko Python (.venv)..."
  python3 -m venv .venv
fi

source .venv/bin/activate
pip install --quiet --upgrade pip
pip install --quiet paramiko requests

python jseries.py --config config.txt "$@"
