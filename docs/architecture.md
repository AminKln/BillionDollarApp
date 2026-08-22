# Architecture

## 1. Project summary

An anomaly detection system on Prometheus metrics that, when an anomaly
segment starts, automatically triggers an LLM agent to review all code
committed since the last known-good commit before the anomaly, and surfaces a
root-cause verdict back onto the monitoring dashboard.

Stack: AWS (kept inside free tier wherever possible), self-hosted
Prometheus/Alertmanager/Grafana, GitHub for source control, Amazon Bedrock
(Claude) for the review agent.

## 2. Architecture — two pipelines

### Pipeline A: Detection & trigger

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

### Pipeline B: Agent review

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

Key semantics: an Alertmanager `firing` webhook = anomaly segment **start**;
the matching `resolved` webhook = segment **end**. That pair is the entire
"anomaly segment" concept — no separate ML anomaly-detection service is used.

## 3. Detection layer — Prometheus rule

Anomaly detection is a plain PromQL statistical rule (3-sigma rolling band),
not a separate ML service:

```yaml
# alert.rules.yml
- alert: LatencyAnomaly
  expr: >
    avg_over_time(http_request_duration_seconds[5m])
    > avg_over_time(http_request_duration_seconds[1h])
      + 3 * stddev_over_time(http_request_duration_seconds[1h])
  for: 3m
  labels: {severity: anomaly}
  annotations: {service: "{{ $labels.job }}"}
```

`for: 3m` matters for demo timing — flip chaos mode on ~3 minutes before you
want the alert to actually fire.

**Decision: why not CloudWatch Anomaly Detection instead?** Considered and
rejected for this project. Reasons: (a) it operates on CloudWatch metrics, not
native Prometheus format — would need an extra scrape/convert hop (CloudWatch
agent Prometheus-scrape mode, or the newer managed Prometheus→CloudWatch
collector); (b) anomaly-detection alarms cost 3x standard alarms internally
($0.30 vs $0.10/alarm-metric/month, because AWS tracks the actual metric plus
upper/lower band as 3 series) — still free-tier feasible for a couple of
alarms, but not worth the extra plumbing plus giving up the "Prometheus
metrics" framing of the project.

## 4. Git-context step (Lambda: fetch commits)

1. Map the alert's `service`/`job` label → `{owner}/{repo}` (hardcoded dict or
   DynamoDB config table is fine).
2. `GET /repos/{owner}/{repo}/commits?until={alert startsAt}&per_page=1` → last
   known-good commit.
3. `GET /repos/{owner}/{repo}/compare/{goodSha}...HEAD` → full diff + commit
   list since then.
4. Truncate/chunk large diffs (cap ~6–8k tokens) before sending to the LLM
   step.

## 5. LLM review step (Lambda: LLM review)

Bedrock `InvokeModel` call, system prompt shape:

> "You are an SRE reviewing a code diff against a Prometheus anomaly. Anomaly:
> {metric, threshold breached, magnitude, duration}. Diff: {diff}. Return the
> most likely culprit commit, confidence, and a one-paragraph explanation."

Ask for structured JSON output (commit SHA, confidence, explanation) so it can
be rendered and stored cleanly.

## 6. Demo payoff (do this — it's the strongest moment)

Since Grafana runs on the same EC2 box as Prometheus, have the final Lambda
POST to Grafana's `/api/annotations` endpoint, spanning the anomaly's time
range, with the LLM verdict as the annotation text. Result: judges see the
graph with a marker reading e.g. "likely caused by commit `a1b2c3` (added
unbounded retry loop) — 87% confidence" directly on the anomaly.

## 7. AWS services & free-tier notes

| Service | Role | Free tier |
|---|---|---|
| EC2 t3.micro | Prometheus + Alertmanager + Grafana + demo app | 750 hrs/mo for 12 months — **only for AWS accounts created before Jul 15, 2025**; newer accounts get $200 signup credit instead. Check Billing → Free Tier before relying on this. |
| Lambda | Ingest, git-fetch, LLM-review, notify | 1M requests + 400k GB-s/mo, always-free |
| API Gateway | Webhook receiver | 1M requests/mo, 12 months |
| DynamoDB | Segment + review state | 25GB storage, always-free |
| Step Functions | Orchestration | 4,000 state transitions/mo, always-free |
| S3 | Diff/report storage | 5GB, 12 months |
| SNS | Notifications | 1M publishes, 1,000 emails/mo, always-free |
| GitHub API | Commit/diff source | Free for public/personal repos |
| Bedrock (Claude) | Root-cause analysis | **No free tier** — pay per token from call one. At hackathon volume, a few cents total; new-account signup credits typically cover it. |

