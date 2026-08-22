# Demo script

## Pre-demo setup

- [ ] EC2 box up, `docker-compose` stack (Prometheus, Alertmanager, Grafana,
      demo app) running and healthy.
- [ ] Full pipeline deployed (API Gateway → ingest → git-context → LLM review
      → notify) and smoke-tested at least once today.
- [ ] The deliberately-bad commit for `infra/demo-app/bad_app.py` prepared via
      `scripts/seed_bad_commit.sh`, but **not yet pushed** — push it live,
      right before flipping chaos on, so the diff the LLM reviews is real.
- [ ] Grafana dashboard open and visible on screen, showing
      `http_request_duration_seconds` and `http_requests_total`.
- [ ] Slack/SNS notification channel visible on a second screen or window.

## Timing

The `LatencyAnomaly` rule has `for: 3m`, so chaos must be flipped on **~3
minutes before** you want to point at the firing alert. Plan the talk track
around that lag — don't flip chaos on and then stand there silently for three
minutes.

| T (min) | Action | Talking point |
|---|---|---|
| T-5 | Push the seeded bad commit to the repo | "Here's a completely normal-looking change — someone just removed a retry cap / added an N+1 query." |
| T-3 | `scripts/trigger_chaos.sh latency on` (or hit `/chaos/latency/on` directly) | "I'm not touching the code path that serves this dashboard — I'm just going to let real traffic hit the app." |
| T-3 → T0 | While waiting on the `for: 3m` window | Walk through Pipeline A/B architecture on the whiteboard/slide; explain the webhook → Lambda → Step Functions flow. |
| T0 | Alertmanager fires, webhook hits API Gateway | "There's the alert." Point at Alertmanager UI or the Grafana panel turning red. |
| T0 + ~30s | Ingest Lambda logs the segment, Step Functions kicks off git-context + LLM review | Show CloudWatch Logs / Step Functions execution graph if time allows. |
| T0 + ~1m | LLM review completes, verdict stored | Show the structured JSON verdict (commit SHA, confidence, explanation). |
| T0 + ~1m | Notify Lambda posts the Grafana annotation + SNS/Slack message | **The payoff**: switch to the Grafana graph — the anomaly window now has an annotation reading something like "likely caused by commit `a1b2c3` (added unbounded retry loop) — 87% confidence." |
| Wrap | `scripts/trigger_chaos.sh latency off` | "And when the app recovers, the alert resolves and closes out the anomaly segment." |

## The payoff line

When the Grafana annotation appears, this is the moment to slow down and let
it land:

> "The system watched the metric, noticed a statistical anomaly, pulled every
> commit since the last known-good state, handed the diff to an LLM, and
> printed a root-cause guess with a confidence score — directly on the graph
> — without a human in the loop."

## Fallback plan

If the live pipeline doesn't fire in time or something breaks mid-demo:

- Have a second terminal ready with a pre-recorded run's CloudWatch Logs /
  Step Functions execution to fall back to.
- Have a screenshot of a previously successful Grafana annotation as a static
  backup slide.
- Know which step is most likely to flake (usually Bedrock latency or the
  GitHub API) and have the explanation ready ("this call typically takes
  X seconds, here's what it looks like when it lands").

## Post-demo

- `scripts/trigger_chaos.sh latency off` and `scripts/trigger_chaos.sh errors off`
  to leave the app in a clean baseline state.
- Note actual timings observed (webhook → verdict latency) for Q&A.
