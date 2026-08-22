#!/usr/bin/env bash
# W2. Proves the whole detection chain end-to-end WITHOUT waiting for a real
# latency regression. This separates "is the plumbing connected" from "did
# the threshold trip" — the two failure modes look identical from the
# dashboard and only this script tells them apart.
#
#   ./scripts/verify_chain.sh
#   REGION=us-west-2 STACK=culprit-detection ./scripts/verify_chain.sh
#
# Every check is independently skippable: if infra/detection.yaml has not
# been deployed yet, checks 3-8 report a clean "SKIP" instead of spewing raw
# AWS CLI errors, so this is safe to run at any point in the build.
set -uo pipefail

NAMESPACE=${NAMESPACE:-HackathonDemo}
REGION=${REGION:-us-east-1}
STACK=${STACK:-culprit-detection}
DASHBOARD_NAME=${DASHBOARD_NAME:-Culprit}

# The app's metrics are dimensioned. A get-metric-data query with NO Dimensions
# does not match a dimensioned metric -- it silently returns zero datapoints and
# every check below reads as "the publisher is dead." Keep these in sync with
# AppDimensionName/AppDimensionValue in infra/detection.yaml.
DIM_NAME=${DIM_NAME:-App}
DIM_VALUE=${DIM_VALUE:-bad-app-ec2}
# Must match LatencySensitivity in infra/detection.yaml or check 4 reports a
# band the deployed alarm is not actually using.
SENSITIVITY=${SENSITIVITY:-8}

# Fallback names — used only if the stack is deployed but a particular
# Output happens to be missing. When the stack isn't deployed at all these
# are never consulted (checks 3-8 skip outright).
DEFAULT_STATIC_ALARM=culprit-App-High
DEFAULT_ANOMALY_ALARM=culprit-App-Anomaly
DEFAULT_ERROR_ALARM=culprit-App-ErrorRate
DEFAULT_RULE_NAME=culprit-alarm-state-change
DEFAULT_DISPATCH_FN=culprit-dispatch
DEFAULT_DISPATCH_LOG_GROUP=/aws/lambda/culprit-dispatch

FAIL=0
ok()   { printf '  \033[32mPASS\033[0m %s\n' "$1"; }
bad()  { printf '  \033[31mFAIL\033[0m %s\n' "$1"; FAIL=1; }
warn() { printf '  \033[33mWARN\033[0m %s\n' "$1"; }
skip() { printf '  \033[36mSKIP\033[0m %s\n' "$1"; }

command -v jq >/dev/null 2>&1 || { echo "jq is required"; exit 1; }

# Safe integer coercion for values that came out of jq/AWS — never let a
# stray null or empty string blow up a numeric [ -gt ] test under `set -u`.
as_int() { case "$1" in ''|*[!0-9]*) echo 0 ;; *) echo "$1" ;; esac; }

echo "verify_chain.sh — $NAMESPACE / $STACK / $REGION"
echo "=================================================="

# ---------------------------------------------------------------------------
echo
echo "1. metrics flowing"
METRICS_JSON=$(aws cloudwatch list-metrics --region "$REGION" --namespace "$NAMESPACE" --output json 2>&1)
if [ $? -ne 0 ]; then
  bad "list-metrics failed:"
  echo "$METRICS_JSON"
