# RailBook glossary and services guide

This page is for anyone, technical or not. Part 1 explains what each piece of RailBook does
in plain words, and where it "lives" (its port). Part 2 is a glossary of the technical terms
used in the other learning notes.

## Part 1: The services, in plain words

### The big picture

RailBook is a small train-ticket website. Think of it as a railway station:

| In the station | In RailBook | What it does |
|---|---|---|
| The entrance gate with a guard | **Kong** | Every visitor comes in through here. It sends you to the right counter, stamps a ticket number on your visit, and stops anyone rushing the booking counter too often. |
| The booking clerks | **RailBook API** (2 copies) | Searches trains, books seats, cancels bookings. Two clerks, so one can take a break (be updated) while the other keeps working. |
| The ledger book | **Postgres** | Where trains, seats and bookings are written down permanently. |
| The safe | **Vault** | Holds every password and key. Clerks get a temporary key each morning; nobody keeps passwords in their pocket (or in the code). |
| The station's dashboard and alarm bells | **Prometheus, Grafana, Alertmanager** | Count everything (bookings, errors, speed), draw it as charts, and ring an alarm when something goes wrong. |
| The CCTV archive | **Splunk** + the **log collector** | The collector picks up every log line written in the station and carries it to the archive; Splunk keeps it so you can search what happened, e.g. one request by its ticket number. |
| The building contractor | **GitHub Actions + the runner on your laptop** | When new code is approved, checks it, packages it and swaps it in without closing the station. |
| The building itself | **kind** (Kubernetes in Docker) | The "mini data centre" on your laptop that runs all of the above and restarts anything that falls over. |

### What is a port?

A computer has one address (for your laptop to itself: `127.0.0.1`, also called `localhost`),
but many programs can listen on it at once. A **port** is a numbered door at that address: the
web server is behind door 80, the database behind door 5432, and so on. A URL like
`http://127.0.0.1:5433` means "address 127.0.0.1, door 5433". When no port is written,
`http://` means door 80.

Inside the cluster, each service also has its own internal address and doors. Those are only
reachable from inside the cluster, which is the point: only Kong's door is opened to your laptop.

### Every service: what it is, where it runs, which port

**What you can open from your laptop** (only on `127.0.0.1`, so nobody else on your network can reach them):

| Open this | What you get | Login |
|---|---|---|
| http://127.0.0.1/ | The RailBook website | none |
| http://127.0.0.1/docs | The API's interactive documentation | none |
| http://grafana.localhost/ | Dashboards (charts) | `admin` + password from `scripts/vault-read.sh secret/monitoring/grafana admin_password` |
| http://prometheus.localhost/ | Raw metrics, queries, alert rules, scrape targets | none |
| http://alertmanager.localhost/ | Alerts currently firing | none |
| http://127.0.0.1:8000/ | Splunk: search the logs (start it with `scripts/splunk.sh`) | Free license has authentication off; if a login page appears: `admin` + `scripts/vault-read.sh secret/splunk/admin password` |

All five go through the same door (port 80) and Kong decides by the name you typed which
service answers. Browsers know `*.localhost` means your own machine. (Old `curl` versions don't:
use `curl -H 'Host: grafana.localhost' http://127.0.0.1/`.)

**Everything running, with its ports.** "Namespace" is the folder it lives in inside the cluster.

