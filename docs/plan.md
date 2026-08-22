# The last stretch — who does what

Written 2026-08-22. **Read §0 and your own lane. Nothing else.**

The pieces all exist. They are not connected to each other. This file says
exactly which wire each person runs, and what "done" looks like for it.

---

## 0. The one design decision everyone needs to know

**The workflow moves to `bad_app_demo`. The dispatch follows it.**

Until now the Lambda dispatched to `AminKln/BillionDollarApp` and the agent was
going to check out `Tehreem404/bad_app_demo` with a PAT. That needed a
classic PAT with `repo` + `workflow` scope, minted by a human, held in a Lambda
environment variable in plaintext.

Instead: **`anomaly-response.yml` lives in `Tehreem404/bad_app_demo`**, and the
Lambda dispatches there. Three things fall out of that, all good:

1. The workflow runs in the repo it needs to write to, so it opens the PR with
   the **built-in `secrets.GITHUB_TOKEN`** — no PAT for PR creation at all.
2. The agent's checkout is the repo it is diagnosing. No cross-repo checkout,
   no "which repo am I in" bug class.
3. The only remaining token is the one the **Lambda** uses to POST
   `/repos/Tehreem404/bad_app_demo/dispatches`. That can be a **fine-grained
   PAT scoped to that one repo with Contents: read/write** — dramatically
   smaller blast radius than the classic `repo` scope.

**On "the repo is public so we don't need a PAT":** public removes the need for
a token to *read*. Every *write* still needs one — `repository_dispatch`,
pushing a branch, opening a PR, opening an issue. There is no anonymous write
path. So: **one fine-grained PAT, one repo, Contents: write.** That is the
floor, and it is much less than we were about to mint.

---

## 1. Lanes

| Lane | Owner | Blocks | Est |
|---|---|---|---|
| **A — the culprit commit** | Tehreem | C (needs a real regression to diagnose) | 15 min |
| **B — the workflow** | JSnelgrove | — | 30 min |
| **C — the agent prompt + report shape** | DENIS | needs B's skeleton | 25 min |
| **D — detection + dispatch retarget** | Amin | B (dispatch must land in the right repo) | done + 10 min |
| **E — secrets** | Tehreem (repo owner) | B, C | 5 min |

A, B, D, E can all start **now, in parallel**. C needs 10 minutes of B first.

---

## Lane A — plant a real defect (Tehreem)

The regression must be a **git commit**, not the SSM chaos knob. Today the knob
flips app behaviour without touching the repo, so an agent running `git log`
finds nothing to blame. That is the single biggest hole in the story.

The patch is written and benchmarked: **`infra/bad-app/culprit.py.patch`** in
`BillionDollarApp`. It adds a per-request "traffic-quality score" whose helper
ranks every element of a 4000-item batch by scanning the whole batch — a
textbook accidental **O(n²)**.

Measured, not guessed: **1.07s per request.** Healthy is 0.05s. The static
threshold is 0.5s. So it is **21× baseline and 2× over the threshold**, and it
burns real CPU, so `cpu_usage_active` rises alongside latency — the same
evidence shape the demo narrates today.

It is also *obviously fixable*, which is what makes it a good demo: the fix is
one `sorted()` plus a `bisect`, O(n log n), and any competent reviewer — human
or agent — spots it in seconds.

```bash
cd bad_app_demo
git apply /path/to/BillionDollarApp/infra/bad-app/culprit.py.patch
git commit -am "Add per-request traffic quality score (#142)"
git push origin main        # <- deploy.yml auto-deploys to EC2
```

**Do not push this until we are rehearsing.** The moment it lands, the app is
slow for everyone.

**Done when:** pushing it makes `culprit-App-High` go red within ~2 min with no
SSM knob touched. Revert with `git revert` — that is also the demo's "the agent
was right" moment.

### The `deploy.sh` problem — Tehreem is the only one who can fix this

`deploy.yml` runs `cd ~/bad_app_demo && ./deploy.sh`, and **`deploy.sh` is not
in the repo.** It exists only on the EC2 box. The auto-deploy pipeline depends
on a file nobody can review, and if the box is ever replaced the pipeline dies
with it.

Correct home is `bad_app_demo`, next to the workflow that calls it. **Do not
write a fresh one and push it** — that is how you break a working pipeline two
hours before a demo. Capture the real one verbatim instead:

```bash
ssh ec2-user@54.205.9.164 'cat ~/bad_app_demo/deploy.sh' > deploy.sh
git add deploy.sh && git commit -m "Track deploy.sh (was untracked on the box)"
```

If nobody has SSH, skip it. The pipeline works today (commit `3da1453` proved
it) and this is hygiene, not a blocker.

---

## Lane B — `anomaly-response.yml` (JSnelgrove)

Lives in **`Tehreem404/bad_app_demo`**, `.github/workflows/anomaly-response.yml`.

```yaml
on:
  repository_dispatch:
    types: [cloudwatch-anomaly]
permissions:
  contents: write
  pull-requests: write
  issues: write
```

The payload contract is **`docs/contracts/dispatch-payload.json`** in
BillionDollarApp, generated from a real capture. Two traps documented there,
both of which will cost you 20 minutes if you skip them:

- **`evidence` keys are fully-qualified CloudWatch SEARCH labels**, e.g.
  `'HackathonDemo/System cpu-total i-091814f7a41456cb0 cpu_usage_active'`.
  `evidence['RequestLatency']` raises `KeyError`. Match on suffix or substring.
- **One incident can arrive twice.** `culprit-App-High` and
  `culprit-App-Anomaly` watch the same metric. The anomaly alarm is now 3-of-3
  so it should always lose the race, but *make the workflow idempotent anyway* —
  key on `client_payload.timestamp` or use `concurrency:` with a group derived
  from the metric name. Two PRs for one regression on stage is embarrassing.

