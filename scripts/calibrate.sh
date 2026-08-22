#!/usr/bin/env bash
# W2. Never guess the alarm threshold — derive it from data actually sitting
# in CloudWatch right now.
#
# Reads RequestLatency straight off the real Flask app (namespace
# HackathonDemo, App=bad-app-ec2) and recommends:
#
#     AppLatencyThresholdSeconds = max(max_observed * 1.6, avg_observed * 3)
#
# then sanity-gates it against reality: the number must sit above every
# baseline spike we have actually measured (so culprit-App-High is quiet when
# nothing is wrong) and comfortably below the regressed value (so it trips
# fast when something is).
#
#   ./scripts/calibrate.sh
#   MINUTES=60 ./scripts/calibrate.sh
#   REGION=us-west-2 ./scripts/calibrate.sh
#
# UNITS ARE SECONDS. The app publishes RequestLatency with Unit=Seconds --
# healthy is ~0.05, the quadratic regression is ~1.1-2.1. An earlier version
# of this script worked in milliseconds against a synthetic feed that no
# longer exists; a number from that version is 1000x wrong here.
set -uo pipefail

NAMESPACE=${NAMESPACE:-HackathonDemo}
DIM_NAME=${DIM_NAME:-App}
DIM_VALUE=${DIM_VALUE:-bad-app-ec2}
REGION=${REGION:-us-east-1}
MINUTES=${MINUTES:-30}
PERIOD=${PERIOD:-60}
# Floor of the regressed range, measured: the culprit patch costs ~1.07s per
# request at SCORE_BATCH=4000 (see infra/bad-app/culprit.py.patch). The
# recommendation has to land below this or the alarm never fires on the demo.
REGRESSED=${REGRESSED:-1.0}

FAIL=0
ok()   { printf '  \033[32mPASS\033[0m %s\n' "$1"; }
bad()  { printf '  \033[31mFAIL\033[0m %s\n' "$1"; FAIL=1; }
warn() { printf '  \033[33mWARN\033[0m %s\n' "$1"; }

command -v jq >/dev/null 2>&1 || { echo "jq is required (this script does all JSON handling with it)"; exit 1; }

END=$(date -u +%Y-%m-%dT%H:%M:%SZ)
START=$(date -u -d "-${MINUTES} minutes" +%Y-%m-%dT%H:%M:%SZ)

echo "calibrating $NAMESPACE/RequestLatency ($DIM_NAME=$DIM_VALUE) in $REGION"
echo "  window: $START .. $END  (${MINUTES}m, ${PERIOD}s period)"
echo

