#!/usr/bin/env bash
# Brings the whole local platform up, in dependency order. Every step is safe to rerun,
# so this also repairs a half-broken setup (e.g. after a WSL restart: re-unseals Vault).
#
#   kind cluster → monitoring CRDs → Kong → Vault → unseal → Vault base config → monitoring
#   → Postgres → Vault DB config → app
set -euo pipefail
cd "$(dirname "$0")/.."

KONG_CHART_VERSION=0.24.0
VAULT_CHART_VERSION=0.34.1
# The CRD chart version must match the operator in kube-prometheus-stack (both v0.94.1).
PROM_CRDS_CHART_VERSION=32.0.1
KPS_CHART_VERSION=92.0.0
# IMAGE_TAG unset = build locally. CI sets it to a git SHA that already exists in GHCR.
IMAGE_TAG="${IMAGE_TAG:-}"

step() { printf '\n\033[1m== %s ==\033[0m\n' "$*"; }

step "kind cluster"
kind get clusters | grep -qx railbook || kind create cluster --config deploy/kind/kind-config.yaml
kubectl config use-context kind-railbook >/dev/null
# Right after `docker start`, the node's container is up but the Kubernetes API server inside it
# needs a few seconds more. Wait for it instead of failing the first Helm call.
timeout 120 bash -c 'until kubectl get --raw /readyz >/dev/null 2>&1; do sleep 2; done'

step "Helm repositories"
helm repo add kong https://charts.konghq.com >/dev/null 2>&1 || true
helm repo add hashicorp https://helm.releases.hashicorp.com >/dev/null 2>&1 || true
helm repo add prometheus-community https://prometheus-community.github.io/helm-charts >/dev/null 2>&1 || true
helm repo update kong hashicorp prometheus-community >/dev/null

step "Prometheus Operator CRDs"
# The new object types (ServiceMonitor, PrometheusRule, ...) must exist before Kong, Vault and
# the app chart create objects of those types. Like a database schema before the data.
helm upgrade --install prometheus-operator-crds prometheus-community/prometheus-operator-crds \
  --version "$PROM_CRDS_CHART_VERSION" -n monitoring --create-namespace --wait

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
scripts/vault-injector-check.sh
scripts/vault-unseal.sh
scripts/vault-setup.sh base

step "Monitoring (Prometheus, Alertmanager, Grafana)"
# After Vault: Grafana's admin password is injected by Vault when its pod starts.
helm upgrade --install monitoring prometheus-community/kube-prometheus-stack \
  --version "$KPS_CHART_VERSION" -n monitoring -f deploy/monitoring/values.yaml \
  --wait --timeout 10m

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
# The pipeline's least-privilege account; kept in sync here so new chart objects are allowed.
kubectl apply -f deploy/rbac/github-deployer.yaml >/dev/null
helm upgrade --install railbook deploy/charts/railbook -n railbook \
  --set image.tag="$IMAGE_TAG" --rollback-on-failure --wait --wait-for-jobs --timeout 5m

step "Log collector (pod logs → Splunk)"
# After Vault (its init container fetches the HEC token, role log-collector) and the monitoring
# CRDs (ServiceMonitor, PrometheusRule). Splunk itself runs outside the cluster: scripts/splunk.sh.
# If Splunk isn't up yet, the collector starts anyway and retries; LogsQueuedForSplunk fires.
kubectl apply -f deploy/logging/otel-collector.yaml >/dev/null
kubectl rollout status -n logging ds/otel-collector --timeout=3m

step "Ready"
echo "Frontend through Kong: http://127.0.0.1/  (API docs: /docs)"
echo "Grafana:      http://grafana.localhost/  (admin / scripts/vault-read.sh secret/monitoring/grafana admin_password)"
echo "Prometheus:   http://prometheus.localhost/"
echo "Alertmanager: http://alertmanager.localhost/"
echo "Splunk:       http://127.0.0.1:8000/  (start it with scripts/splunk.sh; login if asked: admin / scripts/vault-read.sh secret/splunk/admin password)"
