# DevOps for GenAI — Ottawa Hackathon Series 2026 — Team 06

## Anomaly Detection → LLM Code Review & Fix Agent

A system that watches an app's latency and host-level system load
(CPU/RAM/disk), detects anomalies using CloudWatch's built-in ML anomaly
detection, and — when one fires — automatically pulls the code diff since
the last commit before the anomaly started, sends it to an LLM
(Bedrock/Claude) for root-cause analysis, and gets back both a diagnosis
**and a suggested fix**. The result is surfaced on a CloudWatch dashboard and
sent as a notification.

Stack: AWS only, kept inside free tier wherever possible. GitHub for source
control. Amazon Bedrock (Claude) for the review agent.

> This is the CloudWatch-native design — it supersedes an earlier
> self-hosted Prometheus/Alertmanager/Grafana version. See
> [docs/architecture.md](docs/architecture.md) section 2 for what changed
> and why.

## How it works

```
EC2: demo app + CloudWatch agent
    (app pushes custom latency/error metrics via boto3;
     agent reports CPU/RAM/disk)
        │
        ▼
CloudWatch anomaly detection alarm
    (built-in ML band on the latency metric — no custom threshold math)
        │
        ▼
SNS topic  →  Lambda (native SNS trigger, no API Gateway / webhook parsing needed)
        │
        ├─▶ git_context.py   — finds last commit before the alarm's timestamp,
        │                       fetches the diff since then via GitHub API
        │
        ├─▶ llm_review.py    — sends diff + anomaly context to Bedrock (Claude),
        │                       gets back diagnosis + suggested fix as structured JSON
        │
        └─▶ notify.py        — updates a CloudWatch custom widget (Lambda-backed
                                 HTML on the dashboard) with the verdict, and
                                 publishes a second SNS notification (email)
```

Full design, the AWS free-tier accounting, team split, and build order are
in [docs/architecture.md](docs/architecture.md). Live-demo timing (CloudWatch's
~1-minute metric resolution matters here) is in
[docs/demo-script.md](docs/demo-script.md).

## Repo layout

```
repo-root/
├── template.yaml               # SAM template — Lambda, SNS, CloudWatch alarm, dashboard, IAM
├── samconfig.toml
│
├── docs/
│   ├── architecture.md
│   ├── demo-script.md
│   └── contracts/               # alarm-sns-message.json, review-result.json
│
├── infra/
│   ├── cloudwatch-agent/         # config.json — mem_used_percent, disk_used_percent
│   └── demo-app/                 # bad_app.py — pushes RequestLatency/ErrorRate via boto3
│
├── lambda/                       # one deployable function, modular internally
│   ├── handler.py                 # entry point — SNS alarm path or custom-widget render path
│   ├── git_context.py             # GitHub commit/diff lookup
│   ├── llm_review.py              # Bedrock call — diagnosis + suggested fix
│   └── notify.py                  # custom widget + SNS notification
│
└── scripts/
    ├── deploy.sh                 # sam build && sam deploy, secrets from .env
    ├── trigger_chaos.sh           # curl helper: flip bad_app's chaos on/off
    └── seed_bad_commit.sh         # commits + pushes the deliberately-bad demo change
```

## Team

| Person | Owns | Depends on |
|---|---|---|
| A — Infra/metrics | EC2 setup, CloudWatch agent config, demo app deployment | Nothing — starts immediately |
| B — AWS scaffolding | SAM template: Lambda, SNS, CloudWatch alarm + anomaly detection, IAM | Nothing blocking — can scaffold against `docs/contracts/` immediately |
| C — Git integration | `lambda/git_context.py` | A repo with commit history |
| D — LLM agent + payoff | `lambda/llm_review.py`, `lambda/notify.py`, stretch: GitHub comment / draft PR | Bedrock model access request (submit first) |

## Getting started

1. Request Bedrock model access (biggest external dependency — do this first).
2. Copy `.env.example` to `.env` and fill in `GITHUB_TOKEN`, `GITHUB_OWNER`,
   `GITHUB_REPO`, `NOTIFY_EMAIL`.
3. Launch the EC2 instance, install the CloudWatch agent using
   `infra/cloudwatch-agent/config.json`, and run `infra/demo-app/bad_app.py`
   (grant the instance role `cloudwatch:PutMetricData`).
4. Deploy the stack: `./scripts/deploy.sh` (wraps `sam build && sam deploy`,
   confirm the `BedrockModelId` parameter in `template.yaml` against your
   actual Bedrock access first).
5. Confirm the SNS email subscription, then use `scripts/trigger_chaos.sh`
   to flip on latency/error chaos and watch the pipeline fire end to end.

See [docs/architecture.md](docs/architecture.md) for full setup notes, AWS
free-tier accounting, and the build order.
