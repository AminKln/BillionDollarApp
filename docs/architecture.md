# Architecture

> Supersedes the earlier Prometheus/Alertmanager/Grafana design. This is the
> current, final architecture — CloudWatch-native, with a fix-suggestion
> capability added on top of root-cause diagnosis.

## 1. Project summary

A system that watches an app's latency and host-level system load (CPU/RAM/disk),
detects anomalies using CloudWatch's built-in ML anomaly detection, and — when
one fires — automatically pulls the code diff since the last commit before the
anomaly started, sends it to an LLM (Bedrock/Claude) for root-cause analysis,
and gets back both a diagnosis **and a suggested fix**. The result is surfaced
on a CloudWatch dashboard and sent as a notification.

Stack: AWS only, kept inside free tier wherever possible. GitHub for source
control. Amazon Bedrock (Claude) for the review agent.

## 2. Architecture — current design (CloudWatch-native)

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

### What this replaced, and why

Originally designed around self-hosted Prometheus + Alertmanager + Grafana on
EC2. Pivoted to CloudWatch-native because, once the project requirements were
no longer locked to "Prometheus" specifically, CloudWatch removed real setup
surface with no functional loss:

- **3 containers gone** (Prometheus, Alertmanager, Grafana) — only the demo
  app + CloudWatch agent run on EC2 now.
- **No PromQL to write/tune** — anomaly detection is enabled on a metric, not
  hand-written as a threshold rule.
- **No API Gateway or webhook parsing** — SNS invokes the Lambda directly;
  CloudWatch's alarm-state-change message format is well-documented and
  consistent.
- **No EC2 free-tier risk beyond the one unavoidable box** — CloudWatch/SNS/
  Lambda don't carry the EC2-specific "only if your account predates Jul 15,
  2025" caveat.

**What did NOT go away:** the EC2 instance itself. Genuine host-level
CPU/RAM/disk metrics require an instance with the CloudWatch agent installed,
exactly as they would have required `node_exporter` under Prometheus.
Switching monitoring tools doesn't remove this requirement — it's orthogonal
to the tool choice.

**What was given up:** Grafana's arbitrary-text annotation API. Replaced with
a **CloudWatch custom widget** — a Lambda-backed widget on the dashboard that
returns HTML/Markdown and can be refreshed with the LLM's verdict. Confirmed
real AWS feature (`AmazonCloudWatch/latest/monitoring/add_custom_widget_dashboard.md`):
the Lambda receives a `widgetContext` payload and returns a string of HTML,
JSON, or Markdown, which the dashboard renders directly. No JavaScript
allowed in the returned HTML, but CSS and SVG are supported for a polished
look.

**Also intentionally dropped for the MVP** (not because they were wrong
ideas, but because they add zero functional value for a 24-hour demo): API
Gateway, Step Functions, DynamoDB. Alertmanager's `repeat_interval`-style
dedup concern doesn't apply here either — CloudWatch alarms only re-invoke
SNS on state *transitions* (OK→ALARM), not on every evaluation.