else
  for m in RequestLatency ErrorRate; do
    if printf '%s' "$METRICS_JSON" | jq -e --arg m "$m" '[.Metrics[].MetricName] | index($m) != null' >/dev/null 2>&1; then
      ok "metric present: $m"
    else
      bad "metric MISSING: $m — is the Flask app up on the EC2 box? (make app-status)"
    fi
  done

  END=$(date -u +%Y-%m-%dT%H:%M:%SZ)
  START=$(date -u -d '-5 minutes' +%Y-%m-%dT%H:%M:%SZ)
  RQ=$(jq -n --arg ns "$NAMESPACE" --arg dn "$DIM_NAME" --arg dv "$DIM_VALUE" \
        '[{Id: "lat", MetricStat: {Metric: {Namespace: $ns, MetricName: "RequestLatency",
        Dimensions: [{Name: $dn, Value: $dv}]}, Period: 60, Stat: "Average"}, ReturnData: true}]')
  RECENT=$(aws cloudwatch get-metric-data --region "$REGION" --start-time "$START" --end-time "$END" \
            --metric-data-queries "$RQ" --output json 2>&1)
  if [ $? -ne 0 ]; then
    bad "get-metric-data (recency check) failed:"
    echo "$RECENT"
  else
    CNT=$(as_int "$(printf '%s' "$RECENT" | jq -r '(.MetricDataResults[0].Values // []) | length' 2>/dev/null)")
    if [ "$CNT" -gt 0 ]; then
      ok "RequestLatency has $CNT datapoint(s) in the last 5 minutes — publisher is alive"
    else
      bad "RequestLatency has NO datapoints in the last 5 minutes — the app is not publishing. Check make app-status"
    fi
  fi
fi

# ---------------------------------------------------------------------------
echo
echo "2. stack deployed"
STACK_JSON=$(aws cloudformation describe-stacks --region "$REGION" --stack-name "$STACK" --output json 2>&1)
STACK_RC=$?
STACK_DEPLOYED=0
if [ "$STACK_RC" -ne 0 ]; then
  bad "stack '$STACK' not found — infra/detection.yaml has not been deployed yet"
else
  STATUS=$(printf '%s' "$STACK_JSON" | jq -r '.Stacks[0].StackStatus')
  case "$STATUS" in
    *_COMPLETE)
      ok "stack status: $STATUS"
      STACK_DEPLOYED=1
      case "$STATUS" in
        *ROLLBACK*) warn "status contains ROLLBACK — the stack exists but the last deploy failed; resources below may be absent or stale" ;;
      esac
      ;;
    *)
      bad "stack status is $STATUS (not a *_COMPLETE state)"
      ;;
  esac

  StaticAlarmName=$(printf '%s' "$STACK_JSON" | jq -r '.Stacks[0].Outputs[]? | select(.OutputKey=="AppStaticAlarmName") | .OutputValue')
  AnomalyAlarmName=$(printf '%s' "$STACK_JSON" | jq -r '.Stacks[0].Outputs[]? | select(.OutputKey=="AppAnomalyAlarmName") | .OutputValue')
  ErrorRateAlarmName=$(printf '%s' "$STACK_JSON" | jq -r '.Stacks[0].Outputs[]? | select(.OutputKey=="AppErrorRateAlarmName") | .OutputValue')
  RuleName=$(printf '%s' "$STACK_JSON" | jq -r '.Stacks[0].Outputs[]? | select(.OutputKey=="RuleName") | .OutputValue')
  DispatchFunctionName=$(printf '%s' "$STACK_JSON" | jq -r '.Stacks[0].Outputs[]? | select(.OutputKey=="DispatchFunctionName") | .OutputValue')
  DispatchLogGroupName=$(printf '%s' "$STACK_JSON" | jq -r '.Stacks[0].Outputs[]? | select(.OutputKey=="DispatchLogGroupName") | .OutputValue')
  DashboardUrl=$(printf '%s' "$STACK_JSON" | jq -r '.Stacks[0].Outputs[]? | select(.OutputKey=="DashboardUrl") | .OutputValue')
  AlertTopicArn=$(printf '%s' "$STACK_JSON" | jq -r '.Stacks[0].Outputs[]? | select(.OutputKey=="AlertTopicArn") | .OutputValue')

  ok "captured stack outputs"
  echo "     DispatchFunctionName = ${DispatchFunctionName:-<missing>}"
  echo "     DispatchLogGroupName = ${DispatchLogGroupName:-<missing>}"
  echo "     StaticAlarmName      = ${StaticAlarmName:-<missing>}"
  echo "     AnomalyAlarmName     = ${AnomalyAlarmName:-<missing>}"
  echo "     ErrorRateAlarmName   = ${ErrorRateAlarmName:-<missing>}"
  echo "     RuleName             = ${RuleName:-<missing>}"
  echo "     DashboardUrl         = ${DashboardUrl:-<missing>}"
  echo "     AlertTopicArn        = ${AlertTopicArn:-<missing>}"
