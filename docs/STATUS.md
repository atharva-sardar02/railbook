# RailBook — Status

**As of 2026-10-21.** Paused after block 3; block 4 resumes next session.

## Progress

| Block | What | Status | Notes |
|---|---|---|---|
| 0 | Setup: kind, Helm, public repo, runner user | Done | [PRD §5](PRD.md#5-day-plan) |
| 1 | Booking API (Twelve-Factor) | Done | [01-app](learning/01-app.md) |
| 1b | Quick web frontend | Done | [01b-frontend](learning/01b-frontend.md) |
| 2 | kind + Kong + Vault (all secrets from Vault) | Done | [02-kind-kong-vault](learning/02-kind-kong-vault.md) |
| 3 | CI/CD: GitHub Actions → GHCR → self-hosted runner → kind | Done | [03-ci-cd](learning/03-ci-cd.md) |
| 4 | Prometheus + Grafana | **Next** — designed, not built | Plan below |
| 5 | Splunk | To do | Open item: HEC token delivery from Vault (collector image has no shell) |
| 6 | Verify "done when" + Twelve-Factor checklist | To do | |

## Resuming a session

The cluster lives in Docker Desktop, so it survives between sessions, but a restart leaves Vault **sealed** and the runner's deploy token expires after 24 h.

```bash
# 1. Start Docker Desktop (Windows), then in WSL:
cd /mnt/d/projects/irctc-booking
scripts/bootstrap.sh                      # idempotent: re-unseals Vault, repairs anything missing

# 2. New 24 h deploy token for the runner
scripts/setup-runner.sh refresh-token

# 3. Start the runner if you stopped it
sudo systemctl start actions.runner.atharva-sardar02-railbook.railbook-laptop

# 4. Check
kubectl get pods -A                       # everything Running / Completed
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1/   # 200
```

When stopping for the day:

```bash
sudo systemctl stop actions.runner.atharva-sardar02-railbook.railbook-laptop
```

## Block 4 plan (decided, to build next)

| Decision | Detail |
|---|---|
| Stack | `kube-prometheus-stack` chart **92.0.0** (Prometheus Operator v0.94.1) in namespace `monitoring`, release name `monitoring` |
| Discovery | ServiceMonitors labelled `release: monitoring` for the API, Kong and Vault |
| App-owned monitoring | ServiceMonitor, PrometheusRule and Grafana dashboard (ConfigMap) live in the **app chart**, so the pipeline deploys them with the code; `github-deployer` Role gains `servicemonitors` + `prometheusrules` |
| Alerts | 5xx error rate > 5 %, API down, Vault sealed, Kong rate limiting active (info) |
| Grafana password | From **Vault** (`secret/monitoring/grafana`), injected as a file; Grafana reads it via `GF_SECURITY_ADMIN_PASSWORD__FILE` — the chart then creates no Kubernetes Secret |
| Memory | Prometheus limit 512 Mi, 1 day retention, 30 s scrape; etcd/scheduler/controller-manager/kube-proxy monitors off (unreachable on kind) |
| UIs | Through Kong by hostname, on 127.0.0.1 only: `grafana.localhost`, `prometheus.localhost`, `alertmanager.localhost` |
| Order | `bootstrap.sh` installs monitoring **before** the app chart (the app chart uses its CRDs) |

## Known quirks

- Changes go through **pull requests**; `main` is protected (admins included). You merge — merging deploys.
- Local `gh` CLI is v2.4: use `gh api …` for Actions run details.
- Vault upgrades need `--server-side=false` (already in `bootstrap.sh`).