**Demo-timing caveat to plan around:** CloudWatch's standard metric
resolution is ~1 minute (vs. Prometheus's default 15s scrape). Build lead
time into the live demo — flip the chaos toggle well before you want to point
at the fired alarm. High-resolution custom metrics (sub-minute) are possible
at a small extra cost if this becomes a problem in rehearsal.

## 3. Metrics

- **Latency**: pushed by the demo app itself via
  `boto3.client('cloudwatch').put_metric_data()`, batched every 10s (see
  `infra/demo-app/bad_app.py`). Custom namespace `HackathonDemo`, metric
  `RequestLatency`.
- **Error rate**: same app, same batching, metric `ErrorRate`.
- **CPU**: automatic from EC2, no agent needed.
- **RAM / disk**: requires the CloudWatch agent installed and configured on
  the instance (`mem_used_percent`, `disk_used_percent`, see
  `infra/cloudwatch-agent/config.json`) — this is the one piece of "system
  load" instrumentation still needed, equivalent effort to what
  `node_exporter` would have required.

## 4. Anomaly detection & alarm

Enable CloudWatch Anomaly Detection on the `RequestLatency` metric (and
optionally CPU), then create an alarm on "outside the expected band." This
replaces hand-written PromQL threshold logic. Alarm action: publish to an SNS
topic. Implemented in `template.yaml` as an `AWS::CloudWatch::AnomalyDetector`
+ `AWS::CloudWatch::Alarm` pair using a metric-math `ANOMALY_DETECTION_BAND`
expression as the alarm's threshold metric.

Alarm cost note: an anomaly-detection alarm bills as 3 alarm-metrics (actual
value + upper + lower band) instead of 1, so ~$0.30/month vs $0.10 — still
comfortably inside the free tier's first-10-alarm-metrics allowance for a
demo, just worth knowing if more alarms get added later.

## 5. Git-context step (`lambda/git_context.py`)

1. Map the alarm to a `{owner}/{repo}` — hardcoded via `GITHUB_OWNER`/
   `GITHUB_REPO` env vars is fine for a single demo repo.
2. `GET /repos/{owner}/{repo}/commits?until={alarm timestamp}&per_page=1` →
   last known-good commit.
3. `GET /repos/{owner}/{repo}/compare/{goodSha}...HEAD` → diff + commit list
   since then.
4. Truncate/chunk large diffs (cap ~6–8k tokens, ~24k chars) before handing
   to the LLM step.

## 6. LLM review step (`lambda/llm_review.py`)

Bedrock `InvokeModel` call. The prompt goes beyond diagnosis to also request
a fix, tiered by ambition — Tier 1 is built now, Tiers 2 and 3 are stretch
goals in that order:

**Tier 1 — suggested fix as text (built, ~free given the pipeline already
exists).** Prompt addition:

> "In addition to identifying the likely cause, propose the smallest safe
> code fix as a unified diff. If a full fix isn't confidently inferable from
> the diff alone, suggest the smallest safe mitigation instead (e.g. add a
> timeout, add a guard clause, revert the specific hunk) and say why. Note
> any residual risk the fix doesn't cover."

Returns structured JSON (see `docs/contracts/review-result.json`) and renders
it in the custom widget / notification.

**Tier 2 — post the suggestion as a GitHub commit comment (~30–60 min
stretch, not yet implemented).** `POST /repos/{owner}/{repo}/commits/{suspect_sha}/comments`
with the explanation + suggested diff. Reuses the GitHub client already built
for Tier 1/`git_context.py`.

**Tier 3 — auto-open a draft PR with the fix applied (stretch, not yet
implemented; only if 1–2 are solid with time to spare, ~3–4 hrs).**

- Don't have the model output a diff to programmatically apply —
  `git apply`/`patch` fails easily on whitespace/context mismatches. Instead,
  send the full current file content in the prompt and ask for the full
  corrected file back, then `PUT` it via the GitHub Contents API to a new
  branch.
- Scope the fix to a single file only — multi-file changes are much harder
  to apply reliably under time pressure.
- Always open as a **draft PR** (`"draft": true`), never auto-merge. This is
  both the safer engineering choice and a good line for the pitch: the AI
  proposes, a human approves.
- Sequence: `POST /git/refs` (new branch) → `PUT /contents/{path}` (commit
  corrected file) → `POST /pulls` (open as draft).

## 7. Notify step (`lambda/notify.py`)

- Update the CloudWatch custom widget with the verdict + suggested fix
  (HTML/Markdown, per the API details in section 2).
- Publish to SNS (email) with the same content as a fallback/redundant
  channel.
- Tier 2/3 additions from above go here too once built.

**Implementation note on state:** the custom widget is invoked by CloudWatch
fresh each time the dashboard renders — it has no memory of its own, and
DynamoDB/S3 were deliberately dropped from the MVP (section 2). The smallest
zero-new-service way to hand the widget "the latest verdict" is to have
`notify.py` persist it as this same Lambda's own environment variable via
`lambda:UpdateFunctionConfiguration`, and have the widget-rendering branch of
`lambda/handler.py` read it back out. It's a single deployable function
either way (see the repo layout in section 12), so this keeps the whole
pipeline inside one Lambda with no extra storage service. Caveat: this is an
eventually-consistent, single-slot store (last verdict only) — fine for a
demo, not a substitute for a real datastore if this grows past the hackathon.

## 8. AWS services & free-tier notes

| Service | Role | Free tier |
|---|---|---|
| EC2 t3.micro | Demo app + CloudWatch agent | 750 hrs/mo for 12 months — **only for AWS accounts created before Jul 15, 2025**; newer accounts get $200 signup credit instead. Check Billing → Free Tier before relying on this. |
| CloudWatch | Custom metrics, anomaly detection alarm, custom widget | 10 custom metrics, first 10 alarm-metrics/mo free (an anomaly alarm uses 3 of those slots) |
| Lambda | Review agent (git-fetch, LLM call, notify) | 1M requests + 400k GB-s/mo, always-free |
| SNS | Alarm trigger + notifications | 1M publishes, 1,000 emails/mo, always-free |
| GitHub API | Commit/diff source, comments, draft PRs | Free for public/personal repos |
| Bedrock (Claude) | Root-cause analysis + fix generation | **No free tier** — pay per token from call one. At hackathon volume, a few cents total; new-account signup credits typically cover it. |

Guardrails: an AWS Budget alert at $1–2; store the GitHub token as a Lambda
env var encrypted with the default KMS key (Secrets Manager isn't free after
a 30-day per-secret trial).

## 9. Team split (4 people)

| Person | Owns | Depends on |
|---|---|---|
| A — Infra/metrics | EC2 setup, CloudWatch agent config, demo app deployment | Nothing — starts immediately |
| B — AWS scaffolding | SAM/CDK template: Lambda, SNS topic + subscription, CloudWatch alarm + anomaly detection config, IAM roles | Nothing blocking — can scaffold against the contract docs immediately |
| C — Git integration | `lambda/git_context.py` | A repo with commit history — create/use one immediately |
| D — LLM agent + payoff | `lambda/llm_review.py` (diagnosis + fix), `lambda/notify.py` (custom widget + SNS), stretch: GitHub comment / draft PR | **Bedrock model access request** — submit in the first 5 minutes, it can require a use-case form and isn't always instant |

### Known bottlenecks / dependencies

1. **Bedrock model access request** — the one dependency outside the team's
   control; submit before anything else.
2. **Shared AWS account/IAM setup** — needs one owner in the first 15
   minutes.
3. **Undefined JSON contracts** — agree on the alarm/SNS message shape and
   the review-result shape on paper before building (see `docs/contracts/`).
4. **`template.yaml` merge conflicts** — B owns edits; others request
   changes rather than editing directly.
5. **First real integration point** (B+C+D's pieces wired together for real)
   — inherently serial, schedule a dedicated block.
6. **Full end-to-end demo rehearsal** — needs all 4 people and a working
   pipeline; happens last.
7. **CloudWatch's ~1-minute metric resolution** — plan lead time into the
   live demo (see caveat in section 2).

### Suggested sync points

- Hour 0: 15-min huddle — submit Bedrock access, agree on JSON contracts,
  claim the AWS account.
- Hour ~4: check-in, everyone working against mocks.
- Hour ~8–9: scheduled integration session.
- Hour ~13–14: full run using the real demo app end to end.
- Last 2–3 hours: protected time for demo polish/rehearsal.

## 10. Build order (~11–14 hrs of work)

1. EC2 + demo app (boto3 metric push) + CloudWatch agent config — 1.5–2 hrs
2. Enable CloudWatch anomaly detection + alarm + SNS topic — 1 hr
3. Lambda skeleton subscribed to SNS, logging the real alarm message — 1 hr
4. `lambda/git_context.py` — 1–2 hrs
5. `lambda/llm_review.py` (Tier 1: diagnosis + suggested fix) — 1.5–2 hrs
6. `lambda/notify.py` (custom widget + SNS) — 1–1.5 hrs
7. Wire it all together end to end — 2 hrs
8. Buffer + demo rehearsal — 2–3 hrs
9. Stretch (only if time remains, in this order): Tier 2 GitHub comment →
   Tier 3 draft PR → API Gateway/Step Functions/DynamoDB if the hackathon
   scores on AWS service breadth

## 11. Demo app

`infra/demo-app/bad_app.py` + `requirements.txt`. Details:

- Pushes `RequestLatency` and `ErrorRate` to CloudWatch (namespace
  `HackathonDemo`) every 10s via `boto3`, batched rather than per-request.
- Generates its own steady baseline traffic via a background thread — no
  external load generator needed.
- Independent chaos toggles: `GET /chaos/latency/on|off`,
  `GET /chaos/errors/on|off`.
- Requires `cloudwatch:PutMetricData` — simplest via the EC2 instance's IAM
  role, no hardcoded keys.

Demo timing: flip `/chaos/latency/on` well before you want to point at the
fired alarm — see the resolution caveat in section 2.

## 12. Repo / folder structure

```
repo-root/
├── README.md
├── template.yaml              # SAM template — Lambda, SNS, CloudWatch alarm, IAM (Person B owns edits)
├── samconfig.toml
│
├── docs/
│   ├── architecture.md
│   ├── demo-script.md         # pitch timing, incl. the ~1min metric resolution lead-in
│   └── contracts/
│       ├── alarm-sns-message.json
│       └── review-result.json # now includes the suggested_fix object
│
├── infra/                     # Person A
│   ├── cloudwatch-agent/
│   │   └── config.json        # mem_used_percent, disk_used_percent
│   └── demo-app/
│       ├── bad_app.py
│       └── requirements.txt
│
├── lambda/                    # one deployable function, modular internally
│   ├── handler.py             # entry point — parses the SNS/alarm event or widget event, calls the rest
│   ├── git_context.py         # Person C
│   ├── llm_review.py          # Person D
│   ├── notify.py              # Person D
│   └── requirements.txt
│
└── scripts/
    ├── deploy.sh
    ├── trigger_chaos.sh
    └── seed_bad_commit.sh
```

Deliberate choice, unchanged from earlier planning: the demo app lives inside
this same repo under `infra/demo-app/`, so the repo `git_context.py` points
at *is* this repo. "Seeding the bad commit" is a literal commit to
`infra/demo-app/bad_app.py` right before the demo — the LLM reviews a real
diff in a real repo.

## 13. Data contracts

- **`alarm-sns-message.json`** — the CloudWatch alarm state-change message
  shape the Lambda receives via its SNS subscription (`AlarmName`,
  `NewStateValue`, `StateChangeTime`, `Trigger.MetricName`, etc.).
- **`review-result.json`** — what `lambda/llm_review.py` returns and what
  `lambda/notify.py` renders (suspect commit, confidence, explanation, and a
  `suggested_fix` object with `summary`/`diff`/`risk_note`).

## 14. Open decisions / TODOs

- [x] Write the SAM `template.yaml`: Lambda + IAM role (`bedrock:InvokeModel`,
      `sns:Publish`, `lambda:UpdateFunctionConfiguration`), SNS topics +
      Lambda subscription, CloudWatch anomaly detection alarm resource,
      dashboard with the custom widget.
- [x] Write `infra/cloudwatch-agent/config.json` for CPU/RAM/disk reporting.
- [x] Implement `lambda/handler.py` — parse the SNS/alarm event or the
      custom-widget event, call `git_context`, `llm_review`, `notify` in
      sequence.
- [x] Implement `lambda/git_context.py` — GitHub commits/compare lookup, diff
      truncation.
- [x] Implement `lambda/llm_review.py` — Bedrock InvokeModel call with the
      Tier 1 diagnosis+fix prompt, structured JSON parsing.
- [x] Implement `lambda/notify.py` — CloudWatch custom widget update + SNS
      publish.
- [x] Write `scripts/trigger_chaos.sh` and `scripts/seed_bad_commit.sh`.
- [ ] Confirm the exact Bedrock model ID/inference profile once Bedrock
      access is granted — `template.yaml`'s `BedrockModelId` parameter has a
      placeholder default that must be updated to whatever Claude model
      the team's Bedrock access actually covers.
- [ ] Confirm `GithubOwner`/`GithubRepo` parameter values against the real
      demo repo before first deploy.
- [ ] Stretch: Tier 2 GitHub commit comment, Tier 3 draft PR flow.
