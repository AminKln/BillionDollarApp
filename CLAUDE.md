# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project summary

A hackathon project (AWS + GenAI, Ottawa 2026, Team 06): watches an app's
latency/error metrics and host system load, detects anomalies via
CloudWatch's built-in ML anomaly detection, and — when one fires — pulls
context (a git diff and/or CloudWatch logs) and sends it to an LLM for
root-cause diagnosis and a suggested fix. Full design rationale, free-tier
accounting, and build order are in `docs/architecture.md`; demo timing/talk
track is in `docs/demo-script.md`.

## Two coexisting pipeline implementations

The repo contains **two separate, non-integrated implementations** of
"anomaly → LLM diagnosis." Don't assume one supersedes the other without
checking git history/branch — neither doc set (`README.md`/
`docs/architecture.md`) has been updated to describe the second one.

**1. `lambda/` — the documented, SAM-deployed pipeline** (matches
`README.md`, `docs/architecture.md`, `template.yaml`):
- `lambda/handler.py` — entry point, handles both SNS-alarm invocations and
  CloudWatch custom-widget render invocations in one Lambda.
- `lambda/git_context.py` — GitHub REST API: finds the last commit before
  the alarm fired, fetches the diff since then (dependency-free, plain
  `urllib`, no `requests`, so no Lambda build step is needed). Operates on
  *this* repo (the demo app lives in-repo under `infra/demo-app/`).
- `lambda/llm_review.py` — calls **Bedrock** (`bedrock-runtime.invoke_model`)
  with the diff, gets back diagnosis + suggested fix as JSON matching
  `docs/contracts/review-result.json`.
- `lambda/notify.py` — publishes the verdict to an SNS email topic, and
  persists the "latest verdict" as this same Lambda's own environment
  variable (via `lambda:UpdateFunctionConfiguration`) so the CloudWatch
  custom-widget render path can read it back with no database. This is a
  deliberate single-slot, eventually-consistent store — not a real
  datastore (DynamoDB/S3 were dropped from the MVP, see
  `docs/architecture.md` §2/§7).
- Deployed via SAM (`template.yaml`, `scripts/deploy.sh`).

**2. `pipeline/` — a standalone context-builder + LLM diagnosis, not
Lambda-deployed, no docs update yet:**
- `pipeline/diagnose.py` — **the integration point for the anomaly-detection
  side.** `diagnose(event) -> str` wraps everything below into one call:
  give it whatever the detector produces (alarm name, alarm-message dict,
  raw SNS envelope, or a JSON string of one) and it returns Claude's full
  diagnosis as text. Defaults `GIT_REPO_PATH`/`INSTANCE_ID`/`AWS_REGION` to
  this project's fixed demo target so the caller only needs
  `ANTHROPIC_API_KEY` set — no other config required. Bootstraps its own
  `sys.path` so it resolves correctly when imported from any directory
  (verified via `importlib` from an unrelated directory with nothing else
  on the path).