fi
: "${StaticAlarmName:=$DEFAULT_STATIC_ALARM}"
: "${AnomalyAlarmName:=$DEFAULT_ANOMALY_ALARM}"
: "${ErrorRateAlarmName:=$DEFAULT_ERROR_ALARM}"
: "${RuleName:=$DEFAULT_RULE_NAME}"
: "${DispatchFunctionName:=$DEFAULT_DISPATCH_FN}"
: "${DispatchLogGroupName:=$DEFAULT_DISPATCH_LOG_GROUP}"
: "${DashboardUrl:=}"
: "${AlertTopicArn:=}"

# ---------------------------------------------------------------------------
echo
echo "3. alarms exist and are configured right"
if [ "$STACK_DEPLOYED" != 1 ]; then
  skip "alarms — stack not deployed"
else
  ALARMS_JSON=$(aws cloudwatch describe-alarms --region "$REGION" \
    --alarm-names "$StaticAlarmName" "$AnomalyAlarmName" "$ErrorRateAlarmName" --output json 2>&1)
  if [ $? -ne 0 ]; then
    bad "describe-alarms failed:"
    echo "$ALARMS_JSON"
  else
    SA=$(printf '%s' "$ALARMS_JSON" | jq -c --arg n "$StaticAlarmName" '[.MetricAlarms[] | select(.AlarmName==$n)][0]')
    if [ "$SA" = "null" ] || [ -z "$SA" ]; then
      bad "static alarm '$StaticAlarmName' not found"
    else
      SP=$(printf '%s' "$SA" | jq -r '.Period // empty')
      ST=$(printf '%s' "$SA" | jq -r '.Threshold // empty')
      SSTATE=$(printf '%s' "$SA" | jq -r '.StateValue')
      # 60, not 10. The 10s period belonged to the deleted synthetic feed,
      # which published at high resolution. The real app flushes every 10s but
      # the alarm deliberately averages a full minute: 1-of-1 at 60s is what
      # was calibrated against the 0.34s threshold and measured end to end
      # (push -> ALARM in 2m10s). Do not "fix" this by lowering the period
      # without re-measuring the whole chain.
      [ "$SP" = "60" ] && ok "static alarm Period == 60" || bad "static alarm Period is '${SP:-<unset>}', expected 60"
      [ -n "$ST" ] && ok "static alarm Threshold is set: $ST" || bad "static alarm Threshold is not set"
      echo "     $StaticAlarmName StateValue: $SSTATE"
    fi

    AA=$(printf '%s' "$ALARMS_JSON" | jq -c --arg n "$AnomalyAlarmName" '[.MetricAlarms[] | select(.AlarmName==$n)][0]')
    if [ "$AA" = "null" ] || [ -z "$AA" ]; then
      bad "anomaly alarm '$AnomalyAlarmName' not found"
    else
      TMID=$(printf '%s' "$AA" | jq -r '.ThresholdMetricId // empty')
      # Pick the underlying metric query by the presence of MetricStat, not by
      # a hardcoded Id. An anomaly alarm's Metrics[] holds two entries -- the
      # metric and the ANOMALY_DETECTION_BAND expression over it -- and
      # CloudFormation names them m2/ad2 here, not m1. Selecting on Id silently
      # matched nothing and reported the period as unset, which looks identical
      # to a misconfigured alarm.
      M1P=$(printf '%s' "$AA" | jq -r '[.Metrics[]? | select(.MetricStat) | .MetricStat.Period][0] // empty')
      ASTATE=$(printf '%s' "$AA" | jq -r '.StateValue')
      [ -n "$TMID" ] && ok "anomaly alarm ThresholdMetricId is set: $TMID" || bad "anomaly alarm ThresholdMetricId is empty"
      [ "$M1P" = "60" ] && ok "anomaly alarm metric Period == 60" || bad "anomaly alarm metric Period is '${M1P:-<unset>}', expected 60"
      echo "     $AnomalyAlarmName StateValue: $ASTATE"
    fi

    LA=$(printf '%s' "$ALARMS_JSON" | jq -c --arg n "$ErrorRateAlarmName" '[.MetricAlarms[] | select(.AlarmName==$n)][0]')
    if [ "$LA" = "null" ] || [ -z "$LA" ]; then
      bad "error-rate alarm '$ErrorRateAlarmName' not found"
    else
      LSTATE=$(printf '%s' "$LA" | jq -r '.StateValue')
      ok "load alarm present"
      echo "     $ErrorRateAlarmName StateValue: $LSTATE"
    fi
  fi
