#!/usr/bin/env bash
# W2. Never guess the alarm threshold — derive it from data actually sitting
# in CloudWatch right now.
#
# Pulls Average / Maximum / p99 for RequestLatency over the trailing window
# and recommends:
#
#     LatencyThresholdMs = max(p99_observed * 1.6, avg_observed * 3)
#
# rounded to a whole number, then sanity-gates it against reality: the number
# must sit above every baseline spike we've actually measured (so
# culprit-Latency-High is quiet overnight) and comfortably below the ~250ms
# regressed value (so it trips within EvaluationPeriods=2 * Period=10s = 20s
# of the bad commit going live).
#
#   ./scripts/calibrate.sh
#   MINUTES=60 ./scripts/calibrate.sh
#   REGION=us-west-2 NAMESPACE=Culprit ./scripts/calibrate.sh
#
# Re-run this after any overnight drift (see docs/architecture.md §6, 07:30
# slot) and paste the printed LatencyThresholdMs= line into the CFN deploy
# override.
set -uo pipefail

NAMESPACE=${NAMESPACE:-Culprit}
REGION=${REGION:-us-east-1}
MINUTES=${MINUTES:-30}
PERIOD=${PERIOD:-10}

FAIL=0
ok()   { printf '  \033[32mPASS\033[0m %s\n' "$1"; }
bad()  { printf '  \033[31mFAIL\033[0m %s\n' "$1"; FAIL=1; }
warn() { printf '  \033[33mWARN\033[0m %s\n' "$1"; }

command -v jq >/dev/null 2>&1 || { echo "jq is required (this script does all JSON handling with it)"; exit 1; }

END=$(date -u +%Y-%m-%dT%H:%M:%SZ)
START=$(date -u -d "-${MINUTES} minutes" +%Y-%m-%dT%H:%M:%SZ)

echo "calibrating $NAMESPACE/RequestLatency in $REGION"
echo "  window: $START .. $END  (${MINUTES}m, ${PERIOD}s period)"
echo

