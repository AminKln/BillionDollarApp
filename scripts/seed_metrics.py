#!/usr/bin/env python3
"""
Stand-in for W1's app, from the CloudWatch side.

W2 does not need the real app to build detection: CloudWatch cannot tell
whether put_metric_data came from Flask on EC2 or from this script. What
matters is that the *contract* is identical, so when W1's app comes online
this process is simply stopped and nothing downstream changes.

It publishes exactly the I1 contract in docs/architecture.md §4:

    namespace  Culprit          (no dimensions on any metric — dimensioned
                                 metrics do not match dimensionless alarms)
    RequestLatency  Milliseconds   trigger + anomaly band
    SystemLoad1     None           corroboration
    WorkUnits       Count          THE DISCRIMINATOR — stays flat
    RequestCount    Count          rules out "we just got more traffic"

Two modes, switched live by a flag file so the anomaly can be triggered
without restarting and losing the trained band:

    baseline     latency ~40ms,  load ~0.8   <- run this all night
    regression   latency ~250ms, load ~2.6   <- touch the flag file

WorkUnits and RequestCount are IDENTICAL in both modes. That is the whole
point: the same amount of work, on the same amount of traffic, suddenly
costs six times more. Nothing but code complexity explains that.

    ./scripts/seed_metrics.py                       # baseline, forever
    touch /tmp/culprit-regression.flag              # -> regression, live
    rm    /tmp/culprit-regression.flag              # -> back to baseline
    ./scripts/seed_metrics.py --backfill 6           # synth baseline history
                                                      # from 6h ago to 90min
                                                      # ago, then exit

W1: this file is the spec for what the real app must emit. Match the
namespace, metric names, units and StorageResolution exactly.
"""
import argparse
import math
import os
import random
import sys
import time
from datetime import datetime, timezone

import boto3
from botocore.config import Config

NAMESPACE = "Culprit"
FLAG = "/tmp/culprit-regression.flag"

# Baseline is what the endpoint costs before the bad commit; regression is
# after. Tuned so the regression is unmistakable on a chart but not absurd —
# a real accidentally-quadratic dedupe on a 4000-item corpus.
PROFILE = {
    "baseline":   {"latency_ms": 40.0,  "load": 0.80},
    "regression": {"latency_ms": 250.0, "load": 2.60},
}

WORK_UNITS = 4000.0     # corpus size. flat in both modes, by design.
REQS_PER_CYCLE = 20     # open-loop: fixed request rate regardless of latency

# Backfill knobs. The anomaly detector needs ~3h of history before its band
# is queryable and CloudWatch keeps 1s resolution for only 3h anyway, so
# synthesized history is written at 60s granularity/StorageResolution -- the
# live loop is untouched and keeps writing at 1s resolution.
BACKFILL_STEP_SECONDS = 60
BACKFILL_BATCH = 20                # 20 timestamps * 4 metrics = 80 datums/call,
                                    # comfortably under the 1000-datum / 1MB caps
BACKFILL_SAFETY_SECONDS = 90 * 60  # never write into the regression test's window
TWO_WEEKS_SECONDS = 14 * 24 * 3600  # CloudWatch silently drops anything older


def jitter(mean, rel_sd=0.08):
    """Log-normal-ish jitter. Latency distributions are right-skewed; a
    symmetric gaussian would give the anomaly band an unrealistically tidy
    shape and make the demo look staged."""
    return max(0.5, random.lognormvariate(math.log(mean), rel_sd))


def sample(now, mode):
    """One cycle's worth of samples.

    A slow sinusoid on top of the jitter gives the trained band some real
    width. A perfectly flat baseline trains a hairline band that anything
    at all breaks through, which would make the ML detection worthless and
    obviously so to a judge.
    """
    p = PROFILE[mode]
    drift = 1.0 + 0.06 * math.sin(now / 900.0)          # ~15 min period, +-6%
    latencies = [round(jitter(p["latency_ms"] * drift), 2)
                 for _ in range(REQS_PER_CYCLE)]
    load = round(jitter(p["load"] * drift, 0.12), 3)
    return latencies, load


def payload(latencies, load, ts=None, storage_resolution=1):
    ts = ts or datetime.now(timezone.utc)
    common = {"Timestamp": ts, "StorageResolution": storage_resolution}
    return [
        # Values/Counts gives CloudWatch the real distribution, so Average,
        # Maximum and percentile statistics are all meaningful.
        {**common, "MetricName": "RequestLatency", "Unit": "Milliseconds",
         "Values": latencies, "Counts": [1.0] * len(latencies)},
        {**common, "MetricName": "SystemLoad1", "Unit": "None", "Value": load},
        {**common, "MetricName": "WorkUnits", "Unit": "Count", "Value": WORK_UNITS},
        {**common, "MetricName": "RequestCount", "Unit": "Count",
         "Value": float(len(latencies))},
    ]