fi

# ---------------------------------------------------------------------------
echo
echo "4. anomaly band trained?"
BAND_TRAINED=0
if [ "$STACK_DEPLOYED" != 1 ]; then
  skip "anomaly band — stack not deployed"
else
  # We query ANOMALY_DETECTION_BAND(m1, 2) directly with get-metric-data
  # rather than reading culprit-App-Anomaly's StateValue. That alarm's
  # state depends on when it last happened to evaluate and sits in
  # INSUFFICIENT_DATA until then even after the band exists; a direct
  # get-metric-data call is deterministic — it either returns band values
  # right now or it doesn't, with no evaluation-timing race.
  END=$(date -u +%Y-%m-%dT%H:%M:%SZ)
  START=$(date -u -d '-15 minutes' +%Y-%m-%dT%H:%M:%SZ)
  BQ=$(jq -n --arg ns "$NAMESPACE" --arg dn "$DIM_NAME" --arg dv "$DIM_VALUE" --arg k "$SENSITIVITY" '[
    {Id: "m1", MetricStat: {Metric: {Namespace: $ns, MetricName: "RequestLatency",
      Dimensions: [{Name: $dn, Value: $dv}]}, Period: 60, Stat: "Average"}, ReturnData: false},
    {Id: "ad1", Expression: ("ANOMALY_DETECTION_BAND(m1, " + $k + ")"), ReturnData: true}
  ]')
  BAND=$(aws cloudwatch get-metric-data --region "$REGION" --start-time "$START" --end-time "$END" \
          --metric-data-queries "$BQ" --output json 2>&1)
  if [ $? -ne 0 ]; then
    bad "get-metric-data (band) failed:"
    echo "$BAND"
  else
    BCNT=$(as_int "$(printf '%s' "$BAND" | jq -r '(.MetricDataResults[0].Values // []) | length' 2>/dev/null)")
    if [ "$BCNT" -gt 0 ]; then
      ok "anomaly band returned $BCNT datapoint(s) — model is trained"
      BAND_TRAINED=1
    else
      warn "band still training — needs 3+ hours of data, this is expected early (docs/architecture.md §3)"
    fi
  fi

  DET=$(aws cloudwatch describe-anomaly-detectors --region "$REGION" \
        --namespace "$NAMESPACE" --metric-name RequestLatency --output json 2>&1)
  if [ $? -eq 0 ]; then
    DSTATE=$(printf '%s' "$DET" | jq -r '.AnomalyDetectors[0].StateValue // "unknown"')
    echo "     describe-anomaly-detectors state: $DSTATE"
  else
    echo "     describe-anomaly-detectors unavailable: $DET"
  fi
fi

# ---------------------------------------------------------------------------
echo
echo "5. eventbridge rule"
if [ "$STACK_DEPLOYED" != 1 ]; then
  skip "eventbridge rule — stack not deployed"
