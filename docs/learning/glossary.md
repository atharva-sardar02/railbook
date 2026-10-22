I can't create files from this side thread. Ask in the main conversation and it can be saved as `docs/learning/00-glossary.md` and linked from the README. In the meantime, here's the content, grouped by layer and covering what RailBook uses so far.

## RailBook glossary

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
- **Splunk (block 5):** log indexing and search. **HEC (HTTP Event Collector)** receives logs over HTTP. The **OpenTelemetry Collector** ships container logs to it.