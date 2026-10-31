# RailBook

A minimal train ticket booking API, built in one day to learn CI/CD, Prometheus, Splunk and Vault as a Twelve-Factor app, running locally on kind behind Kong.

See [docs/PRD.md](docs/PRD.md) for requirements, design and decisions, and [docs/STATUS.md](docs/STATUS.md) for progress and how to resume a session.

## Run it locally

Needs Docker, kind, Helm and kubectl. Brings up kind, Kong, Vault, monitoring, Postgres, Keycloak and the API in order:

```bash
scripts/bootstrap.sh          # safe to rerun; also re-unseals Vault after a restart
open http://127.0.0.1/         # web frontend through Kong (API docs at /docs)
```

| UI | URL | Login |
|---|---|---|
| RailBook | http://127.0.0.1/ (or http://localhost/) | **Log in** top right: `alice`, `bob` (users), `railadmin` (admin); passwords: `scripts/vault-read.sh secret/keycloak/test-users alice` |
| Keycloak | http://auth.localhost/admin/ | `admin` / `scripts/vault-read.sh secret/keycloak/admin password` |
| Grafana | http://grafana.localhost/ (dashboard: `/d/railbook`) | `admin` / `scripts/vault-read.sh secret/monitoring/grafana admin_password` |
| Prometheus | http://prometheus.localhost/ | — |
| Alertmanager | http://alertmanager.localhost/ | — |
| Splunk | http://127.0.0.1:8000/ (start with `scripts/splunk.sh`) | any password works (Splunk Free has no authentication); the real one: `scripts/vault-read.sh secret/splunk/admin password` |

All of them are published on `127.0.0.1` only.

Vault's unseal keys and root token are written to `~/.railbook/vault-init.json` — keep that file safe and never commit it.

## Learning notes

- [Start here — services, ports and glossary in plain words](docs/learning/00-glossary.md)
- [Block 1 — The booking API, explained](docs/learning/01-app.md)
- [Block 1b — The quick frontend, explained](docs/learning/01b-frontend.md)
- [Block 2 — kind, Kong and Vault, explained](docs/learning/02-kind-kong-vault.md)
- [Block 3 — CI/CD, explained](docs/learning/03-ci-cd.md)
- [Block 4 — Prometheus and Grafana, explained](docs/learning/04-prometheus-grafana.md)
- [Block 5 — Splunk and the log collector, explained](docs/learning/05-splunk.md)
- [Block 6 — Verification against the PRD](docs/learning/06-verify.md)
- [Block 7 — Login with Keycloak (OIDC, PKCE), owners and Alembic, explained](docs/learning/07-auth.md)
- [How the app flows — diagrams of every request, secret, metric and log path](docs/learning/app-flow.html)
- [Bugs found after block 6 — causes, fixes, how each was verified](docs/STATUS.md#bugs-found-after-block-6)
