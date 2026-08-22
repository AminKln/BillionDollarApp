# Culprit — Architecture & Build Plan

**Ottawa Hackathon Series 2026 · DevOps for GenAI · Team 06**

> **What this document is.** The agreed plan for the build, consolidating the
> earlier `docs/architecture.md`, `docs/BUILD-PLAN.md`, and
> `docs/demo-script.md` into one place. It is a plan, not a record of what is
> built — the scaffolding already in `lambda/`, `infra/`, and `template.yaml`
> predates it and is untouched. Where the two disagree, §10 says so explicitly
> and names the owner who decides.
>
> **Change it by editing it.** If your workstream needs a different interface
> or a different order, say so in team chat and update §4 or §6. A plan nobody
> edits is a plan nobody is following.

> **Naming:** the project was briefly called "PromptOps" during design. It is
> now **Culprit** — the agent's job is to name the guilty commit. The
> CloudWatch namespace is `Culprit`. If the team prefers another name, change
> it once, here and in `template.yaml`, before anyone starts building.

---

## 0. What we are building

A web app is monitored in production. Someone merges a commit that makes it
measurably worse. CloudWatch notices on its own, and within a few minutes a
Claude Code session — with the metrics, the git history, and the diffs in
hand — opens a pull request that reverts the specific commit responsible, or
files a triage issue if it cannot find one.

**In one sentence for the judges:** an app gets 6× slower, nobody tells the
system what changed, and it opens the revert PR by itself.

Nothing about the demo is faked. The regression is a real bug, the latency is
real work being done badly, and the alarm fires on real datapoints.

---

## 1. The incident

The whole project stands or falls on the quality of the regression. A
`time.sleep()` toggled by a chaos endpoint is not a regression — it is a
switch, and every judge will say so. Ours is a real bug of a kind that ships
constantly.

### The endpoint

`POST /compute` scores a query against a fixed in-memory corpus and returns
the top matches. Real CPU work, deterministic, no I/O, no external calls.

### The regression

`app/compute.py`, **before**:

```python
out = [item for item in scored if item.score > 0]
```

**after** the commit `fix: dedupe results before returning`:

```python
out, seen = [], []
for item in scored:
    if item.score <= 0:
        continue
    if item.key in seen:        # <-- O(n) membership scan on a list...
        continue
    seen.append(item.key)       #     ...inside an O(n) loop
    out.append(item)
```

`seen = []` where it should be `seen = set()`. Accidentally quadratic.

Why this one:

- **It is correct.** The output is right. Every test passes. Code review
  passes — it reads as a defensive fix.
- **It is real.** This exact bug is in production somewhere right now.
- **The fix is one word in one file** — `[]` → `set()`, `.append` → `.add`.
  That is precisely the size of change we are asking the agent to make, and
  small enough to review live on stage.
- **It is invisible to unit tests and to code review, and only visible in
  production telemetry.** That is the entire argument for the project.

### The four seeded commits

The agent must *discriminate*, not confirm. A one-of-one multiple choice
proves nothing.

| # | Commit message | File | Role |
|---|---|---|---|
| 1 | `chore: bump flask to 3.1.2` | `requirements.txt` | noise |
| 2 | `feat: add build info to /healthz` | `app/main.py` | noise |
| 3 | `refactor: extract the scoring loop into a helper` | `app/compute.py` | **decoy** — same file, hot path, sounds riskier than the culprit |
| 4 | `fix: dedupe results before returning` | `app/compute.py` | **the culprit** |

Commit 3 is the important one. It touches the same file as the culprit, it is
in the hot path, and its message sounds more dangerous. An agent reasoning
from commit prose picks 3. An agent reasoning from evidence picks 4.

**The evidence that separates them:** latency up ~6×, system load up ~3×,
**`WorkUnits` completely flat**, `RequestCount` flat. The input did not grow.
Traffic did not grow. The same amount of work now costs six times more — so
the complexity of the code changed. Commit 3 moved code; only commit 4 added
a nested scan.

### Calibration target

Tune `CORPUS_SIZE` so that:

- baseline p50 latency lands between **30 and 60 ms**
- post-regression p50 lands **above 200 ms**

Start at `CORPUS_SIZE = 4000` (~8M comparisons after the regression) and
**measure it** — do not assume. This is the first thing W4 verifies.

---

## 2. Architecture

