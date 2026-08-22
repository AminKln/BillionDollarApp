# W2 — detection & dispatch

**Status: built, deployed, and watching the real EC2 app — and nothing else.**
Everything from `put_metric_data` to `repository_dispatch` is real AWS
infrastructure.

> **Read this before the rest of the file.** W2 was built against a synthetic
> publisher (`scripts/seed_metrics.py`, namespace `Culprit`) while W1's app was
> still being written. That publisher, its three alarms, its anomaly detector
> and its dashboard widgets were **deleted on 2026-08-22** — two of those alarms
> were dispatch-eligible, meaning the agent could have been handed a regression
> that never happened (`decisions.md` §9). Sections below that describe "two
> feeds", `make metrics` / `make regress` / `make recover`, `culprit-Latency-*`
> alarms, or `WorkUnits` / `RequestCount` are **historical**. They are kept
> because the two-feeds design is *why* the Lambda reads namespace, unit and
> period off the event instead of hardcoding them — which is what made deleting
> half the inputs a zero-line change. The live surface is: namespace
> `HackathonDemo` (`RequestLatency`, `ErrorRate`, dimension `App=bad-app-ec2`)
> plus `HackathonDemo/System`, and alarms `culprit-App-High`,
> `culprit-App-Anomaly`, `culprit-App-ErrorRate`.

The plan this implements is [`architecture.md`](architecture.md) §3 and §4.

---

## Why it could be built first

CloudWatch has no idea where a datapoint came from. A `put_metric_data` call
from W1's Flask app on EC2 and one from `scripts/seed_metrics.py` on a laptop
are byte-identical by the time an alarm evaluates them. So as long as the
synthetic publisher honours the I1 contract *exactly* — same namespace, same
metric names, same units, no dimensions, same storage resolution — every alarm,
rule, Lambda and dashboard downstream is being built against production
conditions, not a mock.

That prediction held. When the real app appeared, adding it cost four
CloudFormation resources and **zero changes to the Lambda, the EventBridge
rule's logic, or the dispatch contract** — the sections below on convention-based
deduplication and event-derived metric identity are why.

Three things did have to be got right, and each one fails silently if you get
it wrong:

| | synthetic feed | real EC2 app |
|---|---|---|
| Namespace | `Culprit` | `HackathonDemo` |
| Dimensions | none | `App=bad-app-ec2` |
| Latency unit | **milliseconds** (~40) | **seconds** (~0.05) |
| Resolution | high (10s) | standard (60s) |

An alarm that omits the dimension matches no metric at all and sits in
`INSUFFICIENT_DATA` forever — green-adjacent, watching nothing. A threshold
copied across the unit boundary is off by 1000x and can never fire. Neither
mistake announces itself. The check that catches both is a one-liner: does the
alarm's `StateReason` quote a real number from the real metric? `culprit-App-High`
reads `0.0496 not greater than 0.5` — that sentence is the proof it is bound.

---

## What is running

```
W1's Flask app on EC2                    scripts/seed_metrics.py
i-091814f7a41456cb0                      the high-resolution rehearsal feed
  │  RequestLatency (Seconds)              │  put_metric_data every 10s,
  │  ErrorRate (Percent)                   │  StorageResolution=1
  │  every 10s, App=bad-app-ec2            │
  │  + CloudWatch agent: cpu/mem/disk      │
  ▼                                        ▼
CloudWatch "HackathonDemo"          CloudWatch "Culprit"
  │  + "HackathonDemo/System"        │   RequestLatency · SystemLoad1
  │                                  │   WorkUnits · RequestCount
  ├─► culprit-App-High      ──┐      ├─► culprit-Latency-High    ──┐
  │   static, 60s, 1/1        │      │   static, 10s, 2/2          │
  ├─► culprit-App-Anomaly   ──┤      ├─► culprit-Latency-Anomaly ──┤
  │   ML band, 60s            │      │   ML band, 60s              │
  └─► culprit-App-ErrorRate   │      └─► culprit-Load-High         │
      corroboration, 60s 1/1  │          corroboration, 10s 3/3    │
      (not wired to dispatch) │          (not wired to dispatch)   │
                              │                                    │
                              └────────────┬───────────────────────┘
                                           │
                                 ├─► SNS topic ─► email (optional)
                                 │
                                 ▼  "CloudWatch Alarm State Change"
                         EventBridge  culprit-alarm-state-change
                                 │     filtered: these 4 alarms, ALARM only
                                 ▼
                         Lambda  culprit-dispatch
                                 │  decodes state.reasonData (a nested JSON
                                 │  *string* — an Input Transformer cannot
                                 │  parse it, which is why this is code);
                                 │  reads namespace/metric/period from
                                 │  detail.configuration, so ONE function
                                 │  serves every alarm on either feed;
                                 │  suppresses the -Anomaly twin of a
                                 │  -High alarm already in ALARM
                                 ▼
                    POST /repos/{owner}/{repo}/dispatches   -> 204
                                 │
                                 ▼
                    GitHub Actions   (W3's agent; W2 ships a receipt job)
```

