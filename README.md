# Culprit

**An app gets 6× slower. Nobody says what changed. It opens the revert PR by itself.**

Ottawa Hackathon Series 2026 · DevOps for GenAI · 24-hour build.

A Flask service on EC2 publishes latency, system load, request count, and
units-of-work to CloudWatch. A commit lands that makes `/compute` accidentally
quadratic. CloudWatch alarms on it, EventBridge fires a Lambda, the Lambda
sends a `repository_dispatch`, and a GitHub Action runs a Claude Code session
with the metric evidence and the git history in hand. It opens a pull request
reverting the specific commit responsible — or files a triage issue if there
is no repo-side cause.

The regression is a real bug (`seen = []` where it should be `seen = set()`),
it passes every test, and it is only visible in production telemetry. That is
the point.

---

## Read this first

**Demo day, in order:**

1. **[`docs/decisions.md`](docs/decisions.md)** — 4 minutes. Three people built
   overlapping pipelines; this picks one of each and says why. It also
   documents the bug that made the demo impossible to trigger (§1) and the two
   things W3 and W4 must change (§6). Read it before you touch anything.
2. **[`docs/runbook.md`](docs/runbook.md)** — the demo itself, minute by
   minute, with measured timings from a real run.
3. **[`docs/architecture.md`](docs/architecture.md)** — the design, the
   component specs, and the interfaces between workstreams. Still the source
   of truth for *how it works*; `decisions.md` overrides it wherever the two
   disagree about *what we are doing tonight*.

---

## Workstreams

Four people, four workstreams, **exclusive file ownership** — nobody edits a
path they do not own.

| | Workstream | Owns | Starts |
|---|---|---|---|
| **W1** | The app & the box | `app/`, EC2, systemd units | immediately |
| **W2** | Detection & dispatch | `infra/detection.yaml`, `scripts/calibrate.sh`, `scripts/deploy_detection.sh` | **done — deployed and watching W1's box** |
| **W3** | The agent | `.github/`, `scripts/gather_evidence.sh`, `scripts/test_dispatch.sh` | **first — zero AWS dependencies** |
| **W4** | The incident & the demo | `scripts/seed_incident.sh`, runbook, rehearsals | after W1 |

W3 goes first: it has no AWS dependency and it covers the highest-uncertainty
integration in the system. If the GitHub chain is broken you want to know at
19:50, not at 08:30.

Full breakdown, including why "metric collection" is not its own workstream:
[architecture.md §5](docs/architecture.md#5-workstreams).

---

## Getting started

```bash
make setup      # system deps: git, curl, jq, unzip, python venv, gh, aws cli v2 (sudo)
make install    # python deps into .venv
make check      # must print "ready." before you build anything
```

`make check` is the first-15-minutes gate from
[architecture.md §6](docs/architecture.md#6-build-order). **All four of you
must see the same AWS account ID.** Re-run it in the morning — credentials
expire overnight. Both `setup` and `install` skip whatever is already there,
so re-running them is safe.

Then W3, before any AWS resource exists:

```bash
export GITHUB_TOKEN=<classic PAT, scopes repo+workflow, 1-day expiry>
make smoke      # must end with: ALL CHECKS PASSED
```

Pin the AWS region in team chat before anyone deploys.

---

## Repo layout

```
Makefile                setup · install · check · smoke · clean
```

**On disk now** — the existing scaffolding, untouched:

```
template.yaml           SAM template: alarms, anomaly detector, Lambdas, SNS, dashboard
samconfig.toml          SAM deploy config
lambda/                 handler.py · git_context.py · llm_review.py · notify.py
infra/                  cloudwatch-agent/config.json · demo-app/bad_app.py
scripts/                deploy.sh · seed_bad_commit.sh · trigger_chaos.sh
```

`docs/architecture.md` §10 lists the defects found in these files — four that
fail at runtime, four worth fixing before the demo — with the file and line
for each. It does not change them; the owner of each path decides.

**Ownership** — one owner per path, nobody edits a path they do not own:

```
W2 — detection & dispatch (BUILT, deployed, end-to-end verified)
  infra/detection.yaml        alarms + anomaly detector + EventBridge + dispatch Lambda
  scripts/seed_metrics.py     second feed: 10s resolution + a trained band, kept
                              deliberately alongside the real app (see w2-detection.md)
  scripts/calibrate.sh        derives the alarm threshold from observed traffic
  scripts/deploy_detection.sh deploys the stack (separate from template.yaml by design)
  scripts/verify_chain.sh     8 checks; proves the chain without waiting for a regression
  .github/workflows/dispatch-receipt.yml  proof the payload arrives (not W3's agent)
  docs/w2-detection.md        how it works, what is still fake, and what replaces it

Still to come — one owner per path:
  app/                        W1  the Flask service is LIVE on EC2 and alarmed
                                  (i-091814f7a41456cb0), but its source is not in
                                  this repo yet. W4 needs it here to plant a culprit
                                  commit, and W3 needs it to open a PR against.
  scripts/gather_evidence.sh  W3  builds INCIDENT.md from CloudWatch + git
  scripts/test_dispatch.sh    W3  GitHub-half smoke test (already here)
  .github/workflows/anomaly-response.yml  W3  the agent run and the verdict gate
  scripts/seed_incident.sh    W4  the four commits, one of which is the culprit

  docs/decisions.md               What we keep, what we drop, who does what.
  docs/runbook.md                 The demo, minute by minute.
  docs/architecture.md            The plan. Read it.
  docs/contracts/                 dispatch-payload.json · verdict.schema.json ·
                                  eventbridge-alarm-event.json — the frozen
                                  interfaces between workstreams.
```

W2 is deployed and needs nothing from anyone. Six alarms across two feeds —
W1's real app on EC2 and a synthetic high-resolution feed we control — all
converge on one EventBridge rule and one Lambda, and `make verify` proves the
alarm → EventBridge → Lambda → payload chain against real CloudWatch without
waiting for a regression. Watch it at the `Culprit` dashboard (`make dashboard`).

One thing is still missing, and it is not ours: the Lambda has no
`GITHUB_TOKEN`, so it logs the payload it would have POSTed instead of sending
it. W3 mints the PAT; completing the chain is then one redeploy
(`GITHUB_TOKEN=… make deploy`). See `docs/w2-detection.md` for the full gap
register.

## Secrets

Nothing secret is committed. Repo secrets: `PAT_TOKEN`, `ANTHROPIC_API_KEY`,
`AWS_READONLY_ACCESS_KEY_ID`, `AWS_READONLY_SECRET_ACCESS_KEY`. Stack
parameters go via `--parameter-overrides`, never into a checked-in file. The
PAT is classic, scoped `repo` + `workflow`, **one-day expiry**, minted from a
named human's account with that name in the secret description.