```
┌─ EC2 t3.small · Amazon Linux 2023 · instance profile ────────────────┐
│  culprit-api.service      Flask :8000  →  /compute  →  put_metric_data│
│  culprit-traffic.service  fixed-rate loop: POST /compute; sleep $SLEEP │
│  culprit-deploy.timer     every 20s: git fetch; if behind → reset+restart│
└───────────────────────────────────┬───────────────────────────────────┘
                                    │  put_metric_data (StorageResolution=1)
                                    ▼
┌─ CloudWatch · namespace "Culprit" ────────────────────────────────────┐
│  RequestLatency ──┬─→ Culprit-Latency-High     static, Period 10  ◀ TRIGGER
│                   └─→ Culprit-Latency-Anomaly  ML band, Period 60 ◀ DASHBOARD
│  SystemLoad1                                                      ◀ EVIDENCE
│  WorkUnits        ← stays flat. this is the discriminator.        ◀ EVIDENCE
│  RequestCount                                                     ◀ EVIDENCE
└───────────────────────────────────┬───────────────────────────────────┘
                                    │  EventBridge default bus
                                    │  "CloudWatch Alarm State Change"
                                    ▼
┌─ Lambda culprit-dispatch · python3.12 · stdlib only · inline in CFN ──┐
│  parse detail.state.reasonData (a nested JSON *string*)               │
│  POST https://api.github.com/repos/{owner}/{repo}/dispatches  → 204   │
└───────────────────────────────────┬───────────────────────────────────┘
                                    ▼
┌─ GitHub Actions · .github/workflows/anomaly-response.yml ─────────────┐
│  checkout fetch-depth:0  →  read-only AWS creds                       │
│  scripts/gather_evidence.sh > INCIDENT.md                             │
│      (get-metric-data before/after for all 4 metrics + the ML band,   │
│       git log --since, diffs, file listing)                           │
│  anthropics/claude-code-action@v1   --max-turns 30                    │
│      → branch auto-fix/*, commit, gh pr create                        │
│      → or gh issue create if no repo-side cause is found              │
│  verify step (plain bash): verdict.json must exist AND its URL must   │
│      resolve via gh pr view / gh issue view, else the run fails red   │
└───────────────────────────────────────────────────────────────────────┘
```

**End to end: 3.5–4.5 minutes from `git push` to a PR URL.** That is why the
demo starts with the push and the talking happens while it runs (§7).

### Why these choices

**EC2, not Lambda or Fargate.** The demo needs `git push` → live in seconds
with no build step. A systemd timer polling git gives us 20 seconds
worst-case. Fargate needs a docker build + ECR push per commit — minutes of
dead air. Lambda cannot host the persistent traffic generator. EC2 is also
SSH-able at 2 a.m., which matters more than it sounds.

**CloudWatch, no Prometheus, no CloudWatch agent.** The app calls
`put_metric_data` itself. Prometheus would mean standing up Prometheus,
Alertmanager, a trained detector, and a webhook receiver; CloudWatch gives us
the metric store, a trained anomaly model, and the event bus as one resource
each. The interesting engineering is not in the collector. `SystemLoad1`
comes from `os.getloadavg()` — stdlib, so we never install the CW agent.

**A Lambda, not an EventBridge API Destination.** The datapoints and
threshold arrive nested inside `detail.state.reasonData` as an *escaped JSON
string*. An EventBridge input transformer has no JSON decode and cannot read
it. The Lambda is ~45 lines of `json` + `urllib`, zero dependencies.

**Plain CloudFormation with an inline `ZipFile`, not SAM.** No `sam build`,
no Docker, no S3 artifact bucket, no build step at all — one
`aws cloudformation deploy`. Nothing in this repo needs to be compiled or
packaged. This deletes an entire class of "it works on my laptop" failure on
a 10-hour clock.

**GitHub Actions for the agent.** It is the only place where a full repo
checkout, git history, `gh` CLI auth, and branch-push credentials all already
exist. Anywhere else we would be rebuilding all four. Nothing runs until an
alarm fires.

---

## 3. Detection: two alarms, two jobs

Both are live. Only one is wired to EventBridge.

### `Culprit-Latency-High` — the trigger (static)

```yaml
Namespace: Culprit
MetricName: RequestLatency
Statistic: Average
Period: 10                 # high-resolution; requires StorageResolution=1 on the metric
EvaluationPeriods: 2
DatapointsToAlarm: 2
Threshold: !Ref LatencyThresholdMs      # from scripts/calibrate.sh — never guessed
ComparisonOperator: GreaterThanThreshold
TreatMissingData: notBreaching
```

