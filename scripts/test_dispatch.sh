#!/usr/bin/env bash
# W3, first task of the night. De-risks the entire GitHub half of the pipeline
# before a single AWS resource exists. Needs only curl, jq and a classic PAT
# with scopes `repo` + `workflow`.
#
#   GITHUB_TOKEN=ghp_xxx ./scripts/test_dispatch.sh
#
# All five checks must pass. Check 4 returning 204 is the one that matters:
# it proves the Lambda's only job will work.
set -uo pipefail

: "${GITHUB_TOKEN:?set GITHUB_TOKEN to a classic PAT with repo+workflow scopes}"

# derive owner/repo from the origin remote so this is not pinned to one fork
ORIGIN=$(git config --get remote.origin.url)
SLUG=$(printf '%s' "$ORIGIN" | sed -E 's#^.*github\.com[:/]##; s#\.git$##')
OWNER=${SLUG%%/*}
REPO=${SLUG##*/}
API="https://api.github.com/repos/$OWNER/$REPO"
H=(-H "Authorization: Bearer ${GITHUB_TOKEN}"
   -H "Accept: application/vnd.github+json"
   -H "X-GitHub-Api-Version: 2022-11-28")
FAIL=0
ok()   { printf '  \033[32mPASS\033[0m %s\n' "$1"; }
bad()  { printf '  \033[31mFAIL\033[0m %s\n' "$1"; FAIL=1; }

echo "target: $OWNER/$REPO"

echo; echo "1. token identity and scopes"
SCOPES=$(curl -sS -D- -o /dev/null "${H[@]}" https://api.github.com/user \
         | tr -d '\r' | awk -F': ' 'tolower($1)=="x-oauth-scopes"{print $2}')
LOGIN=$(curl -sS "${H[@]}" https://api.github.com/user | jq -r '.login // "?"')
echo "  as $LOGIN, scopes: ${SCOPES:-<none>}"
case "$SCOPES" in *repo*)     ok "repo scope present" ;;     *) bad "repo scope MISSING" ;; esac
case "$SCOPES" in *workflow*) ok "workflow scope present" ;; *) bad "workflow scope MISSING — the agent cannot push .github/ changes" ;; esac

echo; echo "2. write access (create then delete a throwaway branch)"
SHA=$(curl -sS "${H[@]}" "$API/git/ref/heads/main" | jq -r '.object.sha // empty')
[ -n "$SHA" ] && ok "read main: ${SHA:0:7}" || bad "cannot read refs/heads/main"
C=$(curl -sS -o /dev/null -w '%{http_code}' -X POST "${H[@]}" "$API/git/refs" \
    -d "{\"ref\":\"refs/heads/smoke-test-delete-me\",\"sha\":\"$SHA\"}")
[ "$C" = 201 ] && ok "create branch (201)" || bad "create branch returned $C"
C=$(curl -sS -o /dev/null -w '%{http_code}' -X DELETE "${H[@]}" "$API/git/refs/heads/smoke-test-delete-me")
[ "$C" = 204 ] && ok "delete branch (204)" || bad "delete branch returned $C"

echo; echo "3. issue path (the fallback artifact when no fix is possible)"
N=$(curl -sS -X POST "${H[@]}" "$API/issues" \
    -d '{"title":"smoke test — delete me","body":"pipeline smoke test"}' | jq -r '.number // empty')
if [ -n "$N" ]; then
  ok "opened issue #$N"
  C=$(curl -sS -o /dev/null -w '%{http_code}' -X PATCH "${H[@]}" "$API/issues/$N" -d '{"state":"closed"}')
  [ "$C" = 200 ] && ok "closed issue #$N" || bad "close returned $C"
else
  bad "could not open an issue"
fi

echo; echo "4. repository_dispatch (this is what the Lambda will do)"
PAYLOAD=$(jq -c '{event_type, client_payload}' docs/contracts/dispatch-payload.json)
C=$(curl -sS -o /dev/null -w '%{http_code}' -X POST "${H[@]}" "$API/dispatches" -d "$PAYLOAD")
[ "$C" = 204 ] && ok "dispatch accepted (204)" || bad "dispatch returned $C (401=scopes, 404=wrong repo or no write, 422=payload)"

echo; echo "5. Actions enabled and the workflow is registered on the default branch"
WF=$(curl -sS "${H[@]}" "$API/actions/workflows")
CNT=$(printf '%s' "$WF" | jq -r '.total_count // 0')
if [ "$CNT" -gt 0 ]; then
  printf '%s' "$WF" | jq -r '.workflows[] | "  \(.name)  [\(.state)]  \(.path)"'
  printf '%s' "$WF" | jq -e '.workflows[] | select(.path | endswith("anomaly-response.yml"))' >/dev/null \
    && ok "anomaly-response.yml registered" \
    || bad "anomaly-response.yml NOT registered — repository_dispatch only fires workflows already merged to the DEFAULT branch"
else
  bad "no workflows registered (Actions disabled, or nothing merged to main yet)"
fi

echo
[ "$FAIL" = 0 ] && echo "ALL CHECKS PASSED — the GitHub half is live." \
                || { echo "SOMETHING FAILED — fix it now, not at 08:00."; exit 1; }