| Service | In plain words | Namespace | Port(s) inside the cluster | Reachable from your laptop? |
|---|---|---|---|---|
| **kind node** | The container that *is* the whole cluster | (Docker) | — | Maps `127.0.0.1:80` → Kong |
| **Kong gateway** (proxy) | The front door for all web traffic | `kong` | **8000** in the pod, Service port 80, NodePort **30080** | Yes: `127.0.0.1:80` |
| Kong gateway (status) | Kong's health and metrics page | `kong` | **8100** (`/metrics`, `/status`) | No |
| Kong gateway (admin) | Kong's settings API, used only by the controller | `kong` | **8444** | No |
| **Kong controller** | Reads Kubernetes routing rules and configures the gateway | `kong` | 10254/10255 (metrics), 443 (validation webhook) | No |
| **RailBook API** | The app: trains, bookings, cancellations, the website files | `railbook` | **8000** in the pod, Service `railbook-api` port 80 | Only via Kong |
| RailBook API extra paths | `/healthz` (alive?), `/readyz` (ready?), `/metrics` (numbers) | `railbook` | 8000 | No: Kong deliberately doesn't route them |
| **RailBook setup Job** | Creates tables and sample trains after each deploy, then exits | `railbook` | none (runs and stops) | No |
| **Postgres** | The database | `railbook` | **5432** | No |
| Test Postgres | A separate database only for running the tests on your laptop | (Docker) | 5432 in the container | Yes: `127.0.0.1:5433` |
| **Vault** | The secrets safe (and its web UI) | `vault` | **8200** (API + UI), 8201 (internal cluster traffic) | No (use `kubectl port-forward`) |
| **Vault Agent Injector** | Adds a small "secret fetcher" container to pods that ask for secrets | `vault` | 8080 in the pod, Service port 443 | No |
| **Prometheus** | Collects and stores the numbers every 30 s | `monitoring` | **9090** | Via Kong: `prometheus.localhost` |
| **Alertmanager** | Receives alarms and decides who to tell (here: nobody, just show them) | `monitoring` | **9093** | Via Kong: `alertmanager.localhost` |
| **Grafana** | Turns the numbers into charts | `monitoring` | **3000** in the pod, Service port 80 | Via Kong: `grafana.localhost` |
| Prometheus Operator | Configures Prometheus from `ServiceMonitor` and `PrometheusRule` objects | `monitoring` | 10250 (its own metrics/webhook) | No |
| kube-state-metrics | Reports the state of Kubernetes objects (how many pods are ready, etc.) | `monitoring` | 8080 | No |
| node-exporter | Reports the machine's CPU, memory and disk | `monitoring` | 9100 | No |
| Kubernetes API server | The cluster's control panel; `kubectl` and Helm talk to it | `kube-system` | 6443 | Yes, a random port on `127.0.0.1` (see `kubectl cluster-info`) |
| CoreDNS | The cluster's phone book: turns names like `railbook-postgres` into addresses | `kube-system` | 53 (DNS), 9153 (metrics) | No |
| **GitHub runner** | Waits for "deploy" jobs from GitHub and runs them on your laptop | (systemd service, user `ghrunner`) | none: it only calls out to GitHub on 443 | — |
| **Splunk** | Log storage and search | (Docker container `splunk`, on the `kind` network, outside the cluster) | **8000** (web UI), **8088** (HEC: receives logs), 8089 (management API), 8191 (internal KV store) | Web UI only: `127.0.0.1:8000`. HEC only from the cluster as `https://splunk:8088`; 8089/8191 inside the container only |
| **Log collector** (OTel Collector) | Reads every `railbook` and `kong` pod's log files on the node and sends them to Splunk | `logging` | 8888 (its own metrics), 13133 (health) | No |

### How one booking travels

1. You click **Book** on http://127.0.0.1/. Your browser sends `POST /bookings` to `127.0.0.1`, port 80.
2. Docker forwards port 80 into the kind node, port 30080, which is **Kong**.
3. Kong gives the request an ID (`X-Request-ID`), checks you haven't booked more than 20 times this
   minute (otherwise: 429 "too many requests"), counts it, logs it, and forwards it to the
   `railbook-api` Service on port 80.
4. Kubernetes picks one of the two **API** pods and delivers it to port 8000.
5. The API locks the seats in **Postgres** (port 5432), using a database user that **Vault**
   created for this pod a few minutes or hours ago, charges the (pretend) payment with a key
   that also came from Vault, saves the booking and answers 201 with your PNR.
6. Every 30 s, **Prometheus** asks the API (`:8000/metrics`), Kong (`:8100/metrics`) and Vault
   (`:8200/v1/sys/metrics`) for their counters. **Grafana** draws them; if errors pile up,
   a rule fires and **Alertmanager** shows the alarm.

## Part 2: Glossary

