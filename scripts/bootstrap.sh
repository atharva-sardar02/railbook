#!/usr/bin/env bash
# Brings the whole local platform up, in dependency order. Every step is safe to rerun,
# so this also repairs a half-broken setup (e.g. after a WSL restart: re-unseals Vault).
#
#   kind cluster → Kong → Vault → unseal → Vault base config → Postgres → Vault DB config → app
set -euo pipefail
cd "$(dirname "$0")/.."

KONG_CHART_VERSION=0.24.0
VAULT_CHART_VERSION=0.34.1
IMAGE_TAG="${IMAGE_TAG:-dev}"

step() { printf '\n\033[1m== %s ==\033[0m\n' "$*"; }

step "kind cluster"
kind get clusters | grep -qx railbook || kind create cluster --config deploy/kind/kind-config.yaml
kubectl config use-context kind-railbook >/dev/null

step "Helm repositories"
helm repo add kong https://charts.konghq.com >/dev/null 2>&1 || true
helm repo add hashicorp https://helm.releases.hashicorp.com >/dev/null 2>&1 || true
helm repo update kong hashicorp >/dev/null

step "Kong (gateway + ingress controller)"
helm upgrade --install kong kong/ingress --version "$KONG_CHART_VERSION" \
  -n kong --create-namespace -f deploy/kong/values.yaml --wait --timeout 5m

step "Vault"
# --server-side=false: the injector writes its own TLS CA (caBundle) into its webhook config at
# runtime. Helm 4's default server-side apply sees that as a field-ownership conflict and fails
# the upgrade; client-side apply leaves fields the chart doesn't set alone.
helm upgrade --install vault hashicorp/vault --version "$VAULT_CHART_VERSION" \
  -n vault --create-namespace -f deploy/vault/values.yaml --server-side=false
kubectl wait -n vault --for=jsonpath='{.status.phase}'=Running pod/vault-0 --timeout=3m
kubectl rollout status -n vault deploy/vault-agent-injector --timeout=3m
scripts/vault-unseal.sh
scripts/vault-setup.sh base

step "Postgres (admin password from Vault)"
helm upgrade --install railbook-postgres deploy/charts/postgres \
  -n railbook --create-namespace --wait --timeout 5m
scripts/vault-setup.sh database

step "RailBook app (image railbook:$IMAGE_TAG)"
if [ "$IMAGE_TAG" = dev ]; then
  # Local image: build it and copy it into kind's own image store (no registry involved).
  docker build -q -t railbook:dev . >/dev/null
  kind load docker-image railbook:dev --name railbook
fi
helm upgrade --install railbook deploy/charts/railbook -n railbook \
  --set image.tag="$IMAGE_TAG" --rollback-on-failure --wait --wait-for-jobs --timeout 5m

step "Ready"
echo "API through Kong: http://127.0.0.1/docs"
