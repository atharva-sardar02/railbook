#!/usr/bin/env bash
# Runs Splunk (Free license) in Docker, next to the kind cluster. Safe to rerun.
#
#   scripts/splunk.sh        # create or start the container, wait until Splunk is up,
#                            # sync the saved searches from git
#
# Why outside kind: Splunk needs ~2 GB and keeps its own data; as a separate container on the
# `kind` Docker network, it survives `kind delete cluster`, and pods reach it as splunk:8088.
#
# Secrets: the admin password and the HEC token live in Vault (scripts/vault-setup.sh base).
# Splunk can't use Vault's Kubernetes agent from outside the cluster, so this script reads them
# from Vault and hands them to the container as environment variables on first creation.
# Trade-off: they are visible to `docker inspect`, i.e. to anyone who can run Docker — who is
# already root-equivalent on this machine. The token's source of truth stays in Vault.
set -euo pipefail
cd "$(dirname "$0")/.."

NAME=splunk
# 10.6.0, pinned by digest (published 2026-10-01).
IMAGE=splunk/splunk:10.6.0@sha256:9280f9ce267b212e5596aa1832cdfd3f9be886c378da35ebc87499323ee447a4

if docker container inspect "$NAME" >/dev/null 2>&1; then
  docker start "$NAME" >/dev/null
else
  admin_password=$(scripts/vault-read.sh secret/splunk/admin password | tr -d '\n')
  hec_token=$(scripts/vault-read.sh secret/splunk/hec token | tr -d '\n')
  docker volume create splunk-var >/dev/null
  docker volume create splunk-etc >/dev/null
  # --network kind      : same Docker network as the kind node, so the collector can reach it.
  # --hostname splunk   : shows up as `host` on Splunk's own internal events.
  # -p 127.0.0.1:8000   : web UI on this machine only.
  #                       HEC (8088) is NOT published: only the cluster sends logs.
  # --memory 2g         : hard cap; Splunk would otherwise take what it can.
  # SPLUNK_LICENSE_URI=Free : Free license, 500 MB/day, no users/auth.
  # SPLUNK_HEC_TOKEN    : creates an HEC input with exactly this token (default index: main).
  # volumes             : indexed data (var) and config (etc) survive container re-creation.
  docker run -d --name "$NAME" --hostname splunk --network kind \
    -p 127.0.0.1:8000:8000 \
    --memory 2g \
    -e SPLUNK_START_ARGS=--accept-license \
    -e SPLUNK_GENERAL_TERMS=--accept-sgt-current-at-splunk-com \
    -e SPLUNK_LICENSE_URI=Free \
    -e SPLUNK_PASSWORD="$admin_password" \
    -e SPLUNK_HEC_TOKEN="$hec_token" \
    -v splunk-var:/opt/splunk/var \
    -v splunk-etc:/opt/splunk/etc \
    "$IMAGE" >/dev/null
fi

# The image has a Docker health check; the first start (Ansible provisioning) takes 2-4 minutes.
printf 'Waiting for Splunk to become healthy'
status=starting
for _ in $(seq 1 60); do
  status=$(docker inspect -f '{{.State.Health.Status}}' "$NAME")
  [ "$status" = healthy ] && break
  printf '.'
  sleep 10
done
if [ "$status" != healthy ]; then
  echo " timed out (status: $status). Check: docker logs $NAME" >&2
  exit 1
fi
echo " ok"

# Our saved searches (deploy/splunk/railbook) are copied in from git on every run, not mounted:
# provisioning chowns everything under /opt/splunk to the splunk user and fails on a read-only
# mount, and a writable mount would let Splunk change files in the git checkout.
# Copy, hand ownership to Splunk (files from /mnt/d arrive as 777: make them owner-writable only),
# then ask Splunk's management API (8089) to re-read the app list and saved searches — no restart.
# The password comes from the container's own env, so it never appears on this machine's command line.
docker cp deploy/splunk/railbook/. "$NAME:/opt/splunk/etc/apps/railbook" >/dev/null
docker exec -u root "$NAME" sh -c \
  'chown -R splunk:splunk /opt/splunk/etc/apps/railbook && chmod -R u=rwX,go=rX /opt/splunk/etc/apps/railbook'
docker exec "$NAME" sh -c '
  curl -skf -u "admin:$SPLUNK_PASSWORD" -o /dev/null https://localhost:8089/services/apps/local/_reload &&
  curl -skf -u "admin:$SPLUNK_PASSWORD" -o /dev/null https://localhost:8089/servicesNS/nobody/railbook/saved/searches/_reload'
echo "Saved searches synced from deploy/splunk/railbook"
