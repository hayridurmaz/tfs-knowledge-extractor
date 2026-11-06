#!/bin/sh
# Nightly refresh on a docker compose server: code graph, then history.
# crontab -e:   0 2 * * * /opt/kg/deploy/nightly.sh >> /opt/kg/nightly.log 2>&1
set -u
cd "$(dirname "$0")/.." || exit 1
echo "=== $(date -Is) nightly start"
docker compose pull -q mcp || true                 # same image as ingest; picks up a new KG_IMAGE tag
docker compose up -d                               # restart the MCP server if its image changed
docker compose run --rm ingest ingest; code=$?     # exit 1 = some repos failed to fetch (graph still updated)
if [ "$code" -gt 1 ]; then
  echo "=== ingest failed ($code), skipping history"; exit "$code"
fi
docker compose run --rm ingest history; hcode=$?
echo "=== $(date -Is) nightly done (ingest=$code history=$hcode)"
[ "$code" -eq 0 ] && [ "$hcode" -eq 0 ]
