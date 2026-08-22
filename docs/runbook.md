# Demo runbook

Everything here has been executed end to end against the real app today.
Timings are measured, not estimated.

## Before you start (once)

```bash
make app-bootstrap   # already done -- creates the three chaos SSM parameters
make app-status      # should print all three = off, and the app agreeing
```

If `app-status` says a parameter is MISSING, chaos cannot be turned on and the
demo has no trigger. Re-run `make app-bootstrap`.

Open two browser tabs:
1. The CloudWatch dashboard (`make dashboard` prints the URL).
2. The repo's **Actions** tab.

## The run

| T+ | What you do | What the audience sees |
|---|---|---|
| 0:00 | `make app-status` | app healthy, latency ~0.05s, CPU ~2% |
| 0:10 | `make app-regress` | nothing yet |
| 0:40 | — | dashboard latency line lifts off the floor |
| 1:00 | — | CPU climbs 2% → ~45% |
| **2:30** | — | **`culprit-App-High` goes red** |
| 2:35 | `make payload` | **the evidence the agent receives** — read it aloud |
| 2:40 | — | Actions tab: a run appears |
| 3:00 | — | the PR |
| — | **`make app-recover`** | **never skip this. shared box.** |

`make payload` is the one to rehearse. It prints the exact JSON the Lambda
handed GitHub, formatted to be read off a projector — five metrics, before →
now → change. It is the moment the demo stops being "an alarm went off" and
starts being "here is the diagnosis." It also works with no `GITHUB_TOKEN` on
the stack, which makes it the fallback below.

Measured over two full rehearsals on the real box: chaos on 17:10 → ALARM
**17:12:53** (2m53s), and chaos on 17:18:54 → ALARM **17:20:35** (1m41s). The
Lambda fires and builds the payload **within 5s** of the alarm either time. The
spread is just where you land inside the 60s metric window — **budget three
minutes** and be pleasantly surprised. Fill it by talking through the dashboard;
that is what the dashboard is for.

## What to say while you wait

The pitch is not "we detected high latency." Thresholds have done that for
thirty years. The pitch is what the payload carries:

> `RequestLatency` **+695%** · `cpu_usage_active` **+183%** · `ErrorRate`,
> `mem_used_percent`, `disk_used_percent` **all flat**

Those are the numbers from the last rehearsal's actual payload — read them off
the screen, do not memorise them, they move run to run (latency has come back
anywhere from +695% to +3937% depending on how much clean baseline sits in the
`before` window).

The shape is what matters and the shape is stable: **latency and CPU moved
together, and nothing else moved at all.** Memory flat rules out a leak. Disk
flat rules out the box filling up. Errors flat means the endpoint is not
failing — so it is not a broken dependency either. What is left is that the
*same request* now burns CPU it did not burn before. That is a code change, and
the agent is handed that conclusion in the alert instead of having to go find
it.

That is why `make app-regress` flips the **cpu** knob and not the latency knob.
The latency knob is a bare `time.sleep()`: it produces the same red alarm with
the CPU flat, and then the evidence says only "it got slower," which is
indistinguishable from a slow database. Same alarm, no diagnosis.

## Known rough edges — say them before someone asks

- **Throughput drops during the regression** (~240 req/min → ~27). The app's
  traffic generator is a closed loop, so slower requests mean fewer requests.
  Do not claim traffic held steady; the dashboard shows otherwise. It does not
  weaken the story — CPU per request is what moved.
- **The dashboard is named `Culprit` but covers both feeds** — its top three
  widgets are the EC2 app's latency, error rate and host CPU/mem/disk, and the
  synthetic widgets sit below them. So the `dashboard_url` in the payload is
  correct even for a `HackathonDemo` alarm; only the name is confusing. Scroll
  to the top three widgets on stage and ignore the rest.
- **One regression can fire two dispatches.** `culprit-App-Anomaly` and
  `culprit-App-High` watch the same metric. The Lambda suppresses the anomaly
  one only if `-High` is *already* red — and in rehearsal #2 the anomaly band
  tripped **5 seconds first** (17:20:28 vs 17:20:33), so both went out. Expect
  up to two Actions runs for one incident. If two PRs appear on stage, that is
  this, not a bug in the agent. W3 dedupes; see `contracts/dispatch-payload.json`.
- **The synthetic `Culprit` feed is still running** as a fallback rig if the
  shared EC2 box dies mid-demo. It is not the story. See `decisions.md` §4.

## If it breaks on stage

| Symptom | Cause | Fix |
|---|---|---|
| `app-status` unreachable | instance stopped | `make app-status` re-resolves the IP by tag; if it still fails the box is down |
| chaos flips but latency does not move | wrong parameter prefix | the app reads `CHAOS_PARAM_PREFIX`, default `/hackathon-demo/chaos` |
| alarm red, no Actions run | no `GITHUB_TOKEN` on the stack | `GITHUB_TOKEN=xxx make deploy`. **If there is no time: run `make payload` and demo it there.** The chain ran; it just stopped one HTTP POST short. |
| `make logs` errors out | that target needs AWS CLI v2, half the team is on v1 | use `make payload`; or `make setup-awscli` |
| Actions run, no PR | workflow is checking out the wrong repo | see `decisions.md` §6 — the culprit lives in `Tehreem404/bad_app_demo` |
| alarm will not re-fire | it is still in ALARM from the last run | wait for OK; a state *transition* is what dispatches |

Last one bites in rehearsal constantly: **the alarm must return to OK before it
can fire again.** Give it 1–2 minutes between takes.
