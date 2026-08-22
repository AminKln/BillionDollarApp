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

**[`docs/architecture.md`](docs/architecture.md) is the single source of
truth** — the design, the component specs, the interfaces between
workstreams, the hour-by-hour build order, and the demo runbook. Everything
else in this repo is subordinate to it.

---

## Workstreams

Four people, four workstreams, **exclusive file ownership** — nobody edits a
path they do not own.

| | Workstream | Owns | Starts |
|---|---|---|---|
| **W1** | The app & the box | `app/`, EC2, systemd units | immediately |
| **W2** | Detection & dispatch | `template.yaml`, `scripts/calibrate.sh`, `scripts/deploy.sh` | immediately (deploys after W1) |
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

**Being added** — one owner per path, nobody edits a path they do not own:

```
app/                    W1  Flask service, /compute, metric emission, traffic generator.
                            app/compute.py is where the regression lives.
scripts/calibrate.sh    W2  derives the alarm threshold from observed traffic
scripts/gather_evidence.sh  W3  builds INCIDENT.md from CloudWatch + git
scripts/test_dispatch.sh    W3  GitHub-half smoke test (already here)
.github/workflows/      W3  anomaly-response.yml — the agent run and the verdict gate
scripts/seed_incident.sh W4 the four commits, one of which is the culprit
docs/architecture.md        The plan. Read it.
docs/contracts/             dispatch-payload.json · verdict.schema.json —
                            the frozen interfaces between workstreams.
```

## Secrets

Nothing secret is committed. Repo secrets: `PAT_TOKEN`, `ANTHROPIC_API_KEY`,
`AWS_READONLY_ACCESS_KEY_ID`, `AWS_READONLY_SECRET_ACCESS_KEY`. Stack
parameters go via `--parameter-overrides`, never into a checked-in file. The
PAT is classic, scoped `repo` + `workflow`, **one-day expiry**, minted from a
named human's account with that name in the secret description.
