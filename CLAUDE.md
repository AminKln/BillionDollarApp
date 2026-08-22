# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project summary

A hackathon project (AWS + GenAI, Ottawa 2026, Team 06): a real CloudWatch
anomaly-detection alarm on a demo app fires -> an SNS-triggered Lambda
gathers real alarm/metric evidence plus the app's real codebase -> both go
into one prompt sent to Claude for a grounded root-cause diagnosis. The demo
app itself lives in a **separate repo**
([`Tehreem404/bad_app_demo`](https://github.com/Tehreem404/bad_app_demo)),
not this one — this repo only reacts to what that app's alarm publishes.

This is `feature/LLM_diagnosis_complete` — the merge of two branches built
in parallel:
- `feature/LLM_diagnosis` -> `agent/`: the actual SNS-triggered, deployed
  Lambda pipeline (stack `anomaly-review-agent`).
- `feature/LLM_diagnosis_context` -> `pipeline/`: a standalone context
  builder + `diagnose()` library call, developed and tested independently.

The integration work this merge did: `agent/`'s codebase-context step
(`agent/github_context.py`) was a placeholder stub; `pipeline/`'s
`code_context.py` had already proven "dump the whole current codebase"
as the design. `agent/github_context.py` now has a real implementation of
that same idea, rebuilt against the GitHub REST API instead of a local
`git clone`, because the Lambda runtime `agent/` deploys into has no `git`
binary. **`docs/current-pipeline.md` is the authoritative, up-to-date
architecture doc — read it before `docs/architecture.md`, which describes
the original (now superseded) Bedrock/`lambda/`-based plan.**

## `agent/` vs `pipeline/` — two implementations, same design, different jobs

**`agent/` — the real, deployed, SNS-triggered pipeline.** This folder
*is* `template.yaml`'s Lambda `CodeUri` (no build/staging step):
- `agent/handler.py` — `lambda_handler(event, context)`, SNS entry point.
  Parses `event.Records[*].Sns.Message`, ignores anything where
  `NewStateValue != "ALARM"`, calls `_handle_alarm()`.
- `agent/context_builder.py` — real CloudWatch alarm/metric/log evidence
  (`boto3`). Handles both classic-threshold and metric-math/anomaly-band
  alarm shapes (`describe_alarms`'s response shape differs between them —
  see the note in `get_alarm_details()`). Log evidence is skipped (not
  faked) when `LOG_GROUP` is unset — no log group exists for the demo app
  yet.
- `agent/github_context.py` — `get_codebase_context_from_github(owner,
  repo, ref, metric_name)`: real fetch of the **target app's** entire
  current source tree via the GitHub REST API (`git/trees` + `git/blobs`),
  ranked by relevance to `metric_name` when it exceeds a 40k-char budget.
  `owner`/`repo` point at `bad_app_demo`, not this repo.
- `agent/llm_agent.py` — `diagnose_incident()`: one Claude call
  (`claude-sonnet-5`), **forced tool use** (`report_root_cause`) so the
  result is always structured (`hypothesis`/`confidence`/
  `supporting_evidence`/`suggested_action`), never free-text-JSON-and-hope.
  `max_tokens=1024` — confirmed *not* to hit the empty-response failure
  mode adaptive thinking can cause on `claude-sonnet-5` at a low cap (see
  `pipeline/`'s note below); that failure mode was only observed on
  free-text (no forced tool) calls.
- `agent/run_manual.py` + `agent/fake_alert.py` + `agent/fixtures.py` — CLI
  entry point against a **fake** alert (canned CloudWatch data via
  `botocore.stub.Stubber`, two scenarios), still calls the real
  `github_context`/`llm_agent` — `./scripts/run_manual.sh [--scenario
  db_timeout|ambiguous_5xx]`.
- `agent/codebase_context.md` — leftover from the original stub design
  (fetch-one-file-from-this-repo). No longer read by anything;
  `github_context.py` now fetches the target app's whole repo instead.
  Left in place, flagged rather than deleted.
- Deployed via SAM (`template.yaml`, `scripts/deploy.sh`), stack
  `anomaly-review-agent`, subscribed to the real `culprit-alerts` SNS topic.

**`pipeline/` — standalone, not Lambda-deployed, for local/manual use and
as a library.** See `pipeline/README.md` for full usage.
- `pipeline/diagnose.py` — **the one-call integration point** for calling
  this from outside a Lambda: `diagnose(event) -> str`. Give it whatever an
  external detector has (alarm name / alarm-message dict / raw SNS
  envelope), it returns Claude's diagnosis as free text (not the structured
  `agent/llm_agent.py` shape). Defaults `GIT_REPO_PATH`/`INSTANCE_ID`/
  `AWS_REGION` to this project's demo target so only `ANTHROPIC_API_KEY` is
  required. Bootstraps its own `sys.path`, so it resolves correctly when
  imported from any directory.
- `pipeline/cloudwatch_context.py` — CloudWatch evidence, same design as
  `agent/context_builder.py` (independently built, same anomaly-band-alarm
  handling). Also honors an SNS message's own `NewStateValue`/
  `StateChangeTime` over the alarm's live (possibly since-changed) state —
  `agent/context_builder.py` does not do this yet.
- `pipeline/code_context.py` — same "whole current codebase, not a diff"
  design as `agent/github_context.py`, but via a **local `git clone`**
  instead of the GitHub API (fine here since this doesn't run in Lambda).
  Keep the skip-list/budget/relevance logic in sync between the two if
  either changes.
- `pipeline/build_prompt.py` — assembles both into one prompt string
  (`build_diagnosis_prompt()`); doesn't call an LLM itself.
- Verified: `claude-sonnet-5` free-text (no forced tool) calls can spend an
  entire low `max_tokens` budget on adaptive thinking and return **empty**
  text — fixed by using `max_tokens=16000`. This is a real, previously-hit
  failure mode, not a hypothetical.

**When editing "the LLM step" or "the anomaly pipeline," check which of
these two the user means.** Same target app, same "full codebase, not
diff" design, same Claude model — but different output shape (structured
tool-use vs. free text), different codebase-fetch mechanism (GitHub API vs.
local git), and only `agent/` is actually wired to fire automatically.

## Running things

There is no test suite or linter in this repo. Dependency manifests:
`agent/requirements.txt` (just `anthropic` — `boto3` ships with the Lambda
runtime) and `pipeline/requirements.txt` (`boto3`, `anthropic`).

**Get a real diagnosis via `pipeline/` (local, no deploy; needs real AWS
creds + `ANTHROPIC_API_KEY`; everything else defaults to this project's
demo target):**
```bash
pip install -r pipeline/requirements.txt
cd pipeline
ANTHROPIC_API_KEY=... python3 diagnose.py <alarm_name_or_path_to_sns_message.json>
```
Or as a library call: `from diagnose import diagnose; diagnose(event)`.

**Test the real, deployed `agent/` pipeline's logic locally without SNS**
(needs real AWS creds + `ANTHROPIC_API_KEY`; set `GITHUB_OWNER=Tehreem404
GITHUB_REPO=bad_app_demo` first):
```python
import handler
handler._handle_alarm({"AlarmName": "culprit-App-Anomaly", "NewStateValue": "ALARM", "NewStateReason": "..."})
```

**Run `agent/` against a fake alert** (no AWS creds needed for the
CloudWatch side, but still makes a real GitHub + Claude call):
```bash
./scripts/run_manual.sh --scenario db_timeout   # or ambiguous_5xx
```

**Deploy/redeploy the SAM-based `agent/` pipeline:**
```bash
cp .env.example .env   # fill in ANTHROPIC_API_KEY, ALARM_TOPIC_ARN, GITHUB_OWNER=Tehreem404, GITHUB_REPO=bad_app_demo
./scripts/deploy.sh    # wraps: sam build && sam deploy --parameter-overrides ...
```
As of this branch's merge, this has **not yet been redeployed** — the live
`anomaly-review-agent-review` function still runs the old placeholder
codebase-context stub until this is run again.

## Key architectural facts worth knowing before editing

- **This repo doesn't own the alarm or the app.** Both live in the
  separate `bad_app_demo` repo/EC2 instance. `template.yaml`'s
  `AlarmTopicArn` parameter subscribes to an **existing** SNS topic
  (`culprit-alerts`) rather than creating one — see `docs/current-pipeline.md`
  §7 for the full list of what changed from the original all-in-one-repo
  design and why.
- **`OK` state transitions also invoke the Lambda** (CloudWatch alarm
  actions fire on both `ALARM` and `OK` transitions) — `_handle_alarm()` in
  `agent/handler.py` explicitly ignores anything but `NewStateValue ==
  "ALARM"`. Don't remove that check.
- **Anomaly-band alarms need special parsing.** `culprit-App-Anomaly` is a
  metric-math (`ANOMALY_DETECTION_BAND`) alarm — `describe_alarms`'s
  response omits top-level `Namespace`/`MetricName`/`Threshold` for these
  and nests them in a `Metrics[]` array instead, with no fixed threshold
  (the band is dynamic). Both `agent/context_builder.py`'s
  `get_alarm_details()` and `pipeline/cloudwatch_context.py`'s
  `get_alarm_definition()` handle this — don't regress either back to
  assuming the simple/classic alarm shape.
- **No `git` binary in Lambda.** `agent/github_context.py` fetches the
  target repo entirely through the GitHub REST API (trees + blobs) for
  exactly this reason — don't "simplify" it to a `git clone` without adding
  a Lambda layer that bundles `git`, or it'll fail at runtime.
- **Data contracts live in `docs/contracts/`** — `alarm-sns-message.json`
  (what `agent/handler.py` and `pipeline/cloudwatch_context.py`'s
  `parse_alarm_message()` both parse) and `review-result.json` (the shape
  `agent/llm_agent.py`'s forced tool-use call returns — `pipeline/` doesn't
  produce this shape, it returns free text).
- **Secrets:** `.env` (gitignored) holds `ANTHROPIC_API_KEY`,
  `ALARM_TOPIC_ARN`, `GITHUB_OWNER`/`GITHUB_REPO`/`GITHUB_REF`/
  `GITHUB_TOKEN` (target app's repo, not this one), `LOG_GROUP` (optional).
  `deploy.sh` sources it and passes values through as `sam deploy
  --parameter-overrides`, never into `samconfig.toml`.