## Two kinds of alarm, on purpose

Each feed gets a matched pair — a static threshold and an ML band over the same
metric — and both halves of both pairs dispatch.

|  | `-High` (static) | `-Anomaly` (ML band) |
|---|---|---|
| Kind | fixed threshold | `ANOMALY_DETECTION_BAND` (AWS built-in) |
| Period | 10s synthetic / 60s app | **60s** — forced |
| Fires in | ~20–60 seconds | minutes |
| Job | trigger the demo, reliably | catch the regression nobody picked a number for |

The static alarm is the one the demo is timed around, because it is the one
that fires inside a sentence. The band is the one that justifies the project:
a threshold only catches the failure you already imagined, and the whole claim
here is that the system catches regressions it was not told to look for. Both
dispatch, and the section below is what stops that from meaning two pull
requests for one bug.

Two AWS constraints drive this split, and both are worth stating out loud when
someone asks why we didn't just use the ML alarm:

1. **Anomaly-detection alarms cannot use high-resolution periods.** Period 10
   is rejected outright; 60 is the floor. That alone puts the ML alarm minutes
   behind the static one.
2. **The band needs 3+ hours of data before it exists** (AWS recommends three
   days). Backfilling does not help — points older than 24h take up to 48h to
   become readable. Until then the alarm sits in `INSUFFICIENT_DATA`.

Hence `TreatMissingData: missing` on the anomaly alarm rather than
`notBreaching`. With `notBreaching` an untrained alarm reports **OK forever**
and looks perfectly healthy while being completely blind. `missing` makes the
untrained state visible. That is a deliberate choice, not a default.

### The third kind: corroboration alarms

`culprit-Load-High` and `culprit-App-ErrorRate` are deliberately **not** on the
EventBridge rule. They exist to be *looked at* — on the dashboard, next to the
alarm that did fire — not to trigger anything. Latency is the story; wiring
these would just mean two dispatches for one incident.

Because they are read by a human mid-incident, their timing has to be right,
and `culprit-Load-High` originally got that wrong in a way worth recording. It
was `Period: 60`, 2-of-2 — 120 seconds minimum — sitting on a feed that
publishes every 10 seconds. In a measured regression it went red at **15:20:24
for an incident that had already recovered at 15:20:04**. It was not broken;
it was corroborating an incident that was over, which on a dashboard reads as
noise rather than evidence. It now runs `Period: 10`, 3-of-3 — 30 seconds,
landing just *after* the latency alarm's 20s, which is the correct order for
something whose job is to confirm. Three points rather than two because
`SystemLoad1` is a one-minute load average: already smoothed, so a real
crossing sustains and only a spike needs filtering.

The general lesson: an alarm on a high-resolution feed inherits nothing from
that resolution. Period is per-alarm, and a 60-second period silently discards
five of every six datapoints you are already paying to publish.

### One incident, one dispatch — across both detectors

A latency regression trips the static alarm in ~35 seconds and the band a
minute or two later, once its 60-second periods accumulate. Left alone that is
two EventBridge events, two Lambda runs, two `repository_dispatch` calls, and
two Claude sessions opening two pull requests for one bug.

The suppression is a naming convention and nothing else. Every alarm is named
`culprit-<feed>-High` or `culprit-<feed>-Anomaly`. An `-Anomaly` alarm rewrites
its own name to `-High`, calls `DescribeAlarms` on the result, and drops itself
if that twin is already in `ALARM`:

```python
primary = name.replace("Anomaly", "High")
if primary != name and dup(primary, region):
    return   # the threshold already reported this incident
```

