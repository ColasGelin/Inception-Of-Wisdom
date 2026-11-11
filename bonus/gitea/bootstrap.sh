#!/bin/sh
# One-shot and idempotent: admin user, API token and the `target` repository, no clicks.
# Runs in the gitea image next to the server (same /data volume) once it is healthy.
set -eu
CONF=/data/gitea/conf/app.ini
API=http://gitea:3000/api/v1

if [ -s /bootstrap/token ]; then
  echo "[gitea] already bootstrapped (user iow, repo iow/target)"
  exit 0
fi

PASS=$(head -c 32 /dev/urandom | base64 | tr -dc 'A-Za-z0-9' | head -c 20)
su-exec git gitea admin user create --config "$CONF" --admin --username iow \
  --password "$PASS" --email iow@localhost --must-change-password=false >/dev/null
TOKEN=$(su-exec git gitea admin user generate-access-token --config "$CONF" --username iow \
  --token-name iow-agent --scopes all --raw)
curl -fsS -X POST "$API/user/repos" -H "Authorization: token $TOKEN" -H 'Content-Type: application/json' \
  -d '{"name":"target","private":false,"default_branch":"main","description":"Mirror of the IoW target workspace: heals arrive as pull requests"}' >/dev/null

umask 022
printf '%s' "$TOKEN" > /bootstrap/token
printf 'url:      http://127.0.0.1:%s/iow/target\nuser:     iow\npassword: %s\n' "${IOW_GITEA_PORT:-3042}" "$PASS" > /bootstrap/credentials
echo "[gitea] created admin user iow, an API token and the repository iow/target"
