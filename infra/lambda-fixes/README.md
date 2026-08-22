# Fix: `KeyError: 'Namespace'` on the anomaly-detection alarm

## Where the source actually lives

The crashing code (`handler.py`, `context_builder.py`) is **not in this git
repo**, in any branch. This repo's own `lambda/handler.py` is a different,
unrelated implementation (git_context/llm_review/notify pipeline, different
line numbers, different function signatures) — it happens to share the same
skeleton (`results.append(_handle_alarm(alarm))`, a `_handle_alarm` helper,
the same "Handling alarm: %s" log line) but it never calls
`build_incident_context` or reads `Namespace`, so it can't have produced the
traceback in the bug report.

The real source was pulled straight from the deployed Lambda:

```
aws lambda get-function --function-name anomaly-review-agent-review \
  --query 'Code.Location' --output text
curl -s -o fn.zip "<presigned url>"
unzip fn.zip -d fn/
```

`fn/handler.py` matches the crash traceback byte-for-byte — line 30 is
`results.append(_handle_alarm(alarm))`, line 46 is
`ctx = build_incident_context(alarm_name, log_group, region=region)`. The
fix in this directory targets `fn/context_builder.py`'s `get_alarm_details`.

**Note on current live state:** the `context_builder.py` inside the deployed
`$LATEST` code (fetched at the time of this investigation) already contains
logic equivalent to the fix below — `get_alarm_details` already branches on
whether `"Namespace"` is a top-level key. Running it against the live
`culprit-App-Anomaly` alarm does **not** currently raise `KeyError`. It's
possible the function was redeployed with a fix since the log excerpt in the
bug report was captured. Either way, the root cause is exactly as described,
and this patch documents/formalizes the fix so it's tracked in version
control rather than living only as an undocumented change in `$LATEST`.
Apply it as a no-op-safe sanity check, or use it as the reference if the
teammate's own source tree still has the naive/unguarded version.

## Root cause (confirmed against live alarms)

`describe-alarms` returns two different shapes depending on the alarm type:

```
aws cloudwatch describe-alarms --alarm-names culprit-App-High culprit-App-Anomaly
```

- **`culprit-App-High`** (plain metric alarm): has top-level `Namespace`,
  `MetricName`, `Dimensions`, `Period`, `Threshold`.
- **`culprit-App-Anomaly`** (anomaly-detection alarm): has **none** of those
  top-level keys. Instead:
  - `Metrics` is a list of two query objects:
    - one entry (id `m2` on this alarm) has a `MetricStat` — this is the
      real underlying metric (`Namespace`/`MetricName`/`Dimensions`/`Period`
      live at `Metrics[i].MetricStat.Metric` / `Metrics[i].MetricStat.Period`).
    - the other entry (id `ad2`) has an `Expression` of
      `ANOMALY_DETECTION_BAND(m2, 8)` — the anomaly band, not a real metric.
  - `ThresholdMetricId` is present (`"ad2"`) instead of a numeric `Threshold`
    — the band is dynamic, so there's no fixed threshold value.

The old `get_alarm_details` unconditionally did `namespace=a["Namespace"]`
(and `a["MetricName"]`, `a["Period"]`, `a["Threshold"]`), which only exists
for the plain-metric shape. For the anomaly alarm this raises
`KeyError: 'Namespace'` — exactly the traceback in the bug report.

## The fix

`get_alarm_details` now branches on whether `"Namespace"` is present at the
top level:

- **Present** → read `Namespace` / `MetricName` / `Dimensions` / `Period` /
  `Threshold` from the top level, same as before.