The convention *is* the mechanism, which is the point: there is no environment
variable naming a primary alarm, no state store, no timestamp window. Adding
the EC2 app's pair required no change to this code at all — `culprit-App-Anomaly`
resolved to `culprit-App-High` for free. The check is a live read rather than a
cached one because the twin sits in `ALARM` for exactly as long as the incident
lasts, which is precisely the window worth suppressing. And when the band fires
on something the threshold never catches — the entire reason the band exists —
the twin reads `OK` and the dispatch goes through.

Verified: forcing `culprit-App-High` to `ALARM` and then `culprit-App-Anomaly`
20 seconds later produced exactly **one** dispatch and one log line,
`dup: culprit-App-High`.

### The other duplicate: flapping

The static alarms use `TreatMissingData: ignore`, and that is load-bearing.

The publisher emits every ~10s into a 10s period, so period boundaries
routinely land between two samples and that period genuinely has no datapoint.
`notBreaching` — the obvious choice, and what this alarm shipped with first —
scores every one of those gaps as healthy. Measured, not theorised: a single
regression drove **three** OK→ALARM transitions in 25 seconds, which is three
EventBridge events, three Lambda invocations, three `repository_dispatch`
calls, and on demo day three Claude runs opening three pull requests for one
bug. The alarm was flapping, not detecting.

`ignore` holds the current state across a gap, so one incident is one alarm is
one dispatch. It also refuses to call a dead publisher healthy — with
`notBreaching`, killing the publisher drives the alarm to a confident OK.

The related failure is a **latched** alarm. `scripts/verify_chain.sh` forces
ALARM by hand to test the wiring; if it dies between the trip and the reset the
alarm stays latched, and a latched alarm never emits another OK→ALARM
transition — so the next real regression produces no event and no dispatch at
all, while every dashboard still reads normal. The script now unlatches from a
`trap ... EXIT INT TERM`. This is the failure mode to suspect first if a
regression fires no dispatch: check `describe-alarm-history`, not the Lambda.

## The discriminator

**Only the synthetic feed carries this today.** `WorkUnits` and `RequestCount`
are flat by construction, in both the healthy and
the regressed state. Same corpus size, same request rate. So when latency and
system load triple and those two do not move, the space of explanations
collapses: it is not more traffic and it is not more work. It is the same work
costing more — which is a code change, which is a commit.

That is the fact that lets the agent go from "something is slow" to "this
commit did it," and it is why the metric contract has four metrics instead of
one.

W1's app publishes `RequestLatency` and `ErrorRate` but neither discriminator,
so a dispatch from `culprit-App-High` carries a weaker evidence block — it can
say latency rose, not that it rose *without* more work arriving. Closing that
is two lines in `bad_app.py` (`RequestCount` is already implicit in the metric's
`SampleCount`; `WorkUnits` needs a counter). It is listed in the gap register
and it is the single highest-value thing another workstream could add.

### Measuring it at the right moment

The discriminator is computed inside the dispatch Lambda, by `evidence()`, and
the window it measures over is the part that took three tries to get right.

The temptation is to read "the last couple of minutes" against "the couple of
hours before that." That is wrong here, and wrong in a way that produces a
confidently inverted answer. The alarm fires 30–45 seconds after a regression
starts, so at the moment the Lambda runs, the incident is only about three
datapoints old. The first version read `Period: 60` and compared the mean of
the last two minutes against the mean of the preceding eighteen. Both of those
one-minute buckets were still entirely pre-regression, and the eighteen-minute
"before" window happened to contain the tail of an earlier test — so it
reported `SystemLoad1` **falling 23%** during an incident that had tripled it.
The payload said, in effect, the machine got faster. An agent reading that
would have gone looking in the wrong direction, and nothing in the pipeline
would have flagged it.

The fix is to match the window to the physics — and, since the two feeds
publish at different resolutions, to express it in *seconds* and let the period
fall out of the alarm:

```python
k, j = max(1, 30 // p), max(1, 300 // p)   # p = the firing alarm's period
now    = mean(values[-k:])                 # the last 30 seconds
before = mean(values[:-j])                 # everything older than 5 minutes
```