**Done when:** a manual
`gh api repos/Tehreem404/bad_app_demo/dispatches -f event_type=cloudwatch-anomaly ...`
with the example payload produces a run that opens a PR. Test with a hardcoded
diff before the agent exists — prove the plumbing, then swap in the brain.

---

## Lane C — the agent (DENIS)

`anthropics/claude-code-action` inside Lane B's workflow. Two things already
built that you should not rebuild:

- **`feature/LLM_diagnosis`'s forced tool use.** `llm_agent.py` pins
  `tool_choice={"type":"tool","name":"report_root_cause"}` with a
  `{hypothesis, confidence, supporting_evidence[]}` schema. That is the PR body
  structure. It is strictly better than free-form prose and it is already
  written.
- **`feature/PRContext`'s `git_context.py`** for the commit diff — but it has a
  known bug: `good_sha` picks the newest commit *at or before* the alarm, which
  is the culprit itself, so the diff comes back empty. Take the commit *before*
  that one. Ten-second fix, currently fatal.

The prompt gets: the alarm, the evidence block, and `git log -p -3`. The
conclusion is already in the evidence — latency and CPU up together, errors and
memory flat — so the prompt's job is to connect that to the diff, not to
rediscover it.

**Fallback if the agent misbehaves:** open an **issue** instead of a PR. Same
workflow, `gh issue create`. Decide this by 15:30 and stop iterating.

---

## Lane D — detection (Amin) — *mostly done, listed so nobody redoes it*

Deployed and verified today. Two defects found and fixed in the last hour:

- **The anomaly band was false-positiving continuously.** At 2 stdev the band
  top sat at 0.052 while the healthy baseline was 0.051–0.054 — the band was
  *below* live traffic. `culprit-App-Anomaly` flapped OK→ALARM four times in
  16 minutes with nothing wrong and fired three spurious Lambda dispatches whose
  own evidence read `RequestLatency 0.053 -> 0.056 flat`. **With a token on the
  stack that would have been three junk PRs.** Widened to 8 stdev (band top
  0.061, ~13% headroom) and 3-of-3 datapoints. Alarm is now OK and stable.
- **Half the alarms were watching fabricated data.** `culprit-Latency-High`,
  `culprit-Latency-Anomaly` and `culprit-Load-High` were on the `Culprit`
  namespace — `seed_metrics.py` generating numbers with `random.lognormvariate`.
  Two of the three were wired into dispatch, so the agent could have been handed
  a regression that never happened. **All of it is deleted**: the publisher, the
  three alarms, the detector, the parameters, the outputs and three dashboard
  widgets. Every metric this stack watches now comes from the real app.
- **`deploy_detection.sh` was quietly undoing the band fix.** It hardcoded
  `LATENCY_SENSITIVITY:-2`, which overrode the template's own default on every
  deploy — so the first fix deploy re-narrowed the band to the broken value.
  Default is now 8. It also passed `MetricNamespace=` / `LatencyThresholdMs=`,
  parameters that no longer exist; `aws cloudformation deploy` drops unknown
  overrides silently, so this failed without failing.
- **Dispatch retargeted.** `GithubOwner`/`GithubRepo` used to be derived from
  *this* repo's git remote, pointing every dispatch at `BillionDollarApp`, where
  no workflow exists to receive it. Now defaults to `Tehreem404/bad_app_demo`
  — deployed and confirmed on the stack.

Remaining: `GITHUB_TOKEN=xxx make deploy` once Lane E mints the PAT. Nothing
else.

---

## Lane E — secrets (Tehreem, repo owner)

In **`Tehreem404/bad_app_demo` → Settings → Secrets → Actions**:

| Secret | Why |
|---|---|
| `ANTHROPIC_API_KEY` | the agent |

That is the whole list. `GITHUB_TOKEN` is injected automatically and is
sufficient for the PR because the workflow now lives in the repo it writes to.

Separately, hand Amin a **fine-grained PAT on `bad_app_demo` only, Contents:
read/write, 1-day expiry** for the Lambda's dispatch call. It goes in via
`GITHUB_TOKEN=xxx make deploy` and never into a file.

> **Rotate the Anthropic key after the demo.** It was pasted into a chat and a
> shell. Treat it as burned.

---

## 2. How the whole thing reads on stage

1. Tehreem pushes the O(n²) commit to `bad_app_demo`.
2. GitHub Actions SSHes to EC2 and redeploys. (~40s)
3. The app's own metrics move: latency 0.05s → 1.07s, CPU up. Real numbers from
   a real Flask app, published by its own `put_metric_data`.
4. `culprit-App-High` goes red. (~1–2 min)
5. EventBridge → Lambda → the Lambda queries CloudWatch for five metrics
   before/after and builds the evidence block → `repository_dispatch`.
6. A second Actions run starts. Claude reads the evidence and `git log -p`, and
   opens a PR reverting or fixing the quadratic loop.

**The commit that broke it and the PR that fixes it are in the same repo,
minutes apart, with nobody touching a keyboard in between.** That is the demo.

## 3. Cut lines, in the order we cut them

If we are behind at these times, cut without discussion:

- **15:30** — agent still not opening PRs → switch Lane C to opening an
  **issue**. Same trigger, same evidence, lower risk.
- **15:45** — dispatch still not landing → demo stops at `make payload`, which
  prints the exact evidence the agent *would* receive. The chain ran; it stopped
  one HTTP POST short, and the audience sees the diagnosis either way.
- **Any time** — the shared EC2 box dies → there is no fallback feed any more
  and there should not be one. Narrate `make payload` from the last good run
  plus the dashboard's retained history, and say plainly that the box is down.
  A fabricated feed on stage is worse than an honest one that stopped.
