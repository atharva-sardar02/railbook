#!/usr/bin/env bash
# Sets up the GitHub Actions self-hosted runner as the unprivileged `ghrunner` user.
#
#   scripts/setup-runner.sh install         # one-time: RBAC, tools, kubeconfig, runner service
#   scripts/setup-runner.sh refresh-token   # daily: new 24 h deploy token for the runner
#   scripts/setup-runner.sh kubeconfig FILE # write the deployer kubeconfig to FILE (for testing)
#
# Run as your normal user (needs `gh` logged in and the kind cluster up); it uses sudo for the
# steps that touch /home/ghrunner and /usr/local/bin.
set -euo pipefail
cd "$(dirname "$0")/.."

REPO=atharva-sardar02/railbook
RUNNER_VERSION=2.338.0
RUNNER_USER=ghrunner
RUNNER_HOME=/home/$RUNNER_USER
RUNNER_DIR=$RUNNER_HOME/actions-runner
CONTEXT=kind-railbook

# A kubeconfig that holds only the github-deployer token (namespace railbook, no cluster-admin).
# The token is short-lived and not stored in the cluster: `kubectl create token` mints it on demand.
write_kubeconfig() {
  local out=$1
  local server ca token
  server=$(kubectl config view --raw --minify --context "$CONTEXT" -o jsonpath='{.clusters[0].cluster.server}')
  ca=$(kubectl config view --raw --minify --context "$CONTEXT" -o jsonpath='{.clusters[0].cluster.certificate-authority-data}')
  token=$(kubectl --context "$CONTEXT" create token github-deployer -n railbook --duration=24h)
  umask 077
  cat > "$out" <<EOF
apiVersion: v1
kind: Config
clusters:
  - name: railbook
    cluster:
      server: $server
      certificate-authority-data: $ca
users:
  - name: github-deployer
    user:
      token: $token
contexts:
  - name: railbook-deployer
    context: { cluster: railbook, user: github-deployer, namespace: railbook }
current-context: railbook-deployer
EOF
}

install_kubeconfig() {
  local tmp
  tmp=$(mktemp)
  write_kubeconfig "$tmp"
  sudo install -d -o "$RUNNER_USER" -g "$RUNNER_USER" -m 700 "$RUNNER_HOME/.kube"
  sudo install -o "$RUNNER_USER" -g "$RUNNER_USER" -m 600 "$tmp" "$RUNNER_HOME/.kube/config"
  rm -f "$tmp"
  echo "Deploy token for $RUNNER_USER refreshed (valid 24 h)."
}

install() {
  echo "== RBAC: github-deployer service account =="
  kubectl --context "$CONTEXT" apply -f deploy/rbac/github-deployer.yaml

  echo "== helm + kubectl for all users =="
  local tool src
  for tool in helm kubectl; do
    # Resolve symlinks first (Docker Desktop links kubectl into /usr/local/bin). Copying a path
    # onto itself makes `install` delete the destination before reading the source.
    src=$(readlink -f "$(command -v "$tool")")
    if [ "$src" = "/usr/local/bin/$tool" ]; then
      echo "$tool already in /usr/local/bin"
    else
      sudo install -m 755 "$src" "/usr/local/bin/$tool"
    fi
  done

  echo "== kubeconfig for $RUNNER_USER =="
  install_kubeconfig

  echo "== runner $RUNNER_VERSION =="
  local tarball=actions-runner-linux-x64-$RUNNER_VERSION.tar.gz
  local tmpdir; tmpdir=$(mktemp -d)
  curl -fsSL -o "$tmpdir/$tarball" \
    "https://github.com/actions/runner/releases/download/v$RUNNER_VERSION/$tarball"
  # Verify against the digest GitHub publishes for the release asset.
  local digest
  digest=$(gh api "repos/actions/runner/releases/tags/v$RUNNER_VERSION" \
    --jq ".assets[] | select(.name==\"$tarball\") | .digest" | cut -d: -f2)
  echo "$digest  $tmpdir/$tarball" | sha256sum -c -
  # The download sits in your private temp dir, which ghrunner can't read: extract as root,
  # then give the whole runner directory to ghrunner.
  sudo install -d -o "$RUNNER_USER" -g "$RUNNER_USER" -m 750 "$RUNNER_DIR"
  sudo tar -xzf "$tmpdir/$tarball" -C "$RUNNER_DIR"
  sudo chown -R "$RUNNER_USER:$RUNNER_USER" "$RUNNER_DIR"
  rm -rf "$tmpdir"

  echo "== register with GitHub =="
  # One-time registration token, valid for 1 hour; minted with your gh login.
  local reg_token
  reg_token=$(gh api -X POST "repos/$REPO/actions/runners/registration-token" --jq .token)
  sudo -u "$RUNNER_USER" bash -c "cd '$RUNNER_DIR' && ./config.sh --unattended --replace \
    --url 'https://github.com/$REPO' --token '$reg_token' \
    --name railbook-laptop --labels kind --work _work"

  echo "== run as a systemd service (starts with WSL) =="
  # Run inside sudo: the runner directory belongs to ghrunner (mode 750), so you can't cd into it.
  sudo bash -c "cd '$RUNNER_DIR' && ./svc.sh install '$RUNNER_USER' && ./svc.sh start"
  gh api "repos/$REPO/actions/runners" --jq '.runners[] | "\(.name): \(.status) labels=\([.labels[].name] | join(","))"'
}

case "${1:-}" in
  install) install ;;
  refresh-token) install_kubeconfig ;;
  kubeconfig) write_kubeconfig "${2:?usage: $0 kubeconfig FILE}" && echo "wrote $2" ;;
  *) echo "usage: $0 install | refresh-token | kubeconfig FILE" >&2; exit 2 ;;
esac
