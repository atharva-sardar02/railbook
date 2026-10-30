# Block 4 — Prometheus and Grafana, explained

How RailBook is measured, how alerts are raised, how the charts get drawn, and the many things
that went wrong on the way. (New to the terms? Start with [00-glossary](00-glossary.md).)

## Contents

1. [Metrics in one page](#1-metrics-in-one-page)
2. [What got installed, and why in this order](#2-what-got-installed-and-why-in-this-order)
3. [How Prometheus finds its targets: ServiceMonitors](#3-how-prometheus-finds-its-targets-servicemonitors)
4. [`deploy/monitoring/values.yaml`, piece by piece](#4-deploymonitoringvaluesyaml-piece-by-piece)
5. [Grafana's password from Vault](#5-grafanas-password-from-vault)
6. [The app chart's monitoring: `templates/monitoring.yaml`](#6-the-app-charts-monitoring-templatesmonitoringyaml)
7. [The alerts, line by line](#7-the-alerts-line-by-line)
8. [The dashboard](#8-the-dashboard)
9. [Kong and Vault metrics](#9-kong-and-vault-metrics)
10. [Scripts and RBAC changes](#10-scripts-and-rbac-changes)
11. [How it was verified](#11-how-it-was-verified)
12. [Problems hit and what they teach](#12-problems-hit-and-what-they-teach)
13. [Day-to-day use and exercises](#13-day-to-day-use-and-exercises)

---

## 1. Metrics in one page

Logs say *what happened* to one request. Metrics say *how much, how often, how fast* across all
of them, as numbers sampled over time. They are cheap to keep and fast to query, which makes
them the right tool for dashboards and alerts.

**Pull, not push.** Each service exposes a plain-text page, usually `/metrics`:

```
railbook_bookings_total{status="confirmed"} 16.0
railbook_bookings_total{status="payment_failed"} 4.0
```

Every 30 seconds Prometheus fetches ("scrapes") that page from every target and stores each
line as a time series: a name, labels, and a list of (time, value) samples. If a scrape fails,
Prometheus records `up = 0` for that target — the most basic health signal there is.

| Type | Behaviour | RailBook example | How you query it |
|---|---|---|---|
| **Counter** | Only goes up (resets to 0 when the process restarts) | `railbook_bookings_total`, `http_requests_total` | `rate(x[5m])` = per-second increase; `increase(x[1h])` = how many in the last hour |
| **Gauge** | Goes up and down | `vault_core_unsealed`, memory used | Use the value directly |
| **Histogram** | Counts observations into buckets (`le` = "less than or equal") | `railbook_booking_duration_seconds` | `histogram_quantile(0.95, sum by (le) (rate(x_bucket[5m])))` = p95 |

**Labels** split a metric into series (`status="confirmed"`). Each label combination is a
separate series that costs memory, so labels must have few possible values: status, route
template, HTTP method — never a PNR or a passenger name (that's why `app/metrics.py` says so).

**PromQL** is the query language. The few functions used in this block: `rate`, `increase`,
`sum by (label)`, `histogram_quantile`, `absent`, `or`, `unless`, `offset`.

---

## 2. What got installed, and why in this order

```
monitoring namespace
├── prometheus-operator-crds        new object types: ServiceMonitor, PrometheusRule, ...
└── monitoring (kube-prometheus-stack 92.0.0)
    ├── Prometheus Operator         turns ServiceMonitors/PrometheusRules into Prometheus config
    ├── Prometheus                  scrapes, stores 1 day, evaluates alert rules     :9090
    ├── Alertmanager                receives firing alerts, groups/silences them     :9093
    ├── Grafana                     dashboards                                       :3000
    ├── kube-state-metrics          "how many pods are ready", "what's the restart count"
    └── node-exporter               the machine: CPU, memory, disk
```

`scripts/bootstrap.sh` now runs:

```
kind → Prometheus Operator CRDs → Kong → Vault → unseal → Vault base config → monitoring
     → Postgres → Vault DB config → app
```

Why each position:

- **CRDs first.** Kong's and Vault's charts create `ServiceMonitor` objects. Kubernetes rejects an
  object whose type doesn't exist yet, so the types must be defined before anything uses them —
  like creating database tables before inserting rows. They come from their own chart
  (`prometheus-operator-crds` 32.0.1, which matches operator v0.94.1) for a second reason too:
  Helm installs CRDs bundled inside a chart once and **never upgrades them**. A separate chart
  makes upgrades explicit. That's why `crds.enabled: false` is set in our values.
- **Monitoring after Vault.** Grafana's admin password is injected by Vault when the Grafana pod
  starts (section 5), so Vault must be unsealed and configured first.
- **Monitoring before the app.** The app chart only creates its ServiceMonitor, alerts and
  dashboard if the monitoring CRDs exist (section 6).

Memory: the monitoring stack uses about 0.9 GB (Prometheus ~315 MB, Grafana ~380 MB plus its
sidecar ~80 MB, the rest small). The whole kind node went from ~2 GB to ~3.3 GB, inside the
8 GB WSL budget.

---

## 3. How Prometheus finds its targets: ServiceMonitors

Prometheus has no hand-written list of URLs. Instead:

1. Someone creates a **ServiceMonitor**: "scrape the Services with label X, on port Y, path Z".
2. The **operator** sees it and rewrites Prometheus's configuration.
3. Prometheus asks Kubernetes for the pods behind those Services and scrapes **each pod
   directly** (not through the Service's load balancing, which would hit a random pod each
   time and mix up the two API pods' counters).

The operator doesn't pick up every ServiceMonitor in the cluster. It uses a **label selector**:
by default kube-prometheus-stack's Prometheus only selects objects labelled
`release: <helm release name>`, here `release: monitoring`. Every ServiceMonitor and
PrometheusRule we add carries that label. Forget it, and the object exists but Prometheus
silently ignores it — the first thing to check when "my target doesn't show up".

The same applies to **PrometheusRule** objects: alert rules, selected by the same label, loaded
by the operator into Prometheus as rule files.

Who ships which:

| ServiceMonitor | Created by | Scrapes |
|---|---|---|
| `railbook-api` | app chart (`templates/monitoring.yaml`) | each API pod `:8000/metrics` |
| `kong-gateway` | Kong chart (`deploy/kong/values.yaml`) | Kong `:8100/metrics` |
| `vault` | Vault chart (`deploy/vault/values.yaml`) | active Vault `:8200/v1/sys/metrics?format=prometheus` |
| ~10 others | kube-prometheus-stack | Kubernetes API server, kubelet, CoreDNS, node-exporter, the stack itself |

See them all: http://prometheus.localhost/targets (16 targets, all `up`).

---

## 4. `deploy/monitoring/values.yaml`, piece by piece

```yaml
crds:
  enabled: false
```
Installed separately, see section 2.

```yaml
kubeEtcd: { enabled: false }
kubeScheduler: { enabled: false }
kubeControllerManager: { enabled: false }
kubeProxy: { enabled: false }
```
In kind, these control-plane parts listen only on `127.0.0.1` inside the node container, so
Prometheus can't reach them. Left on, they'd show as permanently `down` targets and their
default alerts ("etcd has no leader!") would fire forever. Turning them off also removes their
rules: the chart only renders a component's alert rules when that component is enabled.

```yaml
prometheus:
  ingress: { enabled: true, ingressClassName: kong, hosts: [prometheus.localhost], paths: [/] }
  prometheusSpec:
    retention: 1d
    scrapeInterval: 30s
    evaluationInterval: 30s
    externalUrl: http://prometheus.localhost/
    resources: { requests: { cpu: 100m, memory: 256Mi }, limits: { memory: 512Mi } }
```
- **ingress through Kong by hostname.** The same Kong door (127.0.0.1:80) now also serves
  `prometheus.localhost`. Kong prefers a route with a host *and* a path over one with only a path,
  so `prometheus.localhost/` goes to Prometheus while plain `127.0.0.1/` still goes to the app.
  Browsers resolve every `*.localhost` name to your own machine, so no `/etc/hosts` editing.
- **retention 1d, 30 s scrape.** Prometheus's memory grows with the number of series and samples
  kept. One day at 30 s is plenty to learn on and keeps it near 300 MB.
- **evaluationInterval** is how often alert rules are checked.
- **externalUrl** is used to build the "source" links inside alerts.
- **No storageSpec:** data lives in an `emptyDir` and is lost when the Prometheus pod restarts.
  For a 1-day laptop history, not worth managing a disk for.
- Prometheus, Alertmanager and the Prometheus UI have **no login**. That's acceptable only
  because kind publishes them on `127.0.0.1` alone.

```yaml
alertmanager:
  ingress: ...alertmanager.localhost...
  alertmanagerSpec: { externalUrl: ..., resources: ... }
```
The chart's default Alertmanager config sends everything to a `null` receiver: alerts are visible
in the UI but not emailed or posted anywhere. A real setup would add a Slack/email/PagerDuty
receiver here.

The `grafana:` section is section 5. `kube-state-metrics`, `prometheus-node-exporter` and
`prometheusOperator` only get small resource limits.

---

## 5. Grafana's password from Vault

Rule of this project: every secret comes from Vault, never from a Kubernetes Secret. Grafana's
chart normally generates an admin password and stores it in a Secret. Here's how that's avoided.

**In Vault** (`scripts/vault-setup.sh base`):

```sh
vault kv put secret/monitoring/grafana admin_password="$GRAFANA_ADMIN"   # only if missing
vault policy write grafana - <<'HCL'
path "secret/data/monitoring/grafana" { capabilities = ["read"] }
HCL
vault write auth/kubernetes/role/grafana \
  bound_service_account_names=monitoring-grafana \
  bound_service_account_namespaces=monitoring \
  audience=$AUD token_policies=grafana token_ttl=15m
```
Same pattern as the app: a random value generated once, a policy that can read exactly one path,
and a role that gives that policy only to the `monitoring-grafana` service account in the
`monitoring` namespace.

**On the Grafana pod** (`grafana.podAnnotations`; plus `grafana.podLabels: {vault-agent-inject: "true"}`, the label the injector webhook selects on since 2026-10-29):

```yaml
vault.hashicorp.com/agent-inject: "true"
vault.hashicorp.com/role: grafana
vault.hashicorp.com/agent-pre-populate-only: "true"
vault.hashicorp.com/agent-inject-secret-admin-password: secret/data/monitoring/grafana
vault.hashicorp.com/agent-inject-template-admin-password: |
  {{- with secret "secret/data/monitoring/grafana" -}}
  {{ .Data.data.admin_password }}
  {{- end -}}
```
The Vault Agent Injector adds an init container that logs in as `monitoring-grafana`, reads the
secret and writes just the password (the template trims all whitespace) to
`/vault/secrets/admin-password`. `pre-populate-only`: a static password read once needs no
renewing sidecar, which saves a container.

**In Grafana** (`grafana.env`):

```yaml
GF_SECURITY_ADMIN_USER: admin
GF_SECURITY_ADMIN_PASSWORD: $__file{/vault/secrets/admin-password}
```
Any Grafana setting can be given as an env var `GF_<SECTION>_<KEY>`, and Grafana expands
`$__file{path}` to that file's contents. Because both variables are set, the chart's templates
skip creating the admin Secret entirely (`kubectl get secret -n monitoring` shows none for
Grafana; the remaining ones are generated operator config and Helm's release history).

The plan said `GF_SECURITY_ADMIN_PASSWORD__FILE`. That convention is implemented by the shell
script that starts *older* Grafana images. The chart now ships a **distroless** image (no shell),
and its own validation refused the setting with a helpful message pointing to `$__file{}`.

**The sidecars.** Grafana's chart runs "sidecar" containers that watch ConfigMaps and copy
dashboards and datasources into Grafana, then call Grafana's API — as admin, with the password
from a Secret — to reload. Without that Secret:

```yaml
sidecar:
  datasources: { skipReload: true, initDatasources: true, watchMethod: LIST }
  dashboards:  { skipReload: true }
```
- Datasources (the Prometheus and Alertmanager connections) are loaded once by an **init
  container** before Grafana starts. `watchMethod: LIST` makes it read once and exit — see
  section 12 for what happened without it.
- Dashboards are files; Grafana re-reads its dashboard folder every 30 s by itself, so no reload
  call is needed.

Get the password: `scripts/vault-read.sh secret/monitoring/grafana admin_password`. That new
script sends the root token to `vault-0` on stdin (never as a command-line argument) and prints
one field.

---

## 6. The app chart's monitoring: `templates/monitoring.yaml`

The app's own scrape config, alerts and dashboard live **in the app chart**, so they are
versioned with the code and deployed by the pipeline. Add a new metric and a panel for it in the
same PR; they ship together.

```yaml
{{- if .Capabilities.APIVersions.Has "monitoring.coreos.com/v1" }}
...
{{- end }}
```
Helm asks the cluster which APIs exist. If the monitoring CRDs aren't installed (a cluster
without monitoring, or `helm lint` in CI, which has no cluster), these objects are skipped
instead of failing the whole install.

**ServiceMonitor:**

```yaml
kind: ServiceMonitor
metadata:
  name: railbook-api
  labels:
    release: {{ .Values.monitoring.releaseLabel }}   # "monitoring": what Prometheus selects on
spec:
  selector:
    matchLabels:
      app.kubernetes.io/name: railbook-api   # the Service, see api.yaml
  endpoints:
    - port: http          # the Service port's *name*, not a number
      path: /metrics
      interval: 30s
```
`api.yaml`'s Service gained the label `app.kubernetes.io/name: railbook-api` so this selector
finds it. Prometheus attaches `job="railbook-api"` (the Service name), `namespace` and `pod` to
every series it scrapes from here; the alerts filter on those.

`/metrics` isn't routed by Kong (block 2), so the public can't read it; Prometheus reaches the
pods directly inside the cluster.

**New values** (`values.yaml`):

```yaml
monitoring:
  releaseLabel: monitoring
  scrapeInterval: 30s
  errorRateThreshold: 0.05
```

---

## 7. The alerts, line by line

An alert rule is a PromQL expression: **if it returns anything, the alert is active.**
`for: 1m` means it must keep returning something for 1 minute before it **fires** (until then
it's **pending**). That avoids alerting on one bad scrape. Firing alerts go to Alertmanager.

### `RailBookApiDown` (critical)

```promql
(sum(up{namespace="railbook", job="railbook-api"}) or vector(0)) == 0
or
(sum(kube_deployment_status_replicas_available{namespace="railbook", deployment="railbook-api"}) or vector(0)) == 0
```
- `up` is 1 for each pod Prometheus scraped successfully, 0 for a failed scrape.
- `sum(...)` = number of healthy API pods.
- The trap: if **all** pods are gone, there are no targets, so there's no `up` series at all —
  "nothing", not 0. `nothing == 0` is nothing, so a naive `sum(up) == 0` would never fire in
  exactly the case it's for. `or vector(0)` replaces "nothing" with 0.
- **Second half (added 2026-10-29).** Pods can be alive but not Ready, e.g. when the DB rejects
  their user: `/readyz` fails, Kong has nowhere to send traffic, the site returns 503. Prometheus
  still scrapes not-ready pods, so `up` stays 1 and the first half never fires (that's what
  happened on 2026-10-25). kube-state-metrics' `kube_deployment_status_replicas_available` counts
  only Ready pods.

### `RailBookHighErrorRate` (warning)

```promql
sum(rate(http_requests_total{..., status="5xx"}[5m]))
  /
sum(rate(http_requests_total{...}[5m]))
  > 0.05
```
Share of requests that failed with a server error over 5 minutes. Measured **in the app**:
prometheus-fastapi-instrumentator groups status codes into `2xx`/`4xx`/`5xx`. A failed
payment (402) or a sold-out train (409) is a normal business outcome, a 4xx, so it doesn't count.
`for: 2m` because a short blip during a deploy is normal. The summary is built with
`mulf .Values.monitoring.errorRateThreshold 100` ("5%"); see section 12 for why not `mul`.

### `RailBookBookingsRateLimited` (info)

```promql
sum(rate(kong_http_requests_total{exported_service=~"railbook[.].*", code="429"}[5m])) > 0
  or
sum(
  kong_http_requests_total{exported_service=~"railbook[.].*", code="429"}
    unless
  kong_http_requests_total{exported_service=~"railbook[.].*", code="429"} offset 5m
) > 0
```
Kong answers 429 itself; those requests never reach the API, so only Kong's metrics see them.
Three lessons in one expression (details in section 12):
- **`exported_service`**, not `service`: Prometheus renamed Kong's label.
- **`[.]`** matches a literal dot without backslash-escaping inside YAML inside PromQL.
- **Two halves.** `rate()` measures change between samples. Kong creates the `code="429"`
  counter at the first 429, so the very first sample Prometheus sees is already e.g. 5 — no
  change, rate 0, no alert. The second half catches "a 429 counter that didn't exist 5 minutes
  ago": `A unless B` keeps the series of A that have no match in B, and `offset 5m` looks
  5 minutes back.

Info severity, because the limit working is not a problem. The chart's default Alertmanager
config includes an `InfoInhibitor` rule that keeps `info` alerts quiet: they show in Prometheus
(http://prometheus.localhost/alerts) and as "suppressed" in Alertmanager.

### `VaultSealedOrDown` (critical) — in `deploy/vault/values.yaml`

```promql
absent(vault_core_unsealed == 1)
```
A platform alert, so it ships with Vault's chart, not the app's. When Vault is sealed it drops out
of the `vault-active` Service and isn't scraped at all, so — like `ApiDown` — the alert must
fire on the *absence* of data. `absent(x)` returns 1 when `x` returns nothing. "Nothing with
value 1" covers sealed, down and unreachable alike.

### `Watchdog` (from the chart)

Always firing, on purpose. If it ever stops arriving, the alerting pipeline itself is broken.
A real setup forwards it to a "dead man's switch" service that alerts when it goes silent.

---

## 8. The dashboard

`deploy/charts/railbook/dashboards/railbook.json` is a Grafana dashboard in its JSON export
format. `templates/monitoring.yaml` puts it in a ConfigMap:

```yaml
kind: ConfigMap
metadata:
  name: railbook-dashboard
  labels:
    grafana_dashboard: "1"
data:
  railbook.json: |-
    {{- .Files.Get "dashboards/railbook.json" | nindent 4 }}
```
Grafana's dashboard sidecar watches every namespace for ConfigMaps labelled
`grafana_dashboard: "1"` and copies the JSON into Grafana. `.Files.Get` reads a file from the
chart *without* templating it, so the JSON's own `{{handler}}` legend placeholders are left alone.
`editable: false` in the JSON: changes made in the UI would be overwritten anyway; change the
file in git instead (or edit in the UI, export the JSON, and commit it).

Open http://grafana.localhost/d/railbook. Rows and panels:

| Row | Panel | Query idea |
|---|---|---|
| Overview | API pods up · Requests/s · 5xx ratio · Vault unsealed | The same signals as the alerts, at a glance |
| API (measured in the app) | Requests by route | `sum by (handler) (rate(http_requests_total[...]))` — route *templates* like `/bookings/{pnr}` |
| | Responses by status class | 2xx / 4xx / 5xx |
| | Latency p50/p95/p99 | `histogram_quantile` over `http_request_duration_highr_seconds_bucket` |
| | Pod memory | `container_memory_working_set_bytes` from the kubelet, vs. the 192 MiB limit |
| Bookings | Attempts by outcome · Last hour · Duration p95 | The business metrics from `app/metrics.py` |
| Kong (what clients see) | Responses by code · 429s · Kong latency p95 | Includes what never reached the API: 429s, 502/503 |

`$__rate_interval` is a Grafana variable that picks a safe `rate()` window for the zoom level
(at least 4 scrape intervals).

The chart also installs ~25 ready-made Kubernetes dashboards (nodes, pods, namespaces, the API
server…) — worth browsing under *Dashboards*.

---

## 9. Kong and Vault metrics

**Kong** (`deploy/kong/values.yaml`, under `gateway:`):

```yaml
serviceMonitor:
  enabled: true
  interval: 30s
  labels:
    release: monitoring
```
Kong serves metrics on its **status listener**, port 8100, separate from the proxy port, so
they're never exposed through the public door. The chart's ServiceMonitor selects the proxy
Service by an `enable-metrics: "true"` label and scrapes the pod's `status` port. The
`railbook-prometheus` KongPlugin (block 2) is what adds per-route counters with status codes
(`status_code_metrics: true`) and latency histograms.

**Vault** (`deploy/vault/values.yaml`):

```yaml
serverTelemetry:
  serviceMonitor: { enabled: true, selectors: { release: monitoring }, interval: 30s }
  prometheusRules: { enabled: true, selectors: { release: monitoring }, rules: [VaultSealedOrDown] }
```
Vault's metrics endpoint normally needs a token. Block 2 already set
`telemetry { unauthenticated_metrics_access = "true" }` on the listener and a top-level
`telemetry { prometheus_retention_time = "30s" }` (how long Vault keeps a metric in memory
between scrapes), so Prometheus can scrape without a secret.

---

## 10. Scripts and RBAC changes

| File | Change | Why |
|---|---|---|
| `scripts/bootstrap.sh` | Waits for the API server after the kind step | Right after `docker start` the container is up but Kubernetes inside isn't; the first Helm call failed with "cluster unreachable". |
| | Adds `prometheus-community` repo, the CRDs step and the monitoring step | Section 2's order. |
| | `kubectl apply -f deploy/rbac/github-deployer.yaml` before the app | Keeps the pipeline's permissions in sync with what the chart now creates. |
| | Prints the three UI URLs and how to get the Grafana password | |
| `scripts/vault-setup.sh` | Grafana KV secret, policy and role | Section 5. |
| `scripts/vault-read.sh` | New | Read one KV field without putting the root token on a command line. |
| `deploy/rbac/github-deployer.yaml` | `servicemonitors`, `prometheusrules` in `monitoring.coreos.com` | The deploy job runs as this account; without the rule, the first deploy of this chart would fail with *forbidden*. Verified by running `helm upgrade` with the deployer's own token. |

---

## 11. How it was verified

| Check | Result |
|---|---|
| Prometheus targets | All 16 up, including both API pods, Kong and Vault |
| 25 POST /bookings in a row | 16 × 201, 4 × 402 (every 5th simulates a payment failure), 5 × 429 from Kong; the same numbers in `railbook_bookings_total` and `kong_http_requests_total` |
| `RailBookBookingsRateLimited` | Did **not** fire at first (the new-counter problem); after the fix, fired and reached Alertmanager |
| `RailBookApiDown` | Scaled the API to 0: site answered 503, alert pending → firing in ~1 min, critical in Alertmanager; scaled back to 2: resolved |
| `RailBookApiDown`, pods alive but not Ready (2026-10-29) | Revoked the API's DB leases in Vault: pods stayed Running (1/2), site 503, alert pending after ~1.5 min, firing at 2.5 min |
| Grafana login | `admin` + password from Vault works; a wrong password gets 401 |
| Grafana datasource | "Successfully queried the Prometheus API" |
| Dashboard | Loaded from the ConfigMap; all 17 panel queries return data |
| No Kubernetes Secret for Grafana | `kubectl get secret -n monitoring` shows only operator-generated config and Helm release records |
| `vault_core_unsealed` | Exists and is 1, so `VaultSealedOrDown` stays quiet |

---

## 12. Problems hit and what they teach

| Problem | Cause | Fix | Lesson |
|---|---|---|---|
| Chart refused `GF_SECURITY_ADMIN_PASSWORD__FILE` | Grafana image is now distroless; `__FILE` was implemented by the old image's shell start script | `GF_SECURITY_ADMIN_PASSWORD: $__file{...}` | Read the chart's validation errors; they often contain the fix. |
| Docker Desktop died mid-install; `kubectl` got "connection refused", `docker ps` hung | Unknown: the engine log just stops, no out-of-memory record | Restarted Docker Desktop | Infrastructure can vanish under you; scripts must be safe to rerun. |
| `UPGRADE FAILED: another operation is in progress` | The interrupted install left the release in `pending-install` | `helm uninstall monitoring`, install again | Helm records an operation *before* finishing it. A crash leaves it stuck; for a first install, uninstall is the reset. |
| Script "succeeded" though Helm failed | The output was piped into `grep`, which returned the exit code | Run without a pipe and check `$?` | `cmd \| grep` reports grep's status, not cmd's. |
| Grafana stuck at `Init:0/2` | `initDatasources` runs the sidecar as an init container, but `watchMethod: WATCH` (default) never exits | `watchMethod: LIST` | An init container must finish; a watcher never does. |
| Grafana restarted every ~2 min, exit 137, node at 850 % CPU | Grafana 12 needs ~250 MB to start; at a 256Mi limit the Go garbage collector ran nonstop, start-up took minutes and the liveness probe killed it | Limit 512Mi (it settles at ~380 MB) | Exit 137 = killed (SIGKILL). Reason `OOMKilled` → memory limit; reason `Error` with failing probes → too slow. Memory pressure shows up as CPU. |
| Grafana 13 killed by liveness (exit 137) under login + dashboard load (2026-10-25) | At a 512Mi limit it reached 484 MiB and stalled. The working set counts page cache too: idle it shows ~450–550 MiB while the process itself (RSS) is ~240 MiB | Limit 768Mi | A container's memory limit applies to its working set (process memory + active page cache), not just what `ps` shows. |
| 429 alert and Kong panels would never match | Kong labels series `service="railbook.railbook-api.http"`, but Prometheus already sets `service` to the scraped Kubernetes Service; on a clash it keeps its own and renames the target's to `exported_service` | Use `exported_service` | Look at real series (`/api/v1/series`) before writing queries. |
| Alert summary said "above 0%" | Helm's `mul` converts to integers: 0.05 × 100 → 0 | `mulf` | Template functions have types too. |
| Rate-limit alert didn't fire for the first burst | The 429 counter is created at the first 429; its first sample is already 5, so `rate()` sees no increase | Second half with `unless … offset 5m` | `rate()` and `increase()` can't see the jump from "no series" to the first value. Watch for this on any counter with a rarely-seen label. |
| `curl http://prometheus.localhost/` returned nothing | WSL's curl is 7.81; curl resolves `*.localhost` itself only from 7.85 | `curl -H 'Host: prometheus.localhost' http://127.0.0.1/` | Browsers were fine; check the tool before the server. |

---

## 13. Day-to-day use and exercises

```bash
scripts/vault-read.sh secret/monitoring/grafana admin_password   # Grafana login
```

- Dashboard: http://grafana.localhost/d/railbook
- Targets and alert rules: http://prometheus.localhost/targets, http://prometheus.localhost/alerts
- Alerts: http://alertmanager.localhost/

Exercises:

1. **Write a query.** In Prometheus → *Query*, run
   `sum by (status) (increase(railbook_bookings_total[1h]))`. Make a few bookings, including one
   with "simulate payment failure", and run it again.
2. **Trip the rate limit.** Book more than 20 times within a minute (the frontend works, or the
   curl loop in section 11). Watch the Kong 429 panel and `RailBookBookingsRateLimited` at
   /alerts. Note it shows as *suppressed* in Alertmanager (section 7).
3. **Take the API down.** `kubectl -n railbook scale deploy/railbook-api --replicas=0`, watch
   `RailBookApiDown` go *pending* then *firing* (about a minute), then scale back to 2.
4. **Seal Vault.** `kubectl -n vault exec -i vault-0 -- sh -c 'VAULT_TOKEN=$(cat) vault operator seal' < <(python3 -c "import json;print(json.load(open('$HOME/.railbook/vault-init.json'))['root_token'])")`.
   Within ~2 minutes `VaultSealedOrDown` fires and the dashboard's Vault tile turns red. Existing
   pods keep working (their secrets are already on disk); restart one and it can't start. Fix:
   `scripts/vault-unseal.sh`.
5. **Add a panel through the pipeline.** Add a panel to `dashboards/railbook.json` (e.g.
   cancellations per minute: `sum(rate(railbook_cancellations_total[$__rate_interval]))`),
   open a PR, merge, and see it appear in Grafana about 30 s after the deploy.