### Python application
- **Python 3.12:** the language the API is written in.
- **FastAPI:** a web framework for building HTTP APIs. You write functions decorated with `@app.get(...)` or `@router.post(...)`. It validates requests from type hints, serializes responses, and generates OpenAPI docs at `/docs`.
- **ASGI (Asynchronous Server Gateway Interface):** the standard interface between Python async web apps and web servers. FastAPI is an ASGI app.
- **Uvicorn:** an ASGI web server. It listens on a port, speaks HTTP, and hands requests to FastAPI. RailBook runs it with one worker, bound to `$PORT`, from `python -m app`.
- **Starlette:** the toolkit FastAPI is built on. It provides routing, middleware and `StaticFiles` for the frontend.
- **Pydantic:** data validation from type hints. `BookingRequest` (seats 1–6) rejects bad input with a 422 response before your code runs.
- **pydantic-settings:** builds a `Settings` object from environment variables (`DB_HOST`, `PORT` and so on). It's Twelve-Factor config in practice. `SecretStr` hides values like passwords when printed.
- **SQLAlchemy 2:** the database toolkit. It provides the ORM models (`Train`, `Booking`), the engine, sessions and connection pooling.
- **psycopg 3:** the PostgreSQL driver SQLAlchemy uses to talk to Postgres.
- **`SELECT … FOR UPDATE`:** a row lock. Two concurrent bookings can't both take the last seat, and the concurrency test proves it.
- **Transaction:** all-or-nothing database work. Seats are reserved, payment is charged, and the booking is committed together, or nothing is.
- **structlog:** structured logging. Every log line is one JSON object on stdout, which Splunk will index.
- **X-Request-ID / correlation ID:** one ID per request, added by Kong and reused by the API. It links log lines across services.
- **prometheus-client / prometheus-fastapi-instrumentator:** expose `/metrics` with counters and histograms: `http_requests_total`, `railbook_bookings_total` and `railbook_booking_duration_seconds`.
- **uv:** a fast Python package manager. `uv.lock` pins exact versions; `exclude-newer` delays brand-new releases as a supply-chain safety measure.
- **ruff:** a linter and formatter. **pytest:** the test runner, used here against a real Postgres.

### Twelve-Factor App
A methodology for cloud-ready apps. Key factors used in RailBook:
- **Config in env vars (III).**
- **Backing services as attached resources (IV):** Postgres sits behind a URL.
- **Build / release / run (V):** an immutable image tag per git SHA.
- **Stateless processes (VI).**
- **Port binding (VII).**
- **Disposability (IX):** graceful shutdown.
- **Dev/prod parity (X).**
- **Logs as event streams (XI):** stdout.
- **Admin processes (XII):** `python -m app.admin setup` runs as a Job.

### Containers and Kubernetes
- **Docker image / container:** a packaged app plus its dependencies, and a running instance of that package.
- **Multi-stage build:** build in one stage, copy only the result into a slim runtime image. RailBook's image runs as non-root UID 10001.
- **kind (Kubernetes IN Docker):** a full Kubernetes cluster running inside a Docker container on your laptop.
- **Pod:** the smallest unit Kubernetes runs; one or more containers. A **Deployment** keeps N pods running and does rolling updates. A **StatefulSet** gives pods stable identity and storage, as Postgres needs. A **Job** runs to completion.
- **Service:** a stable name and IP in front of pods. **Ingress:** HTTP routing rules from outside the cluster to Services.
- **ConfigMap:** non-secret config. **Secret:** Kubernetes' built-in secret store. RailBook avoids it for app secrets and uses Vault.
- **PVC (PersistentVolumeClaim):** a disk that survives pod restarts. Vault and Postgres use one.
- **Probes:** liveness (is it stuck? if so, restart) and readiness (can it serve? if not, stop sending traffic).
- **ServiceAccount / Role / RoleBinding (RBAC):** pod and user identities and what they're allowed to do. `github-deployer` can only deploy in the `railbook` namespace.
- **Namespace:** a folder-like boundary for objects.
- **CRD (Custom Resource Definition):** adds new object types to Kubernetes, such as `KongPlugin`, `ServiceMonitor` and `PrometheusRule`.
- **Operator:** a controller that watches custom resources and acts on them. The Prometheus Operator turns ServiceMonitors into scrape config.
- **Helm:** the Kubernetes package manager. A **chart** is a set of templates plus `values.yaml`; a **release** is an installed chart with revisions you can roll back. Hooks run Jobs at install or upgrade time.

