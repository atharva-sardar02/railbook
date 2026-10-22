#!/usr/bin/env bash
# Prints one field of a KV secret, e.g. the Grafana admin password:
#
#   scripts/vault-read.sh secret/monitoring/grafana admin_password
#
# Uses the root token from ~/.railbook/vault-init.json, sent on stdin (never as an argument).
set -euo pipefail
path=${1:?usage: $0 KV_PATH FIELD}
field=${2:?usage: $0 KV_PATH FIELD}

INIT_FILE="${RAILBOOK_HOME:-$HOME/.railbook}/vault-init.json"
ROOT_TOKEN=$(python3 -c "import json; print(json.load(open('$INIT_FILE'))['root_token'])")

printf 'export VAULT_TOKEN=%s\nvault kv get -field=%s %s\n' "$ROOT_TOKEN" "$field" "$path" \
  | kubectl exec -i -n vault vault-0 -- sh -s
echo
