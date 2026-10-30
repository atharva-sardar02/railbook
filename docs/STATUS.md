# RailBook — Status

**As of 2026-10-29.** All blocks done. Verification results: [06-verify](learning/06-verify.md).
Bugs found after block 6 are fixed, merged (PR #7) and deployed by the pipeline: [below](#bugs-found-after-block-6).

**Running now:** Helm revision 28, image `ghcr.io/atharva-sardar02/railbook:b527302…` (merge of PR #7).
**Next:** authentication with Keycloak (OIDC); design chosen, not built — [below](#next-authentication).
**Still open:** Alertmanager has no receiver, so alerts fire but notify nobody.

## Progress

| Block | What | Status | Notes |
|---|---|---|---|
| 0 | Setup: kind, Helm, public repo, runner user | Done | [PRD §5](PRD.md#5-day-plan) |
| 1 | Booking API (Twelve-Factor) | Done | [01-app](learning/01-app.md) |
| 1b | Quick web frontend | Done | [01b-frontend](learning/01b-frontend.md) |
| 2 | kind + Kong + Vault (all secrets from Vault) | Done | [02-kind-kong-vault](learning/02-kind-kong-vault.md) |
| 3 | CI/CD: GitHub Actions → GHCR → self-hosted runner → kind | Done | [03-ci-cd](learning/03-ci-cd.md) |
| 4 | Prometheus + Grafana + Alertmanager | Done | [04-prometheus-grafana](learning/04-prometheus-grafana.md) |
| 5 | Splunk + log collector | Done | [05-splunk](learning/05-splunk.md) |
| 6 | Verify "done when" + Twelve-Factor checklist | Done | [06-verify](learning/06-verify.md): 6/6 done-when; 11/12 factors in full, X in part (known gap) |

Plain-language overview of every service and port: [00-glossary](learning/00-glossary.md).

## Bugs found after block 6

Found while using the running system (2026-10-25/26), fixed 2026-10-29.

| Bug | Cause | Fix | Verified |
|---|---|---|---|
| API returns 503 about 1 h after every pod start | `vault-agent-init` and the `vault-agent` sidecar each asked Vault for their **own** DB user. The app started with the init container's user, but only the sidecar's lease was renewed, so Vault dropped the app's user at the 1 h TTL. | `vault.hashicorp.com/agent-cache-enable`: the sidecar starts from the init container's cache and renews **that** lease (one DB user per pod). The DB user is rendered to `/vault/secrets/db.json` and read on every new connection (`app/db.py`), so when Vault issues a new user at `max_ttl` (24 h) the app switches without a restart. | Role TTLs cut to 2 min / 6 min: 293/293 requests OK over 10 min, through 4 renewals and a forced new user, 0 restarts. Then a 75 min soak at the real 1 h / 24 h TTLs: 1490/1490 OK, 0 restarts, both pods still on their first DB user after the 1 h mark (the setup Job's unrenewed user expired on time at 1 h) |
| New pods sometimes start **without** Vault containers and crash-loop (`cannot open /vault/secrets/app.env`) | The webhook's `failurePolicy: Ignore`: when the API server can't call the injector (it was unreachable, or its TLS CA in the webhook config was stale), Kubernetes creates the pod anyway, unmodified. The injector started while the API server was still unreachable after `docker start`. | `failurePolicy: Fail`, so the pod is refused and the controller retries until the injector works. The webhook only applies to pods labelled `vault-agent-inject: "true"`, so a broken injector can't block unrelated pods. Chart anti-affinity removed, so `rollout restart` works on one node. `scripts/vault-injector-check.sh` (run by bootstrap) checks the served certificate against the webhook's CA and restarts the injector if needed. | Injector scaled to 0: the replacement API pod was refused (`failed calling webhook`), not created broken; the site stayed up on the other pod. After scaling it back, the ReplicaSet created the pod, with Vault containers, by itself about 5½ min later. A wrong CA patched into the webhook was put back by the injector itself within seconds. |
| Grafana killed by liveness (exit 137); Kong "failure to get a peer from the ring-balancer" | 512Mi limit: Grafana 13 reached 484 MiB under login + dashboard load and stalled | Limit 768Mi (`deploy/monitoring/values.yaml`) | Deployed |
| `RailBookApiDown` didn't fire while the site returned 503 | Prometheus still scrapes pods that are alive but not Ready, so `up` stayed 1 | Also fires when kube-state-metrics shows 0 available (Ready) replicas | All DB leases revoked in Vault: pods alive but not Ready, site 503; alert pending after ~1.5 min, firing at 2.5 min |
| Docs: unclear whether Splunk Free has a login | The web UI shows a login form, which looked like authentication | Checked: the form accepts any password, even an empty one, and the management API accepts any credentials. Docs say so (05-splunk, glossary, PRD). | Wrong and empty passwords both opened the search app |

### Deploying the fixes: what happened

| Merge | Pipeline deploy | Why |
|---|---|---|
| PR #6 (docs only, 365297e) | **Failed**, Helm rolled back by itself (revision 26 failed → 27 = rollback to 25); site stayed 200 | The webhook on the cluster (changed by `bootstrap.sh`) already required the `vault-agent-inject` label, but `main`'s chart didn't add it yet. The new pod got no Vault containers and crash-looped; `maxUnavailable: 0` kept the old pods serving. |
| PR #7 (b527302) | Succeeded, revision 28; 0 failed requests during the rollout | Chart now carries the label. Both pods run the GHCR image and use their `db.json` DB user. |

Lesson: the pipeline deploys only the app chart. Vault, monitoring, Postgres and the collector
are changed by `bootstrap.sh` from whatever branch you're on. If such a change needs a matching
app-chart change, the cluster is out of step with `main` until that merge. Merge it right away,
or change the infrastructure only after the app side is on `main`.

## Next: authentication

Chosen 2026-10-29, not built yet:

- **Keycloak** in kind as the identity provider (realm `railbook`, client `railbook-web`), reached
  through Kong at `auth.localhost`; its admin and database passwords from Vault.
- The frontend logs in with **OIDC Authorization Code + PKCE** and sends `Authorization: Bearer <JWT>`.
- The **API checks the JWT itself** (signature against Keycloak's public keys (JWKS), `iss`, `aud`, `exp`)
  and stores `sub` as the booking's owner. `/stations` and `/trains` stay public; creating, viewing
  and cancelling bookings need a token, and only the owner may see or cancel a booking (401 / 403).
- Not Kong's auth plugins: Kong OSS has no OIDC plugin, and its `jwt` plugin can't fetch rotating keys.
- Memory: Keycloak needs ~0.5–0.7 GB; check the budget (PRD §6.1) before installing.

## Resuming a session

The cluster lives in Docker Desktop. After a restart the containers are stopped, Vault comes
back **sealed**, and the runner's deploy token expires after 24 h.

```bash
# 1. Start Docker Desktop (Windows), then in WSL:
cd /mnt/d/projects/irctc-booking
docker start railbook-control-plane railbook-test-pg
scripts/bootstrap.sh                      # waits for Kubernetes, checks the Vault injector's TLS, re-unseals Vault, repairs anything missing
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
- If the Vault injector was down, the ReplicaSet backs off exponentially after the refused creates (5½ min in our test, at most ~16 min) before it tries again. `scripts/vault-injector-check.sh`, then `kubectl -n railbook rollout restart deploy/railbook-api` to retry at once.
- Revoking the API's DB leases by hand (`vault lease revoke -prefix database/creds/railbook-api`) makes the site 503 until the sidecar's next renewal attempt notices (up to ~40 min with the 1 h TTL). Use `kubectl -n railbook rollout restart deploy/railbook-api` to recover at once.
- If Docker Desktop dies mid-install, a Helm release can be left `pending-install`; for a first install, `helm uninstall <release> -n <ns>` and rerun `bootstrap.sh`.
- Check free space on C: (`df -h /mnt/c`) before large image pulls: Docker's disk image lives there, and a full C: crashes Docker Desktop.
