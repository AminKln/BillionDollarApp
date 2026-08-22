# Culprit

**An app gets 6× slower. Nobody says what changed. It opens the revert PR by itself.**

Ottawa Hackathon Series 2026 · DevOps for GenAI · 24-hour build.

A Flask service on EC2 ([`Tehreem404/bad_app_demo`](https://github.com/Tehreem404/bad_app_demo))
publishes request latency and error rate to CloudWatch; the CloudWatch agent on
the same box publishes CPU, memory and disk. A commit lands that makes the
request path accidentally quadratic. CloudWatch alarms on it, EventBridge fires
a Lambda, the Lambda queries five metrics before-and-after to build an evidence
block, and sends a `repository_dispatch` to the app's own repo. A GitHub Action
there runs a Claude Code session with that evidence and the git history in hand.
It opens a pull request fixing the specific commit responsible — or files a
triage issue if there is no repo-side cause.

The regression is a real bug — an O(n²) rank-every-item-against-every-item loop
in a per-request scoring helper (`infra/bad-app/culprit.py.patch`, measured at
**1.07 s/request against a 0.05 s baseline**). It passes every test and is only
visible in production telemetry. That is the point.

**Every number in this pipeline comes from the real app.** A synthetic metric
publisher was used to build detection before the app existed; it was deleted on
2026-08-22 because two of its alarms could dispatch, which meant the agent could
be handed a regression that never happened (`docs/decisions.md` §9).

---

## Read this first

**Demo day, in order:**

1. **[`docs/plan.md`](docs/plan.md)** — **start here.** Who runs which wire,
   right now, and what "done" looks like for it. Read §0 and your own lane;
   nothing else.
2. **[`docs/runbook.md`](docs/runbook.md)** — the demo itself, minute by
   minute, with measured timings from real rehearsals.
3. **[`docs/decisions.md`](docs/decisions.md)** — 4 minutes. Three people built
   overlapping pipelines; this picks one of each and says why. §9 and §10 are
   the most recent and the most load-bearing.
4. **[`docs/architecture.md`](docs/architecture.md)** — the design and the
   interfaces between workstreams. Source of truth for *how it works*, but read
   its superseded banner first; `decisions.md` and `plan.md` override it
   wherever they disagree.

---

## Workstreams

Four people, four workstreams, **exclusive file ownership** — nobody edits a
path they do not own.

| | Workstream | Owns | Starts |
|---|---|---|---|
| **W1** | The app & the box | `bad_app_demo`, EC2, `deploy.sh` | live |
| **W2** | Detection & dispatch | `infra/detection.yaml`, `scripts/{calibrate,deploy_detection,verify_chain}.sh` | **done — deployed and watching the real app** |
| **W3** | The agent | `anomaly-response.yml` + the Claude step, in `bad_app_demo` | **critical path** |
| **W4** | The culprit commit & the demo | `infra/bad-app/culprit.py.patch`, runbook, rehearsals | ready to apply |

**The live task assignment is [`docs/plan.md`](docs/plan.md), not this table.**
It splits the same work into five lanes with names against them, and it carries
the one design decision everyone needs: the responding workflow lives in
`bad_app_demo`, so it opens the PR with the built-in `secrets.GITHUB_TOKEN` and
the only PAT anyone has to mint is a fine-grained, single-repo one for the
Lambda's dispatch call.

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
infra/                  cloudwatch-agent/config.json · bad-app/culprit.py.patch
scripts/                deploy.sh · chaos.sh
```

`docs/architecture.md` §10 lists the defects found in these files — four that
fail at runtime, four worth fixing before the demo — with the file and line
for each. It does not change them; the owner of each path decides.

**Ownership** — one owner per path, nobody edits a path they do not own:

```
W2 — detection & dispatch (BUILT, deployed, end-to-end verified)
  infra/detection.yaml        alarms + anomaly detector + EventBridge + dispatch Lambda
  scripts/calibrate.sh        derives the alarm threshold from observed traffic
  scripts/deploy_detection.sh deploys the stack (separate from template.yaml by design)
  scripts/verify_chain.sh     8 checks; proves the chain without waiting for a regression
  .github/workflows/dispatch-receipt.yml  proof the payload arrives (not W3's agent)
  docs/w2-detection.md        how it works (carries its own historical banner)

Still to come — and most of it now lives in the OTHER repo:
  Tehreem404/bad_app_demo:
    the culprit commit             W4  apply infra/bad-app/culprit.py.patch, push
    .github/workflows/anomaly-response.yml   W3  receives the dispatch, runs Claude
    Settings > Secrets: ANTHROPIC_API_KEY    W1  repo owner
    deploy.sh                      W1  exists on the EC2 box only; capture it verbatim

  here:
    GITHUB_TOKEN=… make deploy     W2  once the fine-grained PAT exists

  docs/decisions.md               What we keep, what we drop, who does what.
  docs/runbook.md                 The demo, minute by minute.
  docs/architecture.md            The plan. Read it.
  docs/contracts/                 dispatch-payload.json · verdict.schema.json ·
                                  eventbridge-alarm-event.json — the frozen
                                  interfaces between workstreams.
```

W2 is deployed and needs nothing from anyone. Three alarms, all on the real
app — `culprit-App-High` (static, the trigger), `culprit-App-Anomaly` (ML
band) and `culprit-App-ErrorRate` (corroboration, does not dispatch) —
converge on one EventBridge rule and one Lambda. `make verify` proves the
alarm → EventBridge → Lambda → payload chain against real CloudWatch without
waiting for a regression, and `make payload` prints the exact JSON the Lambda
last handed GitHub. Watch it at the `Culprit` dashboard (`make dashboard`).

One thing is still missing: the Lambda has no `GITHUB_TOKEN`, so it logs the
payload it would have POSTed instead of sending it. Completing the chain is one
redeploy — `GITHUB_TOKEN=… make deploy` — as soon as the fine-grained PAT
exists (`docs/plan.md` Lane E).

## Secrets

Nothing secret is committed, and nothing secret should be. Two tokens exist:

- **`ANTHROPIC_API_KEY`** — a repo secret in `Tehreem404/bad_app_demo`, for the
  agent. Set it through the GitHub web UI; never put it in a file.
- **One fine-grained PAT** for the Lambda's `repository_dispatch` — scoped to
  `bad_app_demo` only, **Contents: read/write**, **1-day expiry**. It reaches
  the stack as `GITHUB_TOKEN=xxx make deploy` and is declared `NoEcho` in the
  template. Never into a checked-in file.

`GITHUB_TOKEN` inside the workflow is GitHub's own auto-injected token — it
needs no setup and is sufficient to open the PR, because the workflow lives in
the repo it writes to. Stack parameters always go via `--parameter-overrides`.
