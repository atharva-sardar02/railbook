#!/usr/bin/env bash
# Checks that the API server can trust the Vault Agent Injector, and restarts the injector if not.
#
# The injector makes its own TLS certificate (auto-TLS) and writes its CA into the webhook config
# (caBundle) so the API server trusts it. If that goes wrong — e.g. the injector starts while the
# API server is still unreachable after `docker start`, or its 24 h certificate isn't renewed —
# the API server can't call the webhook, and with failurePolicy Fail no Vault-injected pod can be
# created. A restarted injector makes a fresh certificate and rewrites the caBundle.
#
# Run by bootstrap.sh; safe to run any time: scripts/vault-injector-check.sh
set -euo pipefail

NS=vault
LOCAL_PORT=18443

# 0 if the certificate the injector serves verifies against the webhook's caBundle and is valid
# for at least another hour.
cert_ok() {
  local ca pf ok=1
  ca=$(mktemp)
  kubectl get mutatingwebhookconfiguration vault-agent-injector-cfg \
    -o jsonpath='{.webhooks[0].clientConfig.caBundle}' | base64 -d > "$ca"
  kubectl port-forward -n "$NS" deploy/vault-agent-injector "$LOCAL_PORT:8080" >/dev/null 2>&1 &
  pf=$!
  timeout 15 bash -c "until (echo > /dev/tcp/127.0.0.1/$LOCAL_PORT) 2>/dev/null; do sleep 0.5; done" || true
  # -verify_return_error: abort the handshake (print no cert) if the chain doesn't verify.
  if echo | openssl s_client -connect "127.0.0.1:$LOCAL_PORT" -CAfile "$ca" -verify_return_error 2>/dev/null \
      | openssl x509 -noout -checkend 3600 >/dev/null 2>&1; then
    ok=0
  fi
  kill "$pf" 2>/dev/null || true
  wait "$pf" 2>/dev/null || true
  rm -f "$ca"
  return "$ok"
}

if cert_ok; then
  echo "vault injector TLS: ok"
  exit 0
fi

echo "vault injector TLS: webhook CA doesn't match the served certificate (or it expires within 1 h) — restarting the injector"
kubectl rollout restart -n "$NS" deploy/vault-agent-injector
kubectl rollout status -n "$NS" deploy/vault-agent-injector --timeout=3m
# The new pod writes its caBundle a few seconds after it is Ready.
for _ in $(seq 1 12); do
  if cert_ok; then
    echo "vault injector TLS: ok after restart"
    exit 0
  fi
  sleep 5
done
echo "vault injector TLS: still failing after restart — see kubectl logs -n $NS deploy/vault-agent-injector" >&2
exit 1
