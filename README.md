# DevOps for GenAI — Ottawa Hackathon Series 2026 — Team 06

## Prometheus Anomaly → LLM Code Review Agent

An anomaly detection system on Prometheus metrics that, when an anomaly segment
starts, automatically triggers an LLM agent to review all code committed since
the last known-good commit before the anomaly, and surfaces a root-cause
verdict back onto the monitoring dashboard.

Stack: AWS (free tier where possible), self-hosted Prometheus / Alertmanager /
Grafana, GitHub for source control, Amazon Bedrock (Claude) for the review
agent.

## How it works

**Pipeline A — Detection & trigger**

```
Prometheus + Alertmanager (EC2, self-hosted)
        │  (webhook on alert firing/resolved)
        ▼
API Gateway
        │
        ▼
Lambda: anomaly ingest  (parses alert, dedups)
        │
        ▼
DynamoDB: anomaly segments  (tracks start/end timestamps)
        │
        ▼
Step Functions  (triggers the review workflow)
```

**Pipeline B — Agent review**

```
Step Functions workflow
        │
        ▼
Lambda: fetch commits  (GitHub REST API)
        │
        ▼
Lambda: LLM review  (Bedrock, Claude)
        │
        ▼
S3 + DynamoDB  (stores diff and verdict)
        │
        ▼
SNS + Grafana annotation  (notifies team, marks the graph)
```

An Alertmanager `firing` webhook marks the anomaly segment **start**; the
matching `resolved` webhook marks its **end**. That pair is the entire
"anomaly segment" concept — no separate ML anomaly-detection service is used.
Detection itself is a plain PromQL 3-sigma rolling-band rule. See
[docs/architecture.md](docs/architecture.md) for the full design and
[docs/demo-script.md](docs/demo-script.md) for the live demo walkthrough.

## Repo layout

```
repo-root/
├── template.yaml              # SAM template — shared IaC (Person B owns edits)
├── samconfig.toml
│
├── docs/
│   ├── architecture.md
│   ├── demo-script.md
│   └── contracts/             # JSON contracts agreed at hour 0
│
├── infra/                     # Person A — Prometheus/Alertmanager/Grafana/demo app
├── functions/                 # one folder per Lambda
│   ├── anomaly_ingest/        # Person B
│   ├── git_context/           # Person C
│   ├── llm_review/            # Person D
│   └── notify/                # Person D
│
└── scripts/                   # deploy + demo helper scripts
```

## Team

| Person | Owns | Depends on |
|---|---|---|
| A — Infra/metrics | EC2 box, docker-compose, Prometheus, Alertmanager, Grafana, demo app | Nothing — starts immediately |
| B — AWS scaffolding | SAM/CDK template, shared IAM roles, API Gateway → ingest Lambda → state store | A sample Alertmanager webhook payload |
| C — Git integration | Service→repo mapping, last-commit lookup, diff fetch via GitHub compare API | A repo with commit history |
| D — LLM agent + payoff | Bedrock prompt + InvokeModel call, Grafana annotation posting, SNS/Slack notify | Bedrock model access request (submit first) |

## Getting started

1. Request Bedrock model access (biggest external dependency — do this first).
2. Set up the shared AWS account, IAM roles, and a billing budget alarm.
3. Bring up `infra/docker-compose.yml` (Prometheus, Alertmanager, Grafana, demo app).
4. Deploy the SAM stack in `template.yaml` via `scripts/deploy.sh`.
5. Use `scripts/trigger_chaos.sh` to flip on latency/error chaos in the demo app
   and watch the pipeline fire end to end.

See [docs/architecture.md](docs/architecture.md) for full setup notes, AWS
free-tier accounting, and the 24-hour build order.
