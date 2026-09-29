#!/bin/sh
set -eu

repository_root="$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)"
compose_file="$repository_root/tests/forgejo-integration/compose.yaml"
project="sentinel-forgejo-integration-$(date +%s)-$$"

cleanup() {
  docker compose --project-name "$project" --file "$compose_file" down --volumes --remove-orphans --rmi local >/dev/null 2>&1 || true
}

trap cleanup EXIT HUP INT TERM

docker compose --project-name "$project" --file "$compose_file" up --build --wait forgejo
docker compose --project-name "$project" --file "$compose_file" run --rm --no-deps forgejo-credentials-init
docker compose --project-name "$project" --file "$compose_file" run --rm --no-deps forgejo-bootstrap
docker compose --project-name "$project" --file "$compose_file" run --rm --no-deps forgejo-integration-test
