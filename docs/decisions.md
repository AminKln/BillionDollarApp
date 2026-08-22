# Decisions — what we keep, what we drop, who does what

Written 2026-08-22, ~2h before the demo. Three people built overlapping
pieces without knowing it. This file picks one of each and says why, so
nobody spends the last two hours reconciling.

Read time: 4 minutes. If you only read one section, read §1 — it describes a
bug that made the demo impossible to trigger, and the fix.

---

## 1. The trigger was broken. It is fixed. This is the important one.

The bad app is `Tehreem404/bad_app_demo`, running in Docker on
`i-091814f7a41456cb0` (`54.205.9.164:8000`). Its source says two things we
had wrong:

**a) There is no `/chaos/<kind>/<state>` HTTP route.** The app's entire
surface is `/`, `/healthz` and `/chaos/status`. `curl .../chaos/latency/on`
returns **404** against the live box — verified. Chaos state lives in **SSM
Parameter Store**, and the app re-reads all three parameters *on every single
request*, which is genuinely nice: flipping a parameter takes effect on the
next request with no redeploy, no restart and no SSH.

`scripts/trigger_chaos.sh` curls the routes that do not exist. It has never
worked against this app. It is left in place (it is not W2's file) but
`scripts/chaos.sh` supersedes it.

**b) The three parameters did not exist.** `aws ssm describe-parameters`
returned **zero parameters in the whole account**. `_get_chaos_state()` does
`values.get(NAME, "off")`, and `get_parameters` returns absent names under
`InvalidParameters` rather than raising — so every missing parameter silently
read as `off`. The bad app was **permanently, unfixably healthy**. There was
no way to make it go bad, so the demo had no trigger at all.

Fixed: `make app-bootstrap` creates all three at `off`. Already run — the
parameters exist now, and flipping `cpu=on` was confirmed end to end:
`/chaos/status` flipped to `{"cpu":true}` and a request went **0.05s →
2.15s**. The trigger is real.

## 2. Use the `cpu` knob, not the `latency` knob

```python
if chaos["latency"]:  time.sleep(random.uniform(1.5, 3.0))   # sleeps
if chaos["cpu"]:      _burn_cpu()                            # 2s of sha256
```

Both push `RequestLatency` past the 0.5s threshold, so both fire the alarm.
They are not equivalent as *evidence*:

- `latency=on` is a bare `sleep`. The box does no work. `cpu_usage_active`
  stays flat. The agent sees "requests got slower" and nothing else — which
  is indistinguishable from a slow downstream dependency, and is an alert,
  not a diagnosis.
- `cpu=on` burns real CPU. Latency rises the same ~40x **and**
  `cpu_usage_active` climbs on `HackathonDemo/System`. Same endpoint, same
  request, now burning CPU it did not burn before. That is a code regression
  with host-level corroboration.

`make app-regress` flips `cpu`. This is the single highest-leverage decision
in the file: it is the difference between the demo showing an alert and the
demo showing a diagnosis.

## 3. Drop the `WorkUnits` / "traffic stayed flat" narrative

The story we had been telling was "same work, same traffic, now costs more,"
carried by `WorkUnits` and `RequestCount` staying flat. **That story does not
survive contact with the real app**, for two independent reasons:

1. The app publishes only `RequestLatency` and `ErrorRate`. There is no
   `WorkUnits`, and there is no meaningful thing for it to measure — every
   request to `/` does identical logical work, so `WorkUnits` would just be
   `RequestCount` with extra steps.
2. Worse, `RequestCount` **cannot** stay flat. `_generate_traffic()` is a
   *closed* loop — one thread that blocks on each request, then sleeps 0.2s.
   Measured baseline is ~237 samples/min (~4 req/s). Under `cpu=on` each
   request takes ~2.2s, so throughput *collapses* to ~27/min. Traffic does
   not hold steady; it drops 9x. Claiming otherwise on stage would be a lie
   the dashboard visibly contradicts.

Making it true would mean rewriting the traffic generator to be open-loop and
inventing a `WorkUnits` metric — a change to someone else's app, 2h out, to
support a narrative we do not need.

**The real-app discriminator, which is true and provable:**

> `RequestLatency` up ~40x · `cpu_usage_active` up · `ErrorRate` flat at 0.

Latency and CPU moved together while the error rate never budged. The
endpoint is not failing and it is not being hammered — it got *expensive*.
That is a code change. It is a weaker claim than "traffic was flat," and it
is the one the data actually supports.

## 4. Keep the synthetic feed running, demote it in the story

