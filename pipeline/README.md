# pipeline/ -- context builder + LLM diagnosis for a fired anomaly

## Quickstart for the anomaly-detection side

This is the one function you need:

```python
from diagnose import diagnose

result = diagnose(event)   # -> Claude's full root-cause diagnosis, as a string
```

### 1. Set up (once)

```bash
pip install -r pipeline/requirements.txt   # boto3, anthropic
export ANTHROPIC_API_KEY=sk-ant-...        # ask for this if you don't have it -- it's in the project's .env
```
That's it. AWS creds come from whatever you already have configured
(`aws configure` / instance role) -- no separate setup for those.

### 2. Verify it works, right now, before wiring anything up

```bash
cd pipeline
python3 diagnose.py culprit-App-Anomaly
```
If this prints a real diagnosis (a few paragraphs of hypothesis + suspect
code + suggested fix), you're set. It takes maybe 10-30 seconds -- it's
doing a live AWS lookup, a git clone, and a real Claude call, not a canned
response.

### 3. Call it from your code

`diagnose()` takes **one required argument** -- `event` -- and accepts
whatever shape your detection code has on hand:

```python
from diagnose import diagnose

# a) just the alarm name (simplest -- fetches the alarm's current live state)
diagnose("culprit-App-Anomaly")

# b) a parsed CloudWatch alarm-state-change message (the shape SNS delivers --
#    see docs/contracts/alarm-sns-message.json at the repo root for every field)
diagnose({
    "AlarmName": "culprit-App-Anomaly",
    "NewStateValue": "ALARM",
    "NewStateReason": "Thresholds Crossed: ...",
    "StateChangeTime": "2026-08-22T17:52:28.788+0000",
    # ...other CloudWatch fields are fine to include but not required
})

# c) the raw event your Lambda/handler receives if SNS invokes you directly
def lambda_handler(event, context):
    return diagnose(event)   # event["Records"][0]["Sns"]["Message"] is unwrapped automatically
```
Prefer (b)/(c) over (a) when you have them: `describe_alarms` only reports
an alarm's *current* state, and this alarm flaps `ALARM <-> OK` in
practice -- if your detector already knows the alarm just went to `ALARM`,
passing that along (not just the name) guarantees the diagnosis is about
*that* incident, not whatever the alarm happens to be doing by the time
`diagnose()` runs.

**No config needed beyond the API key** -- `GIT_REPO_PATH`/`INSTANCE_ID`/
`AWS_REGION` all default to this project's fixed demo target. It also
works when imported from any directory or file location (`sys.path` is
handled internally) -- verified by loading it via `importlib` from an
unrelated directory with nothing else set up, and getting a correct
diagnosis back.

### 4. What you get back

A **plain string** -- Claude's full diagnosis in prose/markdown (hypothesis,
supporting evidence, suspect file/line, suggested fix, and a confidence
note), not JSON and not a dataclass. Print it, log it, email it, post it to
Slack, whatever your detection side needs to do with it. See "Sample
diagnosis" below for a full real example of exactly what comes back.

### 5. If something goes wrong

`diagnose()` doesn't swallow errors -- a broken run raises, it doesn't
return `None` or an empty string. The likely causes, in order of
probability:
- **`KeyError` / `ValueError` on the alarm name** -- the alarm name is
  wrong or doesn't exist in the account/region you're pointed at.
- **`anthropic.AuthenticationError`** -- `ANTHROPIC_API_KEY` isn't set or is
  invalid.
- **`botocore` credential errors** -- AWS creds aren't configured in this
  environment.
- **A `git`/`RuntimeError` failure** -- couldn't clone/read the target repo
  (network issue, or `GIT_REPO_PATH` was overridden to something invalid).

Wrap the call in a `try/except` on your side if you want the detection loop
to keep running after a failed diagnosis rather than crashing it.

---

Everything below this point is how the pieces `diagnose()` calls work
internally, for anyone editing the pipeline itself rather than just calling
it.

Given a fired CloudWatch alarm (an SNS message, or just its name), the
modules below build a single natural-language prompt string combining:

- **Real CloudWatch evidence** -- the alarm's own definition, metric
  datapoints around the trigger, EC2 instance metadata, and log lines if a
  log group is configured.
- **Real codebase evidence** -- the app's **entire current source tree**
  (every tracked, non-binary file, in full), plus a README/docs summary and
  a one-line "as of commit X" freshness note. This intentionally dumps the
  whole codebase rather than recent commit diffs -- for diagnosis, what the
  code *is* right now is more useful than what changed in any one commit.

The output is just a string, ready to drop straight into a Claude API
`messages.create()` call as the user turn. This module never calls an LLM
itself.

Everything is fetched live via boto3 / `git` -- nothing here is mocked. If a
piece of evidence isn't accessible (missing permission, no log group,
no README, alarm isn't EC2-related), the prompt says so explicitly instead
of omitting it silently or making something up.

## Files

