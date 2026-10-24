# RailBook — Status

**As of 2026-10-24.** Block 5 done (in review); block 6 (verify) is next.

## Progress

| Block | What | Status | Notes |
|---|---|---|---|
| 0 | Setup: kind, Helm, public repo, runner user | Done | [PRD §5](PRD.md#5-day-plan) |
| 1 | Booking API (Twelve-Factor) | Done | [01-app](learning/01-app.md) |
| 1b | Quick web frontend | Done | [01b-frontend](learning/01b-frontend.md) |
| 2 | kind + Kong + Vault (all secrets from Vault) | Done | [02-kind-kong-vault](learning/02-kind-kong-vault.md) |
| 3 | CI/CD: GitHub Actions → GHCR → self-hosted runner → kind | Done | [03-ci-cd](learning/03-ci-cd.md) |
| 4 | Prometheus + Grafana + Alertmanager | Done | [04-prometheus-grafana](learning/04-prometheus-grafana.md) |
| 5 | Splunk + log collector | Done (PR) | [05-splunk](learning/05-splunk.md) |
| 6 | Verify "done when" + Twelve-Factor checklist | **Next** | |

Plain-language overview of every service and port: [00-glossary](learning/00-glossary.md).

## Resuming a session

The cluster lives in Docker Desktop. After a restart the containers are stopped, Vault comes
back **sealed**, and the runner's deploy token expires after 24 h.

```bash
# 1. Start Docker Desktop (Windows), then in WSL:
cd /mnt/d/projects/irctc-booking
docker start railbook-control-plane railbook-test-pg
scripts/bootstrap.sh                      # waits for Kubernetes, re-unseals Vault, repairs anything missing
scripts/splunk.sh                         # Splunk has no restart policy: start it every session

# 2. Only if you'll merge PRs this session (merging deploys through the runner):
scripts/setup-runner.sh refresh-token     # new 24 h deploy token
sudo systemctl start actions.runner.atharva-sardar02-railbook.railbook-laptop

# 3. Check
kubectl get pods -A                       # everything Running / Completed
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1/   # 200
```

UIs: http://127.0.0.1/ (app), http://grafana.localhost/, http://prometheus.localhost/,
http://alertmanager.localhost/, http://127.0.0.1:8000/ (Splunk). Grafana password: `scripts/vault-read.sh secret/monitoring/grafana admin_password`.

When stopping for the day:

```bash
sudo systemctl stop actions.runner.atharva-sardar02-railbook.railbook-laptop
docker stop splunk railbook-control-plane railbook-test-pg   # frees ~5 GB of memory
```

## Known quirks

- Changes go through **pull requests**; `main` is protected (admins included). You merge — merging deploys.
- Local `gh` CLI is v2.4: use `gh api …` for Actions run details.
- Local `curl` is 7.81 and doesn't resolve `*.localhost`: use `curl -H 'Host: grafana.localhost' http://127.0.0.1/` (browsers are fine).
- Vault upgrades need `--server-side=false` (already in `bootstrap.sh`).
- If Docker Desktop dies mid-install, a Helm release can be left `pending-install`; for a first install, `helm uninstall <release> -n <ns>` and rerun `bootstrap.sh`.
- Check free space on C: (`df -h /mnt/c`) before large image pulls: Docker's disk image lives there, and a full C: crashes Docker Desktop.