`scripts/seed_metrics.py` fabricates metrics with `random.lognormvariate` —
no HTTP, no app, no work. Per "everything should be based on the actual bad
app," **the demo runs on the real app.** The synthetic feed is not deleted
today for two reasons: it is what trained `culprit-Latency-Anomaly` (the only
anomaly band with enough history to be useful), and it is a live fallback if
the shared EC2 box dies mid-demo. It stays running, and the docs describe it
as the fallback rig rather than the story. Delete it after the demo.

## 5. Three teams built three "alarm → LLM" pipelines. Keep one.

| | Path | Verdict |
|---|---|---|
| **W2 (this repo, deployed)** | EventBridge → Lambda → `repository_dispatch` → Actions → `claude-code-action` | **Keep.** The only one that can open a PR, and the demo's payoff *is* a PR. |
| **`feature/LLM_diagnosis`** (JSnelgrove) | SNS → Lambda → `context_builder` → Anthropic SDK → prints text | Drop the transport, keep two ideas (below). |
| **`feature/PRContext`** (DENIS) | old SAM `lambda/` → Bedrock → SNS notify | Drop for the demo. See below. |

**From `feature/LLM_diagnosis`, worth taking:**
- `llm_agent.py`'s **forced tool use** for structured output (`REPORT_TOOL`
  with `{hypothesis, confidence, supporting_evidence[]}`). W3 should use this
  shape for the PR body — it is strictly better than free-form prose. **Hand
  this to W3 now.**
- `fixtures.py`'s **botocore `Stubber`** pattern, so the agent can be
  developed offline with no AWS. Good engineering. Post-demo.
- `get_log_evidence()` (Logs Insights: error samples + error-count-by-minute).
  Genuinely adds something our metric SEARCH cannot — the *shape* of a spike.
  **Not for today**: the app runs in Docker with no confirmed CloudWatch log
  group, and Insights queries take 5–30s inside a Lambda that must return
  fast. Post-demo.

**Not taking:** `fixtures.py`'s *scenarios* target an ELB / .NET /
`Npgsql.NpgsqlException` DB-timeout incident. That is a different demo. The
prompt tuned for it will not reason well about ours.

**`feature/PRContext` — drop for the demo, two reasons.** It extends the old
SAM `lambda/` stack, which is not what is deployed. And it adds
`get_recent_prs`, while our culprit lands as a **direct push to main** (see
§6) — there is no PR for it to find. It also does not fix the documented
`good_sha` bug in `git_context.py` (it labels the newest commit at-or-before
the alarm — i.e. the culprit itself — as the baseline, so the diff comes back
empty). Fix that bug before this branch is worth anything.

## 6. ⚠️ The dispatch points at the wrong repository — W3/W4 must act

The Lambda dispatches to **`AminKln/BillionDollarApp`** (stack parameters
`GithubOwner=AminKln`, `GithubRepo=BillionDollarApp`). But the culprit commit
will land in **`Tehreem404/bad_app_demo`** — that is the repo with the
push-to-main → EC2 auto-deploy workflow, and it is the only repo whose code
can affect the metrics.

As it stands the agent would wake up and diff a repository the regression is
not in, and find nothing.

**Fix (cheapest, no Lambda redeploy, no contract change):** W3's
`anomaly-response.yml` lives in `BillionDollarApp` — where the dispatch
arrives — but **checks out `Tehreem404/bad_app_demo` and opens the PR there.**

The `client_payload` cannot carry the app repo: it is at GitHub's hard limit
of **10 top-level properties**. Hardcode `Tehreem404/bad_app_demo` in the
workflow. One app, one hackathon, zero coordination cost.

W4 also needs a **`PAT_TOKEN` with write access to `bad_app_demo`**, not just
to this repo.

**Also for W4:** `deploy.yml` there runs `cd ~/bad_app_demo && ./deploy.sh`,
and **`deploy.sh` is not in the repo** — it exists only on the box. The
auto-deploy pipeline depends on an untracked file. Do not break it, and know
that it cannot be reviewed or reconstructed from git.

---

## 7. One regression fires up to two dispatches — W3 must be idempotent

Found in rehearsal #2, so it is a fact and not a worry.

`culprit-App-Anomaly` (the ML band) and `culprit-App-High` (the static 0.5s
threshold) watch the same metric. The Lambda already dedupes, but only in one
direction: an `-Anomaly` event is dropped if its `-High` twin is **already** in
ALARM. In rehearsal #2 the band tripped **5 seconds before** the threshold —
`Anomaly` at 17:20:28, `High` at 17:20:33 — so the dedup had nothing to check
against and **both dispatched**.

