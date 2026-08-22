# Demo script

## Pre-demo setup

- [ ] EC2 box up, demo app running, CloudWatch agent reporting
      `mem_used_percent`/`disk_used_percent`.
- [ ] Anomaly detection enabled on `HackathonDemo/RequestLatency`, alarm ARN
      wired to the `AnomalyAlarmTopic`, Lambda subscribed and smoke-tested
      today with a synthetic SNS message.
- [ ] CloudWatch dashboard open and visible on screen: `RequestLatency` with
      its anomaly band overlay, `ErrorRate`, CPU/RAM/disk, and the custom
      widget (currently showing "No anomaly reviewed yet.").
- [ ] Email inbox / notification channel for the `AnomalyNotifyTopic`
      subscription visible on a second screen or window.
- [ ] The deliberately-bad commit for `infra/demo-app/bad_app.py` prepared
      via `scripts/seed_bad_commit.sh`, but **not yet pushed** — push it
      live, right before flipping chaos on, so the diff the LLM reviews is
      real.

## Timing

CloudWatch's standard metric resolution is ~1 minute, and the anomaly alarm
needs `EvaluationPeriods: 2` consecutive breaching datapoints before it
fires — budget **at least 2–3 minutes** between flipping chaos on and the
alarm actually firing. This is longer than it sounds live; don't flip chaos
on and then stand there silently.

| T (min) | Action | Talking point |
|---|---|---|
| T-6 | Push the seeded bad commit to the repo | "Here's a completely normal-looking change — someone just removed a retry cap / added an N+1 query." |
| T-5 | `scripts/trigger_chaos.sh latency on` (or hit `/chaos/latency/on` directly) | "I'm not touching the monitoring stack — I'm just going to let real traffic hit the app, and CloudWatch will notice on its own." |
| T-5 → T0 | While waiting on the alarm's evaluation window | Walk through the architecture on the whiteboard/slide: EC2 → CloudWatch anomaly alarm → SNS → Lambda → git_context/llm_review/notify. |
| T0 | Alarm transitions OK→ALARM, SNS invokes the Lambda | "There's the alarm." Point at the CloudWatch alarm going red, or the `RequestLatency` line breaking out of its anomaly band on the dashboard. |
| T0 + ~10–20s | `git_context.py` fetches the diff, `llm_review.py` calls Bedrock | Show CloudWatch Logs for the Lambda if time allows — the alarm payload, the commits found, the Bedrock call. |
| T0 + ~30–45s | Verdict returned, `notify.py` stores it and publishes | Show the structured JSON verdict (suspect commit, confidence, explanation, suggested fix). |
| T0 + ~1min | Refresh the dashboard's custom widget | **The payoff**: the widget now renders the diagnosis and suggested fix directly on the dashboard, next to the metric that misbehaved. |
| Wrap | `scripts/trigger_chaos.sh latency off` | "And once the app recovers, the alarm returns to OK." |

## The payoff line

When the custom widget renders on the dashboard, this is the moment to slow
down and let it land:

> "The system watched the metric, let CloudWatch's own ML model decide what
> counted as anomalous, pulled every commit since the last known-good state,
> handed the diff to an LLM, and got back not just a root-cause guess but a
> proposed fix — rendered directly on the dashboard, without a human in the
> loop."

If Tier 2/3 got built in time, add: "...and it also left a comment on the
suspect commit" / "...and it opened a draft PR with the fix, waiting for a
human to approve it."

## Fallback plan

If the live pipeline doesn't fire in time or something breaks mid-demo:

- Have a second terminal ready with a pre-recorded run's CloudWatch Logs to
  fall back to.
- Have a screenshot of a previously successful custom-widget render as a
  static backup slide.
- Know which step is most likely to flake (usually Bedrock latency, the
  GitHub API, or the alarm's evaluation window taking longer than expected)
  and have the explanation ready.

## Post-demo

- `scripts/trigger_chaos.sh latency off` and `scripts/trigger_chaos.sh errors off`
  to leave the app in a clean baseline state.
- Note actual timings observed (alarm fire → verdict latency) for Q&A.