# The Dimensions block is load-bearing. Query this namespace without it and
# CloudWatch matches the undimensioned metric, which does not exist, and
# returns zero datapoints -- indistinguishable from "the app is down."
QUERIES=$(jq -n --arg ns "$NAMESPACE" --arg dn "$DIM_NAME" --arg dv "$DIM_VALUE" --argjson period "$PERIOD" '
  [
    {Id: "avgq", MetricStat: {Metric: {Namespace: $ns, MetricName: "RequestLatency",
                              Dimensions: [{Name: $dn, Value: $dv}]},
                              Period: $period, Stat: "Average"}, ReturnData: true},
    {Id: "mxq",  MetricStat: {Metric: {Namespace: $ns, MetricName: "RequestLatency",
                              Dimensions: [{Name: $dn, Value: $dv}]},
                              Period: $period, Stat: "Maximum"}, ReturnData: true},
    {Id: "mnq",  MetricStat: {Metric: {Namespace: $ns, MetricName: "RequestLatency",
                              Dimensions: [{Name: $dn, Value: $dv}]},
                              Period: $period, Stat: "Minimum"}, ReturnData: true}
  ]')
# No p99 here, deliberately. The app publishes RequestLatency with
# StatisticValues (SampleCount/Sum/Min/Max) rather than raw values, and
# CloudWatch cannot compute percentiles from a statistic set -- a p99 query
# comes back empty, which silently turned the whole recommendation into null.
# Maximum is what a statistic set does carry, so that is what we key off.

RESP=$(aws cloudwatch get-metric-data \
  --region "$REGION" \
  --start-time "$START" --end-time "$END" \
  --metric-data-queries "$QUERIES" \
  --output json 2>&1)
RC=$?
if [ $RC -ne 0 ]; then
  bad "get-metric-data failed:"
  echo "$RESP"
  exit 1
fi

# Guard against the pipe-context trap: `.Values|add/(.Values|length)` re-binds
# `.` to the piped array inside the parens on the right of `/`, so the second
# `.Values` errors with "Cannot index array with string". Bind the arrays to
# names with `as` instead, and compute everything off the bound names.
STATS=$(printf '%s' "$RESP" | jq -c '
  (.MetricDataResults[] | select(.Id == "avgq") | .Values) as $avgs |
  (.MetricDataResults[] | select(.Id == "mxq")  | .Values) as $maxs |
  (.MetricDataResults[] | select(.Id == "mnq")  | .Values) as $mins |
  {
    count:        ($avgs | length),
    avg_observed: (if ($avgs | length) > 0 then (($avgs | add) / ($avgs | length)) else null end),
    max_observed: (if ($maxs | length) > 0 then ($maxs | max) else null end),
    min_observed: (if ($mins | length) > 0 then ($mins | min) else null end)
  }')

COUNT=$(printf '%s' "$STATS" | jq -r '.count')

if [ "$COUNT" = "0" ] || [ "$COUNT" = "null" ] || [ -z "$COUNT" ]; then
  bad "no RequestLatency datapoints in the last ${MINUTES} minutes"
  echo
  echo "  Nothing to calibrate against. The app itself is the publisher:"
  echo "    make app-status      # is the box up and serving?"
  echo
  echo "  If the app is up and this is still empty, the dimension is wrong."
  echo "  Check what actually exists:"
  echo "    aws cloudwatch list-metrics --namespace $NAMESPACE --region $REGION"
  exit 1
fi

AVG=$(printf '%s' "$STATS" | jq -r '.avg_observed')
MAX=$(printf '%s' "$STATS" | jq -r '.max_observed')
MIN=$(printf '%s' "$STATS" | jq -r '.min_observed')

echo "1. samples observed"
ok "$COUNT datapoints over the window (period ${PERIOD}s)"
printf '     baseline avg (p50-ish, use Average): %10.4f s\n' "$AVG"
printf '     observed max (single worst request): %10.4f s\n' "$MAX"
printf '     observed min (fastest request seen):  %10.4f s\n' "$MIN"
SPAN_MIN=$(jq -n --argjson c "$COUNT" --argjson p "$PERIOD" '(($c * $p) / 60) | (. * 10 | round) / 10')
if [ "$(jq -n --argjson s "$SPAN_MIN" --argjson m "$MINUTES" '$s < ($m * 0.8)')" = "true" ]; then
  warn "only ~${SPAN_MIN}m of data available (requested ${MINUTES}m) — the app may have restarted recently; re-run later for a firmer number"
fi
echo

MAX_TERM=$(jq -n --argjson mx "$MAX" '$mx * 1.6')
AVG_TERM=$(jq -n --argjson avg "$AVG" '$avg * 3')
REC=$(jq -n --argjson a "$MAX_TERM" --argjson b "$AVG_TERM" '([$a, $b] | max) | (. * 1000 | round) / 1000')

echo "2. recommendation"
echo "     AppLatencyThresholdSeconds = max(max_observed * 1.6, avg_observed * 3)"
printf '                                = max(%.4f * 1.6, %.4f * 3)\n' "$MAX" "$AVG"
printf '                                = max(%.4f, %.4f)\n' "$MAX_TERM" "$AVG_TERM"
printf '                                = %s\n' "$REC"
echo
echo "     why: $REC sits above the highest baseline spike actually measured"
echo "     ($MAX s), so culprit-App-High stays quiet when nothing is wrong; and"
echo "     below the ${REGRESSED}s floor of the regressed range, so 1 datapoint at a"
echo "     ${PERIOD}s period trips it within ~${PERIOD}s of the bad commit going live."
echo

echo "3. sanity gate — threshold must be > observed max AND < ${REGRESSED}"
ABOVE_MAX=$(jq -n --argjson rec "$REC" --argjson mx "$MAX" '$rec > $mx')
UNDER_REG=$(jq -n --argjson rec "$REC" --argjson r "$REGRESSED" '$rec < $r')

if [ "$ABOVE_MAX" = "true" ]; then
  ok "$REC > observed max ($MAX s) — no false positives from baseline noise"
else
  bad "$REC is NOT above observed max ($MAX s) — this would false-positive on baseline noise"
fi

if [ "$UNDER_REG" = "true" ]; then
  ok "$REC < $REGRESSED — leaves margin below the regressed value"
else
  bad "$REC is NOT under $REGRESSED — the regression would not reliably trip it"
fi

if [ "$ABOVE_MAX" != "true" ] || [ "$UNDER_REG" != "true" ]; then
  echo
  echo "  *** WARNING: recommendation ($REC) is OUTSIDE the sane window"
  echo "  ($MAX < threshold < $REGRESSED). Do not deploy this number as-is."
  echo "  This usually means one of:"
  echo "    - chaos is still ON, or the culprit commit is still deployed, so"
  echo "      the 'baseline' you just measured is the regression"
  echo "      (make app-status, then make app-recover)"
  echo "    - not enough baseline data yet — widen MINUTES and re-run"
  echo "  ***"
fi
echo

if [ "$FAIL" = 0 ]; then
  echo "deploy override:"
  echo "  APP_LATENCY_THRESHOLD_SECONDS=$REC make deploy"
  echo
  echo "calibration OK."
else
  echo "calibration produced a recommendation OUTSIDE the sane window — see WARNING above before deploying."
fi
exit "$FAIL"