- **Absent** → it's a metric-math/anomaly alarm. Find the entry in
  `Metrics[]` that has a `MetricStat` key (as opposed to an `Expression` key
  — that's the anomaly-band formula, not the real metric) and read
  `Namespace` / `MetricName` / `Dimensions` from
  `MetricStat.Metric`, and `Period` from `MetricStat.Period`. `threshold` is
  set to `None` (there's no fixed value; `ThresholdMetricId` is what the
  alarm actually keys off).

```python
if "Namespace" in a:
    # Simple, single-metric alarm -- Namespace/MetricName/Dimensions/
    # Period/Threshold all live at the top level.
    namespace = a["Namespace"]
    metric_name = a["MetricName"]
    dimensions = {d["Name"]: d["Value"] for d in a.get("Dimensions", [])}
    period_seconds = a["Period"]
    threshold = a.get("Threshold")
else:
    # Metric-math alarm (e.g. ANOMALY_DETECTION_BAND) -- there's no
    # single top-level metric; it's one of several entries in Metrics[].
    # Find the one with a MetricStat (the underlying metric query, not
    # the anomaly-band expression) and pull namespace/metric/dims/period
    # from there. No fixed Threshold exists -- the band is dynamic.
    metric_query = next((m for m in a.get("Metrics", []) if "MetricStat" in m), None)
    if metric_query is None:
        raise ValueError(f"Alarm {alarm_name!r} has no MetricStat in its Metrics -- can't determine its metric")
    metric = metric_query["MetricStat"]["Metric"]
    namespace = metric["Namespace"]
    metric_name = metric["MetricName"]
    dimensions = {d["Name"]: d["Value"] for d in metric.get("Dimensions", [])}
    period_seconds = metric_query["MetricStat"]["Period"]
    threshold = None
```

`AlarmDetails.threshold` is `Optional[float]`, and the one place that reads
it (`IncidentContext.to_llm_context`) already has a fallback:

```python
threshold_desc = self.alarm.threshold if self.alarm.threshold is not None \
    else "dynamic (anomaly-detection band, no fixed value)"
```

so `threshold=None` flows through cleanly with no further changes needed.

## Files in this directory

- `anomaly-alarm-shape.patch` — unified diff, `context_builder.py`, ready to
  apply with `patch -p1` or `git apply` from the deployed function's source
  root. Verified: applying it to the naive/unguarded `get_alarm_details`
  reproduces the exact code currently live in `$LATEST`, byte for byte.
- `verify_alarm_shapes.py` — stand-alone script (stdlib only, shells out to
  `aws cloudwatch describe-alarms`) that runs the fixed parsing logic
  against the real `culprit-App-High` and `culprit-App-Anomaly` alarms and
  prints what it resolves. Ran clean against both live alarms:

  ```
  OK   culprit-App-High
         namespace           = 'HackathonDemo'
         metric_name         = 'RequestLatency'
         dimensions          = {'App': 'bad-app-ec2'}
         comparison_operator = 'GreaterThanThreshold'
         threshold           = 0.34
         threshold_metric_id = None
         period_seconds      = 60
  OK   culprit-App-Anomaly
         namespace           = 'HackathonDemo'
         metric_name         = 'RequestLatency'
         dimensions          = {'App': 'bad-app-ec2'}
         comparison_operator = 'GreaterThanUpperThreshold'
         threshold           = None
         threshold_metric_id = 'ad2'
         period_seconds      = 60
  ```

## Other things checked on the anomaly path (no further bugs found)

- `get_metric_anomaly` (`context_builder.py`) consumes `alarm.namespace`,
  `alarm.metric_name`, `alarm.dimensions`, `alarm.period_seconds` — all of
  which are correctly resolved by the fixed `get_alarm_details` for the
  anomaly shape too, so this doesn't need any changes.
- `IncidentContext.to_llm_context` already guards `threshold is None`
  (see above) — no crash there.
- `handler.py` and `llm_agent.py`/`github_context.py` never touch
  `Namespace`/`Threshold`/`Metrics` directly — they only go through
  `AlarmDetails`, so they're unaffected either way.
- Not deployed or applied to the live Lambda — this is a patch artifact
  only, per the task constraints.
