# Block 7 — Login with Keycloak (OIDC), explained

How a user logs in, how the API knows who is calling without asking anyone, who may see which
booking, and how the database learned about owners without losing a row. Built 2026-10-30/31.
(New to the terms? Start with [00-glossary](00-glossary.md#login-block-7).)

## Contents

1. [Login in one page](#1-login-in-one-page)
2. [What got installed, and where](#2-what-got-installed-and-where)
3. [Decisions and why](#3-decisions-and-why)
4. [Vault: Keycloak's secrets](#4-vault-keycloaks-secrets)
5. [Keycloak's own Postgres: one chart, two releases](#5-keycloaks-own-postgres-one-chart-two-releases)
6. [Keycloak: `deploy/charts/keycloak`, piece by piece](#6-keycloak-deploychartskeycloak-piece-by-piece)
7. [The realm as code](#7-the-realm-as-code)
8. [Alembic: giving bookings an owner](#8-alembic-giving-bookings-an-owner)
9. [The API checks the token: `app/auth.py`](#9-the-api-checks-the-token-appauthpy)
10. [Who may do what: authorization](#10-who-may-do-what-authorization)
11. [The browser login: `app/static/auth.js`](#11-the-browser-login-appstaticauthjs)
12. [Tests](#12-tests)
13. [How it was verified](#13-how-it-was-verified)
14. [Problems hit and what they teach](#14-problems-hit-and-what-they-teach)
15. [Not done](#15-not-done)
16. [Day-to-day use and exercises](#16-day-to-day-use-and-exercises)

---

## 1. Login in one page

Two questions, two status codes:

| Question | Name | Answer "no" |
|---|---|---|
| **Who are you?** | Authentication | **401** Unauthorized (no token, or a bad one) |
| **May you do this?** | Authorization | **403** Forbidden (valid token, but not your booking) |

**Keycloak** is the identity provider. It owns usernames and passwords, shows the login page, and
issues **tokens**. RailBook never sees a password.

```
Browser (http://localhost)           Keycloak (http://auth.localhost)              RailBook API
   │ 1. "Log in": random verifier,                                                    │
   │    send only its SHA-256 hash ───> 2. login page; user types password            │
   │                                    3. redirect back: /?code=…&state=…            │
   │ 4. code + verifier ─────────────> 5. checks hash(verifier) = challenge,          │
   │ <──────────────────────────────── access token (JWT, 5 min) + refresh token      │
   │ 6. GET /bookings   Authorization: Bearer <JWT> ─────────────────────────────────>│
   │                                                    7. check signature with the   │
   │                                                       cached PUBLIC key, plus    │
   │                                                       iss / aud / exp            │
   │ <──────────────────────────────────────────────── 8. only this user's bookings   │
```

**A JWT** is three base64url parts: `header.payload.signature`. The payload is **readable by
anyone** (paste one into jwt.io). It is **signed**, not encrypted: the signature proves who
issued it and that nobody changed it. So a token never holds secrets.

alice's access token payload (claims as Keycloak issued them; the two times are examples):

```json
{ "iss": "http://auth.localhost/realms/railbook",   // who issued it
  "aud": "railbook-api",                              // who it's for
  "azp": "railbook-web",                              // which app asked for it
  "sub": "7229e2e1-c2ba-4dba-8921-07875db98cac",      // the user's ID: becomes bookings.user_id
  "preferred_username": "alice",
  "realm_access": { "roles": [] },                    // railadmin: ["railbook-admin"]
  "iat": 1793403600, "exp": 1793403900 }              // issued at, expires 300 s later
```

**How the API knows a token is genuine without asking Keycloak:** Keycloak signs with its
**private key**, which never leaves Keycloak. It publishes the matching **public keys** at a URL
(the **JWKS**). The API downloads them once, caches them, and checks every signature locally.
Only the private key can make a signature that the public key accepts, and changing one
character of the payload breaks it.

## 2. What got installed, and where

| What | Where | Port(s) | Reachable from the laptop |
|---|---|---|---|
| Keycloak 26.8.0 | namespace `keycloak`, Deployment `keycloak` (1 pod + Vault init container) | **8080** login pages, tokens, JWKS · **9000** `/health/*`, `/metrics` | 8080 via Kong: `http://auth.localhost/` · 9000 no |
| Keycloak's Postgres | namespace `keycloak`, StatefulSet `keycloak-postgres` (own 1 Gi disk) | 5432 | No |
| Kong route | Ingress `keycloak`, host `auth.localhost` | — | `127.0.0.1:80` with `Host: auth.localhost` (browsers resolve `*.localhost` to 127.0.0.1 themselves) |
| API: `/auth-config`, `GET /bookings` | namespace `railbook` | 8000 | via Kong |

Files:

| File | Purpose |
|---|---|
| `scripts/vault-setup.sh` | Keycloak's 4 secrets, 2 policies, 2 roles |
| `deploy/charts/postgres/` | now installable twice (names from the release); optional fixed app login |
| `deploy/keycloak/postgres-values.yaml` | what makes the second copy Keycloak's |
| `deploy/charts/keycloak/` | Keycloak: Deployment, Service, Ingress, realm ConfigMap |
| `deploy/charts/keycloak/files/railbook-realm.json` | realm, client, role, test users |
| `app/migrations/`, `alembic.ini` | schema history (Alembic) |
| `app/auth.py` | token check (401/503), the current user |
| `app/routers/bookings.py` | ownership rules (403), `GET /bookings` |
| `app/static/auth.js` | the browser login (PKCE) |
| `scripts/bootstrap.sh` | installs keycloak-postgres and keycloak after Vault + Postgres |

Memory measured: Keycloak **714 MB** (limit 1 GiB), its Postgres ~50 MB. WSL still had ~3 GB
available afterwards.

## 3. Decisions and why

| Decision | Chosen | Instead of | Why |
|---|---|---|---|
| Identity provider | Keycloak in kind | own password table + JWT | Passwords, lockout, sessions and key rotation are a product of their own; OIDC is the standard |
| Who checks tokens | the API itself (PyJWT) | Kong plugin | Kong OSS has no OIDC plugin; its `jwt` plugin can't fetch rotating keys from a JWKS URL |
| Install | own hand-written chart, official image | Keycloak Operator; Bitnami chart | every setting visible; no extra controller pod (~200–300 MB); Bitnami's free images are no longer updated |
| Keycloak's database | separate Postgres instance | second database in `railbook-postgres` | a Keycloak problem can't fill or lock the bookings database |
| Its DB password | static secret in Vault KV | dynamic Vault DB user | Keycloak can't switch DB users while running; a dynamic user would expire under it |
| Realm config | realm JSON, `--import-realm` | keycloak-config-cli; Terraform | official and simple; catch: only imported if the realm doesn't exist |
| Schema change | Alembic | `ALTER` in the setup script; drop the table | versioned, tested, repeatable |
| Roles | `railbook-admin` realm role | owner check only | shows authentication vs authorization |
| Old bookings (no owner) | kept, admin-only | assign to a user; delete | nothing lost |
| Browser login | hand-written PKCE | `keycloak-js`, `oidc-client-ts` | every step of the flow visible |
| Token storage | memory only | `sessionStorage`, `localStorage` | a script injected into the page can't read it from storage |
| Key cache | per-key cache kept until restart | key set re-fetched every 5 min only | tokens keep working while Keycloak is down (see §9) |
| Extras (auth metrics, alerts, `user_id` in logs, Keycloak ServiceMonitor) | **skipped** (2026-10-31) | — | user's choice; see §15 |

## 4. Vault: Keycloak's secrets

`scripts/vault-setup.sh base` adds four KV secrets, generated with `openssl rand` and written only
if missing (re-running never changes a password that's in use):

| Path | Holds | Read by |
|---|---|---|
| `secret/keycloak/postgres-admin` | superuser password of Keycloak's Postgres | `keycloak-postgres` only |
| `secret/keycloak/db` | `keycloak` + password | Postgres (creates the login), Keycloak (logs in) |
| `secret/keycloak/admin` | `admin` + password | Keycloak's first admin (`KC_BOOTSTRAP_ADMIN_*`) |
| `secret/keycloak/test-users` | passwords of `alice`, `bob`, `railadmin` | the realm import |

**Two DB logins.** Keycloak gets a non-superuser login that owns only its database. Compromised,
it couldn't drop other databases, create roles or read server files. Same idea as the API, which
works through `railbook_owner`, never as `postgres`.

**Least privilege, checked** with tokens of each policy (`vault token capabilities`):

| Policy | postgres-admin | db | admin | test-users | railbook/payment |
|---|---|---|---|---|---|
| `keycloak` | deny | read | read | read | deny |
| `keycloak-postgres` | read | read | deny | deny | deny |

Kubernetes-auth roles `keycloak` and `keycloak-postgres` bind each policy to one service account
in namespace `keycloak`. Tokens live 15 min: the secrets are read once at pod start.

**Where's the token signing key?** Not in Vault. Keycloak generates it when the realm is created
and keeps it in **its own database**. It never leaves Keycloak; only the public half is published.

## 5. Keycloak's own Postgres: one chart, two releases

The `postgres` chart had `railbook-postgres` hard-coded. Every name now comes from
`{{ .Release.Name }}`, so the release name picks the name:

```bash
helm upgrade --install railbook-postgres deploy/charts/postgres -n railbook               # as before
helm upgrade --install keycloak-postgres deploy/charts/postgres -n keycloak \
  -f deploy/keycloak/postgres-values.yaml
```

`railbook-postgres` renders the same as before (checked against `helm get manifest` before
applying; its pod wasn't recreated).

The init ConfigMap (runs **only on first start**, when the data folder is empty) now has two
optional parts:

- `01-roles.sql` if `ownerRole` is set (RailBook's group role).
- `02-app-user.sh` if `vault.appUserPath` is set: creates a login with a fixed password and makes
  it owner of the database and its `public` schema.

```sh
psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" \
  -v user="$APP_DB_USER" -v pw="$APP_DB_PASSWORD" -v db="$POSTGRES_DB" <<'SQL'
CREATE ROLE :"user" LOGIN PASSWORD :'pw';
ALTER DATABASE :"db" OWNER TO :"user";
ALTER SCHEMA public OWNER TO :"user";
SQL
```

- The password comes from the **environment** (rendered by Vault into
  `/vault/secrets/postgres.env`), never the ConfigMap, which anyone who can read the namespace
  can read.
- `:"x"` / `:'x'`: psql quotes the value as a name / a string. Odd characters can't change the
  SQL (psql's version of a parameterised query).
- The file isn't executable, so the image's entrypoint **sources** it: it runs in the
  entrypoint's shell and sees the exported variables.
- **Why own the schema?** Since Postgres 15, ordinary users can't create tables in `public`
  (checked: `has_schema_privilege('public','public','CREATE')` = `f`). Only its owner can, and
  that's `pg_database_owner`, meaning whoever owns the database. So `ALTER DATABASE … OWNER`
  is already enough; `ALTER SCHEMA` makes it explicit.

**`pg_hba.conf` surprise.** A wrong password **worked** when testing from inside the pod to
`127.0.0.1`. The official image trusts local connections:

```
host all all 127.0.0.1/32  trust          ← inside the Postgres pod: no password
host all all all           scram-sha-256  ← everyone else, Keycloak included
```

Through the pod's own IP the wrong password was refused. Test from where the real client is.

## 6. Keycloak: `deploy/charts/keycloak`, piece by piece

**`values.yaml`**

- `image: quay.io/keycloak/keycloak:26.8.0@sha256:…`: pinned by digest (a tag can be moved).
  26.8.1 existed but was 7 days old; the project uses releases at least two weeks old.
- `hostname: auth.localhost`: written into every token as `iss` and into every link on the login
  page, so it must be what the **browser** uses, not `keycloak.keycloak.svc`.
- `heapPercent: 50`, limit 1 Gi, request 512 Mi. Java takes its heap as a share of the container
  limit. Keycloak's default 70% leaves ~300 Mi for everything else the JVM needs (class data,
  threads, compiled code): risk of OOMKill.

**The Deployment**

| Setting | Why |
|---|---|
| `strategy: Recreate` | stop the old pod before starting the new one. Two at once would need 2 GiB and, with local caches, not share sessions. Cost: login is down ~1 min during an upgrade; logged-in users aren't affected (the API uses cached keys) |
| Vault annotations, `agent-pre-populate-only` | init container only: renders `keycloak.env` (DB login, admin, test-user passwords) once |
| `command: bash -c ". /vault/secrets/keycloak.env && exec kc.sh start --import-realm"` | load the secrets, then `exec`, so Keycloak receives SIGTERM |
| `start`, not `start-dev` | production mode: real Postgres, strict hostname. `start-dev` silently uses an in-memory H2 database |
| `KC_DB*` | Postgres at `keycloak-postgres`, database `keycloak` |
| `KC_HOSTNAME=http://auth.localhost` | the public URL (`iss`) |
| `KC_HTTP_ENABLED=true` | no TLS on this laptop setup |
| `KC_PROXY_HEADERS=xforwarded` | trust Kong's `X-Forwarded-*` headers for the client's address and the scheme/host |
| `KC_CACHE=local` | one replica: caches in the pod, no cluster discovery |
| `KC_HEALTH_ENABLED`, `KC_METRICS_ENABLED` | `/health/*` and `/metrics` on port 9000 |
| `KC_LOG_CONSOLE_OUTPUT=json` | one JSON object per line, like the API |
| `JAVA_OPTS_KC_HEAP` | the 50% heap |
| startup / readiness / liveness probes on 9000 | `/health/started` (up to 5 min: first start creates ~90 tables), `/health/ready`, `/health/live` |
| `checksum/realm` annotation | the realm file's hash: editing it rolls the pod |
| Service ports `http` 8080, `management` 9000 | Kong routes only 8080; checked: `/health/ready` and `/metrics` through Kong → 404 |

**First start**, from the logs: build ("Changes detected in configuration. Updating the server
image", ~48 s), `Keycloak 26.8.0 … started in 21.0s`, `Realm 'railbook' imported`,
`Created temporary admin user with username admin`.

**Every start rebuilds (~48 s).** `KC_DB`, health, metrics, cache are *build options*. The
stock image wasn't built with ours, so `start` re-runs the build first. The fix would be our own
image (`FROM keycloak` + `RUN kc.sh build`, started with `--optimized`). Not done: it works
without it.

## 7. The realm as code

`files/railbook-realm.json`, loaded into a ConfigMap and imported with `--import-realm`
(strategy `IGNORE_EXISTING`: only if the realm doesn't exist; afterwards Keycloak's database is
the source of truth).

**Client `railbook-web`**

- `publicClient: true`: **no client secret**. Everything a browser runs is downloaded to the
  visitor's computer, so a secret in `app.js` would be published to the world. Apps on the user's
  device (browser, mobile, desktop) are *public* clients; apps on a server you control can be
  *confidential*. PKCE takes the place of the secret.
- `pkce.code.challenge.method: S256`: PKCE is mandatory.
- Only the authorization-code flow: `directAccessGrantsEnabled` (password grant) and implicit
  flow are off.
- `redirectUris` `http://localhost/*`, `http://127.0.0.1/*`: Keycloak sends codes only there.
- `webOrigins`: the origins allowed to call the token endpoint from JavaScript (CORS).
- **Audience mapper**: puts `aud: railbook-api` in the access token. The token is issued *to*
  the frontend (`azp`) but *for* the API (`aud`); the API refuses other audiences.

**Role** `railbook-admin` (realm role, appears in `realm_access.roles`).

**Users** `alice`, `bob` (no roles), `railadmin` (`railbook-admin`):

- **Fixed `id`s.** `sub` is the user ID and becomes `bookings.user_id`. With random IDs, wiping
  Keycloak's database and re-importing would give everyone a new `sub` and orphan every booking.
- Passwords are `${TEST_USER_ALICE_PASSWORD}` placeholders; Keycloak substitutes environment
  variables during the import, and Vault provides them. The JSON in a public repo holds no password.
- `bruteForceProtected`, `failureFactor: 5`: an account is locked out for a while after 5 wrong
  passwords.
- `accessTokenLifespan: 300`: access tokens live 5 minutes.

## 8. Alembic: giving bookings an owner

`create_all()` creates missing tables but **never changes** an existing one, so it can't add a
column. Alembic keeps a numbered history of schema changes and records the applied one in table
`alembic_version`; `upgrade head` runs only what's missing.

| Revision | Change |
|---|---|
| `0001_initial_schema` | the schema exactly as `create_all()` built it (autogenerated against an empty DB from the models *before* `user_id`) |
| `0002_booking_owner_user_id` | `bookings.user_id VARCHAR(255) NULL` + index `ix_bookings_user_id` |

**`0002`, line by line**

- **Nullable, no default**: Postgres only records the column in its catalog, no table rewrite.
  The old API version, still serving during the rollout, never mentions `user_id`, so it keeps
  working. Old rows get NULL: no owner.
- **255**: Keycloak's `sub` is a UUID, but OIDC allows any string up to 255 characters.
- **Index**: "my bookings" filters on `user_id`.

**`env.py`** connects with the app's own `get_engine()`: same Vault DB user, same
`SET ROLE railbook_owner`. New tables get the same owner (checked: `alembic_version` and
`bookings` both owned by `railbook_owner`).

**Baseline** (`app/admin.py: migrate`). The cluster's database had all tables but no
`alembic_version` (built by `create_all`). Running `0001` there would fail on
`CREATE TABLE bookings: already exists`. So when the tables exist without a version, `migrate()`
**stamps** `0001` (records it as applied without running it), then upgrades. Setup Job log:
`Running stamp_revision -> 0001`, `Running upgrade 0001 -> 0002`.

**The setup Job now runs before the new pods** (`helm.sh/hook: post-install,pre-upgrade`):

- Before, it was `post-upgrade`: after the new pods. New code would have queried `user_id` before
  it existed → 500s until the Job finished.
- `pre-upgrade`: schema first, then code. Measured: Job done 23:16:22, new pods 23:16:23 and
  23:16:39. If the migration fails, Helm stops; the old pods keep running.
- On a **first install** it stays `post-install`: before that its ServiceAccount and ConfigMap
  don't exist.
- Consequence: the Job runs with the **previous** release's ConfigMap. New settings must have
  defaults (that's why every `OIDC_*` setting has one).
- **Expand, then contract.** The old code keeps serving on the new schema during the rollout, so
  every migration must work with the old code. Adding a nullable column: safe. Renaming or
  dropping: two releases (stop using it, then remove it).

**Writing the next migration** (local test Postgres):

```bash
DB_HOST=localhost DB_PORT=5433 DB_NAME=railbook DB_USER=railbook DB_PASSWORD=… PAYMENT_API_KEY=x \
  uv run alembic revision --autogenerate --rev-id 0003 -m "what changes"
```

Review the generated file, then `uv run pytest`: `test_migrations_match_models` fails if a model
and the migrations disagree.

## 9. The API checks the token: `app/auth.py`

```python
bearer = HTTPBearer(auto_error=False)
```

Reads `Authorization: Bearer …`. `auto_error=False`: a missing header gives `None`, so we answer
**401** with `WWW-Authenticate: Bearer` (FastAPI's default would be 403). Also adds the
**Authorize** button to `/docs`.

**Getting the key**

```python
jwt.PyJWKClient(settings.oidc_jwks_url, cache_keys=True, lifespan=300, timeout=5)
```

- The token header's `kid` picks the key from the JWKS.
- Tier 1: the whole key set, cached 5 min.
- Tier 2 (`cache_keys=True`): each key once seen, until the pod restarts.
- Unknown `kid` (Keycloak rotated keys) → re-fetch, at most once per 30 s (cooldown), so made-up
  `kid`s can't make the API flood Keycloak.
- Keys are fetched from the **in-cluster** Service (`keycloak.keycloak.svc:8080`), but `iss` must
  equal the **public** URL. Inside a pod, `auth.localhost` means the pod itself.

**Why tier 2** (your choice). With tier 1 only, the first request after the 5-min set expires
needs Keycloak; if it's down → 503 for everyone, even with valid tokens. Shown in a pod with a
2 s lifespan and Keycloak scaled to 0:

```
set cache only         Keycloak DOWN -> PyJWKClientConnectionError (API: 503)
set + per-key cache    Keycloak DOWN -> key found, token checked
```

and the real API answered 200 ×6 with Keycloak stopped. Limits: access tokens live 5 min and
renewing needs Keycloak, so a long outage still logs everyone out within 5 min; and a key that
Keycloak *removes* (say, leaked) stays trusted by running pods until they restart.

**Checking**

```python
jwt.decode(token, key,
    algorithms=["RS256"],
    issuer=settings.oidc_issuer,
    audience=settings.oidc_audience,
    options={"require": ["exp", "iat", "iss", "aud", "sub"]},
    leeway=10)
```

- `algorithms=["RS256"]`: **we** pick the algorithm, never the token. Otherwise a forged token
  could claim `none` (no signature) or HS256 using our *public* key as the shared secret
  ("algorithm confusion").
- `issuer` / `audience` / `exp`: our realm, for our API, not expired (10 s clock leeway).
- `require`: a token missing any of these is refused.

**Errors**: any `PyJWTError` → **401** (logged as `token_rejected` with the reason); keys
unreachable and not cached (`PyJWKClientConnectionError`) → **503**: not the caller's fault.

**Config** (`app/config.py`, ConfigMap `OIDC_*`; not secrets, the API only uses public keys):
`OIDC_ISSUER`, `OIDC_JWKS_URL`, `OIDC_AUDIENCE`, `OIDC_ADMIN_ROLE`, `OIDC_CLIENT_ID`.

## 10. Who may do what: authorization

| Endpoint | Rule |
|---|---|
| `GET /stations`, `/trains`, `/`, `/static/*`, `/auth-config` | public |
| `POST /bookings` | logged in; owner = `user.sub` from the **signed token** (a `user_id` field in the body is ignored, tested) |
| `GET /bookings` (new) | your bookings, newest first; admin: everyone's (50) |
| `GET` / `DELETE /bookings/{pnr}` | owner or `railbook-admin`, else **403** (`booking_forbidden` in the log). NULL owner: admin only |

The check lives in one function, `_load()`, used by viewing and cancelling, so the two can't
drift apart. 403 tells a caller that the PNR exists; returning 404 instead would hide that. We
kept 403 (chosen in the design), and PNRs are 10 random digits.

## 11. The browser login: `app/static/auth.js`

**1. `login()`**

```js
const verifier = randomString();            // 32 random bytes, crypto.getRandomValues
const state = randomString();
sessionStorage.setItem(PKCE_KEY, JSON.stringify({ verifier, state, silent }));
// …code_challenge: b64url(SHA-256(verifier)), code_challenge_method: "S256"
location.assign(`${endpoints.auth}?${params}`);
```

- The verifier must survive a full page navigation, which wipes JavaScript memory, so it waits in
  `sessionStorage` (this tab only). It's useless without a code and deleted on use.
- **`state`**: Keycloak echoes it back. If it doesn't match, this tab didn't start that login:
  stops forged login responses (CSRF).
- Endpoints come from **OIDC discovery**: `${issuer}/.well-known/openid-configuration`. The
  issuer and client ID come from the API's `GET /auth-config` (factor III: config from the
  environment, not hard-coded in `app.js`).

**2.** Keycloak shows its login page on **its own** domain. RailBook's page never sees the password.

**3. `init()` on `/?code=…&state=…`**: checks `state`, deletes the saved verifier, removes
`?code` from the address bar (`history.replaceState`) so it isn't bookmarked or shared, then
POSTs code + verifier to the token endpoint. That's cross-origin (`localhost` →
`auth.localhost`), allowed by `webOrigins` (CORS). Checked: the real POST from `http://localhost`
gets `Access-Control-Allow-Origin: http://localhost`; from `http://evil.example` it gets 403 and
no CORS header (the browser preflight answers yes to anyone; the real check is on the request).

**4. Tokens in memory only** (variables inside the `Auth` module). Refreshed 30 s before the
access token expires; `api()` in `app.js` also refreshes and retries once on a 401.

**5. Reload**: memory is gone, but `railbook.loggedIn` in `sessionStorage` says "try again".
`login({ silent: true })` adds `prompt=none` ("never show a login page"). Keycloak recognises
its own session cookie on `auth.localhost` and sends a code straight back, no password.
If there's no session, it returns `error=login_required` → simply logged out.

**6. `logout()`** clears the tokens **and** ends the Keycloak session (`end_session_endpoint`
with `id_token_hint`); otherwise the next "Log in" would succeed without a password.

**7. `user()`** decodes the token **for display only** (name, admin badge). The browser can't
verify the signature and doesn't need to: the API decides what anyone may do.

**`app.js`**: `Authorization: Bearer …` on every API call, Log in / Log out in the top bar,
"My bookings" list, "Log in to book seats" if you click a class while logged out, 401/403 messages.

## 12. Tests

35 tests, Postgres on 5433 as before. No Keycloak needed: `conftest.py` makes its **own** RSA key
pair, signs tokens with it, and replaces only `auth.jwks_client` (where the public key comes
from). Every check in `decode()` runs for real. Fixtures: `anon` (no token), `client` (alice).

**`tests/test_auth.py`**: 10 kinds of bad token, each refused for the right reason (printed while
writing the tests):

| Token | Refused by |
|---|---|
| not a JWT | `DecodeError` |
| expired | `ExpiredSignatureError` |
| other issuer / other audience | `InvalidIssuerError` / `InvalidAudienceError` |
| signed by another key; payload edited | `InvalidSignatureError` |
| unknown `kid` | no matching key |
| `alg: none` | `InvalidAlgorithmError` |
| HS256 signed with our public key | `InvalidAlgorithmError` |
| no `sub` | `MissingRequiredClaimError` |

Plus: every booking endpoint 401s without a token (with `WWW-Authenticate`); owner comes from the
token, not the body; owner/other/admin view and cancel; lists show only your bookings; NULL owner
admin-only; Keycloak unreachable → 503.

**`tests/test_migrations.py`**: `alembic check` (models and migrations agree; proved by adding a
`seat_no` column without a migration → `AutogenerateDiffsDetected`), and a pre-Alembic database
with a booking upgrades to `0002` without losing it.

## 13. How it was verified

| Check | Result |
|---|---|
| Keycloak discovery through Kong | `issuer = http://auth.localhost/realms/railbook` |
| JWKS from an API pod | `RS256 sig` + `RSA-OAEP enc` keys |
| Scripted PKCE login (Python, through Kong) | alice: `aud railbook-api`, `azp railbook-web`, fixed `sub`, 300 s; railadmin: `['railbook-admin']`; wrong password: no code; code swapped **without** verifier: `400 invalid_grant: PKCE code verifier not specified` |
| Keycloak DB login | not superuser, owns `keycloak`, can't create roles, wrong password refused over the pod network |
| Migration on the cluster | baseline stamp + `0002`; 120 old bookings kept, all `user_id` NULL; 156/156 `GET /bookings/<old PNR>` = 200 during the rollout; `alembic check` on the **live** DB: no differences |
| API through Kong, real tokens | no token 401; garbage 401; `/stations` 200; alice books 201 (DB owner = her `sub`); bob views/cancels it 403; lists alice 1 / bob 0; old booking alice 403 / admin 200; admin cancels alice's 200 |
| Keycloak stopped | warmed pods kept answering 200 with a valid token |
| Real Chromium (headless, Playwright) | logged out view; PKCE login (`S256`, no verifier in URL, code removed after); storage on `localhost`: only `railbook.loggedIn`, no token, no cookie; booking 201 with Bearer; reload → logged in again without password; logout → next login asks for the password; railadmin sees the admin badge and all bookings |
| Your own browser | works (2026-10-31) |

Cluster after the block: Helm `railbook` revision 34 (local image), `keycloak` 1,
`keycloak-postgres` 1.

## 14. Problems hit and what they teach

| Problem | Lesson |
|---|---|
| Wrong DB password accepted inside the Postgres pod | the image trusts `127.0.0.1`; test from where the real client connects |
| Setup Job was `post-upgrade` | with schema changes the migration must run *before* the new code; and old code must work on the new schema |
| `pre-upgrade` Job runs with the old ConfigMap | new settings need defaults |
| `alembic revision` couldn't `import app` | the CLI doesn't add the current directory to `sys.path`: `prepend_sys_path = .` |
| Test inserted `schedules.id = 1` by hand → seed hit a duplicate key | a hand-set ID doesn't move the sequence; let Postgres choose |
| First "alg none" test was refused only for a missing `kid` | check a negative test fails for *the reason it's meant to test* |
| 503 for all requests when Keycloak was down, after 5 min | key-set cache alone isn't enough → per-key cache |
| First Keycloak-down test still gave 503 | brand-new pods had seen no key yet; a cache only helps after a warm-up |
| `kubectl cp` to the API pod failed | read-only root filesystem (our own hardening); pipe the script on stdin |
| Browser preflight said yes to `evil.example` | the CORS check that matters is on the real request (403, no header) |
| Headless Chromium missing `libnss3` etc. | `apt-get download` + `dpkg -x` into a folder + `LD_LIBRARY_PATH`: no `sudo`, nothing installed |
| Browser console: 404 | `/favicon.ico`: Kong has no route. Present since block 1b, harmless |

## 15. Not done

- **Extras skipped** (user's choice, 2026-10-31): Prometheus metrics for 401/403 and failed
  logins, alerts (Keycloak down, login-failure bursts), `user_id` in the API's request log line,
  a ServiceMonitor for Keycloak's `/metrics`. Keycloak's logs already reach Splunk (JSON); its
  metrics endpoint exists on 9000 but nothing scrapes it.
- **No TLS** anywhere (laptop-only, like the rest of the project). With TLS, Keycloak's
  `sslRequired` and secure cookies would apply.
- **Optimized Keycloak image** (§6): every start rebuilds for ~48 s.
- **Logout everywhere**: a logged-out user's access token stays valid until it expires (≤ 5 min);
  the API doesn't ask Keycloak per request (that's the trade-off of local checks).

## 16. Day-to-day use and exercises

```bash
open http://localhost/                                       # Log in (top right)
scripts/vault-read.sh secret/keycloak/test-users alice       # also: bob, railadmin
open http://auth.localhost/admin/                            # Keycloak admin console
scripts/vault-read.sh secret/keycloak/admin password         # user: admin
kubectl -n keycloak logs deploy/keycloak -c keycloak | tail  # Keycloak (JSON lines)
```

Exercises (explain first, then check):

1. Copy alice's access token from the browser's DevTools (Network → a `/bookings` request →
   `Authorization`) and decode the middle part. Which claim becomes `bookings.user_id`?
2. Change one letter in that token's payload and send it with curl. Which status, and which
   check in `decode()` refused it?
3. Log in as bob, try to open one of alice's PNRs. Then as railadmin. What's different, and
   which function decides?
4. Scale Keycloak to 0 while logged in. What keeps working, for how long, and why?
5. Why does `auth.js` remove `?code=…` from the address bar?
6. Why must the setup Job run *before* the new pods, and why must `0002` still work with the old code?