Guardrails to set up: an AWS Budget alert at $1–2, and store the GitHub token
as a Lambda env var encrypted with the default KMS key (Secrets Manager isn't
free after a 30-day per-secret trial).

## 8. Team split (4 people)

| Person | Owns | Depends on |
|---|---|---|
| A — Infra/metrics | EC2 box, docker-compose, Prometheus, Alertmanager, Grafana, demo app | Nothing — starts immediately |
| B — AWS scaffolding | SAM/CDK template, shared IAM roles, API Gateway → ingest Lambda → state store | A sample Alertmanager webhook payload (use AWS docs example, doesn't need to wait on A) |
| C — Git integration | Service→repo mapping, last-commit lookup, diff fetch via GitHub compare API | A repo with commit history — create/use one immediately |
| D — LLM agent + payoff | Bedrock prompt + InvokeModel call, Grafana annotation posting, SNS/Slack notify | **Bedrock model access request** — submit this in the first 5 minutes, it can require a use-case form and isn't always instant |

### Known bottlenecks / dependencies

1. **Bedrock model access request** — the one dependency outside the team's
   control; submit before anything else.
2. **Shared AWS account/IAM setup** — needs one owner in the first 15 minutes
   or permission errors eat an hour.
3. **Undefined JSON contracts** — biggest silent time-sink; agree on the
   webhook shape, anomaly-context shape, and review-result shape on paper
   *before* building (see `docs/contracts/`).
4. **Shared IaC file merge conflicts** — B owns `template.yaml` edits; others
   request changes rather than editing directly.
5. **First real integration point** (B+C+D's Lambdas wired together for real)
   — inherently serial, schedule a dedicated block for it, don't leave it
   implicit.
6. **Full end-to-end demo rehearsal** — needs all 4 people and a working
   pipeline simultaneously; can only happen last.

### Suggested sync points

- Hour 0: 15-min huddle — submit Bedrock access, agree on JSON contracts,
  claim the AWS account.
- Hour ~5: check-in, everyone working against mocks.
- Hour ~10–12: scheduled integration session.
- Hour ~18–20: full run using the real demo app end to end.
- Last 2–3 hours: protected time for demo polish/rehearsal — don't let
  feature work eat this.

## 9. Pre-hackathon setup checklist

**Do beforehand (check hackathon rules on pre-written code first — generic
scaffolding is normally fine, a pre-built solution usually isn't):**

- Request Bedrock model access now (biggest risk item).
- Create/confirm shared AWS account + IAM roles for all 4 people.
- Set a billing alarm/budget ($5–10).
- Confirm Bedrock + the target Claude model are available in the chosen
  region.
- Install/auth tooling on every laptop: AWS CLI, SAM CLI or CDK, Docker.
- Create the demo GitHub repo, generate a PAT, test it against the `commits`
  and `compare` endpoints.
- Skeleton SAM/CDK template with empty Lambda stubs, IAM roles, API Gateway
  route, DynamoDB table.
- `docker-compose.yml` for Prometheus + Alertmanager + Grafana with images
  pre-pulled.
- Minimal Bedrock `InvokeModel` "hello world" test script, confirmed working
  end to end.
- Decide the demo bug in advance (e.g., a commit that removes a rate limit or
  adds an N+1 query) — don't improvise this live.
- Agree on the JSON contracts (see below) and repo/branching conventions.

## 10. 24-hour build order

1. EC2 + docker-compose (Prometheus, Alertmanager, Grafana, demo app) — 1–2 hrs
2. Tune the PromQL rule until it reliably fires on demand — 1–2 hrs
3. API Gateway → Lambda → DynamoDB plumbing (log payload first, add logic
   after) — 2–3 hrs
4. GitHub commits/diff Lambda — 1–2 hrs
5. Bedrock call + prompt iteration — 1–2 hrs
6. Grafana annotation + SNS/Slack notify — 1–2 hrs
7. End-to-end testing, bug fixing, demo prep — 3–5 hrs (always underestimated
   — don't skip)

Cuts to protect the finish line if time gets tight: skip Step Functions and
chain Lambdas directly; skip DynamoDB dedup logic (a flag in S3 or "always
trigger on firing" is fine); pre-stage the "bad commit" rather than relying on
a real regression.

## 11. Demo app

A minimal Flask app (`infra/demo-app/bad_app.py`) that:

- Exposes `/metrics` in Prometheus format (`http_requests_total`,
  `http_request_duration_seconds`).
- Generates its own steady baseline traffic via a background thread — no
  external load generator needed.
- Exposes independent chaos toggles: `GET /chaos/latency/on|off` and
  `GET /chaos/errors/on|off`.
- Point Prometheus's scrape config at this app's `:8000/metrics`; the
  `LatencyAnomaly` rule above should fire against it with no changes.

Demo timing note: hit `/chaos/latency/on` ~3 minutes before you want to point
at the firing alert, since the rule has `for: 3m`.

## 12. Repo / folder structure

```
repo-root/
├── README.md
├── template.yaml              # SAM template — shared IaC (Person B owns edits)
├── samconfig.toml
│
├── docs/
│   ├── architecture.md
│   ├── demo-script.md         # pitch timing: when to flip chaos on, what to say
│   └── contracts/             # agreed at hour 0, rarely touched after
│       ├── alertmanager-webhook.json
│       ├── anomaly-context.json
│       └── review-result.json
│
├── infra/                     # Person A
│   ├── docker-compose.yml
│   ├── prometheus/
│   │   ├── prometheus.yml
│   │   └── alert.rules.yml
│   ├── alertmanager/
│   │   └── alertmanager.yml
│   ├── grafana/
│   │   └── provisioning/
│   └── demo-app/
│       ├── bad_app.py
│       └── requirements.txt
│
├── functions/                 # one folder per Lambda, matches SAM's build model
│   ├── anomaly_ingest/        # Person B
│   │   ├── app.py
│   │   └── requirements.txt
│   ├── git_context/           # Person C
│   │   ├── app.py
│   │   └── requirements.txt
│   ├── llm_review/            # Person D
│   │   ├── app.py
│   │   └── requirements.txt
│   └── notify/                # Person D
│       ├── app.py
│       └── requirements.txt
│
└── scripts/
    ├── deploy.sh              # sam build && sam deploy wrapper
    ├── trigger_chaos.sh       # curl helper: flip bad_app's chaos on/off
    └── seed_bad_commit.sh     # commits the deliberately-bad demo change
```

Deliberate choice: the demo app lives inside this same repo under
`infra/demo-app/`, so the repo the `git_context` Lambda points at *is* this
repo. "Seeding the bad commit" becomes a real, literal commit to
`infra/demo-app/bad_app.py` right before the demo — the LLM reviews an actual
diff in an actual repo, not a mocked one.

## 13. Data contracts

Defined at hour 0, before parallel work starts, so B/C/D can build against
mocks independently. Committed under `docs/contracts/`:

- **Alertmanager webhook shape** (`alertmanager-webhook.json`) — what the
  ingest Lambda receives (`status`, `startsAt`, `endsAt`, `labels`,
  `annotations`).
- **Anomaly-context object** (`anomaly-context.json`) — what gets passed into
  the LLM prompt (metric name, threshold breached, magnitude, duration,
  service/repo mapping).
- **Review-result object** (`review-result.json`) — what the LLM step returns
  and what gets stored/displayed (suspect commit SHA, confidence,
  explanation, suggested action).

## 14. Open decisions / TODOs

- [ ] Confirm whether Lambdas chain directly or via Step Functions (folder
      structure supports either).
- [ ] Write the actual SAM `template.yaml` with IAM roles for all Lambdas, API
      Gateway route, DynamoDB table, S3 bucket, SNS topic.
- [ ] Implement `functions/anomaly_ingest/app.py` — parse Alertmanager
      webhook, write/update DynamoDB segment record.
- [ ] Implement `functions/git_context/app.py` — GitHub commits/compare
      lookup, diff truncation logic.
- [ ] Implement `functions/llm_review/app.py` — Bedrock InvokeModel call,
      structured JSON parsing.
- [ ] Implement `functions/notify/app.py` — SNS publish + Grafana
      `/api/annotations` POST.
- [ ] Write `infra/prometheus/alert.rules.yml`, `infra/alertmanager/alertmanager.yml`,
      `infra/docker-compose.yml`.
- [ ] Write `scripts/trigger_chaos.sh` and `scripts/seed_bad_commit.sh`.