- `diagnose.py` -- **the integration point.** `diagnose(event) -> str` wraps
  everything below into one call to Claude and returns the diagnosis text.
  Also runnable standalone: `python3 diagnose.py <alarm_name_or_sns_file>`.
- `cloudwatch_context.py` -- alarm / metric / EC2 / log collector
  (`build_cloudwatch_context`)
- `code_context.py` -- full-codebase + README + latest-commit collector
  (`build_code_context`)
- `build_prompt.py` -- combines both into the prompt string `diagnose()`
  sends to Claude (`build_diagnosis_prompt`) -- doesn't call an LLM itself
- `sample_sns_message.json` -- a real alarm-state-change payload, captured
  from this project's own live `culprit-App-Anomaly` alarm via
  `aws cloudwatch describe-alarm-history`

## Config (env vars)

| Var | Required | Meaning |
|---|---|---|
| `ANTHROPIC_API_KEY` | yes, for `diagnose()` | only needed to actually call Claude -- `build_prompt.py`/`build_diagnosis_prompt()` alone don't need it |
| `AWS_REGION` | no | region for all CloudWatch/EC2 calls; `diagnose()` defaults to `us-east-1`, `build_diagnosis_prompt()` alone also defaults to `us-east-1` |
| `GIT_REPO_PATH` | no, via `diagnose()` | local checkout path, or a clone URL -- cloned to a temp dir if it doesn't exist locally. `diagnose()` defaults to this project's `bad_app_demo` repo if unset; calling `build_diagnosis_prompt()` directly still requires it |
| `INSTANCE_ID` | no, via `diagnose()` | EC2 instance the app runs on, for host metadata. Not inferred from alarm dimensions -- this project's alarms key off an `App` dimension (e.g. `App=bad-app-ec2`), not `InstanceId`. `diagnose()` defaults to this project's demo instance if unset |
| `LOG_GROUP` | no | CloudWatch Logs group to sample around the trigger window; omitted entirely (with a note) if unset |
| `LOOKBACK_MINUTES` / `LOOKAHEAD_MINUTES` | no | metric window around the trigger, default `30`/`5` |

`code_context.py`'s codebase-size limits (`MAX_TOTAL_CODE_CHARS` = 40,000,
`MAX_FILE_CHARS` = 12,000 per file) are constants in that file, not env
vars -- bump them there if a target repo is bigger than the demo app. When
the total exceeds the budget, files are kept/dropped in order of relevance
to the alarm's metric name (see `score_relevance()`), not just cut off
alphabetically.

AWS credentials come from the environment / instance role, per usual boto3
resolution -- nothing is hardcoded.

## Run it end to end

```bash
pip install -r requirements.txt   # boto3, anthropic
export ANTHROPIC_API_KEY=...      # only thing required -- everything else defaults

# by alarm name (fetches the alarm's current live state):
python3 diagnose.py culprit-App-Anomaly

# or against a real SNS alarm message (its NewStateValue/StateChangeTime win
# over the live state -- important for a flapping alarm, see note below):
python3 diagnose.py sample_sns_message.json
```

Or from code -- this is what `diagnose()` does internally, useful if you
want the prompt without calling an LLM (e.g. to eyeball the evidence, or to
send it somewhere other than Claude):

```python
from build_prompt import build_diagnosis_prompt

prompt = build_diagnosis_prompt(
    sns_message,
    git_repo_path="https://github.com/Tehreem404/bad_app_demo.git",
)

import anthropic
client = anthropic.Anthropic()
response = client.messages.create(
    model="claude-sonnet-5",
    max_tokens=16000,  # Sonnet 5 has adaptive thinking on by default -- a low cap
                        # (e.g. 1024) can get spent entirely on thinking, leaving no
                        # text output. Verified against the real prompt above.
    messages=[{"role": "user", "content": prompt}],
)
diagnosis = "".join(b.text for b in response.content if b.type == "text")
```

**Why the SNS message matters, not just the alarm name:** `describe_alarms`
only ever reports the alarm's *current* state. This project's anomaly alarm
flaps back and forth across its dynamic band in practice -- while testing
this pipeline, the alarm had already flipped `ALARM -> OK` again by the time
the collector ran. `get_alarm_definition()` prefers the SNS message's own
`NewStateValue` / `NewStateReason` / `StateChangeTime` when one is passed in,
so the prompt describes the actual incident that triggered the pipeline, not
whatever the alarm happens to be doing right now.

## Sample output