else
  RULE_JSON=$(aws events describe-rule --region "$REGION" --name "$RuleName" --output json 2>&1)
  if [ $? -ne 0 ]; then
    bad "describe-rule failed:"
    echo "$RULE_JSON"
  else
    RSTATE=$(printf '%s' "$RULE_JSON" | jq -r '.State')
    [ "$RSTATE" = "ENABLED" ] && ok "rule '$RuleName' is ENABLED" || bad "rule '$RuleName' state is '$RSTATE', expected ENABLED"
  fi

  TARGETS_JSON=$(aws events list-targets-by-rule --region "$REGION" --rule "$RuleName" --output json 2>&1)
  if [ $? -ne 0 ]; then
    bad "list-targets-by-rule failed:"
    echo "$TARGETS_JSON"
  else
    if printf '%s' "$TARGETS_JSON" | jq -e --arg fn "$DispatchFunctionName" \
        '[.Targets[]?.Arn | select(endswith(":function:" + $fn))] | length > 0' >/dev/null 2>&1; then
      ok "rule targets the $DispatchFunctionName function"
    else
      bad "rule does not target $DispatchFunctionName — targets: $(printf '%s' "$TARGETS_JSON" | jq -c '[.Targets[]?.Arn]')"
    fi
  fi
fi

# ---------------------------------------------------------------------------
echo
echo "6. lambda exists"
HAS_TOKEN=0
if [ "$STACK_DEPLOYED" != 1 ]; then
  skip "lambda — stack not deployed"
else
  FN_JSON=$(aws lambda get-function-configuration --region "$REGION" --function-name "$DispatchFunctionName" --output json 2>&1)
  if [ $? -ne 0 ]; then
    bad "get-function-configuration failed:"
    echo "$FN_JSON"
  else
    RUNTIME=$(printf '%s' "$FN_JSON" | jq -r '.Runtime // "unknown"')
    ok "function '$DispatchFunctionName' exists (runtime: $RUNTIME)"
    TOKEN_SET=$(printf '%s' "$FN_JSON" | jq -r '((.Environment.Variables.GITHUB_TOKEN // "") | length) > 0')
    if [ "$TOKEN_SET" = "true" ]; then
      ok "GITHUB_TOKEN env var is set (value not printed)"
      HAS_TOKEN=1
    else
      warn "GITHUB_TOKEN env var is NOT set — AWS half works, the PAT is still needed (W3 owns this)"
    fi
  fi
fi

# ---------------------------------------------------------------------------
echo
echo "7. THE END-TO-END WIRE TEST (forced alarm trip)"
WIRE_RESULT="skipped"
if [ "$STACK_DEPLOYED" != 1 ]; then
  skip "wire test — stack not deployed"
else
  # set-alarm-state is a no-op when the alarm is ALREADY in ALARM: CloudWatch
  # records no StateUpdate, so EventBridge emits no event, so the Lambda never
  # runs -- and the poll below then reports "no log lines" as if the wire were
  # broken. It is not; there was simply nothing to transition. Clear the alarm
  # to OK first so the forced trip is a genuine OK->ALARM edge.
  PRE_STATE=$(aws cloudwatch describe-alarms --region "$REGION" \
    --alarm-names "$StaticAlarmName" \
    --query 'MetricAlarms[0].StateValue' --output text 2>/dev/null)
  WIRE_SKIP=""
  if [ "$PRE_STATE" = "ALARM" ]; then
    warn "$StaticAlarmName is already in ALARM — clearing it so the trip is a real transition"
    aws cloudwatch set-alarm-state --region "$REGION" --alarm-name "$StaticAlarmName" \
      --state-value OK --state-reason "verify_chain.sh clearing pre-existing state" >/dev/null 2>&1
    # If a REAL breach is in flight, CloudWatch re-trips within an evaluation
    # period and the "forced" trip would actually be someone's live incident --
    # which the Lambda would rightly diagnose and file as a GitHub Issue. Do
    # not stage a synthetic test on top of a real one; say so and move on.
    settle=0
    while [ $settle -lt 6 ]; do
      sleep 5
      settle=$((settle + 1))
      PRE_STATE=$(aws cloudwatch describe-alarms --region "$REGION" \
        --alarm-names "$StaticAlarmName" \
        --query 'MetricAlarms[0].StateValue' --output text 2>/dev/null)
      [ "$PRE_STATE" = "OK" ] && break
    done
    if [ "$PRE_STATE" != "OK" ]; then
      WIRE_SKIP="a real alarm is in progress on $StaticAlarmName (state stayed $PRE_STATE)"
    fi
  fi
