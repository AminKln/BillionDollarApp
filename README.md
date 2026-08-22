# DevOps for GenAI — Ottawa Hackathon Series 2026 — Team 06

## Anomaly Detection → LLM Code Review & Fix Agent

A system that reacts to a CloudWatch anomaly-detection alarm on an app's
latency — the app and its CloudWatch setup live in a separate repo, this
repo only owns the reaction — by gathering the alarm/metric/log evidence
plus codebase context into one prompt and sending it to Claude for a
grounded root-cause hypothesis. The verdict is logged and returned as the
Lambda's invocation result. Triggering the alarm (real traffic, chaos
injection, whatever) is out of scope here — this just responds to whatever
alerts arrive.

Stack: AWS only, kept inside free tier wherever possible. GitHub for source
control and for the codebase-context fetch. Direct Anthropic API (Claude)
for the review agent.

> This is the CloudWatch-native design — it supersedes an earlier
> self-hosted Prometheus/Alertmanager/Grafana version. See
> [docs/architecture.md](docs/architecture.md) section 2 for what changed
> and why. **[docs/current-pipeline.md](docs/current-pipeline.md) is the
> up-to-date reference** — `architecture.md` still describes the original
> plan (Bedrock, git-diff correlation, notify/dashboard-widget), several
> pieces of which have since changed or been removed.

## How it works

```
(separate repo) app + CloudWatch anomaly detection alarm
    — not owned here; this pipeline just reacts to whatever it publishes
        │
        ▼
SNS topic (culprit-alerts)  →  Lambda (native SNS trigger, no API Gateway / webhook parsing needed)
        │
        ├─▶ context_builder.py    — real alarm/metric/log evidence via boto3
        │                            (describe_alarms, get_metric_statistics,
        │                             Logs Insights)
        │
        ├─▶ github_context.py     — codebase context, fetched live from GitHub
        │                            (Contents API)
        │
        └─▶ llm_agent.py          — combines both into one prompt, sends to
                                     Claude (direct Anthropic API), returns a
                                     grounded hypothesis + confidence +
                                     evidence + suggested action
```

The verdict is logged (CloudWatch Logs, via the Lambda's own execution log)
and returned as the invocation result — no notification/dashboard step by
design, to keep the core pipeline simple. `run_manual.py` runs the same
prompt-building path locally against a fake alert (no AWS needed) for fast
iteration; `handler.py` is the SNS-triggered equivalent against a real one.

Full design, the AWS free-tier accounting, team split, and build order are
in [docs/architecture.md](docs/architecture.md). Live-demo timing (CloudWatch's
~1-minute metric resolution matters here) is in
[docs/demo-script.md](docs/demo-script.md).

## Repo layout

```
repo-root/
├── template.yaml               # SAM template — Lambda, SNS subscription to the existing alarm, dashboard, IAM
├── samconfig.toml
│
├── docs/
│   ├── current-pipeline.md      # up-to-date reference — start here
│   ├── architecture.md          # original plan, partially superseded
│   ├── demo-script.md
│   └── contracts/               # alarm-sns-message.json, review-result.json
│
├── agent/                       # the whole pipeline, one folder — this IS the Lambda's
│   │                              CodeUri, so what you edit is exactly what deploys,
│   │                              no staging/copy step
│   ├── handler.py                 # SNS-triggered entry point (real alert)
│   ├── run_manual.py              # local CLI entry point (fake alert)
│   ├── context_builder.py         # real CloudWatch alarm/metric/log evidence via boto3
│   ├── fixtures.py                # canned CloudWatch API responses for local testing
│   ├── fake_alert.py              # builds a fake IncidentContext from fixtures.py
│   ├── github_context.py          # live codebase-context fetch via GitHub Contents API
│   ├── llm_agent.py               # builds the prompt, calls Claude, parses the response
│   └── codebase_context.md        # hand-maintained architecture/failure-pattern summary
│                                     (this is what github_context.py fetches)
│
└── scripts/
    ├── deploy.sh                 # sam build && sam deploy, secrets from .env
    └── run_manual.sh             # wraps agent/run_manual.py with .env loaded
```

## Team

| Person | Owns | Depends on |
|---|---|---|
| B — AWS scaffolding | SAM template: Lambda, SNS subscription, dashboard | Nothing blocking — can scaffold against `docs/contracts/` immediately |
| D — LLM agent + payoff | `agent/` (context building, GitHub fetch, prompt, Claude call) | Nothing blocking — testable end-to-end locally via `run_manual.py` with no AWS/GitHub deploy needed |

The app, its EC2/CloudWatch-agent setup, and whatever produces the alarm
live in a separate repo — not this one's concern. This repo only reacts to
the alarm once it fires.

## Getting started

1. Copy `.env.example` to `.env` and fill in `ANTHROPIC_API_KEY` and
   `ALARM_TOPIC_ARN` (the existing alarm's SNS topic, e.g. `culprit-alerts`).
   `LOG_GROUP` and the `GITHUB_*` vars are currently optional/unused — see
   `agent/context_builder.py` and `agent/github_context.py`.
2. Try it locally first, no AWS needed: `./scripts/run_manual.sh` — runs the
   full pipeline against a fake alert.
3. Deploy the stack: `./scripts/deploy.sh` (wraps `sam build && sam deploy`)
   — subscribes the Lambda to the existing alarm's SNS topic. Nothing to
   trigger manually after that; it reacts on its own whenever the real
   alarm fires. Check the Lambda's CloudWatch Logs for the verdict.

See [docs/current-pipeline.md](docs/current-pipeline.md) for a full,
up-to-date walkthrough of the request flow, file reference, and merge notes.
[docs/architecture.md](docs/architecture.md) has the original AWS free-tier
accounting and build order, still accurate on those points.
