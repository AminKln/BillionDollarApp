# Current pipeline (`feature/LLM_diagnosis_complete` branch)

> Merges `feature/LLM_diagnosis` (this file's original branch — the
> deployed SNS→Lambda trigger, `agent/`) with `feature/LLM_diagnosis_context`
> (`pipeline/` — a real, full-codebase context builder, tested standalone).
> The merge's actual integration work: `agent/github_context.py`'s
> `get_codebase_context_from_github()` stub is now a real implementation —
> see §5. `architecture.md` describes the *original* hackathon design
> (Bedrock, git-diff correlation, notify/dashboard-widget) and is **not**
> current — this document supersedes it for anything it disagrees on.

## TL;DR

A CloudWatch alarm (owned in a separate repo, not this one) publishes to an
SNS topic. A Lambda subscribed to that topic gathers real alarm/metric/log
evidence via `boto3`, fetches the target app's **entire current codebase**
live from GitHub, combines both into one prompt, and sends it to Claude via
the direct Anthropic API for a grounded root-cause hypothesis. The verdict
is logged and returned as the Lambda's invocation result — there is no
notification or dashboard-widget step; both existed in the original design
and were deliberately removed for a simpler MVP.

Deployed and **verified against real AWS data** (not just fixtures) as of
this writing: stack `anomaly-review-agent`, region `us-east-1`, subscribed
to the real `culprit-alerts` SNS topic behind the real `culprit-App-Anomaly`
alarm. The codebase-context fetch is verified against the real target repo
([`Tehreem404/bad_app_demo`](https://github.com/Tehreem404/bad_app_demo)) —
see §5.

## 1. Request flow

```
(separate repo) app + CloudWatch anomaly-detection alarm
    │  not owned by this repo — this pipeline only reacts to what it publishes
    ▼
SNS topic "culprit-alerts"
    ▼
Lambda  agent/handler.py :: lambda_handler(event, context)
    │  parses event.Records[*].Sns.Message (JSON string) -> alarm dict
    │  ignores anything where NewStateValue != "ALARM"
    ▼
_handle_alarm(alarm)
    │
    ├─▶ context_builder.build_incident_context(alarm_name, log_group, region)
    │       ├─ get_alarm_details()   -- boto3 cloudwatch.describe_alarms
    │       ├─ get_metric_anomaly()  -- boto3 cloudwatch.get_metric_statistics
    │       └─ get_log_evidence()    -- boto3 logs Insights query, SKIPPED
    │                                   if log_group is falsy (see §5)
    │       returns an IncidentContext
    │
    ├─▶ github_context.get_codebase_context_from_github(owner, repo, ref, metric_name)
    │       real GitHub API fetch (git/trees + git/blobs) of the target
    │       repo's entire current source tree, in full up to a 40k-char
    │       budget, ranked by relevance to the alarm's metric (see §5)
    │
    └─▶ llm_agent.diagnose_incident(ctx, codebase_context)
            ├─ ctx.to_llm_context() renders the alarm/metric/log digest
            ├─ prepended with "CODEBASE CONTEXT:\n{codebase_context}\n...\n"
            │  if codebase_context is non-empty
            ├─ one Anthropic API call, model claude-sonnet-5, forced tool
            │  use (report_root_cause), system prompt instructs the model
            │  to ground strictly in the provided evidence
            └─ returns hypothesis / confidence / supporting_evidence /
               suggested_action

logger.info("Verdict for %s: %s", alarm_name, json.dumps(verdict))
return verdict   # no email, no dashboard update, no persistence
```

`agent/run_manual.py` runs the identical `context_builder`-to-`llm_agent`
path locally against a **fake** alert (canned CloudWatch responses from
`agent/fixtures.py`, no AWS credentials needed) — useful for iterating on
the prompt without touching AWS.

## 2. `agent/` file reference

This folder **is** `template.yaml`'s Lambda `CodeUri` — there is no
build/staging step, what's in this folder is exactly what gets deployed.

| File | Role |
|---|---|
| `handler.py` | SNS-triggered entry point (real alert). Only reads `alarm["NewStateValue"]` and `alarm["AlarmName"]` from the SNS message — ignores everything else in the real CloudWatch alarm payload. |
| `run_manual.py` | CLI entry point (fake alert) — `./scripts/run_manual.sh [--scenario db_timeout\|ambiguous_5xx]`. |
| `context_builder.py` | Data model (`AlarmDetails`, `MetricAnomaly`, `LogEvidence`, `IncidentContext`) + real `boto3` collectors + `build_incident_context()` orchestrator. |
| `fixtures.py` | Canned `describe_alarms`/`get_metric_statistics`/Logs-Insights responses (via `botocore.stub.Stubber`) for two scenarios: `db_timeout`, `ambiguous_5xx`. |
| `fake_alert.py` | `build_fake_incident(scenario_name)` — runs the *real* collector functions in `context_builder.py` against the stubbed clients from `fixtures.py`. Used only by `run_manual.py`. |
| `github_context.py` | `get_codebase_context_from_github(owner, repo, ref, metric_name)` — real implementation (see §5): fetches the target repo's entire current source tree via the GitHub REST API (`git/trees` + `git/blobs`, no `git` binary needed — Lambda doesn't have one), ranked by relevance to `metric_name` when the codebase exceeds the char budget. |
| `llm_agent.py` | `diagnose_incident(context, codebase_context="", client=None)` — builds the prompt, calls Claude, parses the structured response. This is the single point where all context sources get combined into one prompt (§4 explains exactly where). |
| `codebase_context.md` | Hand-maintained placeholder from the original stub design (fetch-one-file-from-this-repo). No longer read by `github_context.py`, which now fetches the *target app's* entire repo instead of one hand-maintained summary file. Left in place, unused — flagged rather than deleted since another branch may still reference it. |
| `requirements.txt` | Just `anthropic` — `boto3` ships with the Lambda runtime. |

## 3. Data contracts

- **Alarm input** — `docs/contracts/alarm-sns-message.json`. Real CloudWatch
  alarm-state-change shape; `handler.py` only touches `AlarmName` and
  `NewStateValue`, so extra fields (e.g. `Trigger`) are currently unused but
  present in every real message.
- **Verdict output** — `docs/contracts/review-result.json`:
  `hypothesis` (string), `confidence` (`"low"|"medium"|"high"`),
  `supporting_evidence` (list of `{source, excerpt}`), `suggested_action`
  (string). This is what `handler.py` logs and returns.

## 4. Where context sources get combined into the prompt

Everything funnels through one place —
`agent/llm_agent.py:diagnose_incident()`, lines 100-102:

```python
user_content = context.to_llm_context()
if codebase_context:
    user_content = f"CODEBASE CONTEXT:\n{codebase_context}\n\n{'=' * 40}\n\n{user_content}"
```

Two strings get concatenated into one `user` message: `context.to_llm_context()`
(the alarm/metric/log digest from `IncidentContext`) and whatever string is
passed as `codebase_context`. **This is the seam a git-diff context source
would plug into** — see §6.

## 5. Codebase context: real, full-tree, GitHub-API-based (no `git` binary)

`agent/github_context.py`'s `get_codebase_context_from_github(owner, repo,
ref=None, metric_name=None)` is a real implementation as of this branch —
no more placeholder string. It fetches the **entire current source tree**
of the target app's repo (not one hand-maintained file, and not a diff —
see "Why full codebase instead of diff correlation" below), all via the
GitHub REST API:

1. `GET /repos/{owner}/{repo}/commits?sha={ref}&per_page=1` — latest commit
   (sha/author/date/message), rendered as a one-line freshness note.
2. `GET /repos/{owner}/{repo}/git/trees/{commit_sha}?recursive=1` — every
   file path in the repo at that commit.
3. `GET /repos/{owner}/{repo}/git/blobs/{blob_sha}` per non-binary,
   non-skipped file (images/locks/binaries filtered by extension/basename;
   binary detection is a null-byte/UTF-8-decode check on the blob content)
   — each file's full text.
4. Files are ranked by relevance to `metric_name` (same keyword-hint
   scoring as `pipeline/code_context.py`) and kept in full/truncated/omitted
   in that order if the total exceeds `MAX_TOTAL_CODE_CHARS` (40,000).

Deliberately API-based rather than a local `git clone`: **the Lambda
runtime this deploys into has no `git` binary and no guaranteed writable
disk to clone into.** `pipeline/code_context.py` (this repo's standalone,
non-Lambda tool — see `pipeline/README.md`) does the equivalent thing via a
local `git clone` since it doesn't run inside Lambda; keep the two in sync
if the skip-list/budget/relevance logic changes in either.

Verified against the real target repo
([`Tehreem404/bad_app_demo`](https://github.com/Tehreem404/bad_app_demo))
and the full `_handle_alarm()` path end to end (real alarm, real metric
data, real codebase fetch, real Claude call) — see §8.

**Log evidence** (`agent/context_builder.py`) is still the one remaining
stub: `build_incident_context()` skips the Logs Insights query entirely
when `log_group` is falsy; `IncidentContext.logs` is `Optional[LogEvidence]`,
and `to_llm_context()` renders `"LOG EVIDENCE: not available..."` instead of
fabricating data. Re-enables itself automatically the moment `LOG_GROUP` is
set to a real log group — no code change needed, just a `template.yaml`
parameter. (No log group exists for the target app as of this writing.)

## 6. Why full codebase instead of diff correlation

The **original** design (`architecture.md` §5, and this doc's previous
revision on `feature/LLM_diagnosis` before this merge) planned a
`git_context`-style module: find the commit before the alarm fired, fetch
the diff since then via the GitHub API, feed *that* into the prompt as the
"what changed" signal — mirroring `lambda/git_context.py`'s approach on the
original Bedrock-based branch.

That plan was superseded, not built: `pipeline/code_context.py` (built on
`feature/LLM_diagnosis_context`, merged into this branch) was deliberately
redirected mid-build from diff-based to whole-codebase-based context — the
call was that the current state of a small app's code is more directly
useful for root-cause diagnosis than a diff against an arbitrary prior
commit, especially for a codebase this size (the target app is a handful of
files). `agent/github_context.py`'s real implementation (§5) carries that
same decision forward into the deployed pipeline. A diff-based module could
still be added later as a *third*, independent context source (same seam
described in §4 — nothing about the codebase-context or log-evidence
collectors would need to change), but it's no longer a documented gap this
branch is waiting on.

## 7. What changed from the original design, and why

| Original (`architecture.md`) | Current (this branch) | Why |
|---|---|---|
| Amazon Bedrock `InvokeModel` | Direct Anthropic API (`agent/llm_agent.py`) | Bedrock model access request was an external blocking dependency with no guaranteed turnaround; direct API was already proven working and unblocked an MVP faster. |
| `lambda/git_context.py` — diff correlation | **Not built** (superseded by full-codebase context, §6) | Simplification decision, then superseded rather than revisited — see §6 for why full-codebase won over diff. |
| `lambda/notify.py` — SNS email + CloudWatch custom widget | **Removed** | Scoped down to "just react and log the verdict" — email/dashboard-widget were judged unnecessary complexity for the core loop. |
| `lambda/`, `LLM/`, `cloudWatch/` — three folders, hand-copied duplicates staged into `lambda/` at build time | Single `agent/` folder — Lambda's `CodeUri` *is* the source, no staging | Eliminates drift risk between "what you edit" and "what deploys" entirely, by construction. |
| This repo creates its own `AWS::CloudWatch::AnomalyDetector` + `Alarm` + SNS topic | Subscribes to an **existing**, externally-managed alarm (`culprit-App-Anomaly`) and topic (`culprit-alerts`) via a `AlarmTopicArn` parameter | The real app/alarm turned out to already exist (built in a separate repo), so this stack no longer owns alarm creation — just reaction. |
| `infra/demo-app/`, `infra/cloudwatch-agent/`, `scripts/trigger_chaos.sh`, `scripts/seed_bad_commit.sh` | **Removed** | Not connected to the actual running app (which lives in a different repo) — dead scaffolding. |
| `github_context.py`'s placeholder-string stub | Real GitHub-API fetch of the target repo's **entire current source tree** (§5) | This branch's merge: `pipeline/code_context.py` (built on `feature/LLM_diagnosis_context`) proved the "dump the whole codebase" approach; reimplemented here against the GitHub API since Lambda has no `git` binary. |

## 8. Verified vs. not-yet-verified

- ✅ Full pipeline tested against **real AWS data**: real `describe_alarms`
  against the real `culprit-App-Anomaly` alarm, real `get_metric_statistics`,
  real Claude call, correct verdict logged. One real bug was caught and
  fixed this way: `culprit-App-Anomaly` is a metric-math/anomaly-band alarm,
  whose `describe_alarms` response shape omits top-level `Namespace`/
  `MetricName`/`Threshold` entirely (they're nested in a `Metrics[]` array
  instead) — `get_alarm_details()` now handles both the simple-alarm and
  metric-math-alarm response shapes.
- ✅ **(This branch)** Codebase-context fetch tested against the real target
  repo (`Tehreem404/bad_app_demo`) — correct file tree, correct relevance
  ranking (`app.py`, which actually implements the latency behavior, ranked
  above `Dockerfile`/CI workflow), correct freshness note, no README
  false-positive when none exists.
- ✅ **(This branch)** Full `_handle_alarm()` path re-verified end to end
  with the real codebase context wired in (previously it ran with the
  placeholder string) — real alarm evidence + real codebase (cited in the
  verdict's `supporting_evidence` as `codebase:app.py`) + real structured
  Claude call, correct verdict returned. Also confirms `max_tokens=1024`
  with forced `tool_choice` does **not** hit the empty-response failure mode
  seen on `claude-sonnet-5` with free-text calls at a similarly low cap
  (adaptive thinking spending the whole budget with nothing left for
  output) — that failure mode was previously confirmed on the free-text
  path in `pipeline/diagnose.py`; the forced-tool-use path here returned a
  valid verdict without needing a higher cap.
- ✅ Deployed stack (`anomaly-review-agent`) confirmed `CREATE_COMPLETE`,
  subscribed to the real `culprit-alerts` topic.
- ❓ **Never observed a genuine SNS-triggered invocation** — only direct
  `aws lambda invoke` / direct `_handle_alarm()` test calls that bypass SNS
  delivery. The SNS→Lambda subscription itself is standard AWS-managed
  plumbing (auto-confirmed for Lambda-protocol subscriptions, no custom
  logic involved), so this is low-risk, but it has not been observed firing
  for real as of this writing.
- ❓ **This branch's `agent/github_context.py` changes are not yet deployed**
  — the live `anomaly-review-agent-review` function still runs the
  placeholder-string version until `sam deploy` is run again from this
  branch (see `scripts/deploy.sh`; needs `GithubOwner=Tehreem404`,
  `GithubRepo=bad_app_demo` set as parameters, since they default to empty).
- ❌ Log evidence path (`get_log_evidence()`) has never been exercised
  against a real log group — only against fixtures. Stubbed off in the live
  deploy (§5) specifically because this was never wired up on the real
  app's side.
