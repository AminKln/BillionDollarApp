#!/usr/bin/env bash
# Plant (or revert) the culprit commit in the REAL bad app repo
# (Tehreem404/bad_app_demo) -- the O(n^2) "traffic quality score" regression
# described in infra/bad-app/culprit.py.patch.
#
#   ./scripts/regress_commit.sh apply     commit + push the regression to main
#   ./scripts/regress_commit.sh revert    git revert it + push to main
#
# Both subcommands are idempotent: run "apply" twice and the second run is a
# no-op that reports the existing commit instead of double-applying; run
# "revert" before "apply" has ever landed and it fails loudly instead of
# reverting nothing.
#
# Env overrides:
#   BAD_APP_REPO_URL   git remote to clone/push       (default: git@github.com:Tehreem404/bad_app_demo.git)
#   BAD_APP_CLONE_DIR  scratch working copy            (default: ${TMPDIR:-/tmp}/culprit-bad-app-demo)
#   BAD_APP_BRANCH     branch to commit/push against   (default: main)
#   DRY_RUN=1          do everything except the final `git push`
#
# This script never operates on the user's cwd -- everything happens inside
# BAD_APP_CLONE_DIR, a disposable clone that gets hard-reset to
# origin/$BAD_APP_BRANCH at the start of every run so stale local state from a
# previous half-finished run can never leak into this one.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
PATCH_FILE="${CULPRIT_PATCH:-$REPO_ROOT/infra/bad-app/culprit.py.patch}"

BAD_APP_REPO_URL="${BAD_APP_REPO_URL:-git@github.com:Tehreem404/bad_app_demo.git}"
BAD_APP_CLONE_DIR="${BAD_APP_CLONE_DIR:-${TMPDIR:-/tmp}/culprit-bad-app-demo}"
BAD_APP_BRANCH="${BAD_APP_BRANCH:-main}"
COMMIT_SUBJECT="Add per-request traffic quality score (#142)"
DRY_RUN="${DRY_RUN:-0}"

RED=$'\e[31m'; GRN=$'\e[32m'; YEL=$'\e[33m'; RST=$'\e[0m'
info() { echo "  ${GRN}==>${RST} $*"; }
warn() { echo "  ${YEL}==>${RST} $*"; }
die()  { echo "  ${RED}!!${RST} $*" >&2; exit 1; }

usage() { echo "usage: $0 apply|revert" >&2; exit 1; }

[ $# -eq 1 ] || usage
ACTION="$1"
case "$ACTION" in
  apply|revert) ;;
  *) usage ;;
esac

[ -f "$PATCH_FILE" ] || die "patch not found: $PATCH_FILE"

# --- sync the scratch clone to a known-clean state --------------------------

if [ -d "$BAD_APP_CLONE_DIR/.git" ]; then
  info "reusing scratch clone at $BAD_APP_CLONE_DIR"
  git -C "$BAD_APP_CLONE_DIR" remote set-url origin "$BAD_APP_REPO_URL"
else
  info "cloning $BAD_APP_REPO_URL -> $BAD_APP_CLONE_DIR"
  mkdir -p "$(dirname "$BAD_APP_CLONE_DIR")"
  git clone "$BAD_APP_REPO_URL" "$BAD_APP_CLONE_DIR"
fi

cd "$BAD_APP_CLONE_DIR"
git fetch origin "$BAD_APP_BRANCH"
git checkout -B "$BAD_APP_BRANCH" "origin/$BAD_APP_BRANCH"
# scratch clone is disposable -- hard-reset is safe here, it is never the
# user's working tree.
git reset --hard "origin/$BAD_APP_BRANCH"
git clean -fd >/dev/null

# Idempotency is decided by two independent signals:
#   1. CONTENT   -- is the regression actually live in app.py right now?
#                   (source of truth: what would actually run)
#   2. COMMIT_SHA -- which commit introduced it, by an EXACT subject match.
# Exact match matters: a substring/--grep match on COMMIT_SUBJECT also hits
# git-revert's own message (`Revert "Add per-request traffic quality score
# (#142)"` contains the original subject as a substring), which would make
# "revert" find and re-revert its own revert commit. Exact equality avoids
# that trap entirely.
MARKER="_score_request"
REGRESSED=0
if grep -q "$MARKER" app.py 2>/dev/null; then
  REGRESSED=1
fi

find_commit_by_subject() {
  # $1 = exact commit subject to match. Newest match wins (git log is
  # newest-first), empty output if none.
  git log --format='%H%x09%s' "origin/$BAD_APP_BRANCH" -- app.py \
    | awk -F'\t' -v subj="$1" '$2 == subj { print $1; exit }'
}

CULPRIT_SHA="$(find_commit_by_subject "$COMMIT_SUBJECT")"

push() {
  if [ "$DRY_RUN" = "1" ]; then
    warn "DRY_RUN=1 -- skipping: git push origin $BAD_APP_BRANCH"
  else
    info "pushing to origin/$BAD_APP_BRANCH"
    git push origin "$BAD_APP_BRANCH"
  fi
}

# --- apply --------------------------------------------------------------

if [ "$ACTION" = "apply" ]; then
  if [ "$REGRESSED" = 1 ]; then
    info "regression already live in app.py on origin/$BAD_APP_BRANCH -- nothing to do"
    if [ -n "$CULPRIT_SHA" ]; then
      echo "  commit: $CULPRIT_SHA"
    fi
    exit 0
  fi

  info "applying $PATCH_FILE"
  git apply --check "$PATCH_FILE" || die "patch does not apply cleanly to $(git rev-parse --short HEAD) -- app.py has likely drifted, regenerate the patch"
  git apply "$PATCH_FILE"

  git add app.py
  git commit -m "$COMMIT_SUBJECT" -m "Score incoming traffic by percentile rank within the current
request batch (SCORE_BATCH) so downstream throttling can weight
low-quality traffic lower. See #142." >/dev/null

  NEW_SHA="$(git rev-parse HEAD)"
  push

  info "applied."
  echo "  commit: $NEW_SHA"
  echo "  branch: $BAD_APP_BRANCH"
  exit 0
fi

# --- revert ---------------------------------------------------------------

if [ "$ACTION" = "revert" ]; then
  if [ "$REGRESSED" = 0 ]; then
    if [ -n "$CULPRIT_SHA" ]; then
      REVERT_SHA="$(find_commit_by_subject "Revert \"$COMMIT_SUBJECT\"")"
      info "already reverted -- nothing to do"
      if [ -n "$REVERT_SHA" ]; then
        echo "  revert commit: $REVERT_SHA"
      fi
      exit 0
    fi
    die "culprit commit (\"$COMMIT_SUBJECT\") not found on origin/$BAD_APP_BRANCH -- nothing to revert. Run 'apply' first."
  fi

  [ -n "$CULPRIT_SHA" ] || die "app.py on origin/$BAD_APP_BRANCH contains the regression ($MARKER) but no commit titled \"$COMMIT_SUBJECT\" was found -- can't determine what to revert."

  info "found culprit commit $CULPRIT_SHA"
  info "reverting $CULPRIT_SHA"
  git revert --no-edit "$CULPRIT_SHA" >/dev/null

  NEW_SHA="$(git rev-parse HEAD)"
  push

  info "reverted."
  echo "  culprit commit: $CULPRIT_SHA"
  echo "  revert commit:  $NEW_SHA"
  echo "  branch:         $BAD_APP_BRANCH"
  exit 0
fi