def backfill(cw, hours):
    """Synthesize HOURS of baseline history, ending 90 minutes before now,
    so the anomaly detector clears PENDING_TRAINING without an overnight
    wait. Reuses sample() with historical epoch seconds as `now` so the
    ~15min sinusoid is continuous with whatever the live loop is writing,
    rather than restarting its own drift cycle from zero.

    Re-running this re-publishes into the same minute buckets. That's fine:
    every metric here is read back as Average, and WorkUnits/RequestCount
    are constants regardless of mode, so duplicate points don't skew
    anything -- no need to track what was already written.
    """
    if hours * 3600 > TWO_WEEKS_SECONDS:
        print(f"--backfill {hours} reaches {hours / 24:.1f} days into the past; "
              f"CloudWatch silently drops data older than 14 days. Refusing.",
              file=sys.stderr)
        return 1

    now = time.time()
    start = now - hours * 3600
    end = now - BACKFILL_SAFETY_SECONDS
    if start >= end:
        print(f"--backfill {hours} leaves nothing before the "
              f"{BACKFILL_SAFETY_SECONDS / 60:.0f}-minute safety cutoff "
              f"(that window is reserved for the live regression test).",
              file=sys.stderr)
        return 1

    timestamps = []
    t = start
    while t <= end:
        timestamps.append(t)
        t += BACKFILL_STEP_SECONDS

    print(f"backfilling {len(timestamps)} minutes of baseline history: "
          f"{datetime.fromtimestamp(start, timezone.utc):%Y-%m-%dT%H:%M:%SZ} -> "
          f"{datetime.fromtimestamp(end, timezone.utc):%Y-%m-%dT%H:%M:%SZ}",
          flush=True)

    published, failed_batches = 0, 0
    for i in range(0, len(timestamps), BACKFILL_BATCH):
        chunk = timestamps[i:i + BACKFILL_BATCH]
        data = []
        for epoch in chunk:
            latencies, load = sample(epoch, "baseline")
            data.extend(payload(latencies, load,
                                 ts=datetime.fromtimestamp(epoch, timezone.utc),
                                 storage_resolution=60))
        try:
            cw.put_metric_data(Namespace=NAMESPACE, MetricData=data)
            published += len(chunk)
            print(f"  {published}/{len(timestamps)} timestamps published", flush=True)
        except Exception as e:                     # same posture as the live loop: log and keep going
            failed_batches += 1
            print(f"put_metric_data failed for batch starting "
                  f"{datetime.fromtimestamp(chunk[0], timezone.utc):%H:%M:%SZ}: {e}",
                  file=sys.stderr, flush=True)
            if failed_batches > 10:
                print("too many failed batches, giving up", file=sys.stderr)
                return 1

    print(f"backfill done: {published} timestamps published, "
          f"{failed_batches} failed batches", flush=True)
    return 0 if failed_batches == 0 else 1


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--mode", choices=["baseline", "regression", "flag"],
                    default="flag",
                    help="'flag' (default) follows %s live" % FLAG)
    ap.add_argument("--interval", type=float, default=10.0,
                    help="seconds between publishes (default 10, matches the "
                         "static alarm's Period)")
    ap.add_argument("--duration", type=float, default=0,
                    help="stop after N seconds (default 0 = run forever)")
    ap.add_argument("--region", default=os.environ.get("AWS_REGION", "us-east-1"))
    ap.add_argument("--flag-file", default=FLAG)
    ap.add_argument("--once", action="store_true", help="publish one cycle and exit")
    ap.add_argument("--backfill", type=float, default=None, metavar="HOURS",
                    help="publish baseline history from HOURS ago through 90 "
                         "minutes ago (StorageResolution 60), then exit "
                         "without entering the live loop")
    args = ap.parse_args()

    cw = boto3.client("cloudwatch", region_name=args.region,
                      config=Config(retries={"max_attempts": 5, "mode": "adaptive"}))

    if args.backfill is not None:
        return backfill(cw, args.backfill)

    started = time.time()
    n, errors, last_mode = 0, 0, None
    print(f"publishing {NAMESPACE}/* to {args.region} every {args.interval}s "
          f"(mode={args.mode}, flag={args.flag_file})", flush=True)

    while True:
        now = time.time()
        mode = (("regression" if os.path.exists(args.flag_file) else "baseline")
                if args.mode == "flag" else args.mode)
        if mode != last_mode:
            print(f"[{datetime.now():%H:%M:%S}] mode -> {mode.upper()}", flush=True)
            last_mode = mode

        latencies, load = sample(now, mode)
        try:
            cw.put_metric_data(Namespace=NAMESPACE, MetricData=payload(latencies, load))
            n += 1
            if n % 30 == 0 or args.once:
                avg = sum(latencies) / len(latencies)
                print(f"[{datetime.now():%H:%M:%S}] {n} published  "
                      f"mode={mode} latency~{avg:.1f}ms load={load} "
                      f"work={WORK_UNITS:.0f} reqs={len(latencies)} errors={errors}",
                      flush=True)
        except Exception as e:                     # keep publishing through blips
            errors += 1
            print(f"[{datetime.now():%H:%M:%S}] put_metric_data failed: {e}",
                  file=sys.stderr, flush=True)
            if errors > 50:
                print("too many consecutive failures, giving up", file=sys.stderr)
                return 1

        if args.once:
            return 0
        if args.duration and (time.time() - started) >= args.duration:
            print(f"done: {n} publishes, {errors} errors", flush=True)
            return 0
        time.sleep(max(0.0, args.interval - (time.time() - now)))


if __name__ == "__main__":
    sys.exit(main())
