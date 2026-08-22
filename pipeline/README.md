# pipeline/ -- context builder for automated anomaly diagnosis

Given a fired CloudWatch alarm (an SNS message, or just its name), builds a
single natural-language prompt string combining:

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

- `cloudwatch_context.py` -- alarm / metric / EC2 / log collector
  (`build_cloudwatch_context`)
- `code_context.py` -- full-codebase + README + latest-commit collector
  (`build_code_context`)
- `build_prompt.py` -- combines both into the final prompt string
  (`build_diagnosis_prompt`)
- `sample_sns_message.json` -- a real alarm-state-change payload, captured
  from this project's own live `culprit-App-Anomaly` alarm via
  `aws cloudwatch describe-alarm-history`

## Config (env vars)

| Var | Required | Meaning |
|---|---|---|
| `AWS_REGION` | recommended | region for all CloudWatch/EC2 calls (default `us-east-1`) |
| `GIT_REPO_PATH` | yes (or pass `--git-repo`) | local checkout path, or a clone URL -- cloned to a temp dir if it doesn't exist locally |
| `INSTANCE_ID` | if you want host metadata | EC2 instance the app runs on. Not inferred from alarm dimensions by default -- this project's alarms key off an `App` dimension (e.g. `App=bad-app-ec2`), not `InstanceId` |
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

export AWS_REGION=us-east-1
export INSTANCE_ID=i-091814f7a41456cb0
export GIT_REPO_PATH=https://github.com/Tehreem404/bad_app_demo.git

# by alarm name (fetches the alarm's current live state):
python3 build_prompt.py culprit-App-Anomaly

# or against a real SNS alarm message (its NewStateValue/StateChangeTime win
# over the live state -- important for a flapping alarm, see note below):
python3 build_prompt.py sample_sns_message.json
```

Or from code:

```python
from build_prompt import build_diagnosis_prompt

prompt = build_diagnosis_prompt(sns_message, git_repo_path="...")

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
