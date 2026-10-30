# Block 6 — Verification against the PRD

Every "done when" criterion (PRD §1) and every Twelve-Factor row (PRD §3.3), tested on the running
system on 2026-10-24 against the pipeline's release (Helm revision 19, image
`ghcr.io/atharva-sardar02/railbook:d77cae4`, merge of PR #5). Each result says what was run and
what came back, so it can be repeated. (Terms: [00-glossary](00-glossary.md).)

**Summary:** 6 of 6 "done when" criteria hold; 11 of 12 factors hold in full, factor X in part
(section 3), recorded as a known gap by decision, with the PRD's wording corrected. Two smaller findings: the deploy needs a token refreshed every 24 h (criterion 1),
and CI's image build uses a layer cache, so a clean build was proven separately (factor II).

## Contents

1. [How the live tests were run](#1-how-the-live-tests-were-run)
2. ["Done when"](#2-done-when)
3. [Twelve-Factor checklist](#3-twelve-factor-checklist)
4. [What this block teaches](#4-what-this-block-teaches)

---

## 1. How the live tests were run

Factors VI–IX are about behaviour under change, so they were tested **under traffic**: a small
script sent requests through Kong (`127.0.0.1`) the whole time:

- 4 threads searching trains (`GET /trains`) back to back, ~30 requests/s in total;
- 1 thread booking a seat every 4 s (15/min, under Kong's 20/min booking limit);
- every non-2xx response or connection error counted as a **failed request**;
- afterwards, every confirmed PNR looked up again (**lost booking** if it's gone), then cancelled.

A 20 s baseline with no disruption: 654 requests, 0 failed, 5 bookings, 0 lost. So any failure
in the tests below would have been caused by the disruption.

---

## 2. "Done when"

### 1. A push to `main` tests, builds, scans and deploys with no manual steps — **holds, with a caveat**

Run `40139704909` (merge of PR #5):

| Job | Runs on | Steps |
|---|---|---|
| checks | GitHub-hosted | gitleaks (secret scan) · ruff lint · 11 tests against Postgres 16 · helm lint |
| image | GitHub-hosted | build · **Trivy** scan · push to GHCR as `:d77cae4…` |
| deploy | `railbook-laptop` (self-hosted) | `helm upgrade` with that tag · smoke test through Kong · record what's running |

**Caveat:** the first attempt of `deploy` failed with
`kubernetes cluster unreachable: the server has asked for the client to provide credentials`.
The runner's deploy token (service account `github-deployer`) is valid for 24 h by design, and it
had expired. After `scripts/setup-runner.sh refresh-token`, re-running only the failed job
deployed it. The pipeline itself needs no manual step, but the token refresh is a manual step
**per session**. It's a deliberate trade-off (a stolen token is useless after a day) and is
listed in STATUS → "Resuming a session".

### 2. Every secret comes from Vault, none in code, image, Helm values or GitHub — **holds**

Rather than looking for things that *look* like secrets, the 5 real values were read from Vault
(payment key, Postgres admin password, HEC token, Splunk and Grafana admin passwords) and
searched for everywhere they must not be:

| Where | Hits |
|---|---|
| Git working tree and **full history** (`git log -p --all`) | 0 |
| Helm values and rendered manifests (`railbook`, `railbook-postgres`) | 0 |
| All ConfigMaps in the cluster | 0 |
| All 68 Kubernetes Secrets, decoded, including Helm's release records (gzip inside base64) | 0 |
| The API pod's environment | 0 |
| Every layer of the deployed image (`docker save`, 13 files) | 0 |
| GitHub Actions secrets and variables | none exist |

The only Secrets in `railbook` are Helm's release records. The API gets its secrets from the file
Vault Agent renders, `/vault/secrets/app.env`: `PAYMENT_API_KEY`, and a database user Vault
created for this pod (`v-kubernet-railbook-…-1792881972`, expires with its lease).

### 3. Grafana shows Kong and API metrics: rate, errors, latency, bookings — **holds**

All panel queries of the RailBook dashboard were run **through Grafana's own query API** (same
datasource, last hour): **17 of 17 returned data**, in every row: Overview (pods up, requests/s,
5xx ratio, Vault), API (by route, by status class, p50/p95/p99, pod memory), Bookings (by
outcome, last hour, duration p95), Kong (by status code, 429s, latency p95).

### 4. Splunk has Kong and API logs; one search by `request_id` follows a request — **holds**

One real booking with `X-Request-ID: verify-book-…`, then
`| savedsearch "RailBook - Follow one request" request_id=…`:

```
_time          component  path       status  duration_ms  event
23:01:02.362   api                                        booking_confirmed
23:01:02.363   api        /bookings  201     10.61        request
23:01:02.365   kong       /bookings  201
```
Three lines, not two. The business event `booking_confirmed` carries the ID too, because the
middleware binds it to every log line written during the request (`bind_contextvars`).

### 5. Hitting the booking rate limit shows as 429s in Grafana and Splunk — **holds**

25 `POST /bookings` within a minute: 19 × 422 (empty body, rejected by the API) then 6 × 429
(rejected by Kong; the limit is 20, and the test booking a few seconds earlier counted).

- Grafana panel *Kong 429s*: 0 → 0.066/s.
- Splunk *RailBook - Rate limited (429)*: 6 in the 23:01 minute.

### 6. Every Twelve-Factor row can be shown — **holds for 11 of 12; X in part**

Section 3.

---

## 3. Twelve-Factor checklist

| # | Factor | Result | Evidence |
|---|---|---|---|
| I | Codebase | **Holds** | One repo (`atharva-sardar02/railbook`) holds the app, both charts, Kong, Vault, monitoring, logging and Splunk config, the pipeline and the scripts. Every deploy is `helm upgrade` of this repo's chart at a commit SHA. |
| II | Dependencies | **Holds** | `uv.lock` pins 39 packages with 182 hashes; Docker and CI both install with `uv sync --frozen` (fails if the lock doesn't match). CI's build uses a layer cache (`cache-from: type=gha`), so it doesn't prove a *clean* build: `docker build --no-cache --pull` was run separately and succeeded in 17 s, and the image imports its dependencies. |
| III | Config | **Holds** | `app/config.py` reads everything through `pydantic-settings`. In kind it comes from ConfigMap `railbook-config` (`DB_HOST=railbook-postgres`, `DB_PORT`, `DB_NAME`, `DB_ROLE`, `PORT`, `LOG_LEVEL`) and the Vault file (secrets). A grep of `app/` for hosts, IPs and credentials finds only `db_host = "localhost"`, a default for running on the laptop that the ConfigMap overrides, and `0.0.0.0` as the listen address. |
| IV | Backing services | **Holds** | The same code meets three Postgres instances, chosen only by config: CI's service container (`localhost:5433`), the laptop's `railbook-test-pg` (`localhost:5433`), kind's `railbook-postgres:5432` with Vault credentials. |
| V | Build, release, run | **Holds** | CI exported manifest `sha256:68a99077…`; both API pods run exactly `ghcr.io/…/railbook@sha256:68a99077…`. `helm history` lists the releases (revisions 15–18 were local `bootstrap.sh` builds tagged `dev-<hash>`; 19 is the pipeline's). |
| VI | Processes | **Holds** | One API pod deleted 10 s into a 40 s run: 1,312 requests, **0 failed**; 10 bookings, **0 lost**. All state is in Postgres. |
| VII | Port binding | **Holds** | `api.port` changed 8000 → 9000 with `helm upgrade` during a 70 s run: the app listened on 9000 and no longer on 8000, Service endpoints moved to 9000, Prometheus scraped `:9000/metrics`; 2,345 requests, **0 failed**. Works because the Service, probes and ServiceMonitor refer to the port by its name `http`. Rolled back to revision 19 afterwards (revision 21). |
| VIII | Concurrency | **Holds** | Scaled to 3: `sum(up{job="railbook-api"})` = 3, three targets up. 30 requests to `/stations` split **10 / 10 / 10** across the pods (counted in Splunk, see section 4). Scaled back to 2. |
| IX | Disposability | **Holds** | `kubectl rollout restart` (both pods replaced, 21 s) during a 75 s run: 2,603 requests, **0 failed**; 19 bookings, 0 lost. A new pod is Ready 6 s after its container starts (9–10 s from creation, including Vault Agent's init). |
| X | Dev/prod parity | **In part** | Holds: the **same Postgres image digest** (`postgres:16-alpine@sha256:721873…`) in CI, on the laptop and in kind; the **same 11 tests** pass in CI and locally. Doesn't hold as written in the PRD: (1) CI tests the source code (same lockfile), not the built image; (2) tests use no Vault, they get fake config from env vars; (3) the kind "smoke test" checks 2 URLs, not the test suite. |
| XI | Logs | **Holds** | The API container's root filesystem is read-only, so it *cannot* write a log file; none exist. stdout/stderr (fd 1, 2) are pipes to the container runtime, which the collector reads. No Splunk or HEC code in `app/`. |
| XII | Admin processes | **Holds** | `kubectl get jobs -n railbook`: `railbook-setup` Complete. Same image (`:d77cae4…`), same ConfigMap, same service account and Vault secrets as the API; command `python -m app.admin setup`. |

---

## 4. What this block teaches

- **Test claims with the real values.** "No secrets outside Vault" was checked by searching for
  the actual secret values, including in places a pattern-based scan can't read: compressed Helm
  records and image layers. A clean result means something only if the search could have found
  them.
- **Disruption tests need a baseline and a load.** "Zero failed requests during a restart" is
  only evidence if traffic was flowing and a quiet run was also clean.
- **Named ports make port changes free.** Factor VII worked with no other edits because nothing
  except one value uses the number.
- **Per-pod counters right after a restart lie.** For VIII, Prometheus's
  `increase(http_requests_total[1m])` per pod showed 10 / 0 / missing for an even 10 / 10 / 10
  split. The pods had just been restarted, so each pod's `/stations` counter was *created* by
  these requests, and `increase()` can't see a counter's first value (the same trap as in
  blocks 4 and 5). The logs in Splunk, one line per request, gave the true count.
- **A checklist can be stricter than the system.** Factor X's wording promised more than the
  pipeline does. The verification is what found that.
