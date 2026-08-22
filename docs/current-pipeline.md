# Current pipeline (`feature/LLM_diagnosis` branch)

> Written to support merging in a branch that reintroduces git-context
> (diff) correlation. This documents what's actually deployed and running
> today, how it differs from the original plan in
> [architecture.md](architecture.md), and exactly where a git-context step
> would plug back in. `architecture.md` describes the *original* hackathon
> design (Bedrock, git-diff correlation, notify/dashboard-widget) and is
> **not** current — this document supersedes it for anything it disagrees on.

## TL;DR

A CloudWatch alarm (owned in a separate repo, not this one) publishes to an
SNS topic. A Lambda subscribed to that topic gathers real alarm/metric/log
evidence via `boto3`, combines it with a codebase-context string into one
prompt, and sends it to Claude via the direct Anthropic API for a grounded
root-cause hypothesis. The verdict is logged and returned as the Lambda's
invocation result — there is no notification, dashboard widget, or git-diff
step in this branch; all three existed in the original design and were
deliberately removed for a simpler MVP. **Diff correlation is the main gap
this branch left for a git-context branch to fill in** — see §6.

Deployed and **verified against real AWS data** (not just fixtures) as of
this writing: stack `anomaly-review-agent`, region `us-east-1`, subscribed
to the real `culprit-alerts` SNS topic behind the real `culprit-App-Anomaly`
alarm.

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
    ├─▶ github_context.get_codebase_context_from_github(owner, repo, ref)
    │       currently STUBBED -- returns a fixed placeholder string, no
    │       network call (see §5)
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
| `github_context.py` | `get_codebase_context_from_github(owner, repo, path, ref)` — currently a stub (see §5). The real, already-tested implementation is preserved as `_fetch_from_github()` in the same file. |
| `llm_agent.py` | `diagnose_incident(context, codebase_context="", client=None)` — builds the prompt, calls Claude, parses the structured response. This is the single point where all context sources get combined into one prompt (§4 explains exactly where). |
| `codebase_context.md` | Hand-maintained architecture/failure-pattern summary. This is the file `github_context.py` would fetch live from GitHub once un-stubbed (`agent/codebase_context.md` is the default `path`). |
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

## 5. Current stubs (deliberate, for deploy speed)

Both were stubbed out specifically to unblock a real deploy without waiting
on external setup — real implementations exist and are one change away:

- **Codebase context** (`agent/github_context.py`) — `get_codebase_context_from_github()`
  currently returns a fixed placeholder string, no network call. The real
  implementation (live GitHub Contents API fetch, already tested against
  the real repo) is preserved as `_fetch_from_github()` in the same file —
  swap the stub's body to call it to re-enable.
- **Log evidence** (`agent/context_builder.py`) — `build_incident_context()`
  skips the Logs Insights query entirely when `log_group` is falsy;
  `IncidentContext.logs` is `Optional[LogEvidence]`, and `to_llm_context()`
  renders `"LOG EVIDENCE: not available..."` instead of fabricating data.
  Re-enables itself automatically the moment `LOG_GROUP` is set to a real
  log group — no code change needed, just a `template.yaml` parameter.