### Kong (API gateway)
- **API gateway:** the single entry point that routes, rate-limits, logs and measures traffic.
- **Kong Ingress Controller:** turns Kubernetes Ingress and `KongPlugin` objects into Kong config. It runs DB-less, so the config lives in memory.
- **Plugins used:**
  - correlation-id adds X-Request-ID;
  - prometheus exposes gateway metrics;
  - file-log writes JSON access logs;
  - rate-limiting returns 429 when POST `/bookings` exceeds 20 per minute.

### HashiCorp Vault (secrets)
- **Seal / unseal:** Vault starts sealed with its data encrypted. Shamir secret sharing splits the unseal key into 5 shares, and any 3 unseal it.
- **Raft storage:** Vault's built-in persistent storage.
- **Root token:** full admin access. It's kept outside the repo in `~/.railbook/vault-init.json`.
- **Auth method (Kubernetes):** pods log in with their ServiceAccount token. A **role** maps a ServiceAccount to **policies**, which list the paths a workload may read (least privilege).
- **KV v2:** a versioned key/value store for static secrets: the payment API key and the Grafana password.
- **Database secrets engine:** creates short-lived Postgres users on demand (dynamic secrets). Each has a **lease** with a **TTL**, and Vault revokes it on expiry.
- **Agent Injector:** a webhook that adds a Vault Agent container to pods. Its init container writes secrets to `/vault/secrets/` before the app starts, and an optional sidecar keeps renewing them.

### CI/CD
- **CI (Continuous Integration):** every PR is linted and tested automatically. **CD (Continuous Deployment):** merging to `main` deploys automatically.
- **GitHub Actions:** workflows, made of jobs (checks, image, deploy) made of steps.
- **Self-hosted runner:** your laptop runs the deploy job, as the `ghrunner` user, using a 24-hour token.
- **GHCR:** GitHub Container Registry, where images are tagged by git SHA.
- **Pinning by SHA or digest:** actions and images are referenced immutably, so they can't be swapped under you.
- **Trivy:** an image vulnerability scanner. **gitleaks:** a scanner for leaked secrets in git.
- **Branch protection:** PRs and passing checks are required before merging.
- **Rolling update / `--rollback-on-failure`:** deploy without downtime, and roll back automatically if the deploy fails.

### Observability (blocks 4 and 5)
- **Metrics:** numbers over time. Kinds:
  - **counter:** only goes up;
  - **gauge:** goes up and down;
  - **histogram:** values sorted into buckets, used for latency percentiles such as p95.
- **Prometheus:** pulls (scrapes) `/metrics` from targets on a schedule, stores time series, and answers PromQL queries such as `rate()`, `histogram_quantile()` and `absent()`.
- **ServiceMonitor:** declares which Services to scrape. **PrometheusRule:** declares alert rules, with `for:` as the time a condition must hold.
- **Alertmanager:** receives firing alerts, then groups, silences and routes them.
- **Grafana:** dashboards on top of Prometheus. Dashboards ship as ConfigMaps labelled `grafana_dashboard=1`.
- **kube-prometheus-stack:** one Helm chart bundling the Operator, Prometheus, Alertmanager, Grafana, kube-state-metrics (object state) and node-exporter (machine stats).
- **Splunk:** log indexing and search. **HEC (HTTP Event Collector)** receives logs over HTTP. The **OpenTelemetry Collector** ships container logs to it.
- **Sourcetype / index:** Splunk's "what kind of data" label (ours: `kube:container:<container>`) and the store it's kept in (`main`). **SPL** is Splunk's search language.
- **Saved search:** a named SPL query, kept in git as a Splunk app (`deploy/splunk/railbook`).
- **OTel Collector pipeline:** receivers (read data) → processors (change it) → exporters (send it). Ours: `file_log` → `memory_limiter`, `transform`, `batch` → `splunk_hec`.
- **DaemonSet:** runs exactly one copy of a pod on every node; the usual shape for anything that reads node-local files such as logs.
- **hostPath:** mounts a directory of the node itself into a pod (here `/var/log/pods`, read-only).
- **Checkpoint / at-least-once:** the collector remembers how far it has read each file, so a restart neither skips nor re-sends lines, except a small window if it's killed mid-send. So duplicates are possible; lines are lost only if Splunk stays unreachable longer than the 5-minute retry window, or the collector restarts while batches are still queued in memory.
