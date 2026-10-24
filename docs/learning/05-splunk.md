# Block 5 — Splunk and the log collector, explained

How every log line from Kong and the API ends up searchable in Splunk, how the HEC token gets
there without a Kubernetes Secret, how the pipeline is watched, and what broke on the way.
(New to the terms? Start with [00-glossary](00-glossary.md).)

## Contents

1. [Logs in one page](#1-logs-in-one-page)
2. [What got installed, and where](#2-what-got-installed-and-where)
3. [Splunk in Docker: `scripts/splunk.sh`](#3-splunk-in-docker-scriptssplunksh)
4. [The HEC token: from Vault to the collector](#4-the-hec-token-from-vault-to-the-collector)
5. [The collector: `deploy/logging/otel-collector.yaml`, piece by piece](#5-the-collector-deployloggingotel-collectoryaml-piece-by-piece)
6. [What's in a log line: the API and Kong](#6-whats-in-a-log-line-the-api-and-kong)
7. [Saved searches as code](#7-saved-searches-as-code)
8. [Watching the pipeline: metrics and two alerts](#8-watching-the-pipeline-metrics-and-two-alerts)
9. [Script changes](#9-script-changes)
10. [How it was verified](#10-how-it-was-verified)
11. [Problems hit and what they teach](#11-problems-hit-and-what-they-teach)
12. [Day-to-day use and exercises](#12-day-to-day-use-and-exercises)

---

## 1. Logs in one page

Metrics (block 4) say *how many, how fast*. Logs say *what happened to this one request*: which
route, which status, how long, and — the point of this block — **which other log lines belong to
the same request**.

**Twelve-Factor XI: logs are a stream.** The app never opens a log file and knows nothing about
Splunk. It writes one JSON object per line to stdout. Shipping those lines somewhere is the
platform's job:

```
API / Kong ──stdout──> containerd ──writes──> /var/log/pods/<ns>_<pod>_<uid>/<container>/0.log
                                                     │  (on the kind node)
                                     OTel Collector (DaemonSet) tails the files
                                                     │
                                     HTTPS POST to Splunk HEC  splunk:8088
                                                     │
                                     Splunk indexes, you search
```

Change the destination (Splunk → Loki → Elastic) and the app doesn't change at all.

**One request, one ID.** Kong stamps `X-Request-ID` on every request (block 2) and the API logs
it as `request_id`. Searching for that one value returns Kong's line *and* the API's line — the
request followed end to end (PRD S4).

**Splunk vocabulary** you'll meet below:

| Term | Meaning |
|---|---|
| **Event** | One indexed record — here, one log line |
| **Index** | Where events are stored; we use the default `main` |
| **Sourcetype** | "What kind of data this is"; we set one per container (`kube:container:api`) |
| **Source** | Where it came from; we set `kubernetes` for everything the collector sends |
| **HEC** | HTTP Event Collector: Splunk's HTTPS endpoint that accepts events with a token |
| **SPL** | Search Processing Language: `index=main status>=400 \| stats count by path` |
| **App** | A folder of `.conf` files; ours holds the saved searches |

---

## 2. What got installed, and where

```
Docker network "kind"
├── railbook-control-plane          the kind node (the whole cluster)
│   └── logging namespace
│       └── otel-collector          DaemonSet: 1 pod per node (here: 1)
│           ├── vault-agent-init    writes the HEC token to /vault/secrets/hec-token, exits
│           └── otel-collector      tails pod logs, sends to splunk:8088     :8888 metrics, :13133 health
└── splunk                          Splunk Enterprise 10.6.0, Free license (container, not in the cluster)
```

| Splunk port | What | Reachable from |
|---|---|---|
| **8000** | Web UI | Your laptop: `http://127.0.0.1:8000` only |
| **8088** | HEC (receives logs) | The `kind` Docker network only: pods use `https://splunk:8088` |
| 8089 | Management REST API (used by `splunk.sh` to reload apps) | Inside the container only |
| 8191 | Splunk's internal KV store | Inside the container only |

**Why Splunk is outside the cluster.** It needs ~1.6 GB and keeps its own data. As a separate
container it survives `kind delete cluster`, so rebuilding the cluster doesn't lose logs or
saved searches. Joining the `kind` Docker network makes Docker's DNS resolve the name `splunk`
for the node — and pods inherit the node's upstream DNS, so `splunk` resolves inside pods too.

**Why a hand-written collector, not the Splunk OTel Collector Helm chart (PRD L2 changed).** The
chart always passes the HEC token through a Kubernetes Secret. This project's rule is that no
Kubernetes Secret holds an application secret (PRD V3). Writing the DaemonSet by hand lets Vault
Agent put the token in a file instead (section 4). It's also a better lesson: every line of the
collector is visible in one 270-line file instead of spread across a chart's templates and values.

Memory: Splunk ~1.6 of its 2 GiB cap; the collector ~200 MiB of its 256 MiB limit.

---

## 3. Splunk in Docker: `scripts/splunk.sh`

Safe to rerun: creates the container the first time, just starts it after that. Docker gives it
**no restart policy**, so after a reboot you start it with this script (it's separate from
`bootstrap.sh` because it isn't part of the cluster).

**First run only — secrets from Vault:**

```bash
admin_password=$(scripts/vault-read.sh secret/splunk/admin password | tr -d '\n')
hec_token=$(scripts/vault-read.sh secret/splunk/hec token | tr -d '\n')
```
Both are generated once by `scripts/vault-setup.sh base` (random hex password, a UUID for the
token — HEC tokens are GUIDs). Splunk runs outside Kubernetes, so the Vault Agent Injector can't
reach it; the script reads the values and passes them as environment variables. The trade-off is
written in the script: they are visible to `docker inspect`, i.e. to anyone who can run Docker,
who is already root-equivalent on this machine. Vault stays the source of truth.

**`docker run`, flag by flag:**

| Flag | Why |
|---|---|
| `--network kind` | Same Docker network as the kind node, so pods reach `splunk:8088` |
| `--hostname splunk` | The `host` field on Splunk's own internal events |
| `-p 127.0.0.1:8000:8000` | Web UI on your machine only. **8088 is not published**: only the cluster sends logs |
| `--memory 2g` | Hard cap; Splunk would otherwise take what it can of the 8 GB WSL budget |
| `SPLUNK_START_ARGS=--accept-license`, `SPLUNK_GENERAL_TERMS=...` | Required to start the image non-interactively |
| `SPLUNK_LICENSE_URI=Free` | Free license: 500 MB/day indexed, and **authentication off**: the management API answered without any credentials. That's why nothing is published beyond `127.0.0.1` |
| `SPLUNK_PASSWORD` | Admin password (the image's provisioning sets it) |
| `SPLUNK_HEC_TOKEN` | Creates and enables an HEC input with exactly this token, default index `main` |
| `-v splunk-var:/opt/splunk/var`, `-v splunk-etc:/opt/splunk/etc` | Indexed data and config survive deleting the container |
| `image@sha256:...` | Pinned by digest: the tag can be moved, the digest can't |

**Waiting.** The image has a Docker health check. The first start runs Ansible provisioning and
takes 2–4 minutes; the script polls `docker inspect -f '{{.State.Health.Status}}'` for up to 10.

**Syncing the saved searches.** Every run copies `deploy/splunk/railbook` from git into the
container and asks Splunk to re-read it, no restart:

```bash
docker cp deploy/splunk/railbook/. splunk:/opt/splunk/etc/apps/railbook
docker exec -u root splunk sh -c 'chown -R splunk:splunk ... && chmod -R u=rwX,go=rX ...'
docker exec splunk sh -c 'curl ... https://localhost:8089/services/apps/local/_reload &&
                          curl ... https://localhost:8089/servicesNS/nobody/railbook/saved/searches/_reload'
```
- **Copied, not mounted** — see section 11 for why a read-only mount broke provisioning.
- **chmod:** files from `/mnt/d` (the Windows drive) arrive with mode 777; Splunk's config should
  be writable only by its owner.
- **The password stays inside the container:** the `curl` runs in the container and uses its own
  `$SPLUNK_PASSWORD`, so the password never appears in a command line on your machine (where
  `ps` would show it).

---

## 4. The HEC token: from Vault to the collector

This was block 5's open question: the collector image is minimal (no shell), so the usual trick
of a wrapper script that exports the token can't work. The answer has three parts.

**1. Vault: a policy and a role** (`scripts/vault-setup.sh base`):

```sh
vault policy write log-collector - <<'HCL'
path "secret/data/splunk/hec" { capabilities = ["read"] }
HCL
vault write auth/kubernetes/role/log-collector \
  bound_service_account_names=otel-collector \
  bound_service_account_namespaces=logging \
  audience=$AUD token_policies=log-collector token_ttl=15m
```
Only a pod running as service account `otel-collector` in namespace `logging` gets a token that
can read exactly that one path.

**2. Vault Agent writes a file** (pod annotations in the DaemonSet):

```yaml
vault.hashicorp.com/agent-inject: "true"
vault.hashicorp.com/role: log-collector
vault.hashicorp.com/agent-pre-populate-only: "true"
vault.hashicorp.com/agent-inject-secret-hec-token: secret/data/splunk/hec
vault.hashicorp.com/agent-inject-template-hec-token: |
  {{- with secret "secret/data/splunk/hec" -}}
  {{ .Data.data.token }}
  {{- end -}}
```
Same pattern as Grafana in block 4: an init container logs in with the pod's service account
token, renders the template to `/vault/secrets/hec-token` (just the token, whitespace trimmed by
the `{{-` / `-}}`), and exits. `pre-populate-only` because the token is static: no sidecar needed.

**3. The collector reads the file itself:**

```yaml
token: ${file:/vault/secrets/hec-token}
```
The collector's config supports *config providers*: `${env:X}` reads an environment variable,
`${file:path}` reads a file's contents, at startup. No shell, no wrapper, no Secret.

Check: `kubectl get secret -n logging` lists nothing.

---

## 5. The collector: `deploy/logging/otel-collector.yaml`, piece by piece

The file holds seven objects: Namespace, ServiceAccount, ConfigMap (the pipeline), DaemonSet,
Service + ServiceMonitor (metrics for Prometheus) and PrometheusRule (alerts, section 8).

### Why a DaemonSet

Container logs are files **on the node** that runs the container. To read every pod's logs you
need one reader on every node: a DaemonSet runs exactly one pod per node, and a new node gets one
automatically. kind has one node, so there's one pod.

### The ServiceAccount has no permissions

```yaml
kind: ServiceAccount
metadata: { name: otel-collector, namespace: logging }
```
No Role, no RoleBinding. It exists only so Vault can recognise the pod (section 4). The
collector doesn't call the Kubernetes API: namespace, pod and container names come from the log
file path. (The Helm chart adds a `k8sattributes` processor that asks the API for labels too;
not needed to answer "which pod said this".)

### The pipeline (ConfigMap `config.yaml`)

An OpenTelemetry Collector is a pipeline: **receivers → processors → exporters**, plus
**extensions** for things that aren't data (health, storage).

```yaml
extensions:
  health_check: { endpoint: 0.0.0.0:13133 }
  file_storage: { directory: /var/lib/otel-collector }
```
- `health_check` answers `/` with 200 when the pipeline is running. The probes use it.
- `file_storage` remembers **how far each file has been read** (a checkpoint). Without it, every
  restart would either re-send all old lines or skip what was written while it was down. The
  directory is a `hostPath` on the node, so checkpoints outlive the pod.

```yaml
receivers:
  file_log:
    include:
      - /var/log/pods/railbook_*/*/*.log
      - /var/log/pods/kong_*/*/*.log
    start_at: beginning
    storage: file_storage
    include_file_path: true
    operators:
      - type: container
        add_metadata_from_filepath: true
```
- **include:** only our two namespaces (PRD L2). The Free license allows 500 MB/day, and
  Kubernetes' own components are chatty. Path pattern:
  `/var/log/pods/<namespace>_<pod>_<uid>/<container>/<restart-count>.log`.
- **start_at: beginning** applies only to files the checkpoint has never seen; afterwards
  `file_storage` decides ("Resuming from previously known offset(s)" in the log).
- **include_file_path: true** attaches each line's file path as the attribute `log.file.path`.
  Off by default — and the next operator needs it (section 11, first problem).
- **The `container` operator** undoes what the container runtime did. containerd wraps every
  line as `<time> <stdout|stderr> <F|P> <the line>` (CRI format; `P` = partial, a long line split
  in pieces). The operator strips that wrapper, rejoins partial lines, takes the runtime's
  timestamp, and — with `add_metadata_from_filepath` — parses the path into
  `k8s.namespace.name`, `k8s.pod.name`, `k8s.pod.uid`, `k8s.container.name`.

```yaml
processors:
  memory_limiter: { check_interval: 1s, limit_mib: 160, spike_limit_mib: 40 }
  transform/sourcetype:
    log_statements:
      - context: resource
        statements:
          - set(attributes["com.splunk.sourcetype"], Concat(["kube:container:", attributes["k8s.container.name"]], ""))
          - set(attributes["k8s.cluster.name"], "railbook")
  batch: { timeout: 5s }
```
- **memory_limiter** first in the chain: when the Go heap passes 160 MiB, the collector refuses
  new data (the receiver gets an error and slows down) instead of growing until it's killed. It counts only the heap; the container's 256 MiB limit must also cover the
  runtime and the page cache of the files being read, hence the wide gap.
- **transform** uses OTTL (OpenTelemetry Transformation Language). The Splunk HEC exporter reads
  the attribute `com.splunk.sourcetype` as the event's sourcetype, so this gives every container
  its own: `kube:container:api`, `kube:container:proxy`, `kube:container:vault-agent`, ... —
  which is what the saved searches filter on.
- **batch** groups lines into one HTTP request (at most every 5 s): far fewer requests to Splunk.

```yaml
exporters:
  splunk_hec:
    endpoint: https://splunk:8088/services/collector
    token: ${file:/vault/secrets/hec-token}
    index: main
    source: kubernetes
    tls: { insecure_skip_verify: true }
```
- `splunk` resolves through Docker's DNS on the `kind` network (section 2).
- Resource attributes (`k8s.pod.name`, ...) are sent as HEC **fields**, so they're searchable
  directly: `k8s.namespace.name=kong`.
- **insecure_skip_verify:** HEC uses Splunk's self-signed certificate. Acceptable only because
  both ends are on this machine and the traffic never leaves the Docker network; anywhere else,
  give the collector Splunk's CA.
- Not configured but important, the defaults: an **in-memory sending queue** and **retry with
  backoff for up to 5 minutes**, then the batch is dropped (section 8 measured this).

```yaml
service:
  telemetry:
    metrics:
      readers:
        - pull: { exporter: { prometheus: { host: 0.0.0.0, port: 8888 } } }
  pipelines:
    logs:
      receivers: [file_log]
      processors: [memory_limiter, transform/sourcetype, batch]
      exporters: [splunk_hec]
```
The collector's own metrics (lines read, sent, failed, queue size) on `:8888` for Prometheus.
The `pipelines` block is what actually wires components together: anything defined above but
not listed here is ignored.

### The DaemonSet

```yaml
annotations:
  railbook/config-version: "3"
```
The collector reads its config **only at startup**. `kubectl apply` updates the ConfigMap but
the running pod keeps the old config. Changing an annotation in the pod *template* changes the
spec, so the DaemonSet replaces the pod. Bump it whenever the ConfigMap changes. (Helm charts do
the same with a hash of the ConfigMap; by hand, a number.)

```yaml
image: quay.io/signalfx/splunk-otel-collector:0.161.0@sha256:...
env:
  - name: SPLUNK_MEMORY_TOTAL_MIB
    value: "256"
```
Splunk's distribution of the OpenTelemetry Collector (it includes `splunk_hec`, `file_log`,
`file_storage` and the `${file:}` provider). It sets Go's memory target to 90 % of
`SPLUNK_MEMORY_TOTAL_MIB`, which defaults to 512 — it must match the real container limit.

```yaml
resources:
  requests: { cpu: 20m, memory: 128Mi }
  limits: { memory: 256Mi }
```
Sized from measurement, not guessed: 128 Mi was too small (section 11). Steady state is ~200 Mi.

```yaml
securityContext:
  runAsUser: 0
  runAsNonRoot: false
  allowPrivilegeEscalation: false
  readOnlyRootFilesystem: true
  capabilities: { drop: [ALL] }
```
The node's log files are owned by root with mode 0640, so the collector runs as root — and gets
nothing else: no Linux capabilities, no way to gain more, an unwritable image filesystem. The
only writable path is the checkpoint directory.

```yaml
volumes:
  - name: pod-logs
    hostPath: { path: /var/log/pods }                                  # mounted readOnly
  - name: checkpoints
    hostPath: { path: /var/lib/otel-collector, type: DirectoryOrCreate }
```
`hostPath` mounts a directory **of the node** into the pod — the only way to read files that
belong to other pods. That's powerful, which is why the log mount is read-only.

---

## 6. What's in a log line: the API and Kong

**The API** (`app/main.py`, middleware `request_context`):

```python
request_id = request.headers.get(REQUEST_ID_HEADER) or str(uuid.uuid4())
structlog.contextvars.bind_contextvars(request_id=request_id)
...
log.info("request", method=..., path=getattr(route, "path", ...), status=..., duration_ms=...)
```
`bind_contextvars` attaches `request_id` to *every* log line written while handling this request,
not just the `request` line. `path` is the route **template** (`/bookings/{pnr}`), so errors
group by route rather than by every PNR. A line in Splunk:

```json
{"method": "GET", "path": "/trains", "status": 422, "duration_ms": 13.43, "event": "request",
 "request_id": "e2e-1792879371", "level": "info", "logger": "railbook.http", "timestamp": "..."}
```

**Kong** (`templates/kong-plugins.yaml`, plugin `file-log` writing to `/dev/stdout`) logs one
JSON object per request: client IP, latencies, request and response headers including
`x-request-id`, upstream status. Block 5 added:

```yaml
custom_fields_by_lua:
  route: "return nil"
  service: "return nil"
```
By default every line carries Kong's full route and service definitions (~3 KB), identical on
every request. They waste the 500 MB/day license, and they pushed fields like `response.status`
past the first 5,000 characters, which is all Splunk's automatic JSON field extraction reads. A
Lua snippet that returns `nil` removes the field. Lines are now ~1.1–1.3 KB.

**Splunk reads the JSON at search time.** No parsing config: Splunk sees a JSON object and
extracts its fields when you search, nested ones with dots: `response.status`,
`request.headers.x-request-id`.

Today's volume, for scale: ~670 events, 114 KB of license use. The 500 MB/day limit is far away.

---

## 7. Saved searches as code

A Splunk **app** is just a folder of `.conf` files. `deploy/splunk/railbook/`:

| File | Purpose |
|---|---|
| `default/app.conf` | Names the app, `is_visible = false` (no menu entry; it holds only searches) |
| `default/savedsearches.conf` | The three searches |
| `metadata/default.meta` | `export = system`: share them with every app, so they show under *Search & Reporting → Reports*; read for everyone, write for admin |

The searches live in git (reviewed in PRs, rebuilt on a fresh Splunk) instead of being clicked
together in the UI and lost with the container.

**`RailBook - Errors by route`**

```spl
index=main sourcetype="kube:container:api" event=request status>=400
| stats count by method, path, status
| sort - count
```
Only `event=request` lines (one per request), grouped by route template.

**`RailBook - Follow one request`**

```spl
index=main "$request_id$"
| eval component=case(sourcetype=="kube:container:proxy", "kong", sourcetype=="kube:container:api", "api", true(), sourcetype)
| eval status=coalesce(status, 'response.status'), path=coalesce(path, 'request.uri')
| table _time component path status duration_ms event
| sort _time
```
- `"$request_id$"` is a **search-time argument**: run it as
  `| savedsearch "RailBook - Follow one request" request_id=<id>`. A plain-text search for the
  value finds it wherever it appears — API `request_id`, Kong's `x-request-id` header.
- Kong and the API name the same thing differently; `coalesce` takes whichever field exists, so
  both lines land in one table. Field names with dots must be quoted with **single** quotes in
  `eval`.

Result for a real request:

```
_time                    component  path     status  duration_ms  event
22:02:51.373             api        /trains  422     13.43        request
22:02:51.376             kong       /trains  422
```
Kong's line is 3 ms *later*: Kong logs after the response has passed back through it.

**`RailBook - Rate limited (429)`**

```spl
index=main sourcetype="kube:container:proxy" response.status=429
| timechart span=1m count by request.method
```
Kong answers these itself, so **only Kong's logs see them** — the API never knew. Same reason
block 4's 429 alert uses Kong's metrics.

---

## 8. Watching the pipeline: metrics and two alerts

Logs about the app are useless if the pipeline that ships them is silently broken. The
ServiceMonitor lets Prometheus scrape the collector's `:8888` (target `otel-collector`, `up`):

| Metric | Meaning |
|---|---|
| `otelcol_receiver_accepted_log_records` | Lines read from the files |
| `otelcol_exporter_sent_log_records` | Lines Splunk accepted |
| `otelcol_exporter_send_failed_log_records` | Lines given up on (dropped) |
| `otelcol_exporter_queue_size` | Batches waiting to be (re)sent |

Note: **no `_total` suffix** — this collector version exports counters without it.

**What happens when Splunk is down — measured** by `docker stop splunk` for 7 minutes:

```
22:18  Splunk stopped
22:19  queue_size 1   a batch failed, waits in the queue, retried with growing backoff
 ...   queue_size 1   nothing lost yet: if Splunk came back now, it would be delivered
22:24  queue_size 0, send_failed 2    5 min of retries (the default max) → batch dropped
```

So there are two moments worth an alert, and the PrometheusRule has both:

```promql
# LogsQueuedForSplunk (warning, for: 2m)
sum(otelcol_exporter_queue_size) > 0
```
Normally the queue empties within a second. Two minutes of non-empty queue means sends keep
failing. Nothing is lost yet — this is the "act now" alert.

```promql
# LogsDroppedBeforeSplunk (critical, no for)
sum(rate(otelcol_exporter_send_failed_log_records[5m])) > 0
  or
sum(
  otelcol_exporter_send_failed_log_records
    unless
  otelcol_exporter_send_failed_log_records offset 5m
) > 0
```
- No `for:` — one drop already means data is gone.
- The second half is block 4's lesson again ([04 §7](04-prometheus-grafana.md#railbookbookingsratelimited-info)):
  the counter only exists after the first drop, it appears already at 2, and `rate()` sees no
  increase in a first sample. `unless … offset 5m` catches "a counter that didn't exist 5 minutes
  ago".

Seen in the test: `LogsDroppedBeforeSplunk` firing, `LogsQueuedForSplunk` pending → firing after
2 minutes, both resolved after Splunk came back, and every line written during the outage
(still within the 5-minute retry window) arrived in Splunk afterwards.

The limit of this design: the queue is in memory. A collector restart during an outage loses
what's queued (the checkpoint has already moved past those lines). The fix would be
`sending_queue.storage: file_storage` — a persistent queue on the node's disk.

---

## 9. Script changes

| File | Change | Why |
|---|---|---|
| `scripts/splunk.sh` | New | Section 3 |
| `scripts/vault-setup.sh` | `secret/splunk/admin`, `secret/splunk/hec`, policy + role `log-collector` | Section 4 |
| `scripts/bootstrap.sh` | Applies the collector after the app; prints the Splunk URL | After Vault (token) and the monitoring CRDs (ServiceMonitor, PrometheusRule). If Splunk isn't up yet the collector starts anyway and `LogsQueuedForSplunk` tells you |
| `deploy/charts/railbook/templates/kong-plugins.yaml` | `custom_fields_by_lua` | Section 6 |

Order for a fresh session: `docker start ...` → `scripts/bootstrap.sh` → `scripts/splunk.sh`.
Either order of the last two works; logs written before Splunk is up are read from the files once
it is (as long as the collector hasn't spent 5 minutes failing on them).

---

## 10. How it was verified

| Check | Result |
|---|---|
| Splunk container | Healthy; Free license; HEC health OK; 3 saved searches loaded |
| DNS and network | From a pod, `splunk` → the container's IP, `https://splunk:8088` reachable; 8088 *not* reachable from the laptop |
| No Kubernetes Secret | `kubectl get secret -n logging`: none |
| Collector | Running, 0 restarts; ~200 MiB |
| Events by sourcetype | `api`, `proxy`, `ingress-controller`, `postgres`, `setup`, `vault-agent`, `vault-agent-init` — only `railbook` and `kong` namespaces |
| One request end to end | `curl -H "X-Request-ID: e2e-..." 127.0.0.1/trains` → the API line and the Kong line, same ID |
| Errors by route | `GET /trains 422` counted |
| Rate limit | 25 POSTs in a minute: 20 × 422 (empty body), then 5 × 429; the 429 search shows 5 in that minute |
| Prometheus | Target `otel-collector` up; collector metrics present |
| Splunk outage (7 min) | Section 8: queued → retried → dropped after 5 min; both alerts fired and resolved; lines from inside the retry window delivered after restart |

---

## 11. Problems hit and what they teach

| Problem | Cause | Fix | Lesson |
|---|---|---|---|
| C: drive full → Docker Desktop crashed during the Splunk image pull | Docker's disk image lives on C: (`docker_data.vhdx`); the Splunk image pull filled the last space | Freed space on C:; Docker restarted without data loss | Check `df -h /mnt/c` before big pulls. The cluster's state is in that one file. |
| Splunk provisioning failed with a read-only app mount | The image's Ansible `chown`s everything under `/opt/splunk` to the `splunk` user, and fails on a read-only mount; a writable mount would let Splunk change the git checkout | `docker cp` + `chown`/`chmod` + REST `_reload` | Container images make assumptions about their filesystem; read the provisioning error, not just the exit code. |
| `/debug/refresh` returned 404 | It's a web UI endpoint (port 8000), not management (8089) | `apps/local/_reload` and `saved/searches/_reload` on 8089 | Splunk has two HTTP APIs; scripts use 8089. |
| Every line failed: `'log.file.path' is missing` | `add_metadata_from_filepath` needs the path, but `file_log` only attaches it with `include_file_path: true` (off by default) | Turn it on | Defaults are a design decision someone else made; read what the next operator needs. |
| Collector restarting every ~30 s at first | The error above was logged *with a stack trace* for every line; the flood kept it from answering health checks | Fixing the config | One misconfiguration can look like a resource problem. Fix errors first, then measure. |
| Restarts with Splunk down; exit code 0, not `OOMKilled`; probes "context deadline exceeded" | 128 Mi limit: working set sat at 120–125 Mi (Go runtime + page cache of the files read); the kernel reclaimed constantly, the process stalled past the 1 s probe timeout; each restart lost the in-memory retry queue ("Dropping data") | 256 Mi limit, `SPLUNK_MEMORY_TOTAL_MIB` 256, `memory_limiter` 160/40 | Same as Grafana in block 4: memory pressure shows up as slowness and probe failures, not always as OOM. Raising the probe timeout would have hidden it. |
| Alert could never fire | Expression used `..._total`; this version exports counters without the suffix | Use the real name | Look at `/api/v1/label/__name__/values` before writing a rule. |
| Drop counter invisible to `rate()` | Created at the first drop | `unless … offset 5m` half | Block 4's lesson, again: any counter that appears only on failure needs this. |
| 50 log lines in Splunk twice (same file, same offset, indexed in the same second) | All from the moment the crash-looping first pod was replaced. The checkpoint is saved separately from sending; a pod killed between "sent" and "checkpoint saved" makes its successor re-send that window | None needed; a clean restart flushes first. Not seen again in later restarts | Log shipping is **at-least-once**: duplicates are possible. Lines are lost only if Splunk stays unreachable past the 5-minute retry window, or the collector restarts while batches wait in its in-memory queue (section 8). Searches that count should tolerate a rare duplicate; `dedup _raw` removes them. |
| `"filelog" alias is deprecated` warning | Renamed upstream | `file_log` | Same component, new name. |

---

## 12. Day-to-day use and exercises

```bash
scripts/splunk.sh                                       # start Splunk (each session)
scripts/vault-read.sh secret/splunk/admin password      # if the web UI shows a login page: admin + this
kubectl -n logging logs ds/otel-collector               # collector's own log
```

- Splunk: http://127.0.0.1:8000 → *Search & Reporting*; saved searches under *Reports*.
- Every response carries `X-Request-ID` — copy it from the browser's dev tools (Network tab).

Exercises:

1. **Follow your own request.** Book a ticket in the frontend, copy `X-Request-ID` from the
   response headers, then search
   `| savedsearch "RailBook - Follow one request" request_id=<id>`. Add a booking with
   "simulate payment failure" and find its 402 in *Errors by route*.
2. **Explore the fields.** Search `index=main sourcetype="kube:container:proxy"` and open one
   event: find `latencies.proxy`, `response.status`, `request.headers.x-request-id` in the field
   sidebar. Then `... | stats avg(latencies.proxy) by request.uri`.
3. **Break the pipeline.** `docker stop splunk`, make a few requests, and watch
   http://prometheus.localhost/alerts: `LogsQueuedForSplunk` pending → firing. Start it again
   within 5 minutes with `scripts/splunk.sh` and find your requests in Splunk. Then repeat, but
   wait more than 5 minutes: `LogsDroppedBeforeSplunk` fires and those lines never arrive.
4. **Restart the collector, look for duplicates.** `kubectl -n logging rollout restart ds/otel-collector`,
   make a few requests, then search
   `index=main earliest=-1h | stats count by _raw, log.file.path | where count>1`.
   A clean restart flushes and checkpoints, so expect none. Then find the 50 from 2026-10-24
   (section 11) with `earliest=-30d` and explain why a *killed* pod causes them.
5. **Make the queue survive restarts.** Add `sending_queue: { storage: file_storage }` to the
   exporter, bump `config-version`, and repeat exercise 3 with a collector restart during the
   outage.