Both stubs mean the current live verdict quality is lower than it will be
once real codebase context and log evidence are wired back in — confidence
has been observed dropping to `"low"`/`"medium"` accordingly, which is the
system prompt working as intended (it's instructed not to fabricate).

## 6. Where git-context (diff) correlation should plug in

The **original** design (`architecture.md` §5) had `lambda/git_context.py`
find the last commit before the alarm's timestamp and fetch the diff since
then via the GitHub API, then feed that diff into the Bedrock prompt. **This
branch removed that step entirely** — there is currently no diff/commit
context anywhere in the prompt. To merge it back in:

1. **New module**, e.g. `agent/git_context.py` (naming/shape can follow the
   old `lambda/git_context.py` almost exactly — `get_commit_diff(owner, repo, until_timestamp)`
   returning `{good_commit_sha, commits, diff, diff_truncated}`). It's
   dependency-free `urllib.request` + `GITHUB_TOKEN` bearer auth, same
   pattern as `github_context.py`'s `_fetch_from_github()` — reuse that
   pattern rather than adding a new HTTP client dependency.
2. **Wire it into `diagnose_incident()`'s prompt assembly** (§4) — either
   add a third parameter (`diff_context: str = ""`) alongside
   `codebase_context` and prepend/append it the same way, or fold it into
   `IncidentContext` itself as a new optional field (`IncidentContext.diff`)
   and render it inside `to_llm_context()`, following the same `Optional[...]`
   pattern already used for `logs` (§5) — recommended, since it's evidence
   *about the incident*, conceptually closer to alarm/metric/log evidence
   than to the static codebase-context summary.
3. **Wire it into `handler.py`'s `_handle_alarm()`** — call the new function
   with `alarm.get("StateChangeTime")` as the timestamp (present in every
   real alarm message per `docs/contracts/alarm-sns-message.json`, just
   currently unread) and the same `GITHUB_OWNER`/`GITHUB_REPO`/`GITHUB_TOKEN`
   env vars `github_context.py` already uses — no new config plumbing
   needed, `template.yaml` already has all three as parameters (currently
   passed through unused since the codebase-context fetch is stubbed).
4. **Update `SYSTEM_PROMPT`** in `llm_agent.py` to tell the model a diff is
   now one of its evidence sources and how to weigh it relative to the
   CloudWatch evidence (the existing prompt's "CloudWatch evidence is the
   primary basis... codebase context is background" framing is the pattern
   to extend, not replace).
5. **`docs/contracts/review-result.json`** may need a `suspect_commit`-style
   field added back if the diagnosis should name a specific commit — current
   schema has no field for that (removed along with the Bedrock/diff design).

None of this requires touching `context_builder.py`'s CloudWatch collectors
or `github_context.py`'s codebase-context fetch — they're independent
context sources that happen to converge in the same prompt-building
function.

## 7. What changed from the original design, and why

| Original (`architecture.md`) | Current (this branch) | Why |
|---|---|---|
| Amazon Bedrock `InvokeModel` | Direct Anthropic API (`agent/llm_agent.py`) | Bedrock model access request was an external blocking dependency with no guaranteed turnaround; direct API was already proven working and unblocked an MVP faster. |
| `lambda/git_context.py` — diff correlation | **Removed** | Simplification decision; this is the gap §6 addresses. |
| `lambda/notify.py` — SNS email + CloudWatch custom widget | **Removed** | Scoped down to "just react and log the verdict" — email/dashboard-widget were judged unnecessary complexity for the core loop. |
| `lambda/`, `LLM/`, `cloudWatch/` — three folders, hand-copied duplicates staged into `lambda/` at build time | Single `agent/` folder — Lambda's `CodeUri` *is* the source, no staging | Eliminates drift risk between "what you edit" and "what deploys" entirely, by construction. |
| This repo creates its own `AWS::CloudWatch::AnomalyDetector` + `Alarm` + SNS topic | Subscribes to an **existing**, externally-managed alarm (`culprit-App-Anomaly`) and topic (`culprit-alerts`) via a `AlarmTopicArn` parameter | The real app/alarm turned out to already exist (built in a separate repo), so this stack no longer owns alarm creation — just reaction. |
| `infra/demo-app/`, `infra/cloudwatch-agent/`, `scripts/trigger_chaos.sh`, `scripts/seed_bad_commit.sh` | **Removed** | Not connected to the actual running app (which lives in a different repo) — dead scaffolding. |

## 8. Verified vs. not-yet-verified

- ✅ Full pipeline tested against **real AWS data**: real `describe_alarms`
  against the real `culprit-App-Anomaly` alarm, real `get_metric_statistics`,
  real Claude call, correct verdict logged. One real bug was caught and
  fixed this way: `culprit-App-Anomaly` is a metric-math/anomaly-band alarm,
  whose `describe_alarms` response shape omits top-level `Namespace`/
  `MetricName`/`Threshold` entirely (they're nested in a `Metrics[]` array
  instead) — `get_alarm_details()` now handles both the simple-alarm and
  metric-math-alarm response shapes.
- ✅ Deployed stack (`anomaly-review-agent`) confirmed `CREATE_COMPLETE`,
  subscribed to the real `culprit-alerts` topic.
- ❓ **Never observed a genuine SNS-triggered invocation** — only direct
  `aws lambda invoke` test calls that bypass SNS delivery. The SNS→Lambda
  subscription itself is standard AWS-managed plumbing (auto-confirmed for
  Lambda-protocol subscriptions, no custom logic involved), so this is
  low-risk, but it has not been observed firing for real as of this writing.
- ❌ Log evidence path (`get_log_evidence()`) has never been exercised
  against a real log group — only against fixtures. Stubbed off in the live
  deploy (§5) specifically because this was never wired up on the real
  app's side.
