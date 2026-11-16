#!/usr/bin/env bash
# The catalog restore drill, locally (deploy/azure/README.md, "Catalog backup and restore").
#
#   deploy/azure/restore-drill.sh            # Postgres 17 (the server major the template allows besides 18)
#   PG_IMAGE=postgres:18 deploy/azure/restore-drill.sh
#
# Starts a throwaway Postgres container of its own (a unique name, a free port on 127.0.0.1), then runs
# service/tests/api/test_restore_drill.py against it: seed a catalog through the api, pg_dump it, change it once
# more, pg_restore into a new database, run catalog.feed_restored(), and check that the edge's cursor gets
# reset_required, the snapshot is the restored one in the new incarnation, ids move on by 2^40, and the feed
# reads on. Then runs the edge's re-snapshot unit test. The container is removed on exit, whatever happens.
# Nothing in Azure is touched.
set -euo pipefail

repo="$(cd "$(dirname "$0")/../.." && pwd)"
image="${PG_IMAGE:-postgres:17}"
name="omnifile-restore-drill-$$-$RANDOM"
password="drill-$RANDOM$RANDOM"

cleanup() { docker rm -f "$name" >/dev/null 2>&1 || true; }
trap cleanup EXIT

docker run -d --name "$name" -e POSTGRES_PASSWORD="$password" -p 127.0.0.1::5432 "$image" >/dev/null
port="$(docker port "$name" 5432/tcp | head -n1 | sed 's/.*://')"
echo "drill: $image as $name on 127.0.0.1:$port"

for _ in $(seq 1 60); do
  if docker exec "$name" pg_isready -U postgres -h localhost >/dev/null 2>&1; then break; fi
  sleep 1
done
docker exec "$name" pg_isready -U postgres -h localhost >/dev/null
echo "drill: server $(docker exec "$name" psql -U postgres -tAc 'SHOW server_version')"

export OMNIFILE_TEST_DATABASE_URL="postgresql://postgres:$password@127.0.0.1:$port/postgres"
export OMNIFILE_RESTORE_DRILL=1
export OMNIFILE_RESTORE_DRILL_EXEC="docker exec -i -e PGPASSWORD=$password $name"
export OMNIFILE_RESTORE_DRILL_SERVER="postgresql://postgres@localhost:5432/postgres"

(cd "$repo/service" && uv run --quiet pytest -q -p no:cacheprovider tests/api/test_restore_drill.py "$@")

# The edge's half: a re-snapshot after a restore applies the wound-back rows and finds the lost ones.
(cd "$repo/edge" && cargo test --quiet -p omnifile-replica a_resnapshot_after_a_restore 2>&1 | tail -n 3)
