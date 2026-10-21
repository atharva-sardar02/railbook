#!/usr/bin/env bash
# Brings the whole local platform up, in dependency order. Every step is safe to rerun,
# so this also repairs a half-broken setup (e.g. after a WSL restart: re-unseals Vault).
#
#   kind cluster → Kong → Vault → unseal → Vault base config → Postgres → Vault DB config → app
set -euo pipefail
cd "$(dirname "$0")/.."

KONG_CHART_VERSION=0.24.0
VAULT_CHART_VERSION=0.34.1
# IMAGE_TAG unset = build locally. CI sets it to a git SHA that already exists in GHCR.
IMAGE_TAG="${IMAGE_TAG:-}"

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

if [ -z "$IMAGE_TAG" ]; then
  # Local build. Tag = the image's content hash, so new code => new tag => Helm sees a change
  # and rolls the pods. A fixed tag like "dev" would never change, so `helm upgrade` would
  # leave the old pods running old code (factor V: a release must name the exact build).
  docker build -q -t railbook:build . >/dev/null
  IMAGE_TAG="dev-$(docker image inspect -f '{{.Id}}' railbook:build | cut -d: -f2 | cut -c1-12)"
  docker tag railbook:build "railbook:$IMAGE_TAG"
  # Copy it into kind's own image store (no registry involved). Skipped if already there.
  kind load docker-image "railbook:$IMAGE_TAG" --name railbook
fi

step "RailBook app (image railbook:$IMAGE_TAG)"
helm upgrade --install railbook deploy/charts/railbook -n railbook \
  --set image.tag="$IMAGE_TAG" --rollback-on-failure --wait --wait-for-jobs --timeout 5m

step "Ready"
echo "Frontend through Kong: http://127.0.0.1/  (API docs: /docs)"
