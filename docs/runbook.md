# Demo runbook

Everything here has been executed end to end against the real app. Timings are
measured, not estimated.

## What actually happens

```
make regress-commit          you push a real commit to Tehreem404/bad_app_demo
  -> Deploy to EC2           the repo's own workflow SSHes in and restarts the app
  -> RequestLatency          0.03s -> ~1.2s, published by the app itself
  -> culprit-App-High        static alarm, threshold 0.34s, 1-of-1 at 60s
  -> SNS -> culprit-dispatch Lambda builds the evidence payload
  -> GitHub                  a PR with the fix, or an Issue with the diagnosis
make revert-commit           you push the revert; the box redeploys clean
```

There is no chaos knob any more. The trigger is a git commit, which is the
whole point: the thing the agent has to find is a thing that was really pushed.

## Before you start (once)

```bash
make check              # alarm exists, Lambda exists, app is publishing
./scripts/regress_commit.sh apply   # DRY_RUN=1 first if you want to rehearse
```

Confirm three things and nothing else matters:

1. `culprit-App-High` is in **OK**. An alarm already in ALARM cannot fire again
   — a state *transition* is what dispatches.
2. `RequestLatency` is around **0.03s**. If it is already high, a previous run
   was not reverted: `make revert-commit`.
3. Your SSH key can push to `Tehreem404/bad_app_demo`. `DRY_RUN=1
   ./scripts/regress_commit.sh apply` proves it without pushing.

Open two browser tabs:
1. The CloudWatch dashboard (`make dashboard` prints the URL).
2. `https://github.com/Tehreem404/bad_app_demo` — Actions, Issues and Pulls.

## The run

| T+ | What you do | What the audience sees |
|---|---|---|
| 0:00 | show the dashboard | app healthy, latency ~0.03s, CPU ~2% |
| 0:05 | `make regress-commit` | the commit lands on GitHub — **show it, it is 20 lines** |
| 0:30 | — | Actions: *Deploy to EC2* runs and goes green |
| 1:00 | — | dashboard latency line lifts off the floor |
| **~2:30** | — | **`culprit-App-High` goes red** |
| +5s | `make payload` | **the evidence the agent receives** — read it aloud |
| +30s | — | GitHub: the PR, or the Issue with the diagnosis |
| — | **`make revert-commit`** | **never skip this. shared box.** |

`make payload` is the one to rehearse. It prints the exact JSON the Lambda
handed GitHub, formatted to be read off a projector — five metrics, before →
now → change. It is the moment the demo stops being "an alarm went off" and
starts being "here is the diagnosis." It needs no GitHub token, which makes it
the fallback if the GitHub half misbehaves.

**Budget three minutes** from push to red. The spread is just where the push
lands inside the 60s metric window. Fill it by talking through the dashboard;
that is what the dashboard is for.

Measured on the real box, 2026-08-22:

| | clock | from push |
|---|---|---|
| `make regress-commit` pushes `73f3b39` | 19:22:46Z | — |
| `Deploy to EC2` green, app restarted | 19:23:28Z | +42s |
| latency visible in CloudWatch | 19:24:12Z | +1m26s |
| **`culprit-App-High` -> ALARM** | **19:24:56Z** | **+2m10s** |
| Lambda dispatched, probed, took the incident back | 19:25:03Z | +2m17s |
| **diagnosed Issue on GitHub** | **19:25:25Z** | **+2m39s** |

Push to a GitHub artifact naming the culprit commit: **two minutes
thirty-nine seconds**, no human in the loop.

## What to say while you wait

The pitch is not "we detected high latency." Thresholds have done that for
thirty years. The pitch is what the payload carries:

> `RequestLatency` **+695%** · `cpu_usage_active` **+183%** · `ErrorRate`,
> `mem_used_percent`, `disk_used_percent` **all flat**

Read the numbers off the screen, do not memorise them — they move run to run
depending on how much clean baseline sits in the `before` window.

The shape is what matters and the shape is stable: **latency and CPU moved
together, and nothing else moved at all.** Memory flat rules out a leak. Disk
flat rules out the box filling up. Errors flat means the endpoint is not
failing — so it is not a broken dependency either. What is left is that the
*same request* now burns CPU it did not burn before. That is a code change, and
the agent is handed that conclusion in the alert instead of having to go find
it.

