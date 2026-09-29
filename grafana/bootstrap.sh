#!/bin/sh
set -eu

grafana_url="${GRAFANA_URL:-http://grafana:3000}"
service_name="sentinel-portal"
token_file="${GRAFANA_API_TOKEN_FILE:-/run/grafana-service-credentials/grafana_api_token}"

umask 077
mkdir -p "$(dirname "$token_file")"

if [ -s "$token_file" ]; then
  echo "Retained existing Grafana portal service credential."
  exit 0
fi

if [ -n "${GRAFANA_ADMIN_PASSWORD_FILE:-}" ]; then
  admin_password="$(cat "$GRAFANA_ADMIN_PASSWORD_FILE")"
else
  admin_password="${GRAFANA_ADMIN_PASSWORD:-}"
fi

if [ -z "${GRAFANA_ADMIN_USER:-}" ] || [ -z "$admin_password" ]; then
  echo "Grafana bootstrap administrator credentials are unavailable." >&2
  exit 1
fi

for _ in $(seq 1 30); do
  if curl --fail --silent --show-error "$grafana_url/api/health" >/dev/null 2>&1; then
    break
  fi
  sleep 1
done

admin_auth="${GRAFANA_ADMIN_USER}:${admin_password}"
create_status="$(curl --silent --show-error --output /tmp/service-account.json --write-out '%{http_code}' \
  --user "$admin_auth" \
  --header 'Content-Type: application/json' \
  --request POST \
  --data "$(jq -nc --arg name "$service_name" '{name:$name,role:"Viewer",isDisabled:false}')" \
  "$grafana_url/api/serviceaccounts")"

case "$create_status" in
  200|201)
    service_id="$(jq -r '.id // empty' /tmp/service-account.json)"
    ;;
  409)
    curl --fail --silent --show-error --user "$admin_auth" \
      "$grafana_url/api/serviceaccounts/search?query=$service_name&perpage=100" \
      > /tmp/service-accounts.json
    service_id="$(jq -r --arg name "$service_name" '.serviceAccounts[]? | select(.name == $name) | .id' /tmp/service-accounts.json | head -n 1)"
    ;;
  *)
    echo "Grafana portal service account could not be initialized." >&2
    exit 1
    ;;
esac

if [ -z "$service_id" ] || ! printf '%s' "$service_id" | grep -Eq '^[0-9]+$'; then
  echo "Grafana portal service account is unavailable." >&2
  exit 1
fi

token_name="sentinel-portal-$(tr -dc 'a-f0-9' </dev/urandom | head -c 12)"
token_status="$(curl --silent --show-error --output /tmp/service-token.json --write-out '%{http_code}' \
  --user "$admin_auth" \
  --header 'Content-Type: application/json' \
  --request POST \
  --data "$(jq -nc --arg name "$token_name" '{name:$name}')" \
  "$grafana_url/api/serviceaccounts/$service_id/tokens")"

if [ "$token_status" != "200" ]; then
  echo "Grafana portal service credential could not be initialized." >&2
  exit 1
fi

token="$(jq -r '.key // empty' /tmp/service-token.json)"
if [ -z "$token" ]; then
  echo "Grafana returned an unusable portal service credential." >&2
  exit 1
fi

temporary_file="$token_file.$$"
printf '%s' "$token" > "$temporary_file"
chmod 600 "$temporary_file"
mv "$temporary_file" "$token_file"
echo "Created Grafana portal service credential."
