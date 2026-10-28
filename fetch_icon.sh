#!/usr/bin/env bash
# Downloads the current game icon to output/icon.png (e.g. for the Discord Developer Portal)
set -euo pipefail
cd "$(dirname "$0")"
mkdir -p output
cd output
"${PYTHON:-python3}" -B ../src/main.py --fetch-icon