| | value | why |
|---|---|---|
| `Period` | **read from the event**, not hardcoded | 10 for the synthetic feed, 60 for the EC2 app; the same code serves both |
| `now` | last **30 seconds** | the whole regression, and nothing but the regression |
| `before` | everything **older than 5 minutes** | clean baseline |
| the 4.5-minute hole between them | discarded | it is the ramp — and it is where a just-recovered prior incident sits |

At `p=10` this reproduces the original 3-bucket / 30-bucket behaviour exactly;
at `p=60` it becomes 1 point against 5. The window is a duration, so it stays
correct as resolution changes instead of silently becoming a 3-minute "now".

That middle exclusion is not tidiness. Without it, back-to-back demo runs
poison each other's baselines: the previous regression is still inside the
"before" window, so `before` reads high and the real regression looks like an
improvement. Dropping the most recent five minutes from the baseline is what
makes the number survive being run twice in a row.

`evidence()` also stopped naming the metrics it wants. It issues a single
`SEARCH('Namespace="<ns>"', 'Average', <period>)` against whatever namespace
raised the alarm, which is dimension-agnostic — it picks up the EC2 app's
`App=bad-app-ec2` series without being told the dimension exists, and it will
pick up any metric a future workstream adds without a redeploy. A hardcoded
metric list would have needed editing the moment the second feed appeared.

The threshold for calling a metric `flat` is ±25%. `WorkUnits` and
`RequestCount` are constants plus noise, so they read `flat` reliably; the
band is wide enough that ordinary jitter never manufactures a fake signal.

---

## The dashboard

`https://console.aws.amazon.com/cloudwatch/home?region=us-east-1#dashboards:name=Culprit`

Eight widgets, ordered as the demo script rather than as the architecture:

| row | widget |
|---|---|
| top | how to trigger a regression, in one copyable line |
| 1 | **EC2 app RequestLatency against its expected band**, with the static threshold drawn |
| 1 | alarm-state panel — all six alarms, both feeds |
| 1 | EC2 app ErrorRate |
| 2 | EC2 host CPU / memory / disk, via `SEARCH` so no instance id is hardcoded |
| 2–3 | the synthetic feed: latency + band, SystemLoad1, work & traffic |

The band on the app graph is the *same* `ANOMALY_DETECTION_BAND` expression the
alarm evaluates, not a redrawing of it — so the shaded region on screen is
literally what `culprit-App-Anomaly` is comparing against. Watching the line
leave the shading and the panel beside it turn red, in that order, is the demo.

---

## Runbook

```bash
make calibrate      # derive the threshold from observed data. never guess it.
make deploy         # alarms, anomaly detectors, EventBridge, Lambda, dashboard
make verify         # prove the chain end to end
```

`make verify` is the important one. It forces the alarm with

```bash
aws cloudwatch set-alarm-state --alarm-name culprit-App-High --state-value ALARM
```

then watches the Lambda's log group. This separates two failures that look
identical from the outside — *"the wiring is broken"* and *"the threshold never
tripped"* — and it works before there is any real regression to wait for.

To demo against the **real app** (this is the one to show):

```bash
make app-status     # is the box reachable, and is chaos already on?
make app-regress    # flip the real app into the slow state
make alarms         # culprit-App-High -> ALARM (~60-90s)
make logs           # watch the dispatch Lambda POST it
make app-recover    # turn chaos back off — do not skip this
```

`make app-regress` finds the instance by its `Name=hackathon-demo` tag rather
than a pinned IP, because the public IP changes across a stop/start and a
demo that dies on a stale address is not worth the two lines it saves.

The app's chaos toggle takes latency from ~0.05s to a `random.uniform(1.5, 3.0)`
sleep — a 45x jump. That is why `culprit-App-High` is 1-of-1 rather than 2-of-2:
a jump that size is never noise, so a second confirming period would buy no
accuracy and cost a minute of demo silence.

There is no second way to demo this any more, and that is deliberate. The
synthetic feed was the fallback; it was also a second source of truth wired into
dispatch. `make payload` — which reprints the exact JSON the Lambda last handed
GitHub — is the fallback now, and it is an honest one.

---

## Gap register — what is fake and what replaces it

