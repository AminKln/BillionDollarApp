#!/usr/bin/env bash
# Print the most recent payload the dispatch Lambda built, pretty.
#
# Why this exists: `make logs` uses `aws logs tail`, which only exists in AWS
# CLI v2. Half the team is on v1 (run `make setup-awscli` to fix), and on stage
# is the wrong moment to find that out. This works on both.
#
# It is also the DEMO FALLBACK. If the GITHUB_TOKEN is not on the stack yet,
# the chain still runs end to end — alarm fires, EventBridge routes, the Lambda
# assembles the full evidence payload — it just stops short of POSTing to
# GitHub. This shows that payload. It is the same JSON W3's agent receives, so
# you can read the diagnosis off the screen even with no PR.
set -euo pipefail

REGION="${REGION:-us-east-1}"
GROUP="/aws/lambda/culprit-dispatch"
N="${1:-1}"          # how many recent payloads to show

STREAM=$(aws logs describe-log-streams \
  --log-group-name "$GROUP" --region "$REGION" \
  --order-by LastEventTime --descending \
  --query 'logStreams[0].logStreamName' --output text 2>/dev/null || true)

if [ -z "$STREAM" ] || [ "$STREAM" = "None" ]; then
  echo "no log streams in $GROUP — the Lambda has never run."
  echo "fire an alarm first:  make app-regress   (then wait ~2 min)"
  exit 1
fi

aws logs get-log-events \
  --log-group-name "$GROUP" --log-stream-name "$STREAM" \
  --region "$REGION" --query 'events[].message' --output text \
| python3 -c '
import json, sys, re
# Lambda print()s the dispatch body as one JSON line; everything else is
# START/END/REPORT noise. Pull the JSON lines back out.
msgs = [m.strip() for m in re.split(r"\t|\n", sys.stdin.read()) if m.strip().startswith("{")]
n = int(sys.argv[1])
if not msgs:
    print("the Lambda ran but never built a payload — check `make logs` for a traceback.")
    raise SystemExit(1)
for raw in msgs[-n:]:
    d = json.loads(raw)
    p = d.get("client_payload", {})
    print("=" * 72)
    print("  %s   %s" % (p.get("alarm_name", "?"), p.get("timestamp", "?")))
    print("=" * 72)
    for k in ("namespace", "metric_name", "threshold", "datapoints", "region"):
        print("  %-14s %s" % (k, p.get(k, "")))
    print("  %-14s %s" % ("state_reason", (p.get("state_reason") or "")[:120]))
    print()
    print("  EVIDENCE  (this is the diagnosis — what moved, what did not)")
    ev = json.loads(p.get("evidence", "{}") or "{}")
    if not ev:
        print("    (empty — alarm was forced with set-alarm-state, no real window)")
    for k, v in sorted(ev.items(), key=lambda kv: kv[0].split()[-1]):
        short = k.split()[-1]
        print("    %-22s %10s -> %-10s %s"
              % (short, v.get("before"), v.get("now"), v.get("change")))
    print()
    print("  dashboard: %s" % p.get("dashboard_url", ""))
    print()
' "$N"
