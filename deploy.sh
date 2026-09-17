#!/bin/bash
# Push local changes to the droplet. Run from this repo on a machine with the key.
#
# Only the three files the scraper needs. .env is deliberately NOT synced — it
# holds the ingest secret and is managed on the droplet directly.
set -euo pipefail

HOST="${HOST:-root@139.59.61.222}"
KEY="${KEY:-$HOME/.ssh/trripah_droplet}"
DEST=/opt/fk-flight-finder

scp -i "$KEY" scrape.py routes.json run.sh "$HOST:$DEST/"
ssh -i "$KEY" "$HOST" "chmod +x $DEST/run.sh && cd $DEST && ./.venv/bin/python -c 'import ast;ast.parse(open(\"scrape.py\").read());print(\"scrape.py parses\")'"
echo "deployed to $HOST:$DEST"