QUERIES=$(jq -n --arg ns "$NAMESPACE" --argjson period "$PERIOD" '
  [
    {Id: "avgq", MetricStat: {Metric: {Namespace: $ns, MetricName: "RequestLatency"},
                              Period: $period, Stat: "Average"}, ReturnData: true},
    {Id: "mxq",  MetricStat: {Metric: {Namespace: $ns, MetricName: "RequestLatency"},
                              Period: $period, Stat: "Maximum"}, ReturnData: true},
    {Id: "p99q", MetricStat: {Metric: {Namespace: $ns, MetricName: "RequestLatency"},
                              Period: $period, Stat: "p99"},     ReturnData: true}
  ]')

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
  (.MetricDataResults[] | select(.Id == "p99q") | .Values) as $p99s |
  {
    count:        ($avgs | length),
    avg_observed: (if ($avgs | length) > 0 then (($avgs | add) / ($avgs | length)) else null end),
    max_observed: (if ($maxs | length) > 0 then ($maxs | max) else null end),
    p99_observed: (if ($p99s | length) > 0 then ($p99s | max) else null end)
  }')

COUNT=$(printf '%s' "$STATS" | jq -r '.count')

if [ "$COUNT" = "0" ] || [ "$COUNT" = "null" ] || [ -z "$COUNT" ]; then
  bad "no RequestLatency datapoints in the last ${MINUTES} minutes"
  echo
  echo "  Nothing to calibrate against. Start the publisher first:"
  echo "    ./scripts/seed_metrics.py"
  echo
  echo "  (docs/architecture.md §3: high-resolution datapoints only live 3h,"
  echo "   so this always looks at recent data, never stale history.)"
  exit 1
fi

AVG=$(printf '%s' "$STATS" | jq -r '.avg_observed')
MAX=$(printf '%s' "$STATS" | jq -r '.max_observed')
P99=$(printf '%s' "$STATS" | jq -r '.p99_observed')

echo "1. samples observed"
ok "$COUNT datapoints over the window (period ${PERIOD}s)"
printf '     baseline avg (p50-ish, use Average): %10.2f ms\n' "$AVG"
printf '     observed p99 (worst per-period p99): %10.2f ms\n' "$P99"
printf '     observed max (single worst request): %10.2f ms\n' "$MAX"
SPAN_MIN=$(jq -n --argjson c "$COUNT" --argjson p "$PERIOD" '(($c * $p) / 60) | (. * 10 | round) / 10')
if [ "$(jq -n --argjson s "$SPAN_MIN" --argjson m "$MINUTES" '$s < ($m * 0.8)')" = "true" ]; then
  warn "only ~${SPAN_MIN}m of data available (requested ${MINUTES}m) — the publisher may have started recently; re-run later for a firmer number"
fi
echo

P99_TERM=$(jq -n --argjson p99 "$P99" '$p99 * 1.6')
AVG_TERM=$(jq -n --argjson avg "$AVG" '$avg * 3')
REC=$(jq -n --argjson a "$P99_TERM" --argjson b "$AVG_TERM" '([$a, $b] | max) | round')

echo "2. recommendation"
echo "     LatencyThresholdMs = max(p99_observed * 1.6, avg_observed * 3)"
printf '                        = max(%.2f * 1.6, %.2f * 3)\n' "$P99" "$AVG"
printf '                        = max(%.2f, %.2f)\n' "$P99_TERM" "$AVG_TERM"
printf '                        = %s\n' "$REC"
echo
echo "     why: $REC sits comfortably above the highest baseline spike we"
echo "     actually measured ($MAX ms max), so the static alarm stays quiet"
echo "     overnight; it sits far below the ~250ms regressed value, so 2/2"
echo "     datapoints at a ${PERIOD}s period trips the alarm within $((PERIOD * 2))s of the"
echo "     bad commit going live."
echo

echo "3. sanity gate — threshold must be > observed max AND < 200"
ABOVE_MAX=$(jq -n --argjson rec "$REC" --argjson mx "$MAX" '$rec > $mx')
UNDER_200=$(jq -n --argjson rec "$REC" '$rec < 200')

if [ "$ABOVE_MAX" = "true" ]; then
  ok "$REC > observed max ($MAX ms) — no false positives from baseline noise"
else
  bad "$REC is NOT above observed max ($MAX ms) — this would false-positive on baseline noise"
fi

if [ "$UNDER_200" = "true" ]; then
  ok "$REC < 200 — leaves margin below the ~250ms regressed value"
else
  bad "$REC is NOT under 200 — leaves little or no margin before the ~250ms regressed value"
fi

if [ "$ABOVE_MAX" != "true" ] || [ "$UNDER_200" != "true" ]; then
  echo
  echo "  *** WARNING: recommendation ($REC) is OUTSIDE the sane window"
  echo "  ($MAX < threshold < 200). Do not deploy this number as-is. This"
  echo "  usually means one of:"
  echo "    - the publisher is currently in REGRESSION mode (check for"
  echo "      /tmp/culprit-regression.flag — 'rm' it to go back to baseline)"
  echo "    - not enough baseline data yet — widen MINUTES or wait and re-run"
  echo "  ***"
fi
echo

# Only publish a number that passed the gate. .run/threshold is what
# deploy_detection.sh reads, so writing a rejected value here would silently
# arm an alarm that can never fire -- the one failure mode that still looks
# healthy on every dashboard.
if [ "$FAIL" = 0 ]; then
  mkdir -p .run
  printf '%s' "$REC" > .run/threshold
  echo "wrote .run/threshold ($REC)"
  echo
  echo "deploy override:"
  echo "  LatencyThresholdMs=$REC"
else
  echo "NOT writing .run/threshold — $REC failed the sanity gate above."
  if [ -f .run/threshold ]; then
    echo "  left in place: $(cat .run/threshold) (from a previous good run)"
  else
    echo "  no previous value; deploy would fall back to its built-in default."
  fi
fi
echo

if [ "$FAIL" = 0 ]; then
  echo "calibration OK."
else
  echo "calibration produced a recommendation OUTSIDE the sane window — see WARNING above before deploying."
fi
exit "$FAIL"