**Superseded by §8 — fixed at the alarm layer instead.** The original decision
was to leave it and make the consumer idempotent, because the Lambda's inline
source is at **4095 of CloudFormation's 4096-byte limit** and there is no room
to extend the dedup. But §8 had to retune the anomaly alarm anyway, and raising
it to 3-of-3 at a 60s period means it now needs **three minutes** to fire while
`culprit-App-High` is 1-of-1 and fires in **one**. The anomaly alarm can no
longer win the race, so the Lambda's existing "drop -Anomaly if -High is already
ALARM" dedup works as designed and one regression produces one dispatch. Cost:
zero bytes — alarm properties live outside `ZipFile`.

**W3 should still be idempotent** — belt and braces, and it is two lines of
`concurrency:`. But it is no longer load-bearing.

**W3:** dedupe on `metric_name` + a short window (60s is plenty), or make the
PR/issue creation idempotent on the alarm's `timestamp`. Documented with the
real capture in `contracts/dispatch-payload.json`.

---

## Who does what, next two hours

- **W2 (done):** trigger fixed and proven, alarms live, Lambda dispatching.
  Remaining: a real `GITHUB_TOKEN` on the stack (one `make deploy`).
- **W3:** point the workflow at `Tehreem404/bad_app_demo` (§6); use the
  forced-tool-use report shape (§5); **dedupe double dispatches** (§7); and
  parse `evidence` keys by suffix, not exact match — they are fully-qualified
  CloudWatch labels now (`contracts/dispatch-payload.json`).
- **W4:** plant the culprit commit in `bad_app_demo`, not here; mint a PAT
  that can write there (§6).
- **Everyone:** the demo trigger is `make app-regress` (cpu knob). Always
  finish with `make app-recover` — it is a shared box.

---

## 8. The anomaly band was firing on healthy traffic. Fixed.

Found while answering "does the ML arm have enough baseline yet?" The answer
turned out to be worse than "not yet."

`culprit-App-Anomaly` was **in ALARM with no chaos running**, and had flapped
OK→ALARM **four times in sixteen minutes**. It had already fired **three
spurious dispatches**, whose own evidence block read `RequestLatency 0.053 ->
0.056 flat`. With a `GITHUB_TOKEN` on the stack that is three junk PRs in eight
minutes, discovered on stage.

**Why.** The healthy app is *too stable*. `RequestLatency` Average sits in
0.051–0.054s, so 1σ ≈ 0.0015. At the configured `ANOMALY_DETECTION_BAND(m2, 2)`
the band was only ±0.003 wide. The model's mean (~0.049) was learned from
early-morning traffic while the live baseline had drifted up ~5% — and 5% drift
is more than a ±6% band can absorb. **The band top sat below live healthy
traffic**, so healthy traffic read as anomalous, permanently.

Measured band tops against the observed baseline:

| width | band top | vs healthy peak 0.054 |
|---|---|---|
| 2 | 0.052 | **breached continuously** |
| 4 | 0.055 | marginal |
| 6 | 0.058 | ok |
| **8** | **0.061** | **~13% headroom** ← chosen |
| 10 | 0.064 | ok |

**Widening costs nothing in detection.** The regression averages ~2.06s, which
is ~32× the width-8 band top. There is no width in this table that would miss
it. The band is not what catches the regression — the 0.5s static threshold is,
and it fires in one minute.

Also raised both anomaly alarms to **3-of-3** datapoints, which kills the
flapping and (see §7) makes the double dispatch structurally impossible.

**Narrow it again only when the detectors leave `TRAINED_INSUFFICIENT_DATA`.**
Both still report that state — CloudWatch wants days of history and has hours.

## 9. Three of the six alarms were watching fabricated data

`culprit-Latency-High`, `culprit-Latency-Anomaly` and `culprit-Load-High` are on
the **`Culprit`** namespace, which is `scripts/seed_metrics.py` generating
numbers with `random.lognormvariate`. No app, no HTTP, no work. Only
`culprit-App-High`, `culprit-App-Anomaly` and `culprit-App-ErrorRate` are on
`HackathonDemo`, the real app's own `put_metric_data`.

All four dispatch-eligible alarms in the EventBridge rule used to include the
two synthetic ones. **They are now excluded from the rule**, and
`culprit-App-ErrorRate` was added in their place. The synthetic alarms stay
deployed and stay on the dashboard as a fallback rig if the shared EC2 box dies
mid-demo — but they can no longer reach the agent. A PR written about invented
latency is worse than no PR.