Real output from the commands above, run against the live AWS account and
the real `bad_app_demo` repo (metric datapoints and most file bodies
trimmed here for length -- nothing here is fabricated, this is
`build_prompt.py`'s actual stdout):

````
## What fired
Alarm: culprit-App-Anomaly -- EC2 demo app latency outside its modeled expected band.
Metric: HackathonDemo/RequestLatency [App=bad-app-ec2]
Trigger time: 2026-08-22T17:52:28.788000+00:00
Detection: CloudWatch anomaly-detection band (dynamic threshold)
Threshold at trigger: 0.05205592797068189
State reason (from CloudWatch): Thresholds Crossed: 2 out of the last 2 datapoints [0.05446682934068207 (22/08/26 17:51:00), 0.05454111508545446 (22/08/26 17:50:00)] were greater than the upper thresholds [0.05342948924695477, 0.05358105251591999] (minimum 2 datapoints for OK -> ALARM transition).

Metric datapoints, 2026-08-22T17:22:28.788000+00:00 to 2026-08-22T17:57:28.788000+00:00 (Average/Maximum per 60s):
  2026-08-22T13:22:00-04:00: Average=0.053896001044740065, max=0.08393025398254395 Seconds
  ... (33 more one-minute datapoints)

## Host (EC2 instance)
Instance: i-091814f7a41456cb0 (hackathon-demo)
Type: t3.micro, AMI: ami-0332d564d76dbd8d6, AZ: us-east-1b
State: running, launched: 2026-08-22T13:53:43+00:00, uptime: 4:14:52.525356

## Relevant logs
no LOG_GROUP configured -- skipping log evidence (set the LOG_GROUP env var if the app logs to CloudWatch Logs)

## What's running
Repo: https://github.com/Tehreem404/bad_app_demo.git
no README/architecture doc found

## Codebase (current state)
As of latest commit 3da1453306 (2026-08-22T12:41:12-04:00) by Tehreem Nazar: Add comment to test deploy pipeline

### .github/workflows/deploy.yml
```yaml
name: Deploy to EC2
... (full file)
```

### Dockerfile
```
FROM python:3.12-slim
... (full file)
```

### app.py
```python
"""
Demo app — EC2 + Docker version.
...
"""
import hashlib
import os
import random
import threading
import time
import urllib.request

import boto3
from flask import Flask, jsonify

app = Flask(__name__)
...

@app.route("/")
def index():
    global _error_count, _request_count
    start = time.time()
    status = 200
    chaos = _get_chaos_state()

    if chaos["latency"]:
        time.sleep(random.uniform(1.5, 3.0))
    else:
        time.sleep(random.uniform(0.01, 0.05))
    ...
```

### requirements.txt
```
flask
boto3
```

## Your task
Using only the evidence above:
1. Hypothesize the most likely root cause of this anomaly.
2. Point to the specific suspect file/line in the codebase above.
3. Suggest a concrete fix (or, if the evidence doesn't support one confidently, the smallest safe mitigation and why).
Be explicit about which pieces of evidence support your hypothesis, and say plainly if the evidence is too thin to be confident.
````

Every tracked, non-binary file in the real repo is included in full here
(it's a 4-file demo app, well under the 40k-char budget) -- `app.py`'s
`index()` route, which contains the actual `time.sleep(random.uniform(1.5,
3.0))` chaos-latency branch, is right there for the model to point at
directly, rather than having to infer it from a diff.

## Sample diagnosis (real Claude Sonnet 5 call on the prompt above)

Real, unedited output from sending the prompt above to `claude-sonnet-5`
(`max_tokens=16000` per the note above) -- not a scripted example:

> **Root-cause hypothesis:** The alarm fired on a 1-2ms drift in average
> latency (0.0545s vs. a ~0.0535s upper band) -- not a real "chaos" incident.
> The `latency` chaos toggle injects `random.uniform(1.5, 3.0)` seconds of
> sleep, which would blow the average up by orders of magnitude; none of the
> datapoints in the window come close, so that flag was almost certainly off.
>
> What's actually visible: `index()` calls `_get_chaos_state()` -- a
> synchronous `ssm.get_parameters()` network call -- on *every* request,
> inside the very block being timed. That call's latency depends on SSM
> round-trip time and periodic boto3 credential refresh, which lines up with
> the sporadic Max-latency spikes (13:40, 13:48, 13:54) in the data.
>
> **Suggested fix:** stop calling SSM synchronously per-request -- cache the
> chaos state and refresh it on a timer in a background thread (code
> provided), removing network jitter from the request hot path and cutting
> SSM call volume ~99%. Smallest mitigation if code can't be touched right
> now: widen the anomaly detector's band (2σ → 3σ), since the deviation is
> within normal jitter, not a functional regression.
>
> **Confidence note (self-flagged):** no logs or CPU metrics were available
> to directly confirm SSM latency as the cause -- that's inferred from the
> code path and timing pattern. Confident the event was *not* a real
> latency-chaos trigger; less confident about the exact jitter source.

This is a genuinely useful diagnosis: it correctly ruled out the obvious
chaos toggle by magnitude, pointed at real code (`_get_chaos_state()` inside
`index()`'s timed block, not somewhere unrelated), proposed a concrete fix,
and was explicit about what it couldn't confirm rather than overclaiming.

## What "clean" means here

This replaces an earlier, partial `LLM/` + `cloudWatch/` pair from an
earlier experiment (GitHub-API-only git context against a fictional
"order-service", offline fixtures, a broken demo script referencing a
module that didn't exist). That code is gone -- this directory is the
single, current implementation.
