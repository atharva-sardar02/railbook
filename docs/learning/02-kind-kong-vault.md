# Block 2 — kind, Kong and Vault, explained

What runs where, why each file exists, and what went wrong along the way (the mistakes are part of the lesson).

## Contents

1. [The big picture](#1-the-big-picture)
2. [kind — Kubernetes inside Docker](#2-kind--kubernetes-inside-docker)
3. [Kong — Ingress controller and gateway](#3-kong--ingress-controller-and-gateway)
4. [Vault — concepts](#4-vault--concepts)
5. [Vault — the files](#5-vault--the-files)
6. [The Postgres chart](#6-the-postgres-chart)
7. [The app chart](#7-the-app-chart)
8. [`scripts/bootstrap.sh` — bring-up order](#8-scriptsbootstrapsh--bring-up-order)
9. [Problems hit and what they teach](#9-problems-hit-and-what-they-teach)
10. [Commands to explore it yourself](#10-commands-to-explore-it-yourself)

---

## 1. The big picture

```
Windows browser
   │ http://127.0.0.1:80
   ▼
WSL2 → Docker Desktop → kind node container "railbook-control-plane"
   │ NodePort 30080
   ▼
┌──────────────────────── kind cluster ───────────────────────────┐
│ ns kong:     kong-controller (brain) ──config──▶ kong-gateway   │
│                                                   │ (muscle)    │
│ ns railbook:  ┌──────────────────────────────────┘              │
│               ▼                                                 │
│   railbook-api pod ×2  [vault-agent-init] [api] [vault-agent]   │
│        │ SQL as v-kubernet-…                                    │
│        ▼                                                        │
│   railbook-postgres-0  [vault-agent-init] [postgres] + PVC      │
│                                                                 │
│ ns vault:  vault-0 (Raft data on PVC)   vault-agent-injector    │
└─────────────────────────────────────────────────────────────────┘
```

Files added in this block:

```
deploy/kind/kind-config.yaml        cluster definition (1 node, port 80 mapping)
deploy/kong/values.yaml             Kong settings
deploy/vault/values.yaml            Vault settings
deploy/charts/postgres/             Postgres chart (own lifecycle)
deploy/charts/railbook/             app chart: API, Service, Ingresses, KongPlugins, setup Job
scripts/vault-unseal.sh             init (first time) + unseal (every restart)
scripts/vault-setup.sh              Vault configuration as code
scripts/bootstrap.sh                everything above, in order, rerunnable
```

---

## 2. kind — Kubernetes inside Docker

A Kubernetes **node** is a machine running `kubelet` (the agent that starts pods) and a container runtime. **kind** fakes the machine: each node is a Docker container, with the Kubernetes components and its own `containerd` inside. Your pods are containers *inside* that container.

### `deploy/kind/kind-config.yaml`

```yaml
nodes:
  - role: control-plane
    extraPortMappings:
      - containerPort: 30080   # port on the node
        hostPort: 80           # port on your machine
        listenAddress: "127.0.0.1"
```

- **One node.** Each extra node is another ~1 GB container and teaches nothing here.
- **`extraPortMappings`** works like `docker run -p`. Docker publishes ports only **when a container is created**, so this must exist before `kind create cluster`. Changing it later means recreating the cluster.
- **`127.0.0.1`**, not `0.0.0.0`: the API is reachable from your machine only, not from your Wi-Fi.
- **Why 30080?** Kubernetes `NodePort` Services must use ports 30000–32767. Kong's proxy is a NodePort on 30080; kind maps your port 80 to it.

What you get for free: a default **StorageClass** (`standard`, local-path provisioner). When a pod asks for a disk (a PersistentVolumeClaim), it creates a folder inside the node container. That's what Postgres and Vault use.

**Images:** kind's node has its own image store, separate from your Docker. `kind load docker-image railbook:dev` copies a local image in. Block 3 replaces this with pulls from GHCR.

---

## 3. Kong — Ingress controller and gateway

### Service vs Ingress vs Ingress controller

| Object | What it is |
|---|---|
| **Service** | A stable name and virtual IP for a set of pods — *inside* the cluster only. |
| **Ingress** | A rule: "requests for path X go to Service Y". Just data; does nothing alone. |
| **Ingress controller** | The program that reads Ingress rules and actually routes traffic. Not built into Kubernetes — you install one (nginx, Traefik, **Kong**…). |

### Kong's two parts

```
kong-controller  ── watches Ingress, Service, EndpointSlice, KongPlugin ──┐
                                                                         │ Admin API (8444)
browser → :80 → NodePort 30080 → kong-gateway (nginx + Lua) ◀────────────┘
                                     │ to pod IPs directly
                                     ▼
                                API pod 1, API pod 2
```

- **DB-less mode** (`database: "off"`): Kong's config lives in memory, rebuilt by the controller from Kubernetes objects. There is nothing to edit by hand — **Git is the source of truth**.
- **Pod IPs, not the Service IP.** The controller follows EndpointSlices; when a pod's readiness probe fails, it disappears from Kong within seconds. This is how `/readyz` (block 1) protects users.

### `deploy/kong/values.yaml`, line by line

| Setting | Why |
|---|---|
| `database: "off"` | DB-less mode (above). |
| `nginx_worker_processes: "1"` | Default is one worker per CPU (12 here) = hundreds of MB for one user. |
| `proxy_access_log: "off"` | Kong's default log is plain nginx text without our request ID. Replaced by the `file-log` plugin (JSON). |
| `admin_access_log: "off"` | The controller calls the Admin API on every sync — noise in Splunk. |
| `proxy.type: NodePort`, `nodePort: 30080` | Matches the kind port mapping. |
| `tls.enabled: false` | Plain HTTP on localhost; TLS out of scope. |
| `manager.enabled: false` | Kong Manager UI out of scope (and read-only in DB-less mode). |
| `resources` | Memory limits to stay inside the 8 GB budget. |

### `templates/kong-plugins.yaml` — the four plugins

| KongPlugin | Config | Why |
|---|---|---|
| `correlation-id` | `header_name: X-Request-ID`, `generator: uuid`, `echo_downstream: true` | Adds an ID if the client didn't send one; the API reuses it, so Kong and API log lines share it. |
| `prometheus` | status code + latency + upstream health metrics | Gateway-side metrics for block 4. 429s never reach the API — only Kong sees them. |
| `file-log` | `path: /dev/stdout` | One JSON line per request, including request headers (so `x-request-id`). |
| `rate-limiting` | `minute: 20`, `limit_by: ip`, `policy: local` | `local` = counters in Kong's memory, no Redis; exact with one gateway pod. |

### `templates/ingress.yaml` — why two Ingresses

A plugin applies to a whole **route**, and an Ingress can't say "POST only". So:

| Ingress | Matches | Plugins |
|---|---|---|
| `railbook` | `/trains`, `/bookings…`, `/docs` (prefix), `/openapi.json` — any method | correlation-id, prometheus, file-log |
| `railbook-create-booking` | exactly `/bookings`, `konghq.com/methods: POST` | the same + **rate-limiting** |

Kong picks the **most specific** route: `POST /bookings` matches the second (path + method), everything else the first.

**Not routed on purpose:** `/metrics`, `/healthz`, `/readyz`. They're for Prometheus and Kubernetes inside the cluster. Verified: `curl 127.0.0.1/metrics` → 404.

### Verified

- `GET /trains` via Kong → 200, `x-request-id` in the response, `Via: 1.1 kong/3.9.4`.
- 25 bookings in a row → 20 × `201`, then 5 × `429`.
- Kong's log line is JSON with `request.headers.x-request-id`.

---

## 4. Vault — concepts

### Why not Kubernetes Secrets?

A Kubernetes Secret is only **base64-encoded**, not encrypted: anyone who can read Secrets in the namespace (or etcd backups) has the password. It's also static — the same password forever, shared by every pod. Vault gives encryption at rest, per-workload access rules, an audit trail, and **dynamic** credentials that expire.

### Sealed and unsealed

Vault encrypts everything it stores. The decryption key is itself encrypted by a **master key** that is never stored whole.

- `vault operator init` creates the master key and splits it into **5 key shares** with Shamir's Secret Sharing; **any 3** rebuild it.
- After every start Vault is **sealed**: running, but unable to read its data. Entering 3 shares **unseals** it.
- In a company, 5 people hold one share each, so no single person can open Vault alone.

### Secret zero

Something must open the safe, and it can't be inside the safe. Here that's `~/.railbook/vault-init.json` (5 shares + root token): `chmod 600`, outside the repo. Lose it → Vault's data is unrecoverable. Leak it → someone can open Vault.

### Storage: Raft on a PVC, not dev mode

**Dev mode** keeps everything in memory, starts unsealed, uses a root token. One restart wipes it all — including the Postgres admin password, while Postgres keeps the old one on its disk. Nobody could log in until you wiped Postgres too. **Raft** is Vault's built-in storage engine; on a PVC it survives restarts. The price is init + unseal.

### Kubernetes auth — how a pod proves who it is

```
pod (service account railbook-api)
  │ 1. sends its service account token (a signed JWT)
  ▼
Vault ── 2. "is this token real? which account?" ──▶ Kubernetes API (TokenReview)
  │ 3. account railbook-api in namespace railbook → Vault role railbook-api
  ▼
policy railbook-api: may read the payment key + DB creds. Nothing else.
```

No password needed to get the first password — the pod's identity *is* the credential.

**Step by step:**

- **Before anything happens:** Kubernetes gives every pod an identity, its **service account** (here `railbook-api`). It also mounts a **service account token** inside the pod. The token is a JWT saying "I am `railbook-api` in namespace `railbook`". Kubernetes signs it, so nobody can forge it, and it expires after a short time.
1. **The pod sends its token to Vault.** Vault Agent in the pod is saying "here's my ID card, log me in".
2. **Vault asks Kubernetes whether the token is real.** Vault doesn't trust the token blindly. It sends it to the Kubernetes API's **TokenReview** endpoint, a "verify this ID" service. Kubernetes checks the signature and expiry and answers either "valid: service account `railbook-api`, namespace `railbook`" or "invalid".
3. **Vault matches the identity to a role.** `scripts/vault-setup.sh` created the Vault **role** `railbook-api`: "accept **only** service account `railbook-api` from namespace `railbook`". A pod with any other service account or namespace is refused, even if its token is valid.
4. **The role decides the policy.** The role is linked to the **policy** `railbook-api`, the list of paths it may use: read the payment API key (KV) and request database credentials. Vault returns a short-lived **Vault token** limited to that policy. Anything else (the Grafana password, the Splunk HEC token) gets "permission denied".

Vault Agent then uses that Vault token to fetch the secrets and writes them as files in `/vault/secrets/`, where the app reads them (next section).

**Analogy — the badge office at a building:**

| Vault concept | Badge office |
|---|---|
| Service account token | Your employee ID card, issued by HR (Kubernetes) |
| TokenReview | Security phones HR: "is this card real?" |
| Role | "Staff of the Bookings department, in the Railbook office, may enter" |
| Policy | The badge you're given: it opens only the payment-key room and the database room |

**Why it's designed this way:**

- **Pods need no secret of their own.** Their identity comes from the platform they run on, so no password is baked into an image, an env var or git. (The only "secret zero" is the unseal keys above, held by a human.)
- **Least privilege.** Each pod gets only what its policy allows. If the API is hacked, the attacker still can't read Grafana's or Splunk's secrets.
- **Short-lived.** Both the Kubernetes token and the Vault token expire, so a stolen one stops working soon.

### The Agent Injector

You don't write Vault code in the app. You add **annotations** to the pod; a Kubernetes **mutating webhook** rewrites the pod as it's created:

1. **`vault-agent-init`** (init container): logs in, renders the templates to `/vault/secrets/app.env` and `/vault/secrets/db.json`, exits. The app container starts only after this.
2. **`vault-agent`** (sidecar): keeps the login and the DB credential's lease renewed while the pod lives, and re-renders `db.json` when Vault issues a new user at `max_ttl`.

With `agent-cache-enable` the init container saves its Vault token and leases to a shared in-memory cache, and the sidecar starts from it. Without that (until 2026-10-29) the sidecar asked for a **second** DB user and renewed only that one, while the app used the first, which Vault dropped after 1 h (STATUS: bugs after block 6).

### Dynamic database credentials

Vault holds the Postgres admin login. When a pod reads `database/creds/railbook-api`, Vault runs:

```sql
CREATE ROLE "v-kubernet-railbook-3AkA…" WITH LOGIN PASSWORD '…' VALID UNTIL '…' IN ROLE railbook_owner;
```

and returns that username/password, with a **lease** (1 h). The sidecar renews it up to `max_ttl` (24 h). When a lease expires or is revoked, Vault runs the revocation SQL (kill its connections, `DROP ROLE`). A leaked password works for one pod, for a limited time.

---

## 5. Vault — the files

### `deploy/vault/values.yaml`

| Setting | Why |
|---|---|
| `ha.enabled: true`, `replicas: 1`, `raft.enabled: true` | Raft storage, single node. "HA mode" is just how the chart enables Raft; one replica isn't actually HA. |
| `tls_disable = 1` | HTTP inside the cluster only. |
| `unauthenticated_metrics_access`, `telemetry {}` | Lets Prometheus scrape Vault's metrics in block 4 without a token. |
| `disable_mlock = true` | `mlock` keeps memory from being swapped to disk; Raft uses memory-mapped files, which don't work with it. HashiCorp recommends disabling it with Raft. |
| `service_registration "kubernetes"` | Vault labels its own pod with its state (sealed/active) — visible in `kubectl get pod --show-labels`. |
| `ui.enabled: true` | Web UI on a ClusterIP Service; reach it with `kubectl port-forward`, never public. |
| `injector.agentDefaults` | The chart's defaults (250m CPU, 64–128 Mi per sidecar) are production-sized. |

### `scripts/vault-unseal.sh`

1. If `initialized` is false → `vault operator init -key-shares=5 -key-threshold=3`, save JSON to `~/.railbook/vault-init.json` with `umask 077` (file created as `-rw-------`).
2. If `sealed` is true → send 3 shares.
3. Keys go to the pod on **stdin** (`read -r key`), never as command-line arguments, which appear in process lists.

### `scripts/vault-setup.sh`

Vault configuration **as code**: repeatable after a rebuild, reviewable in Git, safe to rerun.

**`base`** (before Postgres exists):

| Step | Why |
|---|---|
| Enable **KV v2** at `secret/` | Static secrets, versioned (every write keeps the old version). |
| Store `postgres-admin` and `payment` **only if missing** | Reruns never rotate passwords. Verified: still version 1 after 3 runs. |
| Enable **Kubernetes auth**, point it at the API server | So Vault can verify service account tokens. |
| Policies `railbook-api`, `railbook-postgres` | Least privilege: exact paths, `read` only. |
| Roles bound to **service account + namespace + audience** | A pod with any other account, namespace, or token audience gets nothing. |

**`database`** (after Postgres is running):

| Step | Why |
|---|---|
| Enable the **database** engine, `database/config/railbook` | Vault's admin connection to Postgres. The admin password is read from KV *inside* Vault; it never leaves. |
| `database/roles/railbook-api` | The SQL to create and drop users; `default_ttl=1h`, `max_ttl=24h`. |
| `creation_statements … IN ROLE railbook_owner` | Every Vault user can `SET ROLE railbook_owner` (block 1's design), so tables never belong to an expiring user. |
| `revocation_statements` | Terminates the user's connections, then `DROP ROLE`. |

Verified: a test credential appeared as a Postgres role (`in_owner = t`), and revoking its lease dropped it (0 left).

---

## 6. The Postgres chart

### Why its own chart

Order is forced: Postgres must run **before** Vault's database engine can point at it, and that must exist **before** API pods can get credentials. In one chart the API pods would start too early and hang. It's also correct by design: the database is a **backing service (factor IV)** with its own lifecycle — every code push redeploys the app, never the database.

### `deploy/charts/postgres/templates/postgres.yaml`

| Piece | Why |
|---|---|
| `ServiceAccount railbook-postgres` | The identity Vault's `railbook-postgres` role accepts. |
| `ConfigMap` with `01-roles.sql` | Mounted at `/docker-entrypoint-initdb.d`; the image runs it **only on first start** (empty data dir). Creates `railbook_owner`. |
| Headless `Service` (`clusterIP: None`) | DNS `railbook-postgres` points straight at the pod; no load balancing needed for one database. |
| `StatefulSet` + `volumeClaimTemplates` | Stable name `railbook-postgres-0` and its own disk that survives restarts. A Deployment would lose the data. |
| Vault annotations, `agent-pre-populate-only` | Init container only: the admin password is needed once at first start, so no sidecar. |
| `command: sh -c '. /vault/secrets/postgres.env && exec docker-entrypoint.sh postgres'` | Load the password, then hand over to the image's normal entrypoint. `exec` keeps Postgres as PID 1 so it receives SIGTERM. |
| `PGDATA=…/pgdata` | The volume root has a `lost+found` folder, which `initdb` refuses to use. |
| `readinessProbe: pg_isready` | Not Ready until it accepts connections. |

`initdb: warning: enabling "trust" authentication for local connections` in the logs is the official image's default for connections from **inside** the container only; network connections still need a password.

---

## 7. The app chart

### Helm in one paragraph

Kubernetes objects are YAML. Helm is a **template engine + package manager**: templates (`{{ .Values.image.tag }}`) are filled from `values.yaml` and `--set`. Every install/upgrade is a numbered **revision** — that's what `helm rollback` returns to. Release = build (image) + config (values): factor V.

### `templates/_helpers.tpl`

Reusable snippets, so the API Deployment and the setup Job can't drift apart:

| Helper | What it renders |
|---|---|
| `railbook.labels` | Common labels → `kubectl get all -l app.kubernetes.io/instance=railbook`. |
| `railbook.vaultAnnotations` | The injector annotations; `prePopulateOnly` switches the sidecar off for Jobs. |
| `railbook.apiEnv` | `envFrom` the ConfigMap only — no Secrets. |
| `railbook.command` | `sh -c '. /vault/secrets/app.env && exec <cmd>'`. |
| `railbook.apiSecurityContext` | Non-root, read-only filesystem, no privilege escalation, all Linux capabilities dropped. |

**The `{{\` \`}}` wrapping:** Vault Agent templates use the same `{{ }}` syntax as Helm. Wrapping them in a raw string (`` {{` ... `}} ``) tells Helm "print this literally", so Vault Agent sees it.

The rendered files:

```sh
# /vault/secrets/app.env — static secret, loaded into the env once at start
export PAYMENT_API_KEY="pay_test_…"
```

```json
// /vault/secrets/db.json — dynamic DB user; app/db.py reads it on every new connection
{"password":"…","username":"v-kubernet-railbook-…"}
```

### `templates/configmap.yaml`

Non-secret config only: `PORT`, `LOG_LEVEL`, `DB_HOST`, `DB_PORT`, `DB_NAME`, `DB_ROLE`, `DB_CREDENTIALS_FILE` (the path, not the secret). Factor III, with the secret half delivered by Vault.

### `templates/api.yaml`

| Piece | Why |
|---|---|
| `ServiceAccount railbook-api` | The identity Vault checks. |
| `replicas: 2` | Factor VIII; Kong load-balances them. |
| `RollingUpdate maxSurge: 1, maxUnavailable: 0` | Start a new pod, wait until Ready, then stop an old one — capacity never drops during a deploy. |
| `checksum/config` annotation | Env vars are read only at startup. A ConfigMap change alters this hash → the pod template changes → Kubernetes rolls new pods. Without it, config changes would silently not apply. |
| Vault annotations (sidecar on) | Init container renders secrets; sidecar renews the DB lease. |
| `livenessProbe /healthz`, `readinessProbe /readyz` | Restart if stuck; stop traffic if the DB is unreachable (block 1 §11). |
| `preStop: sleep 5` | On shutdown, SIGTERM and "remove from Kong" happen at the same moment. The pause lets Kong stop routing here before the app stops accepting. |
| `terminationGracePeriodSeconds: 30` | Must exceed 5 s (preStop) + 20 s (app's graceful shutdown), or the pod is force-killed mid-drain. |
| Native `sleep` action | Kubernetes runs the pause itself; the image needs no `sleep` binary. |
| `Service railbook-api` (port 80 → `http`) | Stable name for Kong; refers to the container port by **name**, so changing `PORT` doesn't break it. |

### `templates/setup-job.yaml`

| Piece | Why |
|---|---|
| Helm hook `post-install,post-upgrade` | Runs `python -m app.admin setup` after every deploy (factor XII). |
| `hook-delete-policy: before-hook-creation` | Jobs can't be edited once created; the old one is deleted just before the new run. The finished Job stays for `kubectl logs job/railbook-setup`. |
| `agent-pre-populate-only` | **A Job completes only when all containers exit; the sidecar never exits.** With a sidecar this Job would run forever. |
| Same service account, env, image | Same config as the API — the definition of factor XII. |

---

## 8. `scripts/bootstrap.sh` — bring-up order

```
kind cluster → Kong → Vault → unseal → vault-setup base → Postgres → vault-setup database → app
```

Each arrow is a real dependency:

- Postgres needs Vault unsealed and the KV password + `railbook-postgres` role.
- Vault's database engine needs Postgres accepting connections.
- API pods need the database engine to issue credentials.

Every step is idempotent (`helm upgrade --install`, "only if missing" checks), so the same script builds from scratch, repairs a broken setup, or does nothing. Verified: two runs in a row, no pod restarted.

**After a WSL/Docker restart:** Vault comes back sealed. Run `scripts/vault-unseal.sh` (or `bootstrap.sh`). Pods that start meanwhile wait in `Init` until Vault is unsealed — they never start without secrets.

---

## 9. Problems hit and what they teach

| Problem | Cause | Fix | Lesson |
|---|---|---|---|
| `vault operator unseal -` rejected the key | Key piped through `kubectl exec` with a trailing newline | Shell in the pod reads it: `read -r key && vault operator unseal "$key"` | Stdin plumbing through `kubectl exec` is fiddly; still keep secrets off the command line. |
| Vault warned "no audience configured" | Roles accepted tokens for any audience | `audience=https://kubernetes.default.svc.cluster.local` (decoded from a real token) | Check the `aud` claim so tokens for other services can't be replayed. |
| `export … export …` on one line (caught before deploy) | `{{-` trims the newline *before* it | Move trim markers: `{{ with … -}}` / `{{ end -}}` | Go template whitespace control is easy to get wrong; always look at the rendered file. |
| Helm upgrade of Vault failed: conflict on `caBundle` | Helm 4 uses server-side apply; the injector writes its own CA into the webhook at runtime → two owners of one field | `--server-side=false` for the Vault release | Server-side apply tracks field ownership; controllers that patch their own config conflict with it. `--force-conflicts` would have blanked the CA and broken injection. |
| First version used Kubernetes Secrets | A temporary step before Vault | Replaced by Vault (your rule: all secrets from Vault) | "Temporary" secrets tend to stay; going straight to Vault avoided that. |

---

## 10. Commands to explore it yourself

```bash
# What did the injector add to an API pod?
kubectl get pod -n railbook -l app.kubernetes.io/name=railbook-api \
  -o jsonpath='{.items[0].spec.initContainers[*].name} | {.items[0].spec.containers[*].name}{"\n"}'

# Which Vault-made users are connected to Postgres right now?
kubectl exec -n railbook railbook-postgres-0 -c postgres -- \
  psql -U postgres -d railbook -c "SELECT usename, count(*) FROM pg_stat_activity WHERE usename LIKE 'v-%' GROUP BY 1"

# Kill an API pod: a new one gets a NEW database user; the old one's lease expires within an hour
kubectl delete pod -n railbook -l app.kubernetes.io/name=railbook-api --wait=false

# Vault status / UI
kubectl exec -n vault vault-0 -- vault status
kubectl port-forward -n vault svc/vault-ui 8200:8200   # then http://localhost:8200, log in with the root token

# Kong's JSON access log
kubectl logs -n kong deploy/kong-gateway -c proxy --tail=5

# Helm releases and their revisions
helm list -A
helm history railbook -n railbook
```