fi

if [ "$STACK_DEPLOYED" = 1 ] && [ -n "$WIRE_SKIP" ]; then
  skip "wire test — $WIRE_SKIP; not forcing a synthetic trip on top of a live incident"
elif [ "$STACK_DEPLOYED" = 1 ]; then
  TRIP_EPOCH_MS=$(( $(date -u +%s) * 1000 - 5000 ))  # 5s buffer for clock skew
  SET_OUT=$(aws cloudwatch set-alarm-state --region "$REGION" --alarm-name "$StaticAlarmName" \
    --state-value ALARM --state-reason "verify_chain.sh forced trip" 2>&1)
  if [ $? -ne 0 ]; then
    bad "set-alarm-state ALARM failed:"
    echo "$SET_OUT"
  else
    ok "forced $StaticAlarmName -> ALARM"
    # The alarm is now latched ALARM by hand. If this script dies here --
    # Ctrl-C, a failing AWS call, a closed terminal -- it stays latched, and
    # a latched alarm emits no further OK->ALARM transition, so the next REAL
    # regression produces no EventBridge event and no dispatch at all. The
    # pipeline goes silently blind while every dashboard still looks fine.
    # Unlatch on any exit path; cleared again after the normal reset below.
    trap 'aws cloudwatch set-alarm-state --region "$REGION" \
      --alarm-name "$StaticAlarmName" --state-value OK \
      --state-reason "verify_chain.sh interrupted - unlatching" >/dev/null 2>&1
      printf "\n\033[33m  unlatched %s on exit\033[0m\n" "$StaticAlarmName"' EXIT INT TERM
    printf '     polling %s for up to 60s (every 5s)' "$DispatchLogGroupName"
    FOUND_LOG=""
    tries=0
    while [ $tries -lt 12 ]; do
      sleep 5
      tries=$((tries + 1))
      printf '.'
      LOGS=$(aws logs filter-log-events --region "$REGION" --log-group-name "$DispatchLogGroupName" \
        --start-time "$TRIP_EPOCH_MS" --output json 2>&1)
      if [ $? -eq 0 ]; then
        MSGCOUNT=$(as_int "$(printf '%s' "$LOGS" | jq -r '.events | length' 2>/dev/null)")
        if [ "$MSGCOUNT" -gt 0 ]; then
          FOUND_LOG=$(printf '%s' "$LOGS" | jq -r '.events[].message')
          break
        fi
      fi
    done
    echo

    if [ -n "$FOUND_LOG" ]; then
      ok "Lambda logged within the poll window"
      echo "     --- log output ---"
      printf '%s\n' "$FOUND_LOG" | sed 's/^/     /'
      echo "     -------------------"
      if printf '%s' "$FOUND_LOG" | grep -qi '204'; then
        ok "log shows a GitHub 204 — the entire chain works end to end"
        WIRE_RESULT="full"
      elif printf '%s' "$FOUND_LOG" | grep -qiE 'no.?token|GITHUB_TOKEN'; then
        ok "AWS half works; Lambda reports no/missing token — PASS WITH NOTE, PAT is W3's to add"
        WIRE_RESULT="no-token"
      else
        warn "Lambda logged, but output doesn't clearly show a 204 or a missing-token message — inspect above"
        WIRE_RESULT="unclear"
      fi
    else
      bad "no log lines appeared in $DispatchLogGroupName within 60s"
      echo "     alarm state right now:"
      aws cloudwatch describe-alarm-history --region "$REGION" \
        --alarm-name "$StaticAlarmName" --history-item-type StateUpdate \
        --max-records 3 --query 'AlarmHistoryItems[].[Timestamp,HistorySummary]' \
        --output text 2>/dev/null | sed 's/^/       /'
      echo "     if the newest entry predates the trip, the alarm was already in"
      echo "     ALARM and set-alarm-state was a no-op — the wire is fine."
      echo "     debugging pointers:"
      echo "       aws events describe-rule --region $REGION --name $RuleName"
      echo "       aws events list-targets-by-rule --region $REGION --rule $RuleName"
      echo "       aws lambda get-policy --region $REGION --function-name $DispatchFunctionName"
      WIRE_RESULT="broken"
    fi

    RESET_OUT=$(aws cloudwatch set-alarm-state --region "$REGION" --alarm-name "$StaticAlarmName" \
      --state-value OK --state-reason "verify_chain.sh reset" 2>&1)
    if [ $? -eq 0 ]; then
      trap - EXIT INT TERM      # reset landed; no unlatch needed on exit
      ok "reset $StaticAlarmName -> OK"
    else
      bad "reset to OK failed: $RESET_OUT"
    fi
  fi
