# Block 3 — CI/CD, explained

How a commit becomes running pods, why each safeguard exists, and the mistakes made along the way.

## Contents

1. [CI vs CD](#1-ci-vs-cd)
2. [The pipeline end to end](#2-the-pipeline-end-to-end)
3. [`.github/workflows/ci-cd.yml`, job by job](#3-githubworkflowsci-cdyml-job-by-job)
4. [Supply-chain safety: pinning](#4-supply-chain-safety-pinning)
5. [The self-hosted runner](#5-the-self-hosted-runner)
6. [Least privilege: `github-deployer`](#6-least-privilege-github-deployer)
7. [Branch protection](#7-branch-protection)
8. [GHCR and image tags](#8-ghcr-and-image-tags)
9. [Problems hit and what they teach](#9-problems-hit-and-what-they-teach)
10. [Day-to-day workflow and exercises](#10-day-to-day-workflow-and-exercises)

---

## 1. CI vs CD

| | Question it answers | Here |
|---|---|---|
| **CI** — continuous integration | Is this change safe to merge? | Every PR and every push: secret scan, lint, tests, chart lint, image build, vulnerability scan. |
| **CD** — continuous deployment | How does merged code get running? | Every push to `main` is deployed to kind automatically. Nobody runs `helm upgrade` by hand. |

---

## 2. The pipeline end to end

```
 you: branch → commit → push → open PR
                                   │
                                   ▼
 GitHub-hosted runner (fresh Ubuntu VM, thrown away after)
   checks: gitleaks → ruff → pytest + Postgres → helm lint
   image:  docker build → Trivy scan                    (PR: no push)
                                   │ both green + branch up to date
                                   ▼
 you: merge PR  →  push to main
                                   │
                                   ▼
 GitHub-hosted runner
   checks (again, on the merged code)
   image:  build → Trivy → PUSH ghcr.io/atharva-sardar02/railbook:<git sha>
                                   │
                                   ▼
 self-hosted runner on your laptop (user ghrunner, label "kind")
   deploy: helm upgrade --set image.tag=<git sha> --rollback-on-failure --wait
           smoke test via Kong (/ and /stations → 200)
                                   │
                                   ▼
 kind: new API pods (Vault injects secrets) → setup Job → old pods drained
```

First real run: merge commit `71bad3b` → Helm revision 6 running `ghcr.io/atharva-sardar02/railbook:71bad3b…`, smoke test 200/200.

---

## 3. `.github/workflows/ci-cd.yml`, job by job

### Triggers and default permissions

```yaml
on:
  pull_request:
  push:
    branches: [main]
permissions:
  contents: read
```

- PRs and pushes to `main` both run CI; only `main` deploys.
- Every run gets an automatic `GITHUB_TOKEN`. The top-level `permissions` makes it **read-only by default**; a job must ask for more. A compromised step can only do what its job's token allows.

### Job `checks`

| Step | Why |
|---|---|
| `services: postgres` | A real Postgres in a container next to the job, on port 5433 (the test defaults). Row locks and `ON CONFLICT` behave as in kind — factor X. Its password is a throwaway for a database that dies with the VM. |
| `checkout` with `fetch-depth: 0` | Full history, so gitleaks scans **past commits** too. A secret deleted in a later commit is still in history. |
| `persist-credentials: false` | Checkout normally leaves the token in `.git/config`; later steps (including third-party tools) could read it. |
| **gitleaks** | Finds keys/passwords by pattern and entropy. `--redact` hides any match in the log, so the log doesn't leak it again. |
| `setup-uv` + `uv sync --frozen` | Exactly the locked versions (factor II); fails if `uv.lock` is stale. |
| `ruff check` / `ruff format --check` | Bugs, security patterns, consistent style. |
| `pytest` | Behaviour, including the concurrency test. |
| `helm lint` both charts | Catches template errors before deploy time. |

### Job `image` (`needs: checks`)

| Step | Why |
|---|---|
| `permissions: packages: write` | Only this job may push to GHCR — and the push steps only run on `main`. |
| `build-push-action` with `load: true, push: false` | Build once into the runner's Docker, so Trivy scans **exactly** the image that gets pushed. |
| `labels: org.opencontainers.image.source / revision` | Links the GHCR package to this repo (it inherited the repo's **public** visibility) and records the commit inside the image. |
| `cache-from/to: type=gha` | Reuses layers between runs: dependency layers are rebuilt only when `uv.lock` changes. |
| **Trivy** `--severity HIGH,CRITICAL --ignore-unfixed --exit-code 1` | Fails on serious vulnerabilities **that have a fix**. Unfixed ones are reported but can't be acted on, so they don't block. |
| `login-action` with `secrets.GITHUB_TOKEN` | Token minted for this run, gone when it ends. No registry password stored anywhere. |
| `docker push …:<sha>` | Only on `main`: only reviewed code becomes a published image. |

### Job `deploy` (`needs: image`)

| Setting/step | Why |
|---|---|
| `if: push && main` | Never for PRs — PR code never runs on your laptop. |
| `runs-on: [self-hosted, kind]` | Only a runner with both labels takes it: yours. |
| `environment: local-kind` | GitHub records each deployment (commit, time, status) under the repo's *Environments*. |
| `concurrency: deploy-local-kind`, `cancel-in-progress: false` | Two merges in quick succession deploy one after the other, never at the same time, and a running deploy is never killed halfway. |
| `helm upgrade … --set image.repository/tag` | Release = this commit's image + the chart's config (factor V). |
| `--rollback-on-failure --wait --wait-for-jobs --timeout 5m` | Waits until new pods are Ready and the setup Job succeeded. If not, Helm restores the previous revision **by itself** — a bad commit can't leave the app broken. |
| Smoke test via Kong | "Pods are Ready" ≠ "users can use it". Requests through the real entry point prove the whole path (Kong → Service → pods → Postgres). `--retry-all-errors` rides over the few seconds Kong needs to pick up new pod IPs. |
| Record what is running | `helm history` + the image in the log: an audit trail per deploy. |

---

## 4. Supply-chain safety: pinning

```yaml
uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v7.0.1
TRIVY_IMAGE: aquasec/trivy:0.75.0@sha256:af6acf9a…
```

- A tag like `@v7` or `:0.75.0` can be **moved** by whoever controls the repository. If an action's account is compromised, attackers re-point tags at malicious code and every workflow using the tag runs it — this has happened to popular actions. A **commit SHA** or **image digest** identifies exact content and can't be re-pointed.
- The comment keeps it readable; updating means replacing the SHA deliberately.
- Every pinned version was at least two weeks old when chosen (same rule as `exclude-newer` in block 1): compromised releases are usually caught within days.

---

## 5. The self-hosted runner

### Why one is needed

GitHub's VMs are on the internet; your kind cluster is on `127.0.0.1` in WSL. They can't reach it. A runner **on your laptop** opens an outgoing HTTPS connection to GitHub and asks "any jobs for me?". Nothing on your machine listens for incoming connections.

### Why it's risky on a public repo

Whoever can get a job onto the runner runs code on your laptop. Defences, in layers:

| Layer | Stops |
|---|---|
| Only `deploy` uses it, only on `push` to `main` | PR code (from anyone) never reaches it. |
| Outside contributors' PR workflows need approval (block 0) | Drive-by PRs triggering anything. |
| Branch protection (§7) | Unreviewed / failing code reaching `main`. |
| Runs as `ghrunner`: no sudo, can't read your home | A job escaping to your files or root. |
| `github-deployer` RBAC (§6) | A job touching Vault, Kong, or other namespaces. |
| Stop it when idle | Any of the above while you're not watching. |

### `scripts/setup-runner.sh install`

1. Apply the RBAC objects.
2. Copy `helm` and `kubectl` to `/usr/local/bin` (ghrunner can't read your `~/.local/bin`).
3. Write ghrunner's kubeconfig (§6), `chmod 600`.
4. Download runner v2.338.0 and verify it against the **digest GitHub publishes** for the release asset.
5. Register it with a one-time token (`gh api …/registration-token`, valid 1 hour) as `railbook-laptop`, label `kind`.
6. Install it as a **systemd** service running as ghrunner, so it starts with WSL.

### Day-to-day

```bash
sudo systemctl stop  actions.runner.atharva-sardar02-railbook.railbook-laptop   # when idle
sudo systemctl start actions.runner.atharva-sardar02-railbook.railbook-laptop
scripts/setup-runner.sh refresh-token    # the deploy token lasts 24 h
```

---

## 6. Least privilege: `github-deployer`

kind's own kubeconfig is **cluster-admin**: it can read Vault's data, change Kong, delete anything. The runner gets a different identity:

```
ServiceAccount github-deployer (namespace railbook)
  Role: create/update/delete configmaps, services, serviceaccounts, secrets*,
        deployments, jobs, ingresses, kongplugins — in railbook only
        read pods, pods/log, events, replicasets — what `helm --wait` watches
```

\* Helm stores each release revision as a Secret in the namespace. The namespace holds no application secrets — Vault does.

**Token:** `kubectl create token github-deployer --duration=24h` mints a signed, expiring token on demand. It isn't stored as a Secret object in the cluster; it lives only in ghrunner's `~/.kube/config` and stops working after 24 h.

**How it was tested:**

| Check (`kubectl auth can-i … --as=…:github-deployer`) | Result |
|---|---|
| patch deployments / create jobs / create kongplugins in railbook | yes |
| get pods / list secrets in **vault** | **no** |
| delete statefulsets (Postgres) in railbook | **no** |
| `pods/exec` in railbook | **no** |
| get nodes | **no** |

Then a **real** `helm upgrade` with only that token: succeeded (revision 5). And `kubectl get pods -n vault` with it: `Forbidden`. `can-i` checks rules one at a time; a real deploy proves the set is sufficient.

---

## 7. Branch protection

Set on `main`:

| Rule | Why |
|---|---|
| Pull request required (0 approvals — solo project) | Every change goes through CI before `main`. |
| Required checks `checks` + `image` | Red CI = merge button disabled. |
| `strict`: branch must be up to date | Tests ran against what `main` will actually become, not an older base. |
| `enforce_admins` | Without it, your admin account could bypass all of the above. |
| No force-push, no deletion | History on `main` can't be rewritten or lost. |

Since `main` deploys automatically, **merging is deploying** — which is why Claude Code's auto mode refused to merge PR #1 itself ("merge without review"): that decision stays with you.

---

## 8. GHCR and image tags

- **Tag = full git SHA.** `kubectl get deploy … -o jsonpath='{..image}'` → `…:71bad3b…` tells you exactly which commit is live; `helm rollback` returns to an exact earlier image.
- **Never `latest`.** A moving tag means "whatever was pushed last" — you can't tell what's running, and pods may pull different code on restart.
- **Public package.** kind pulls anonymously, so no image pull secret (no extra credential) is needed. Verified with an anonymous registry request → HTTP 200.
- **Local vs CI images.** `bootstrap.sh` builds `railbook:dev-<content hash>` and loads it into kind; CI deploys `ghcr.io/…:<git sha>`. Both obey the same rule: new code → new tag.

---

## 9. Problems hit and what they teach

| Problem | Cause | Fix | Lesson |
|---|---|---|---|
| `gh run list --branch` unknown | The machine's `gh` is v2.4 (2022) | Query the REST API with `gh api` | Distro packages lag; the API is stable. |
| Merge refused by Claude Code | Auto mode treats merging to an auto-deploying branch as a reviewed decision | You merged it | Merge = deploy; keep a human on that button. |
| `setup-runner.sh` **deleted** `/usr/local/bin/kubectl` | It was a Docker Desktop symlink; `install X X` removes the destination before reading the source | Resolve with `readlink -f`, skip if already there; reinstalled kubectl v1.37.1 (checksum-verified) | Never copy a path onto itself; resolve symlinks first. |
| `tar: Permission denied` | Download in *your* private temp dir; `tar` ran as ghrunner | Extract as root, then `chown -R ghrunner` | Each command runs as *some* user — check what that user can read. |
| (Caught before running) `cd` into ghrunner's 750 dir as you | Same class of bug | Run the whole step inside `sudo bash -c` | After one permission bug, audit every `sudo`/`cd` line. |
| Runner showed `offline` | Status checked 10 ms after start | Seconds later: `online`, picked up the queued deploy | Queued jobs wait for a matching runner; nothing is lost. |

---

## 10. Day-to-day workflow and exercises

```bash
git switch -c my-change
# edit, then:
uv run ruff check . && uv run pytest -q     # same checks CI will run
git commit -am "…" && git push -u origin my-change
gh pr create                                 # CI runs; merge when green → auto-deploy
```

Exercises:

1. **Watch a rollback.** On a branch, set `readinessProbe.httpGet.path: /nope` in `api.yaml`, merge. New pods never become Ready → after 5 min Helm rolls back → the deploy job fails, the old version keeps serving. `helm history railbook -n railbook` shows the failed revision and the rollback. Then revert via another PR.
2. **Catch a leaked secret.** On a branch, add a file containing `AKIA` + 16 random capitals (an AWS-key-shaped string). The `checks` job fails at gitleaks. Delete the branch.
3. **Trace a deploy.** In GitHub: *Environments → local-kind* lists each deployment and its commit; compare with `kubectl get deploy railbook-api -n railbook -o jsonpath='{..image}'`.