That is why the culprit commit is an **O(n²) ranking loop** and not a
`time.sleep()`. A sleep produces the same red alarm with the CPU *flat* — worse,
measured live at **CPU −36%**, because slower requests in a closed-loop
generator mean lower throughput. Same alarm, no diagnosis. The commit we push
adds `_rank_all()`, which compares each of 6000 samples against all the others
on every request: 0.03s → ~1.19s, and the CPU moves with it.

## The culprit commit

`infra/bad-app/culprit.py.patch`, applied by `scripts/regress_commit.sh` in a
scratch clone at `/tmp/culprit-bad-app-demo`. It is a plausible feature, not
sabotage — "Add per-request traffic quality score (#142)":

```python
SCORE_BATCH = 6000

def _rank_all(values):
    """Rank every sample against the rest of the batch."""
    ranks = []
    for value in values:
        position = 0
        for other in values:
            if other < value:
                position += 1
        ranks.append(position)
    return ranks
```

`6000` was chosen by measurement, not by feel: 4000 gave 0.53s (1.6× the
threshold — too thin to be convincing), 6000 gives **1.187s (3.5×)**, 7000 gives
1.64s and starts to look silly. The script is idempotent in both directions —
applying twice is a no-op, and so is reverting twice.

## Known rough edges — say them before someone asks

- **Throughput drops during the regression** (~240 req/min → ~27). The app's
  traffic generator is a closed loop, so slower requests mean fewer requests.
  Do not claim traffic held steady; the dashboard shows otherwise. It does not
  weaken the story — CPU per request is what moved.
- **The dashboard is named `Culprit`** — that is the stack's name, not a
  feed's. Every widget on it is the real app: latency, error rate, the anomaly
  band, and host CPU/mem/disk. The three synthetic widgets that used to sit
  below them are gone (`decisions.md` §9).
- **One regression used to fire two dispatches.** `culprit-App-Anomaly` and
  `culprit-App-High` watch the same metric, and in rehearsal the band tripped
  **5 seconds first**, so both went out. Fixed at the alarm layer: the anomaly
  alarm is now 3-of-3 at a 60s period, so it needs three minutes and can never
  beat the 1-of-1 static alarm. One regression, one dispatch. If two Actions
  runs still appear, that is a real bug — say so.

## Two ways the response can land, and both are fine

The Lambda does not assume GitHub Actions will answer. It POSTs the
`repository_dispatch`, then **checks whether anything actually picked it up** —
`repository_dispatch` returns 204 whether or not a single workflow is listening,
so the POST status proves nothing. It looks for a real Actions run, and then
polls that run's `conclusion`.

| Situation | What lands on GitHub |
|---|---|
| workflow present, run succeeds | **the PR** — Actions owns it, the Lambda stays quiet |
| workflow present, run **fails** | the Lambda takes the incident back and files **the diagnosed Issue** |
| workflow present, no run appears in 30s | same — the Lambda files the diagnosed Issue |
| no workflow at all | same — the Lambda files the diagnosed Issue |

All four branches are covered by a test harness that stubs GitHub and drives the
real fixture event; all four pass. **Every path ends with a GitHub artifact that
contains a diagnosis** — there is no combination that produces silence.

## If it breaks on stage

| Symptom | Cause | Fix |
|---|---|---|
| push rejected | your key is not on `bad_app_demo` | `DRY_RUN=1 ./scripts/regress_commit.sh apply` to check before you are on stage |
| `Deploy to EC2` red | the box is unreachable or `deploy.sh` failed | open the run; the SSH step prints the remote output |
| deploy green, latency flat | the app did not restart | the deploy step is `./deploy.sh` on the box — check it |
| alarm red, no dispatch | no `GITHUB_TOKEN` on the stack | `GITHUB_TOKEN=xxx make deploy`. **If there is no time: run `make payload` and demo it there.** The chain ran; it just stopped one HTTP POST short. |
| `make logs` errors out | that target needs AWS CLI v2, half the team is on v1 | use `make payload`; or `make setup-awscli` |
| Actions run red at the Claude step | the repo's `ANTHROPIC_API_KEY` secret is mistyped | **the Lambda already handles this** — the diagnosed Issue appears anyway. Only a repo admin can re-paste the secret. |
| alarm will not re-fire | it is still in ALARM from the last run | wait for OK; a state *transition* is what dispatches |

Last one bites in rehearsal constantly: **the alarm must return to OK before it
can fire again.** Give it 1–2 minutes between takes.