- `pipeline/cloudwatch_context.py` — real CloudWatch alarm definition
  (handles both classic-threshold and metric-math/anomaly-detection alarm
  shapes), metric datapoints around the trigger, EC2 instance metadata
  (via `INSTANCE_ID` env var — this project's alarms dimension on `App`,
  not `InstanceId`, so it isn't inferred from the alarm), and log lines if
  `LOG_GROUP` is set. Every field that can't be fetched carries an explicit
  note instead of being silently omitted or faked.
- `pipeline/code_context.py` — pulls the **entire current source tree**
  (every tracked, non-binary file, in full via local `git`/subprocess, not
  the GitHub API or a diff) from **a different, external repo**
  ([`Tehreem404/bad_app_demo`](https://github.com/Tehreem404/bad_app_demo),
  set via `GIT_REPO_PATH` — a local checkout or a clone URL), plus a
  one-line "as of commit X" freshness note. Deliberately whole-codebase, not
  commit diffs — the full current code was judged more useful for diagnosis
  than PR/commit-level history. Files are ranked by relevance to the
  alarm's metric name only for trimming order if the codebase exceeds the
  40k-char prompt budget (`MAX_TOTAL_CODE_CHARS`), not for filtering.
- `pipeline/build_prompt.py` — combines both into one natural-language
  prompt string (`build_diagnosis_prompt()`), meant to be dropped straight
  into a **direct Anthropic API** `messages.create()` call. Does not call
  the LLM itself.
- Not deployed anywhere — no `template.yaml` resource references it; run
  locally or adapt into a Lambda handler as needed.

When editing "the LLM step" or "the anomaly pipeline," check which of
these two the user means — they target different app repos (this repo's
`infra/demo-app/` vs. the external `bad_app_demo`), use different LLM
providers (Bedrock vs. direct Anthropic API), and different git sourcing
(GitHub API diff-since-last-good-commit vs. a full local-checkout codebase
dump with no diff/commit history beyond a one-line freshness note).

## Running things

There is no test suite or linter in this repo. Dependency manifests:
`lambda/requirements.txt` (deliberately near-empty — `boto3` ships with the
Lambda runtime), `infra/demo-app/requirements.txt` (`flask`, `boto3`), and
`pipeline/requirements.txt` (`boto3`, `anthropic`).

**Get a real diagnosis from `pipeline/` (needs real AWS creds +
`ANTHROPIC_API_KEY`; everything else defaults to this project's demo
target):**
```bash
pip install -r pipeline/requirements.txt
cd pipeline
ANTHROPIC_API_KEY=... python3 diagnose.py <alarm_name_or_path_to_sns_message.json>
```
Or as a library call — this is the integration point for whoever builds the
anomaly-detection side: `from diagnose import diagnose; diagnose(event)`.

See `pipeline/README.md` for the full env var list and a real sample of the
assembled prompt. `build_prompt.py`/`build_diagnosis_prompt()` alone only
builds the prompt string without calling Claude, per the module docstring in
`pipeline/build_prompt.py`.

**Deploy the SAM-based `lambda/` pipeline:**
```bash
cp .env.example .env   # fill in GITHUB_TOKEN, GITHUB_OWNER, GITHUB_REPO, NOTIFY_EMAIL
./scripts/deploy.sh    # wraps: sam build && sam deploy --parameter-overrides ...
```
Confirm the `BedrockModelId` parameter in `template.yaml` against actual
granted Bedrock access before deploying — it's a placeholder
(`anthropic.claude-3-5-sonnet-20241022-v2:0`).

**Demo helpers for the `lambda/` pipeline:**
```bash
./scripts/trigger_chaos.sh latency on|off   # flips infra/demo-app/bad_app.py chaos toggles
./scripts/trigger_chaos.sh errors on|off    # DEMO_APP_HOST env var to target a non-localhost app
./scripts/seed_bad_commit.sh "<message>"    # commits+pushes the pre-staged bad change in bad_app.py
```

## Key architectural facts worth knowing before editing

- **One Lambda, two invocation shapes.** `lambda/handler.py` is invoked
  both by SNS (real alarm) and directly by CloudWatch (custom-widget
  render, `event["widgetContext"]`) — same function, branch at the top of
  `lambda_handler()`. Don't split this into two functions without updating
  `template.yaml`'s `DashboardWidgetInvokePermission` and the self-referencing
  `PersistLastVerdict` IAM policy (which hardcodes the function name via
  `${AWS::StackName}-review`).
- **Anomaly detection, not threshold alarms.** `template.yaml`'s
  `RequestLatencyAnomalyAlarm` uses a metric-math `ANOMALY_DETECTION_BAND`
  expression as `ThresholdMetricId`, not a hand-set number — this replaced
  an earlier Prometheus/Alertmanager design (see `docs/architecture.md` §2
  for the full "what changed and why").
- **CloudWatch's ~1 minute metric resolution + `EvaluationPeriods: 2`**
  means at least 2–3 minutes between triggering chaos and the alarm firing.
  This governs demo pacing (`docs/demo-script.md`) and is relevant to
  anything timing-sensitive.
- **`OK` state transitions also invoke the Lambda** (alarm actions fire on
  both `AlarmActions` and `OKActions`) — `_handle_alarm()` in
  `lambda/handler.py` explicitly ignores anything but `NewStateValue ==
  "ALARM"`. Don't remove that check.
- **Diff truncation cap:** `lambda/git_context.py` caps diffs at ~24k chars
  (`MAX_DIFF_CHARS`) before handing them to the LLM.
- **Data contracts live in `docs/contracts/`** — `alarm-sns-message.json`
  (what `lambda/handler.py` receives; `pipeline/cloudwatch_context.py`
  parses the same shape via `parse_alarm_message()`) and
  `review-result.json` (what `lambda/llm_review.py` returns / what
  `notify.py` renders — `pipeline/` doesn't produce this shape, it only
  builds the prompt, not a parsed verdict).
- **Secrets:** `.env` (gitignored) holds `GITHUB_TOKEN`, `GITHUB_OWNER`,
  `GITHUB_REPO`, `NOTIFY_EMAIL` for the `lambda/` pipeline; `deploy.sh`
  sources it and passes values through as `sam deploy
  --parameter-overrides`, never into `samconfig.toml`. `ANTHROPIC_API_KEY`
  (for sending `pipeline/`'s prompt to Claude) is expected as a bare
  environment variable, not currently listed in `.env.example`.