Fires ~30–50 s after the bad code goes live. This is the one that starts the
pipeline.

### `Culprit-Latency-Anomaly` — the ML band

```yaml
ComparisonOperator: GreaterThanUpperThreshold
ThresholdMetricId: ad1
EvaluationPeriods: 2
Metrics:
  - Id: m1
    MetricStat:
      Metric: { Namespace: Culprit, MetricName: RequestLatency }
      Period: 60           # anomaly alarms CANNOT use high-resolution periods
      Stat: Average
    ReturnData: true
  - Id: ad1
    Expression: !Sub "ANOMALY_DETECTION_BAND(m1, ${LatencySensitivity})"
    ReturnData: true
```

**Not wired to EventBridge**, deliberately. Anomaly alarms are capped at a
60-second minimum period, so this fires 60–90 s after the static one. Racing
them would produce two dispatches and two workflow runs.

Instead the band earns its place two other ways:

1. It is the left-hand widget on the dashboard — the visual that makes the
   regression obvious on stage.
2. `gather_evidence.sh` queries the band expression directly via
   `get-metric-data`, so `INCIDENT.md` contains a line like
   `expected latency band: 34–48 ms; observed: 251 ms`. The agent cites it in
   the PR body. This is deterministic and does not depend on alarm timing at
   all.

**Why have the band if a static alarm fires first?** Because the static
threshold is a number we calibrated for this one endpoint tonight, and it is
wrong the moment traffic patterns change. Nobody hand-writes an alarm at
80 ms on a 40 ms endpoint. The band learned the real distribution overnight
and flags a 6× move that no human-authored threshold would have caught. The
static alarm is there for demo speed and we say so.

### The one that will bite you

An `ANOMALY_DETECTION_BAND` needs **at least 3 hours** of data before it
produces a band (AWS recommends 3 days), and backfilling does not help —
datapoints older than 24 h take up to 48 h to become readable. Until the band
exists the alarm sits in `INSUFFICIENT_DATA`, and with
`TreatMissingData: notBreaching` it reports **OK forever and never tells you
it is broken**.

Mitigation: the traffic generator runs **all night at `SLEEP=15`**. That is
what makes the band real by morning. Do not stop it.

---

## 4. Interfaces

These four contracts are what let all four workstreams build in parallel from
hour zero. **Agree them before anyone writes code, then treat them as
frozen.** If one has to change, it gets announced in team chat, not merged
quietly.

### I1 — Metrics · W1 → W2

Namespace `Culprit`. No dimensions on any metric (dimensioned metrics do not
match dimensionless alarms — this is what broke the old dashboard). All
published at `StorageResolution: 1`.

| Metric | Unit | Role | Behaviour during the incident |
|---|---|---|---|
| `RequestLatency` | `Milliseconds` | trigger + ML band | ~40 ms → ~250 ms |
| `SystemLoad1` | `None` | corroboration | ~3× |
| `WorkUnits` | `Count` | **discriminator** | **flat** |
| `RequestCount` | `Count` | rules out traffic growth | **flat** |

### I2 — Dispatch payload · W2 → W3

`POST /repos/{owner}/{repo}/dispatches`, `event_type: "anomaly"`. GitHub caps
`client_payload` at **10 top-level properties** and 64 KB. We use 9. A sample
lives at `docs/contracts/dispatch-payload.json` so W3 can replay it via
`gh workflow run` before the Lambda exists.

```json
{
  "alarm_name":    "culprit-Latency-High",
  "state_reason":  "Threshold Crossed: 2 out of 2 datapoints ...",
  "timestamp":     "2026-08-22T09:14:02Z",
  "metric_name":   "RequestLatency",
  "namespace":     "Culprit",
  "threshold":     "95.0",
  "datapoints":    "[251.3, 248.9, 40.2, 39.8]",
  "region":        "us-east-1",
  "dashboard_url": "https://console.aws.amazon.com/cloudwatch/..."
}
```

### I3 — Verdict · W3 → the gate

The agent must write `verdict.json` at the repo root before it finishes.
Schema at `docs/contracts/verdict.schema.json`.

