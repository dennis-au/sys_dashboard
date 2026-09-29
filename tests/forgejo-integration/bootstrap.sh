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

# These passwords exist only in this disposable bootstrap process. The test
# process authenticates solely with the repository-limited token written below.
owner_password="$(tr -dc 'A-Za-z0-9' </dev/urandom | head -c 48)"
service_password="$(tr -dc 'A-Za-z0-9' </dev/urandom | head -c 48)"

$forgejo admin user create \
  --username "$owner_username" \
  --password "$owner_password" \
  --email "$owner_email" \
  --must-change-password=false >/dev/null

repository_status="$(curl --silent --show-error --output /tmp/repository.json --write-out '%{http_code}' \
  --user "$owner_username:$owner_password" \
  --header 'Content-Type: application/json' \
  --request POST \
  --data '{"name":"sentinel-playbooks","description":"Disposable integration test repository","private":true,"auto_init":true,"default_branch":"main"}' \
  http://forgejo:3000/api/v1/user/repos)"

if [ "$repository_status" != "201" ]; then
  echo "Unable to create the disposable Forgejo repository." >&2
  exit 1
fi

$forgejo admin user create \
  --username "$service_username" \
  --password "$service_password" \
  --email "$service_email" \
  --must-change-password=false >/dev/null

collaborator_status="$(curl --silent --show-error --output /tmp/collaborator.json --write-out '%{http_code}' \
  --user "$owner_username:$owner_password" \
  --header 'Content-Type: application/json' \
  --request PUT \
  --data '{"permission":"write"}' \
  "http://forgejo:3000/api/v1/repos/$owner_username/$repository/collaborators/$service_username")"

if [ "$collaborator_status" != "204" ]; then
  echo "Unable to grant the test service account repository access." >&2
  exit 1
fi

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
protection_status="$(curl --silent --show-error --output /tmp/protection.json --write-out '%{http_code}' \
  --user "$owner_username:$owner_password" \
  --header 'Content-Type: application/json' \
  --request POST \
  --data "$protection" \
  "http://forgejo:3000/api/v1/repos/$owner_username/$repository/branch_protections")"

if [ "$protection_status" != "201" ]; then
  echo "Unable to protect the disposable main branch." >&2
  exit 1
fi

token_name="integration-$(tr -dc 'a-f0-9' </dev/urandom | head -c 12)"
token_status="$(curl --silent --show-error --output /tmp/service-token.json --write-out '%{http_code}' \
  --user "$service_username:$service_password" \
  --header 'Content-Type: application/json' \
  --request POST \
  --data "{\"name\":\"$token_name\",\"scopes\":[\"read:repository\",\"write:repository\"],\"repositories\":[{\"owner\":\"$owner_username\",\"name\":\"$repository\"}]}" \
  "http://forgejo:3000/api/v1/users/$service_username/tokens")"

if [ "$token_status" != "201" ]; then
  echo "Unable to create the disposable Forgejo service credential." >&2
  exit 1
fi

token="$(sed -n 's/.*"sha1":"\([^"]*\)".*/\1/p' /tmp/service-token.json)"
if [ -z "$token" ]; then
  echo "Forgejo returned an unusable disposable service credential." >&2
  exit 1
fi

printf '%s' "$token" > "$token_file"
chmod 600 "$token_file"
echo "Prepared disposable private repository and service credential."
