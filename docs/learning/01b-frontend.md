# Block 1b — The quick frontend, explained

A single page for searching, booking, looking up and cancelling — added after block 2.

## Files

```
app/static/index.html   page structure: search form, results area, PNR panel, booking dialog
app/static/app.js       behaviour: calls the API with fetch(), renders results, handles errors
app/static/styles.css   look: CSS variables, dark mode, mobile layout
app/main.py             serves /static/* and / (index.html)
app/routers/trains.py   new GET /stations for the dropdowns
deploy/charts/railbook/templates/ingress.yaml   Kong routes for /, /static, /stations
```

## Why static files served by the API

| Option | Verdict |
|---|---|
| **Static files in the API image (chosen)** | Nothing new to deploy; same pipeline, same image. Page and API share an **origin** (`http://127.0.0.1`), so the browser allows the calls without CORS rules. |
| Separate nginx container + own Kong route | Realistic for big apps, but a second image, Deployment and pipeline path. |
| React/Vue with a build step | Node tooling in CI; teaches nothing about CI/CD, metrics, logs or secrets. |

**Same origin, explained:** browsers block JavaScript from reading responses from a *different* origin (scheme + host + port) unless that server sends CORS headers. Serving the page from the same place as the API avoids the whole topic.

## How the backend serves it (`app/main.py`)

```python
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")
```

- `StaticFiles` serves any file under `app/static/` at `/static/<name>`, with the right `Content-Type`.
- `/` returns the page. `include_in_schema=False` keeps it out of the Swagger docs.
- `/static` is excluded from Prometheus request metrics — CSS/JS downloads would drown out real API traffic.
- The Dockerfile already copies `app/`, so the files ship in the image; `readOnlyRootFilesystem` is fine because they're only read.

## Kong routing: why `/` is Exact

```yaml
Prefix: /trains /bookings /stations /static /docs
Exact:  / /openapi.json
```

A `Prefix` of `/` matches **every** path — including `/metrics`, which would make internal metrics public. `Exact` `/` matches only the home page. Verified after deploy: `/` → 200, `/metrics` → 404.

## `app/static/app.js`

| Piece | Why |
|---|---|
| `api()` wrapper around `fetch` | One place that returns `{ok, status, data, requestId}`. `requestId` is the `X-Request-ID` header Kong added. |
| `ERRORS` table | A clear message per status. **429 comes from Kong**, not the API — the page says so, which shows the gateway at work. 409 uses the API's own text ("only 3 seats left"). |
| `el()` helper + `textContent` | Builds DOM nodes; text always goes in as text. **Never `innerHTML`** with API data: a passenger named `<img src=x onerror=alert(1)>` would otherwise run script for everyone who sees it (XSS). |
| Request ID shown on screen | After a booking or error you can copy the ID and search for it in Splunk (block 5): one ID → Kong line + API lines. |
| `<dialog>` for booking | Native modal: focus handling and Esc-to-close for free, no library. |
| "Simulate a declined payment" checkbox | Sends `simulate_payment_failure` so you can produce 402s on demand for Grafana and Splunk. |
| Re-search after book/cancel | Seat counts update immediately, so you can watch inventory change. |

## `app/static/styles.css`

- **CSS variables** (`--bg`, `--brand`…) hold every colour; dark mode just redefines them under `@media (prefers-color-scheme: dark)`.
- **System font stack** — no downloads, no third-party font servers.
- **Grid layouts** that collapse at 640 px for phones.

## Why the image tag changed to `dev-<hash>`

Before: every local build was `railbook:dev`. Helm compares the *rendered* objects; if the tag is the same, the Deployment is unchanged and **no pods restart** — you'd keep running old code after a "successful" deploy.

Now `bootstrap.sh` tags the build with its content hash (`railbook:dev-e0b5d4abdd20`). New code → new hash → new tag → Kubernetes rolls the pods. Same code → same tag → nothing happens. This is **factor V**: a release must say exactly which build it runs. CI will use the git SHA the same way (block 3).

## Try it

1. Open http://127.0.0.1/ and search New Delhi → Mumbai Central.
2. Book a seat: the toast shows the PNR and the request ID; seat counts drop.
3. Tick "Simulate a declined payment": 402 message, seats unchanged.
4. Book 21+ times within a minute: Kong's 429 message appears.
5. Look up the PNR and cancel: status → CANCELLED, seats return.