```json
{
  "action":            "pr",
  "url":               "https://github.com/owner/repo/pull/42",
  "suspect_sha":       "a1b2c3d",
  "confidence":        0.9,
  "metrics_that_moved": ["RequestLatency", "SystemLoad1"],
  "metrics_flat":       ["WorkUnits", "RequestCount"],
  "ruled_out":          ["e4f5g6h: refactor only, no complexity change"]
}
```

`action` is one of `pr` | `issue` | `comment`. Anything else fails the run.

### I4 — Repo layout · W4 → W1

The regression lives in `app/compute.py` and nowhere else. W1 must keep the
scoring logic in that file, in a function that can be edited in isolation, so
that W4's seeded commit is a clean single-file diff and the agent's fix is
too.

---

## 5. Workstreams

Four people, four workstreams, **strict single ownership of files**. The
number one way a hackathon project dies at 3 a.m. is two people editing the
same file in the same hour.

| | Workstream | Owns these paths, exclusively | Blocked by |
|---|---|---|---|
| **W1** | The app & the box | `app/`, EC2, systemd units, instance profile | nothing |
| **W2** | Detection & dispatch | `template.yaml`, `scripts/calibrate.sh`, `scripts/deploy.sh` | W1 for calibration only |
| **W3** | The agent | `.github/`, `scripts/gather_evidence.sh`, `scripts/test_dispatch.sh` | nothing |
| **W4** | The incident & the demo | `scripts/seed_incident.sh`, the runbook, rehearsals | W1 |

### On your proposed split

You suggested: (1) bad app, (2) metric collection, (3) anomaly detection and
alerting, (4) claude fix. Three of those are right. Four notes:

**"Metric collection" is not a workstream.** It is about ten lines inside the
app — `boto3.client("cloudwatch").put_metric_data(...)` in the same request
handler that measures the latency. Splitting it out puts two people in
`app/main.py` at the same time for no benefit. It folds into W1.

**Nobody owned the box.** The EC2 instance, its instance profile, the three
systemd units, and the 20-second git-poll redeploy loop. Without that there
is no "push → live," and "push → live" is the spine of the demo. Folded into
W1, since it is the same person's mental model.

**Nobody owned the glue.** The EventBridge rule, the dispatch Lambda, and the
`repository_dispatch` call sit exactly on the seam between "alerting" and
"claude fix". Unowned seams are where these projects die. Explicitly W2, and
W2 also owns the smoke test that proves it.

**Nobody owned the demo.** The four seeded commits, the threshold
calibration, two full rehearsals, the runbook, the fallbacks, and the backup
recording. That is a full night's work, and it is the only part the judges
actually see. That is W4.

So the recut is the same headcount and the same four boxes, with metric
collection absorbed and the demo promoted.

### Critical path

```
W3 ────────────────────────────────────────►  (zero AWS dependencies)
W1 ──────────────►
                  W2 ──────────►
                                 W4 ──────────►
```

