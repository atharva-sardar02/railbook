# RailBook

A minimal train ticket booking API, built in one day to learn CI/CD, Prometheus, Splunk and Vault as a Twelve-Factor app, running locally on kind behind Kong.

See [docs/PRD.md](docs/PRD.md) for requirements, design and decisions.

## Run it locally

Needs Docker, kind, Helm and kubectl. Brings up kind, Kong, Vault, Postgres and the API in order:

```bash
scripts/bootstrap.sh          # safe to rerun; also re-unseals Vault after a restart
open http://127.0.0.1/         # web frontend through Kong (API docs at /docs)
```

Vault's unseal keys and root token are written to `~/.railbook/vault-init.json` — keep that file safe and never commit it.

## Learning notes

- [Block 1 — The booking API, explained](docs/learning/01-app.md)
- [Block 1b — The quick frontend, explained](docs/learning/01b-frontend.md)
- [Block 2 — kind, Kong and Vault, explained](docs/learning/02-kind-kong-vault.md)
- [Block 3 — CI/CD, explained](docs/learning/03-ci-cd.md)