| Faked now | Replaced by | Swap cost |
|---|---|---|
| ~~`seed_metrics.py` is the only source~~ | **done.** W1's app on EC2 is live and alarmed (`culprit-App-*`). The synthetic feed is kept deliberately — 10s resolution, a trained band, and the two discriminator metrics. | already paid: 4 CloudFormation resources, no Lambda change |
| The app emits no `WorkUnits` / `RequestCount`, so app-triggered dispatches carry weaker evidence | W1 adds a work counter and a request counter to `bad_app.py` | ~2 lines in W1; nothing in W2 — `evidence()` finds new metrics in the namespace by `SEARCH`, no redeploy |
| `culprit-App-Anomaly` is `INSUFFICIENT_DATA` — the band needs ~3h and the box booted at 13:53Z | time (usable ~17:00Z) | none. Not backfillable: writing synthetic points into a teammate's real metric stream would corrupt the very baseline it is meant to learn. |
| `make regress` flips a flag file; `make app-regress` flips the app's chaos toggle | W1 pushes the bad commit; the box redeploys; latency really rises | none — the alarm cannot tell the difference, and `make app-regress` already exercises the real app over the real network |
| `GITHUB_TOKEN` may be empty at deploy | W3 mints the classic PAT (`repo` + `workflow`, 1-day expiry) and it goes in via `--parameter-overrides` | redeploy. Until then the Lambda logs the exact payload it *would* have sent, so the payload is still reviewable. |
| `.github/workflows/dispatch-receipt.yml` just echoes the payload | W3's `anomaly-response.yml` runs Claude and opens the PR | none — both listen for `event_type: anomaly` and can run side by side |
| Synthetic threshold is calibrated; the app's `0.5s` was reasoned, not measured | recalibrate against the app's observed baseline | `make deploy` with a new `AppLatencyThresholdSeconds`. Healthy is ~0.05 and chaos is 1.5–3.0s, so 0.5 sits an order of magnitude clear of both — this is low-risk. |

One row is not in that table because it is not a gap in the design, it is a
gap in the *testing*: `make app-regress` → `culprit-App-High` → dispatch has
never been run as a single unbroken sequence. Every link in it is individually
proven — the alarm evaluates the real metric (`0.0496 not greater than 0.5`),
forcing it to ALARM does produce exactly one dispatch carrying
`namespace: HackathonDemo`, and the chaos endpoint does respond — but the
arithmetic step in the middle (chaos actually pushing the 60s average past
0.5s) has only been reasoned about, not observed. It should be: healthy is
~0.05 and the chaos sleep is 1.5–3.0s. Run it once before the demo; it takes
about three minutes and it is a shared box, so `make app-recover` is not
optional.

Nothing else in that table blocks anything else in it. Each row can be swapped
independently, in any order.

---

## Known weaknesses

- **The PAT sits in a Lambda environment variable**, which renders in plaintext
  in the console. Mitigated by a one-day expiry, not by encryption. The correct
  answer is an SSM SecureString and it is a ten-minute change we chose not to
  spend. Say this before a judge asks.
- **The anomaly band is the weakest link on demo day.** If the publisher dies
  overnight the band regresses to untrained and the ML alarm shows
  `INSUFFICIENT_DATA`. `make metrics-status` is the check. The static alarm is
  unaffected, which is exactly why the demo triggers on it. The app's band has
  the same exposure for a different reason: it has simply not existed long
  enough yet.
- **`culprit-App-High` is 1-of-1.** A single 60-second period over the app's
  threshold dispatches. That is correct for a 45x chaos jump and wrong for a
  subtler regression, where one unlucky period could dispatch on noise. The
  trade was made for demo latency, knowingly. `culprit-Latency-High` on the
  synthetic feed is 2-of-2 and is the more defensible setting.
- **The EC2 app is not in this repo.** It was deployed by hand, so nothing here
  reproduces it and `infra/detection.yaml` hardcodes no instance id (host
  metrics come in via `SEARCH`) precisely so a replaced box does not break the
  dashboard. But if that instance dies, W2 has no way to bring it back.
- **The dispatch Lambda has no DLQ.** A GitHub outage during the demo loses the
  event. Acceptable for a hackathon, and recoverable: the Lambda prints the
  full request body to CloudWatch Logs *before* it POSTs, so the exact payload
  survives the failure and can be re-sent with a curl to `/dispatches`.
  (`scripts/test_dispatch.sh` is a scope smoke-test, not a replayer — it sends
  a probe payload of its own.)
- **One region, one account.** No multi-region failover, by choice.