**W3 starts first and finishes first.** It has no AWS dependencies whatsoever
and it covers the highest-uncertainty external integration in the system
(GitHub PAT scopes, `repository_dispatch` semantics, the action's behaviour).
If that chain is broken you want to know at 19:50 with the whole night ahead,
not at 08:30.

**W1 gates W2**: you cannot calibrate a threshold against a metric that does
not exist yet.

---

## 6. Build order

Times assume a 19:00 start and a demo the following morning. Adjust the
offsets, keep the order.

| Time | Who | Task | Done when |
|---|---|---|---|
| 19:00–19:15 | **all** | Prereq check on every laptop: `aws`, `gh`, `jq`, `python3`, `git`, then `aws sts get-caller-identity`. **W2 owns distributing AWS credentials and must finish inside these 15 minutes.** Pin the region in team chat. Confirm `main` has no branch protection. | four people print the same account ID |
| 19:15–19:50 | W3 | Mint a **classic** PAT — scopes `repo` + `workflow`, **1-day expiry**, from a named human's account, and write whose into the secret description. Set repo secrets `PAT_TOKEN`, `ANTHROPIC_API_KEY`, and the read-only `AWS_READONLY_*` pair. Check the Anthropic key has credit *now*. **Push `anomaly-response.yml` to `main`** — `repository_dispatch` only fires workflows already on the default branch. | workflow appears in the Actions tab |
| 19:50–20:00 | W3 | `scripts/test_dispatch.sh` — curl only, no AWS. Proves PAT scopes, branch create/delete, issue open/close, dispatch, workflow registration. | `POST /dispatches` returns **204** |
| 20:00–20:45 | W3 | Iterate the prompt and the verify gate using `gh workflow run` against `docs/contracts/dispatch-payload.json` and a hand-made fake diff, until the PR path is reliable. Then a no-cause payload until the issue path is reliable. | both paths return a real URL and the verify step is green |
| 19:20–20:30 | W1 | `app/main.py`, `app/compute.py`, `app/traffic.py`. Run locally, confirm baseline p50 is 30–60 ms. | `aws cloudwatch list-metrics --namespace Culprit` shows 4 |
| 19:20–20:30 | W2 | Write `template.yaml`, `deploy.sh`, `calibrate.sh`. Cannot deploy yet — the threshold is unknown. | `aws cloudformation validate-template` passes |
| 20:30–21:30 | W1 | Launch EC2 with the instance profile and user data. SSH in, confirm all three systemd units are green. **Start the overnight generator at `SLEEP=15`.** | metrics arriving from the box, not from a laptop |
| 21:30–21:45 | W2 | `scripts/calibrate.sh` against ≥20 minutes of real traffic. | a threshold derived from observed max × 2, not guessed |
| 21:45–22:15 | W2 | First `aws cloudformation deploy`. Smoke test **in this order**: ① `aws cloudwatch set-alarm-state --alarm-name culprit-Latency-High --state-value ALARM --state-reason smoke` — this isolates *is the wiring connected* from *did the threshold fire*. ② only then a real spike. | a PR exists that nobody wrote |
| 22:15–23:00 | W4 | `scripts/seed_incident.sh`. **Verify the culprit actually moves p50 above 200 ms before trusting it.** Write the runbook. | before/after latency measured and written down |
| 22:15–23:00 | all | **Reserved debug buffer. Do not schedule over it.** | — |
| 23:00–23:45 | all | **Rehearsal 1, stopwatched.** Record elapsed time at each stage: push → redeploy → metric moves → ALARM → Actions starts → PR exists. | six timestamps written down |
| 23:45–00:30 | all | Fix whatever rehearsal 1 broke. | — |
| 00:30–01:15 | all | **Rehearsal 2**, including the issue path and the `workflow_dispatch` fallback. **Record the screen capture during this run** — it is the last-resort backup and it must exist. | an `.mp4` on two laptops |
| 01:15–01:45 | W4 | Reset: revert `main`, close rehearsal PRs and issues, traffic back to `SLEEP=15`, confirm the band is drawing. **Do not stop the EC2 instance** — the public IP will change. | dashboard shows a band, repo is clean |
| 01:45–07:30 | — | Sleep. Traffic keeps running. This is what trains the band. | — |
| 07:30–08:15 | all | `test_dispatch.sh` again. EC2 IP unchanged, all units green. **Re-run `calibrate.sh`** — overnight drift may have moved the baseline. One full rehearsal. | green end to end |
| 08:15–08:45 | W4 | Stage setup: disable laptop sleep and screen lock, dashboard auto-refresh to 10 s, four tabs open, traffic to `SLEEP=2`, backup recording open in a tab. | tabs open, screensaver off |

---

## 7. Demo runbook

Five-minute slot. The pipeline takes ~4 of them. **So start it before you
start talking.** That single decision is what makes a real, unfaked,
end-to-end run fit in the slot.

### Setup, T minus 15 seconds

Four tabs: CloudWatch dashboard on 10-second auto-refresh · GitHub Actions ·
the repo's Pull requests tab · a terminal where `seed_incident.sh` has
already run and `git push origin main` is typed but **not entered**.

| T | Do | Say |
|---|---|---|
| 0:00 | **press enter on the push** | "I just pushed four commits to a production service. One of them is about to make it six times slower. Nobody in this room knows which one." |
| 0:15 | dashboard | "This is a scoring API on EC2. Every request publishes four metrics: latency, system load, request count, and units of work done." |
| 0:35 | point at the band widget | "CloudWatch has been learning what this endpoint's latency looks like since nine last night. That grey band is the model." |
| 1:00 | — | "The box polls git every twenty seconds. It just picked up the push." |
| 1:20 | latency breaks out of the band; alarm chip goes red | "There it goes." |
| 1:35 | **the pivot** — point at `WorkUnits`, then `RequestCount` | "Work units are flat. Request count is flat. We are not doing more work and we are not serving more traffic — the same work now costs six times more. That is not load. That is complexity. Something in the code got worse." |
| 1:55 | switch to Actions, a run is live | "EventBridge caught the alarm state change, a forty-line Lambda posted a repository_dispatch, and a real Claude Code session is now running against a full checkout of this repo. It has the git history, the metric evidence, and read-only CloudWatch credentials to go pull more." |
| 2:20 | expand the live step | "It is reading the incident briefing, running git log, querying CloudWatch for before and after." |
| 3:30 | **PR appears** — open it, read the body | It names the alarm, names which metrics moved and which stayed flat, names the suspect SHA, and rules out the other three. |
| 4:00 | show the diff | "One file, one word. A list where it should have been a set — an accidentally quadratic dedupe." |
| 4:15 | **the payoff** — back to the commit list | "Three other commits landed in the same window. One of them says 'refactor the scoring loop' and touches the same file. It picked the right one, from the telemetry, not from the commit message." |
| 4:35 | show a pre-made triage issue | "And when there is no repo-side cause, it does not guess — it files a triage issue saying what it ruled out. That branch is a deterministic check in the workflow, not a hope." |
| 4:50 | close | "This bug passes every unit test, passes code review, and returns correct results. It is only visible in production telemetry. That is the class of bug this closes the loop on — from an anomaly to a reviewable diff, with no human in between." |

### Fallbacks, in order

1. **The alarm does not fire in time.** In a second terminal, already typed:
   `aws cloudwatch set-alarm-state --alarm-name culprit-Latency-High --state-value ALARM --state-reason demo`.
   Identical downstream path — 100% of the pipeline is still live.
2. **AWS is unreachable entirely.**
   `gh workflow run anomaly-response.yml -f alarm_name=... -f datapoints=...`
   Still produces a real PR in about 90 seconds. *A recording cannot be
   questioned; a live manual trigger can.* Always prefer this over the video.
3. **Nothing works.** Play the rehearsal-2 recording, and say plainly that
   you are playing a recording.

---

## 8. Questions you will get

**"Isn't your detection just a static threshold with 'anomaly' in the name?"**
Both are live. The static one triggers because CloudWatch anomaly alarms are
capped at a 60-second period and a five-minute demo cannot wait on it. The
trained band is on the same metric, it is on the dashboard, and the agent
queries it directly and cites the expected range in the PR body. The static
threshold is a number we calibrated for this endpoint tonight; the band
learned the distribution and would still be right next week.

**"How is this different from AWS DevOps Agent or CloudWatch Investigations?"**
Those reason over AWS telemetry and produce findings. Neither has repository
context. Our output is a diff, not a finding, and the correlation we make is
metric-to-commit.

**"Would you auto-merge this?"** No, deliberately. The output is a PR with
evidence and the human decision stays human. What we removed is the twenty
minutes of correlating a dashboard against a git log at 3 a.m.

**"What if it picks the wrong commit?"** It has to pass a deterministic gate:
`verdict.json` must exist and resolve to a real PR or issue URL or the
workflow fails loudly. And you just watched it beat three decoys, one of
which touches the same file and sounds more dangerous.

**"Why not Prometheus?"** We would have stood up Prometheus, Alertmanager, a
trained detector, and a webhook receiver. CloudWatch gives us the metric
store, the trained model, and the event bus as one resource each. The
interesting engineering is not in the collector.

**"What does it cost to run?"** Four custom metrics, two alarms, one Lambda
invocation per incident, one Actions runner-minute per incident. The only
meaningful cost is the agent session, and it only runs when an alarm fires.

**"The agent has write access to your repo."** A classic PAT scoped to one
repo with a one-day expiry, which only ever pushes to `auto-fix/*` branches
and never merges. The whole run is in the Actions log. In production that is
a GitHub App with branch-scoped permissions.

---

## 9. Known weaknesses — say these before a judge finds them

- The PAT lives in a Lambda environment variable, which renders in plaintext
  in the console. Mitigated by a one-day expiry, not by encryption. The
  production answer is an SSM SecureString.
- The static threshold is one calibrated number from ~20 minutes of baseline
  plus an overnight re-check. It is not a learned model. The band is.
- The fix-versus-issue decision is model judgement. The gate verifies that
  *an artifact exists and resolves*, not that the judgement was correct.
- End-to-end latency is 3.5–4.5 minutes. That is real, and it is why the demo
  starts before the talking does.
- The agent is scoped to single-file fixes. Multi-file causes fall to the
  issue path, by design.

---

## 10. Findings on the code already in the repo

Nothing here has been deleted — this section is a review, not a changelog.
Each item is a defect found in the existing scaffolding while designing the
above. **They are listed so the owner of each path can decide what to do**,
not so anyone else edits their files.

### Blocking — these fail at runtime, not at review time

| Where | Finding |
|---|---|
| `template.yaml:24` | `BedrockModelId` defaults to `anthropic.claude-3-5-sonnet-20241022-v2:0`, which **AWS retired on 2025-10-28**. The first real invoke throws. Needs a current model ID, or Bedrock drops out entirely if the agent moves to Actions (§2). |
| `template.yaml:111,126` | The anomaly detector and alarm watch namespace `HackathonDemo`, which has never received a datapoint. No datapoints → no trained band → the alarm sits in `INSUFFICIENT_DATA`; combined with `TreatMissingData: notBreaching` on line 143 it reports **OK forever and never signals that it is broken**. This is the single most dangerous item in the file, because it fails silently. |
| `lambda/git_context.py:33,39` | `/commits?until={alarm_ts}&per_page=1` returns the newest commit *at or before* the alarm — which is the bad one — and it is labelled `good_sha`. It is then fed to `compare/{good_sha}...HEAD`, so the suspect becomes the baseline and **the diff comes back empty**. Needs a window that starts before the incident (e.g. `since=alarm_ts - 2h`). |
| `lambda/handler.py:45` | Early-returns on non-`ALARM` state, but the alarm has both `AlarmActions` and `OKActions` pointing at the same topic (`template.yaml:140-143`), so every recovery invokes it too. Either drop `OKActions` or make the guard deliberate. |

### Should fix before the demo

| Where | Finding |
|---|---|
| `template.yaml:100-105` | `DashboardWidgetInvokePermission` grants `lambda:InvokeFunction` to `cloudwatch.amazonaws.com` with **no `SourceAccount` or `SourceArn`** — any CloudWatch principal can invoke it. Separately, a Lambda-backed custom widget makes **every dashboard viewer click through an IAM trust prompt** before it renders. On stage that dialog is the demo. |
| `template.yaml:172-174` | The dashboard references `ErrorRate`, `mem_used_percent`, and `disk_used_percent` **without dimensions**, while the CloudWatch agent publishes the latter two *with* host/path/device dimensions. Those widgets render empty. `ErrorRate` has no publisher at all. |
| `infra/cloudwatch-agent/config.json` | Collects only `mem` and `disk`. There is **no `cpu` block**, yet the dashboard and the design both assume CPU. Also at 60s interval, which is too coarse to see a spike inside a 5-minute demo. |
| `lambda/notify.py:39` | Persists the verdict by calling `UpdateFunctionConfiguration` **on its own function**. That races concurrent invokes, forces a cold start on every write, and the env filter can silently drop later variables. State belongs in S3, DynamoDB, or the PR body itself. |

### Design-level, for the team to decide together

These are not bugs — they are the choices §2 argues against. Listed so the
disagreement is explicit rather than discovered at 3 a.m.

| Current | The case for changing it |
|---|---|
| SNS topic + email subscription | Email is not a demoable artifact and cannot be clicked on stage. EventBridge → Lambda → `repository_dispatch` produces a PR instead, and a PR is the whole payoff (§2). |
| `infra/demo-app/bad_app.py` + `scripts/trigger_chaos.sh` — a chaos flag that toggles slowness | A toggle is a switch, not a regression, and a judge will say so. §1's accidentally-quadratic commit is a real bug that passes every test and is only visible in telemetry — which is the entire argument for the project. This is the change that most affects how the demo lands. |
| SAM (`samconfig.toml`, `scripts/deploy.sh`) | `sam build` needs Docker and an S3 artifact bucket. Plain CFN with an inline `ZipFile` handler needs neither — one `aws cloudformation deploy`, no build step, on a 10-hour clock. |
| Bedrock invoked from a Lambda | A Lambda has no repo checkout, no git history, no `gh` auth, and no push credentials. GitHub Actions has all four already (§2). |

**The one thing to change regardless of everything else:** the original build
order had the pull request as step 9, *"stretch — only if time remains."* The
PR is the demo. It is the only clickable, verifiable artifact the project
produces. In §6 it is the **first** thing de-risked, at 19:50, before a single
AWS resource exists.
