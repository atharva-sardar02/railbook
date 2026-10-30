# Block 1 — The booking API, explained

Why each file exists and why it is written the way it is. Read alongside the code.

## Contents

0. [Project map](#0-project-map)
1. [`pyproject.toml` + `uv.lock` — dependencies (factor II)](#1-pyprojecttoml--uvlock--dependencies-factor-ii)
2. [`app/config.py` — config from the environment (factor III)](#2-appconfigpy--config-from-the-environment-factor-iii)
3. [`app/db.py` — the connection pool and `SET ROLE`](#3-appdbpy--the-connection-pool-and-set-role)
4. [`app/models.py` — the tables](#4-appmodelspy--the-tables)
5. [`app/schemas.py` — the API's contract](#5-appschemaspy--the-apis-contract)
6. [`app/routers/bookings.py` — the core logic](#6-approutersbookingspy--the-core-logic)
7. [`app/routers/trains.py` — search](#7-approuterstrainspy--search)
8. [`app/payment.py` — mock payment provider](#8-apppaymentpy--mock-payment-provider)
9. [`app/metrics.py` — business metrics](#9-appmetricspy--business-metrics)
10. [`app/log_config.py` — JSON logs to stdout (factor XI)](#10-applog_configpy--json-logs-to-stdout-factor-xi)
11. [`app/main.py` — wiring it together](#11-appmainpy--wiring-it-together)
12. [`app/__main__.py` — starting the server (factors VII, VIII)](#12-app__main__py--starting-the-server-factors-vii-viii)
13. [`app/admin.py` — one-off admin process (factor XII)](#13-appadminpy--one-off-admin-process-factor-xii)
14. [Tests](#14-tests)
15. [`Dockerfile` + `.dockerignore` (factor V)](#15-dockerfile--dockerignore-factor-v)

---

## 0. Project map

```
pyproject.toml / uv.lock   → what the app depends on (factor II)
app/config.py              → reads all settings from env vars (factor III)
app/db.py                  → database connection pool
app/models.py              → database tables
app/schemas.py             → shape of API requests/responses
app/routers/trains.py      → search endpoint
app/routers/bookings.py    → book / lookup / cancel (the core logic)
app/payment.py             → fake payment provider
app/metrics.py             → business metrics for Prometheus
app/log_config.py          → JSON logging (factor XI)
app/main.py                → middleware, health checks, /metrics
app/__main__.py            → starts the web server on $PORT (factor VII)
app/admin.py               → one-off setup/seed process (factor XII)
tests/                     → tests against a real Postgres
Dockerfile, .dockerignore  → the container image (factor V)
```

---

## 1. `pyproject.toml` + `uv.lock` — dependencies (factor II)

Factor II: an app **declares all its dependencies explicitly** and never relies on something already installed on the machine.

- **`pyproject.toml`** lists what the app needs:
  - `fastapi` — web framework
  - `uvicorn` — the web server that runs it
  - `sqlalchemy` + `psycopg` — talk to Postgres
  - `pydantic-settings` — read config from env vars
  - `structlog` — JSON logs
  - `prometheus-client`, `prometheus-fastapi-instrumentator` — metrics
- **`uv.lock`** records the *exact* version and checksum of every package, including indirect ones. Without it, building on Monday and Friday could install different versions. With it, every build is identical.
- **`exclude-newer = "2026-10-06"`** makes uv ignore releases newer than two weeks. Malicious versions of popular packages are usually caught and pulled within days, so the delay protects you.
- **`[dependency-groups] dev`** — `pytest`, `httpx`, `ruff` are for testing only. The Docker image skips them (`--no-dev`): smaller image, less to attack.
- **`ruff` rules** `E,F,I,B,UP,S`:
  - `E`, `F`, `I` — style, errors, import order
  - `B` — likely bugs
  - `UP` — modern syntax
  - `S` — security checks (Bandit). For example it flags `random` for anything security-sensitive; that's why PNRs use `secrets` (§6).

---

## 2. `app/config.py` — config from the environment (factor III)

```python
class Settings(BaseSettings):
    port: int = 8000
    db_host: str = "localhost"
    db_password: SecretStr = SecretStr("")
    db_role: str | None = None
    payment_api_key: SecretStr = SecretStr("")
```

Factor III: anything that changes between environments (hosts, passwords, ports) lives in **environment variables**, never in code. The same image runs in tests, in Docker and in kind; only env vars change.

- **`BaseSettings`** fills each field from the matching env var: `DB_HOST` → `db_host`, `PORT` → `port`. It converts types (`PORT="9000"` → `9000`), so a bad value fails at startup, not mid-request.
- **`SecretStr`** prints as `**********`. If the settings object is ever logged by mistake, the password doesn't reach Splunk. You must call `.get_secret_value()` deliberately.
- **Username and password are separate fields, not one `DATABASE_URL`.** Vault generates a username and password per pod. Locally (and in tests) they come from `DB_USER` and `DB_PASSWORD`. In the cluster Vault Agent renders them to `/vault/secrets/db.json` and `DB_CREDENTIALS_FILE` points there: `app/db.py` reads the file on every new connection, because Vault replaces the user while the pod runs. Host and port come from a plain ConfigMap. Secret and non-secret config stay apart.
- **`@lru_cache` on `get_settings()`** reads settings once. Env vars don't change while a process runs.

---

## 3. `app/db.py` — the connection pool and `SET ROLE`

```python
engine = create_engine(settings.database_url, pool_size=..., pool_pre_ping=True)
```

- **Engine and pool.** Opening a Postgres connection is slow (network round trips + authentication). The engine keeps a **pool** of open connections and lends one per request. `pool_size=5` caps it at 5 per pod.
- **`pool_pre_ping=True`** checks a connection with a tiny query before lending it. After a Postgres restart, pooled connections are dead; without this, the first request after the restart fails.

### Why the `SET ROLE` hook exists

```python
@event.listens_for(engine, "connect")
def _set_role(dbapi_conn, _record):
    cur.execute(f'SET ROLE "{role}"')
```

It prevents a problem you'd hit in block 4 (Vault):

1. Vault creates a temporary Postgres user, e.g. `v-railbook-abc123`, valid 24 h.
2. In Postgres, whoever creates a table owns it.
3. If that temporary user created the tables, Vault couldn't delete it later ("user owns objects"), and the next pod's new user couldn't access the tables.

Fix: a permanent, login-less group role `railbook_owner` owns the tables. Every Vault user is a member, and the app switches to it on every new connection. Users come and go; ownership never moves. If `DB_ROLE` is unset (tests), nothing happens.

- **`get_session()`** — a FastAPI dependency. Each request gets its own session, closed automatically afterwards even if the request fails. Otherwise connections leak until the pool runs out.
- **`ping()`** runs `SELECT 1`; used by the readiness check.

---

## 4. `app/models.py` — the tables

| Table | Why it exists |
|---|---|
| `stations` | Station codes (NDLS…). |
| `trains` | Number, route, timings. Direct trains only, to keep the domain small. |
| `schedules` | A train on a specific date. `UniqueConstraint(train_number, travel_date)` stops duplicates. |
| `inventory` | Seats per class per schedule: `total`, `available`, `fare`. **The row locked when booking.** |
| `bookings` | One row per PNR, status `CONFIRMED` / `CANCELLED`. |

- **Why a separate `available` counter** rather than counting bookings? Counting scans and locks many rows; one row per class is cheap to lock and update.
- **`CheckConstraint("available >= 0 AND available <= total")`** — even with a bug in Python, the database refuses to oversell. A cheap second line of defence.
- **`Numeric(10, 2)` for money, never `float`.** Floats can't store 0.1 exactly (`0.1 + 0.2 = 0.30000000000000004`). `Decimal` is exact, which is why the test can expect `"3500.00"`.

---

## 5. `app/schemas.py` — the API's contract

Pydantic classes define what clients send and receive. They're separate from the database models so tables can change without breaking clients, and vice versa.

- **`seats: int = Field(ge=1, le=6)`** — FastAPI rejects 0, negative or 50 seats with `422` before your code runs.
- **`simulate_payment_failure`** — triggers the failure path on demand, so you can see `402`s in Grafana and Splunk during the demo.

---

## 6. `app/routers/bookings.py` — the core logic

### The race condition locking prevents

Two people book the last seat at the same instant, without locking:

```
Request A: read available = 1   ✓ enough
Request B: read available = 1   ✓ enough
Request A: write available = 0, create booking
Request B: write available = 0, create booking   ← two bookings, one seat
```

`with_for_update()` generates `SELECT … FOR UPDATE`:

```python
inv = session.scalar(select(Inventory).where(...).with_for_update())
```

It **locks that inventory row** until the transaction ends. B's `SELECT` waits for A to commit, then sees `available = 0` and gets `409`. The concurrency test proves it: 20 requests for 5 seats → exactly 5 succeed, 15 get `409`.

### Order of steps

```python
with BOOKING_DURATION.time(), session.begin():
    1. find schedule            → 404 if none
    2. lock inventory row       → 404 if class doesn't exist
    3. check seats              → 409 if not enough
    4. charge payment           → 402 if it fails
    5. decrease available, insert booking
# leaving the block = COMMIT (or ROLLBACK if anything raised)
```

- **`session.begin()`** — one transaction: all or nothing. If payment fails at step 4, the exception rolls everything back and seats are untouched (`test_payment_failure_keeps_seats`).
- **Cheapest checks first**, so bad input fails before any lock is taken.
- **Trade-off:** payment runs while the row is locked. With a real provider taking 2 s, every other booking for that class would wait 2 s. Real systems reserve seats, pay outside the lock, then confirm. With an instant mock, the simple version is fine.

### Status codes

| Code | When | Why |
|---|---|---|
| `201 Created` | Booking made | A new resource was created. |
| `404 Not Found` | Unknown train/date/class/PNR | It doesn't exist. |
| `409 Conflict` | Sold out, already cancelled | Valid request, but the current state doesn't allow it. |
| `402 Payment Required` | Payment declined | Standard code for payment failure. |

Distinct codes matter for **monitoring**: Grafana can separate "sold out" (normal business) from `500` (bugs), and alerts fire only on bugs.

### Metrics, logs, PNR

- **`BOOKINGS.labels(status="sold_out").inc()`** — every outcome is counted, so Grafana can chart bookings per minute by outcome.
- **`bind_contextvars(pnr=…)`** — the PNR is attached to every later log line in that request, so Splunk can search by PNR.
- **`secrets.randbelow(10**10)`, not `random`.** `random` is predictable; from a few PNRs someone could guess others. `secrets` is cryptographically random.

### Cancellation

Locks the **booking** first (two simultaneous cancels can't both refund seats), then the inventory row; adds seats back, marks `CANCELLED`. A second cancel gets `409`.

---

## 7. `app/routers/trains.py` — search

One SQL query joins trains, schedules and inventory, then groups rows into one result per train.

- **`Query(alias="from")`** — the URL uses `?from=NDLS`, but `from` is a Python keyword, so the variable is `from_station`.
- **`.upper()`** — `ndls` and `NDLS` both work.

---

## 8. `app/payment.py` — mock payment provider

Refuses to work if `PAYMENT_API_KEY` is empty. That gives Vault a real job: the app needs a secret to function. If Vault injection breaks in block 4, bookings fail with `402 payment provider not configured`, so the problem is visible at once.

---

## 9. `app/metrics.py` — business metrics

```python
BOOKINGS = Counter("railbook_bookings_total", "...", ["status"])
BOOKING_DURATION = Histogram("railbook_booking_duration_seconds", ..., buckets=(...))
```

- **Counter vs Histogram.**
  - **Counter** only goes up (bookings made). Prometheus turns it into a rate (bookings/second).
  - **Histogram** sorts durations into buckets (<10 ms, <25 ms…). Prometheus computes **p95 latency** from it: 95% of bookings were faster than this.
- **Labels must have few values.** `status` has 4. Each unique label combination is a separate series in Prometheus memory. Labelling by PNR would add a series per booking and memory would grow without limit (**high cardinality**). PNRs belong in **logs**, not metrics — that's the split between Prometheus and Splunk.
- **`railbook_` prefix** groups your metrics and avoids clashes with Kong and Kubernetes metrics.

---

## 10. `app/log_config.py` — JSON logs to stdout (factor XI)

Factor XI: the app **doesn't** manage log files or ship logs. It writes events to stdout; the platform routes them. In kind, the Splunk OTel Collector reads container stdout and sends it to Splunk. The app has no Splunk code, so changing log tools needs no code change.

- **Why JSON?** Splunk extracts fields (`status=500`, `pnr=…`) from JSON automatically; plain text needs parsing rules.
- **`merge_contextvars`** adds request-bound values (`request_id`, `pnr`) to every line automatically.
- **`ProcessorFormatter`** — uvicorn and SQLAlchemy use Python's standard `logging`. This bridge makes *their* messages JSON too, instead of a mix of JSON and plain text.
- **`uvicorn.access` disabled** — our middleware writes a better access line (request ID, route template). Keeping both would log every request twice and eat into Splunk's 500 MB/day.

---

## 11. `app/main.py` — wiring it together

### Lifespan: startup and shutdown

```python
async def lifespan(_app):
    log.info("startup")
    yield                       # app serves requests here
    get_engine().dispose()      # on shutdown: close all DB connections
```

When Kubernetes stops a pod (deploy, scale-down, node drain) it sends **SIGTERM**. Uvicorn stops accepting new requests, finishes in-flight ones, then runs the code after `yield` to close the DB pool. This is **factor IX, disposability**: pods can be killed any time without dropping requests or leaving half-open DB connections.

### `/metrics` (Instrumentator)

Adds `http_requests_total` and `http_request_duration_seconds` per route, method and status. `/metrics`, `/healthz`, `/readyz` are excluded — they're called every few seconds by Prometheus and Kubernetes and would swamp real traffic.

### Request middleware

```python
request_id = request.headers.get("X-Request-ID") or str(uuid.uuid4())
```

- **Reuses Kong's ID.** Kong's correlation-id plugin (block 2) sets `X-Request-ID` and logs it. The API reuses it, so one Splunk search returns the Kong line *and* the API lines — end-to-end tracing. Without Kong (tests), the API generates one.
- **Echoes it in the response**, so a user reporting a problem can quote it.
- **Logs `route.path` (`/bookings/{pnr}`), not the real URL** (`/bookings/9534117849`), so Splunk can group by endpoint. The PNR is still in its own field.
- **`try/finally`** — the access line is written even if the request crashes.

### `/healthz` vs `/readyz`

| Probe | Question | If it fails, Kubernetes… | Checks |
|---|---|---|---|
| **Liveness** `/healthz` | Is the process stuck? | **Restarts** the pod | Nothing beyond answering |
| **Readiness** `/readyz` | Can it serve traffic now? | **Stops sending traffic**, no restart | Database reachable |

Liveness must **not** check the database. If Postgres is down for a minute, a DB-checking liveness probe would restart *every* API pod in a loop — it doesn't fix Postgres and slows recovery. Readiness takes pods out of rotation until the DB is back.

---

## 12. `app/__main__.py` — starting the server (factors VII, VIII)

```python
uvicorn.run("app.main:app", host="0.0.0.0", port=settings.port, workers=1,
            log_config=None, timeout_graceful_shutdown=...)
```

- **Factor VII, port binding:** the app *is* the web server and listens on `$PORT`. No Apache or Nginx needed around it. Verified: `PORT=9000` → listens on 9000.
- **`host="0.0.0.0"`** — inside a container `127.0.0.1` only accepts connections from that container, so Kong and Prometheus couldn't reach it. `0.0.0.0` means all interfaces.
- **`workers=1`** — factor VIII: scale with **more pods**, not more workers in one pod. Also keeps Prometheus simple: multiple workers each have separate counters and need a special multi-process mode.
- **`log_config=None`** — stops uvicorn replacing our JSON logging.
- **`timeout_graceful_shutdown=20`** — after SIGTERM, wait up to 20 s for in-flight requests. Kubernetes force-kills after 30 s by default, so 20 fits.
- **Why `python -m app` instead of the `uvicorn` command?** The port comes from the validated settings, same as all other config.

---

## 13. `app/admin.py` — one-off admin process (factor XII)

Factor XII: admin tasks (create tables, seed data) run as **one-off processes from the same code and config** as the app — not manual SQL typed into a database. In kind it runs as a Kubernetes **Job** with the same image.

- **`create_all`** creates missing tables, skips existing ones.
- **`on_conflict_do_nothing()` makes it idempotent** — safe to run repeatedly. The pipeline runs it on *every* deploy; without this the second run would crash on duplicate keys.
- **The schedule insert trick:**
  ```python
  .on_conflict_do_update(index_elements=[...], set_={"train_number": train_number}).returning(Schedule.id)
  ```
  Inventory rows need the schedule's `id`. With `DO NOTHING`, Postgres returns *no row* if the schedule exists. A no-op update makes `RETURNING id` work for new and existing rows.
- **Starts from yesterday** — containers run in UTC. In your evening it's already tomorrow in UTC, so the seed must also cover your local "today". (Bug found while testing the container.)

---

## 14. Tests

- **Real Postgres, not SQLite.** Row locking (`FOR UPDATE`) and `ON CONFLICT` are Postgres features; SQLite would ignore or reject them and tests would pass while hiding bugs. This is **factor X, dev/prod parity**.
- **Env vars set before importing the app.** Settings are cached on first read, so test defaults must exist before `app.main` is imported. `setdefault` lets CI override them.
- **`fresh_db` (`autouse=True`)** — every test starts from a reset, seeded database; tests can't affect each other.

| Test | Proves |
|---|---|
| `health_and_readiness` | The probes Kubernetes will call work. |
| `search_returns_seeded_train` | Search works, case-insensitive. |
| `book_lookup_and_cancel` | Full lifecycle; seats go down then back; double cancel → `409`. |
| `unknown_train_and_pnr` | `404` paths. |
| `sold_out` | `409` when seats run out. |
| `payment_failure_keeps_seats` | Transaction rolls back on payment failure. |
| `concurrent_bookings_never_oversell` | Row locking works under contention. |
| `request_id_is_echoed` | Kong's request ID is reused, or one is generated. |
| `metrics_exposed` | Prometheus will find the custom metrics. |

Run them:

```bash
docker run -d --name railbook-test-pg -e POSTGRES_USER=railbook -e POSTGRES_PASSWORD=railbook \
  -e POSTGRES_DB=railbook -p 127.0.0.1:5433:5432 postgres:16-alpine
uv run pytest -q
```

---

## 15. `Dockerfile` + `.dockerignore` (factor V)

```dockerfile
FROM python:3.12-slim AS build          # stage 1: install dependencies
COPY --from=ghcr.io/astral-sh/uv:0.13.4 /uv /bin/uv
RUN uv sync --frozen --no-dev

FROM python:3.12-slim                   # stage 2: what actually ships
RUN useradd --uid 10001 ... app
COPY --from=build /app/.venv /app/.venv
COPY app ./app
ENV ... PYTHONUNBUFFERED=1 PORT=8000
USER 10001
CMD ["python", "-m", "app"]
```

- **Multi-stage build.** Stage 1 has uv and build caches; stage 2 copies only the finished virtualenv and code. Smaller image, fewer tools for an attacker.
- **uv version pinned** (`uv:0.13.4`) — repeatable builds.
- **`uv sync --frozen`** — install *exactly* what `uv.lock` says; fail if the lock is stale. Factor II enforced at build time.
- **Lockfile copied before the code.** Docker caches each step. Dependencies change rarely, code often, so a code-only change reuses the cached dependency layer — rebuilds take seconds.
- **Non-root (`USER 10001`).** An attacker who exploits the app is an ordinary user, not root. Kubernetes can enforce "must not run as root", which needs a numeric UID.
- **`PYTHONUNBUFFERED=1`** — Python normally buffers output. Without this, logs reach Splunk late or are lost when the container is killed.
- **Exec-form `CMD ["python", "-m", "app"]`, not shell form** (`CMD python -m app`). Exec form makes Python **process 1**, so it receives SIGTERM. Shell form makes `/bin/sh` process 1, which doesn't pass SIGTERM on — no graceful shutdown, force-kill after 30 s.
- **`.dockerignore` as an allow-list.** It ignores everything (`*`) and adds back only `app/`, `pyproject.toml`, `uv.lock`. `.venv`, `.git`, tests or a stray `.env` can never get into the image. Safe by default, unlike a list of exclusions.

---

## What was verified by running it

| Factor | Evidence |
|---|---|
| II | `uv.lock` pins everything; image built with `--frozen`. |
| III | All config from env vars; tests and container use different values with the same code. |
| VII | `PORT=9000` → listening on 9000. |
| IX | `docker stop` → clean shutdown in ~100 ms. |
| XI | Every line JSON on stdout; `request_id` and `pnr` on booking lines. |
| XII | `python -m app.admin setup` ran twice: no errors, no duplicates. |
