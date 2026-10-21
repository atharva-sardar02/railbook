#!/usr/bin/env bash
# Initialises Vault the first time, and unseals it every time (Vault starts sealed after any restart).
#
# "Secret zero": the unseal key shares and root token can't be stored in Vault itself, so they live
# in ~/.railbook/vault-init.json — outside the repo, readable only by you. Lose this file and the
# data in Vault is unrecoverable; leak it and someone can open Vault.
set -euo pipefail

NS=vault
POD=vault-0
INIT_FILE="${RAILBOOK_HOME:-$HOME/.railbook}/vault-init.json"

vault_exec() { kubectl exec -i -n "$NS" "$POD" -- "$@"; }
status_field() { vault_exec vault status -format=json 2>/dev/null | python3 -c "import sys,json; print(json.load(sys.stdin)['$1'])"; }

if [ "$(status_field initialized)" = "False" ]; then
  mkdir -p "$(dirname "$INIT_FILE")" && chmod 700 "$(dirname "$INIT_FILE")"
  umask 077
  # 5 key shares, any 3 rebuild the master key (Shamir's Secret Sharing).
  vault_exec vault operator init -key-shares=5 -key-threshold=3 -format=json > "$INIT_FILE"
  echo "Vault initialised; keys and root token saved to $INIT_FILE"
fi

if [ "$(status_field sealed)" = "True" ]; then
  [ -f "$INIT_FILE" ] || { echo "Vault is sealed and $INIT_FILE is missing" >&2; exit 1; }
  # Keys are sent on stdin, never as command-line arguments (those show up in process lists).
  for i in 0 1 2; do
    python3 -c "import json; print(json.load(open('$INIT_FILE'))['unseal_keys_b64'][$i])" \
      | vault_exec sh -c 'read -r key && vault operator unseal "$key"' >/dev/null
  done
  echo "Vault unsealed"
fi

vault_exec vault status | grep -E 'Initialized|Sealed'
