#!/bin/sh
set -eu

config=/var/lib/gitea/custom/conf/app.ini
forgejo="forgejo --work-path /var/lib/gitea --config $config"
owner_username=sentinel
owner_email=sentinel@localhost
repository=sentinel-playbooks
service_username=sentinel-api
service_email=sentinel-api@localhost
credentials_dir=/run/sentinel-forgejo-credentials
token_file="$credentials_dir/forgejo_api_token"

umask 077
mkdir -p "$credentials_dir"

# This password exists only within this bootstrap process. It provisions the
# internal repository owner and is never used by Sentinel at runtime.
owner_password="$(tr -dc 'A-Za-z0-9' </dev/urandom | head -c 48)"

if $forgejo admin user create \
  --username "$owner_username" \
  --password "$owner_password" \
  --email "$owner_email" \
  --must-change-password=false >/dev/null 2>&1; then
  echo "Created internal Forgejo repository owner."
else
  $forgejo admin user change-password \
    --username "$owner_username" \
    --password "$owner_password" \
    --must-change-password=false >/dev/null
fi

status="$(curl --silent --show-error --output /tmp/repository.json --write-out '%{http_code}' \
  --user "$owner_username:$owner_password" \
  --header 'Content-Type: application/json' \
  --request POST \
  --data '{"name":"sentinel-playbooks","description":"Sentinel-managed playbook source","private":true,"auto_init":true,"default_branch":"main"}' \
  http://forgejo:3000/api/v1/user/repos)"

case "$status" in
  201) echo "Created private sentinel-playbooks repository." ;;
  409) echo "Private sentinel-playbooks repository already exists." ;;
  *)
    cat /tmp/repository.json >&2
    exit 1
    ;;
esac

# The API is a repository-scoped collaborator, never the repository owner. Its
# password is random bootstrap-only material; Sentinel uses only its API token.
service_password="$(tr -dc 'A-Za-z0-9' </dev/urandom | head -c 48)"
if $forgejo admin user create \
  --username "$service_username" \
  --password "$service_password" \
  --email "$service_email" \
  --must-change-password=false >/dev/null 2>&1; then
  echo "Created internal Forgejo API service account."
else
  $forgejo admin user change-password \
    --username "$service_username" \
    --password "$service_password" \
    --must-change-password=false >/dev/null
fi

collaborator_status="$(curl --silent --show-error --output /tmp/collaborator.json --write-out '%{http_code}' \
  --user "$owner_username:$owner_password" \
  --header 'Content-Type: application/json' \
  --request PUT \
  --data '{"permission":"write"}' \
  "http://forgejo:3000/api/v1/repos/$owner_username/$repository/collaborators/$service_username")"

case "$collaborator_status" in
  204) echo "Granted the API service account repository write access." ;;
  *)
    cat /tmp/collaborator.json >&2
    exit 1
    ;;
esac

# Direct writes to main are prohibited. Sentinel will create profile edit
# branches and pull requests; a review is required before a revision is merged.
protection='{
  "rule_name":"main",
  "enable_push":false,
  "enable_push_whitelist":false,
  "enable_merge_whitelist":false,
  "required_approvals":1,
  "dismiss_stale_approvals":true,
  "block_on_rejected_reviews":true,
  "block_on_outdated_branch":true,
  "block_on_official_review_requests":true,
  "apply_to_admins":true
}'
protection_url="http://forgejo:3000/api/v1/repos/$owner_username/$repository/branch_protections/main"
protection_status="$(curl --silent --show-error --output /tmp/protection.json --write-out '%{http_code}' \
  --user "$owner_username:$owner_password" \
  --header 'Content-Type: application/json' \
  --request PATCH \
  --data "$protection" \
  "$protection_url")"

case "$protection_status" in
  200) echo "Updated main branch review protection." ;;
  404)
    protection_status="$(curl --silent --show-error --output /tmp/protection.json --write-out '%{http_code}' \
      --user "$owner_username:$owner_password" \
      --header 'Content-Type: application/json' \
      --request POST \
      --data "$protection" \
      "http://forgejo:3000/api/v1/repos/$owner_username/$repository/branch_protections")"
    if [ "$protection_status" = "201" ]; then
      echo "Created main branch review protection."
    else
      cat /tmp/protection.json >&2
      exit 1
    fi
    ;;
  *)
    cat /tmp/protection.json >&2
    exit 1
    ;;
esac

if [ ! -s "$token_file" ]; then
  # Token names are immutable in Forgejo. A unique label makes bootstrap
  # recovery safe if an interrupted first run created a token before its value
  # reached the local secret volume.
  token_name="sentinel-api-local-$(tr -dc 'a-f0-9' </dev/urandom | head -c 12)"
  token_status="$(curl --silent --show-error --output /tmp/service-token.json --write-out '%{http_code}' \
    --user "$service_username:$service_password" \
    --header 'Content-Type: application/json' \
    --request POST \
    --data "{\"name\":\"$token_name\",\"scopes\":[\"read:repository\",\"write:repository\"],\"repositories\":[{\"owner\":\"$owner_username\",\"name\":\"$repository\"}]}" \
    "http://forgejo:3000/api/v1/users/$service_username/tokens")"
  if [ "$token_status" != "201" ]; then
    cat /tmp/service-token.json >&2
    exit 1
  fi
  token="$(sed -n 's/.*"sha1":"\([^"]*\)".*/\1/p' /tmp/service-token.json)"
  if [ -z "$token" ]; then
    echo "Forgejo returned an unusable API service credential." >&2
    exit 1
  fi
  temp_token_file="$token_file.$$"
  printf '%s' "$token" > "$temp_token_file"
  chmod 600 "$temp_token_file"
  mv "$temp_token_file" "$token_file"
  echo "Created local Forgejo API service credential."
else
  echo "Retained existing local Forgejo API service credential."
fi