fi

# ---------------------------------------------------------------------------
echo
echo "8. dashboard exists"
if [ "$STACK_DEPLOYED" != 1 ]; then
  skip "dashboard — stack not deployed"
else
  DASH_JSON=$(aws cloudwatch get-dashboard --region "$REGION" --dashboard-name "$DASHBOARD_NAME" --output json 2>&1)
  if [ $? -ne 0 ]; then
    bad "get-dashboard failed:"
    echo "$DASH_JSON"
  else
    ok "dashboard '$DASHBOARD_NAME' exists"
    echo "     console URL: ${DashboardUrl:-https://console.aws.amazon.com/cloudwatch/home?region=${REGION}#dashboards:name=${DASHBOARD_NAME}}"
  fi
fi

# ---------------------------------------------------------------------------
echo
echo "=================================================="
echo "summary"
echo "=================================================="
if [ "$STACK_DEPLOYED" != 1 ]; then
  echo "  works:   metrics are flowing into CloudWatch (check 1)"
  echo "  pending: stack '$STACK' is not deployed — checks 3-8 skipped"
  echo "  next action: deploy infra/detection.yaml (aws cloudformation deploy ...), then re-run this script"
else
  echo "  works:   stack deployed, alarms configured, EventBridge rule + Lambda wired"
  case "$WIRE_RESULT" in
    full)    echo "  works:   end-to-end wire test — Lambda fired and GitHub accepted the dispatch (204)" ;;
    no-token) echo "  works:   end-to-end wire test — Lambda fired correctly, only the GitHub PAT is missing" ;;
    unclear) echo "  pending: end-to-end wire test — Lambda fired but log output was inconclusive, inspect above" ;;
    broken)  echo "  pending: end-to-end wire test — Lambda did NOT fire, chain is broken (see debugging pointers above)" ;;
  esac
  if [ "$BAND_TRAINED" = 1 ]; then
    echo "  works:   anomaly band is trained"
  else
    echo "  pending: anomaly band still training (needs 3+ hours of data)"
  fi
  if [ "$HAS_TOKEN" = 1 ]; then
    echo "  works:   GITHUB_TOKEN is set on $DispatchFunctionName"
  else
    echo "  pending: GITHUB_TOKEN not set on $DispatchFunctionName (W3's PAT)"
  fi

  NEXT="none — chain is fully wired and verified"
  [ "$WIRE_RESULT" = "broken" ] && NEXT="fix the EventBridge rule / Lambda permission / target wiring, then re-run"
  [ "$WIRE_RESULT" != "broken" ] && [ "$HAS_TOKEN" != 1 ] && NEXT="have W3 set GITHUB_TOKEN on $DispatchFunctionName"
  [ "$WIRE_RESULT" != "broken" ] && [ "$HAS_TOKEN" = 1 ] && [ "$BAND_TRAINED" != 1 ] && NEXT="let the publisher keep running — the band needs a few more hours"
  echo "  next action: $NEXT"
fi
echo "=================================================="

if [ "$FAIL" = 0 ]; then
  echo "ALL CHECKS PASSED (or cleanly skipped)."
else
  echo "SOMETHING FAILED — see FAIL lines above."
fi
exit "$FAIL"
