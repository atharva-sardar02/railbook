#!/usr/bin/env bash
# Configures Vault for RailBook. Safe to run repeatedly.
#
#   scripts/vault-setup.sh base       # KV secrets, Kubernetes auth, policies, roles  (before Postgres)
#   scripts/vault-setup.sh database   # dynamic Postgres credentials                  (after Postgres is up)
#
# Commands run inside the vault-0 pod. The root token and any generated secrets are sent on
# stdin, never as command-line arguments, so they don't appear in process lists.
set -euo pipefail

INIT_FILE="${RAILBOOK_HOME:-$HOME/.railbook}/vault-init.json"
ROOT_TOKEN=$(python3 -c "import json; print(json.load(open('$INIT_FILE'))['root_token'])")

run_in_vault() {  # $1 = shell script to run inside the pod
  { printf 'set -e\nexport VAULT_TOKEN=%s\n' "$ROOT_TOKEN"; printf '%s\n' "$1"; } \
    | kubectl exec -i -n vault vault-0 -- sh -s
}

base() {
  # Generated locally, only stored if the secret doesn't exist yet (re-runs don't rotate them).
  local pg_admin payment_key
  pg_admin=$(openssl rand -hex 24)
  payment_key="pay_test_$(openssl rand -hex 16)"

  run_in_vault "
PG_ADMIN='$pg_admin'
PAYMENT_KEY='$payment_key'
$(cat <<'EOF'
# --- KV v2: static secrets, versioned (every write keeps the previous version).
vault secrets list | grep -q '^secret/' || vault secrets enable -path=secret kv-v2
vault kv get secret/railbook/postgres-admin >/dev/null 2>&1 \
  || vault kv put secret/railbook/postgres-admin password="$PG_ADMIN"
vault kv get secret/railbook/payment >/dev/null 2>&1 \
  || vault kv put secret/railbook/payment api_key="$PAYMENT_KEY"

# --- Kubernetes auth: pods prove who they are with their service account token.
# Vault checks the token with the Kubernetes API (the chart grants Vault that permission).
vault auth list | grep -q '^kubernetes/' || vault auth enable kubernetes
vault write auth/kubernetes/config \
  kubernetes_host="https://$KUBERNETES_SERVICE_HOST:$KUBERNETES_SERVICE_PORT"

# --- Policies: least privilege. Each one lists exactly the paths a workload may read.
vault policy write railbook-api - <<'HCL'
path "secret/data/railbook/payment" { capabilities = ["read"] }
path "database/creds/railbook-api"  { capabilities = ["read"] }
HCL
vault policy write railbook-postgres - <<'HCL'
path "secret/data/railbook/postgres-admin" { capabilities = ["read"] }
HCL

# --- Roles: service account + namespace -> policies. A pod with any other account gets nothing.
# audience: only accept tokens issued for the Kubernetes API (kind's default "aud" claim), so a
# token minted for some other service can't be replayed against Vault.
AUD=https://kubernetes.default.svc.cluster.local
vault write auth/kubernetes/role/railbook-api \
  bound_service_account_names=railbook-api \
  bound_service_account_namespaces=railbook \
  audience=$AUD token_policies=railbook-api token_ttl=1h
vault write auth/kubernetes/role/railbook-postgres \
  bound_service_account_names=railbook-postgres \
  bound_service_account_namespaces=railbook \
  audience=$AUD token_policies=railbook-postgres token_ttl=15m
EOF
)"
}

database() {
  run_in_vault "$(cat <<'EOF'
vault secrets list | grep -q '^database/' || vault secrets enable database

# Vault logs into Postgres as the admin user to create and drop short-lived users.
# The admin password is read from KV inside the pod; it never leaves Vault.
vault write database/config/railbook \
  plugin_name=postgresql-database-plugin \
  connection_url='postgresql://{{username}}:{{password}}@railbook-postgres.railbook.svc:5432/railbook?sslmode=disable' \
  username=postgres \
  password="$(vault kv get -field=password secret/railbook/postgres-admin)" \
  allowed_roles=railbook-api

# Each request for database/creds/railbook-api runs creation_statements with a new name and
# password. Users join railbook_owner (owner of the tables; the app does SET ROLE to it).
# default_ttl: lease length; the injected agent sidecar renews it while the pod lives.
# max_ttl: hard limit; after it the app must restart to pick up new credentials.
vault write database/roles/railbook-api \
  db_name=railbook \
  default_ttl=1h max_ttl=24h \
  creation_statements="CREATE ROLE \"{{name}}\" WITH LOGIN PASSWORD '{{password}}' VALID UNTIL '{{expiration}}' IN ROLE railbook_owner;" \
  revocation_statements="SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE usename = '{{name}}'; DROP ROLE IF EXISTS \"{{name}}\";"
EOF
)"
}

case "${1:-}" in
  base) base ;;
  database) database ;;
  *) echo "usage: $0 base|database" >&2; exit 2 ;;
esac
echo "vault-setup $1: done"
