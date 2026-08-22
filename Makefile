# Culprit — see docs/architecture.md
#
#   make setup     system-level dependencies (needs sudo, run once per machine)
#   make install   python dependencies into .venv
#   make check     verify this machine is ready — run this first, and again in the morning
#
# setup and install are idempotent: both skip whatever is already present,
# so re-running them is always safe.

SHELL   := /bin/bash
VENV    := .venv
PY      := $(VENV)/bin/python
PIP     := $(VENV)/bin/pip

# every requirements.txt in the repo; app/ appears once W1 adds it.
# lambda/requirements.txt is comment-only (boto3 ships in the runtime).
REQS    := $(wildcard requirements.txt app/requirements.txt)

.DEFAULT_GOAL := help
.PHONY: help setup install check smoke clean setup-gh setup-awscli calibrate deploy verify logs alarms dashboard destroy regress-commit revert-commit

# ─────────────────────────────────────────────────────────────────────────────

help:
	@echo "Culprit — anomaly to pull request, unattended."
	@echo
	@echo "  make setup      system deps: git, jq, curl, unzip, python venv, gh, aws  (sudo)"
	@echo "  make install    python deps from requirements.txt into $(VENV)"
	@echo "  make check      verify prerequisites — do this before anything else"
	@echo "  make smoke      de-risk the GitHub half (needs GITHUB_TOKEN)"
	@echo "  make clean      remove $(VENV) and __pycache__"
	@echo
	@echo "W2 — detection & dispatch:"
	@echo "  make deploy         alarms + anomaly detectors + EventBridge + dispatch Lambda"
	@echo "  make alarms         state of every alarm"
	@echo "  make dashboard      the CloudWatch dashboard URL"
	@echo "  make logs           tail the dispatch Lambda"
	@echo
	@echo "  demo against the REAL EC2 app (this is the one to show):"
	@echo "    make app-status     is it reachable, is chaos on?"
	@echo "    make app-regress    latency 0.05s -> 1.5-3.0s  -> alarm  -> dispatch"
	@echo "    make app-recover    turn it back off — SHARED BOX, do not skip"
	@echo
	@echo "  source-level regression, in bad_app_demo itself (a real commit, real PR):"
	@echo "    make regress-commit  plant the culprit commit on bad_app_demo main, push"
	@echo "    make revert-commit   git revert it and push — the fix the PR should look like"
	@echo
	@echo "  supporting targets:"
	@echo "    make calibrate      re-derive the alarm threshold from observed data"
	@echo "    make verify         prove the chain end to end (forces a real alarm)"
	@echo "    make payload        the last JSON the Lambda handed GitHub  (AWS CLI v1 ok)"
	@echo "    make destroy        tear the stack down"
	@echo
	@echo "First time on a new machine:  make setup && make install && make check"
	@echo "W2 from scratch:              make deploy && make verify"

# ── system dependencies ──────────────────────────────────────────────────────

setup: setup-gh setup-awscli
	@echo "==> apt packages"
	@sudo apt-get update -qq
	@sudo apt-get install -y -qq \
		git curl unzip jq \
		python3 python3-pip python3-venv
	@echo "==> system dependencies ready. next: make install"

# gh is not in the Ubuntu archive; it needs GitHub's own apt repo.
setup-gh:
	@if command -v gh >/dev/null 2>&1; then \
		echo "==> gh already installed ($$(gh --version | head -1))"; \
	else \
		echo "==> installing gh (GitHub CLI)"; \
		curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
			| sudo dd of=/usr/share/keyrings/githubcli-archive-keyring.gpg status=none; \
		sudo chmod go+r /usr/share/keyrings/githubcli-archive-keyring.gpg; \
		echo "deb [arch=$$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
			| sudo tee /etc/apt/sources.list.d/github-cli.list >/dev/null; \
		sudo apt-get update -qq && sudo apt-get install -y -qq gh; \
	fi

# apt ships AWS CLI v1; we need v2. That only comes from the bundled installer.
# AWS CLI v1 is in the Ubuntu 20.04 archive (1.18, from 2020) and v2 is not.
# Installing v2 does NOT remove v1: apt's copy stays at /usr/bin/aws, v2 lands
# in /usr/local/bin/aws, and which one you get depends on PATH order. Worse,
# the apt package survives and any later `apt install/upgrade` reasserts it.
# So: remove v1 from every place it can hide, THEN install v2, THEN prove the
# thing on PATH is actually v2 -- an unverified install is how you end up
# debugging a v1-only error message against v2 documentation.
setup-awscli:
	@if aws --version 2>&1 | grep -q 'aws-cli/2'; then \
		echo "==> aws cli v2 already on PATH ($$(aws --version 2>&1))"; \
	else \
		if command -v aws >/dev/null 2>&1; then \
			echo "==> removing aws cli v1 ($$(aws --version 2>&1 | cut -d' ' -f1)) at $$(command -v aws)"; \
		fi; \
		dpkg -s awscli >/dev/null 2>&1 && sudo apt-get remove -y -qq awscli || true; \
		python3 -m pip show awscli >/dev/null 2>&1 && sudo python3 -m pip uninstall -y -q awscli || true; \
		python3 -m pip show --user awscli >/dev/null 2>&1 && python3 -m pip uninstall -y -q awscli || true; \
		echo "==> installing aws cli v2"; \
		curl -fsSL "https://awscli.amazonaws.com/awscli-exe-linux-$$(uname -m).zip" -o /tmp/awscliv2.zip; \
		unzip -oq /tmp/awscliv2.zip -d /tmp; \
		sudo /tmp/aws/install --update; \
		rm -rf /tmp/awscliv2.zip /tmp/aws; \
		hash -r 2>/dev/null || true; \
		if aws --version 2>&1 | grep -q 'aws-cli/2'; then \
			echo "==> aws cli v2 installed ($$(aws --version 2>&1))"; \
		else \
			echo "!! aws on PATH is still $$(aws --version 2>&1)"; \
			echo "!! v2 is at /usr/local/bin/aws -- something earlier in PATH shadows it:"; \
			echo "$$PATH" | tr ':' '\\n' | sed 's/^/     /'; \
			exit 1; \
		fi; \
	fi

# ── application dependencies ─────────────────────────────────────────────────

install: $(VENV)
	@if [ -z "$(REQS)" ]; then \
		echo "==> no requirements.txt found yet — nothing to install"; \
	else \
		for r in $(REQS); do \
			echo "==> pip install -r $$r"; \
			$(PIP) install -q -r $$r || exit 1; \
		done; \
		echo "==> done. activate with: source $(VENV)/bin/activate"; \
	fi

$(VENV):
	@echo "==> creating $(VENV) with $$(python3 --version)"
	@python3 -m venv $(VENV)
	@$(PIP) install -q --upgrade pip

# ── verification ─────────────────────────────────────────────────────────────

# Mirrors the first 15 minutes of the build order in docs/architecture.md §6.
# Run it again in the morning: credentials expire and baselines drift overnight.
check:
	@fail=0; \
	echo "── commands ─────────────────────────────"; \
	for c in git curl jq unzip python3 aws gh; do \
		if command -v $$c >/dev/null 2>&1; then \
			printf '  \033[32mok\033[0m   %-8s %s\n' "$$c" "$$(command -v $$c)"; \
		else \
			printf '  \033[31mMISS\033[0m %-8s run: make setup\n' "$$c"; fail=1; \
		fi; \
	done; \
	echo; echo "── aws ──────────────────────────────────"; \
	if aws --version 2>&1 | grep -q 'aws-cli/2'; then \
		printf '  \033[32mok\033[0m   %s\n' "$$(aws --version 2>&1)"; \
	else \
		printf '  \033[33mwarn\033[0m aws cli v1 detected; v2 expected — run: make setup\n'; \
	fi; \
	if ident=$$(aws sts get-caller-identity --output text --query Account 2>/dev/null); then \
		printf '  \033[32mok\033[0m   account %s  region %s\n' "$$ident" "$$(aws configure get region || echo UNSET)"; \
		printf '       every teammate must print this same account id\n'; \
	else \
		printf '  \033[31mMISS\033[0m no aws credentials — ask W2\n'; fail=1; \
	fi; \
	echo; echo "── github ───────────────────────────────"; \
	if gh auth status >/dev/null 2>&1; then \
		printf '  \033[32mok\033[0m   gh authenticated as %s\n' "$$(gh api user -q .login 2>/dev/null)"; \
	else \
		printf '  \033[31mMISS\033[0m gh not authenticated — run: gh auth login\n'; fail=1; \
	fi; \
	if [ -n "$$GITHUB_TOKEN" ]; then \
		printf '  \033[32mok\033[0m   GITHUB_TOKEN is set (make smoke will verify its scopes)\n'; \
	else \
		printf '  \033[33mwarn\033[0m GITHUB_TOKEN unset — needed by make smoke\n'; \
	fi; \
	echo; echo "── python ───────────────────────────────"; \
	printf '  \033[32mok\033[0m   %s\n' "$$(python3 --version)"; \
	if [ -d "$(VENV)" ]; then \
		printf '  \033[32mok\033[0m   $(VENV) present\n'; \
	else \
		printf '  \033[33mwarn\033[0m no $(VENV) — run: make install\n'; \
	fi; \
	echo; \
	if [ $$fail -eq 0 ]; then \
		echo -e "\033[32mready.\033[0m"; \
	else \
		echo -e "\033[31mnot ready — fix the MISS lines above.\033[0m"; exit 1; \
	fi

# Proves PAT scopes, branch write, the issue path, dispatch 204, and that the
# workflow is registered on the default branch. Costs nothing, catches the
# failures that are expensive to find at 8am.
smoke:
	@test -n "$$GITHUB_TOKEN" || { echo "set GITHUB_TOKEN first (classic PAT, scopes repo+workflow)"; exit 1; }
	@./scripts/test_dispatch.sh

# ── housekeeping ─────────────────────────────────────────────────────────────

clean:
	@rm -rf $(VENV)
	@find . -name __pycache__ -type d -prune -exec rm -rf {} + 2>/dev/null || true
	@echo "==> cleaned"

# ---------------------------------------------------------------------------
# W2 - detection & dispatch
#
# Every target below acts on the REAL app (Tehreem404/bad_app_demo on
# i-091814f7a41456cb0). The synthetic metric publisher and its three alarms
# were deleted on 2026-08-22 -- see docs/decisions.md 9.
# ---------------------------------------------------------------------------
STACK   := culprit-detection
REGION  ?= us-east-1

# The EC2 box's public IP changes across a stop/start, so look it up by tag
# rather than pinning it. Lazy (=), so the API call only happens if a target
# below actually needs it.
APP_HOST = http://$(shell aws ec2 describe-instances --region $(REGION) \
  --filters Name=tag:Name,Values=hackathon-demo Name=instance-state-name,Values=running \
  --query 'Reservations[0].Instances[0].PublicIpAddress' --output text 2>/dev/null):8000

# The app has NO /chaos/<kind>/<state> route -- reading its source
# (Tehreem404/bad_app_demo, app.py) shows only /, /healthz and /chaos/status,
# and curling /chaos/latency/on against the live box returns 404. Chaos state
# lives in SSM Parameter Store; the app re-reads all three parameters on every
# single request, so a put-parameter takes effect on the next request with no
# redeploy, no restart and no SSH. That is why these targets shell out to
# scripts/chaos.sh (aws ssm put-parameter) instead of curling the app.

app-status:        ## is the real EC2 app reachable, and is chaos on?
	@echo "host: $(APP_HOST)"
	@./scripts/chaos.sh status

app-bootstrap:     ## RUN THIS ONCE: create the chaos parameters the app reads
	@echo "The app reads three SSM parameters on every request and falls back to"
	@echo "'off' for any that is missing. If they do not exist, chaos can never be"
	@echo "turned on -- the app is permanently healthy and the demo has no trigger."
	@aws ssm put-parameter --name /hackathon-demo/chaos/latency --value off --type String --overwrite >/dev/null
	@aws ssm put-parameter --name /hackathon-demo/chaos/cpu     --value off --type String --overwrite >/dev/null
	@aws ssm put-parameter --name /hackathon-demo/chaos/errors  --value off --type String --overwrite >/dev/null
	@echo "created all three at 'off' -- no behaviour change, the app already"
	@echo "behaved as if they were off. now 'make app-regress' can actually bite."

app-regress:       ## flip the REAL EC2 app expensive (fires culprit-App-High)
	@./scripts/chaos.sh cpu on
	@echo
	@echo "REGRESSION on: the cpu knob, not the latency knob."
	@echo "  cpu=on runs a real 2s sha256 loop per request. latency goes"
	@echo "  ~0.05s -> ~2.05s (40x) AND cpu_usage_active climbs on the host."
	@echo "  the latency knob is a bare time.sleep() -- it moves latency but"
	@echo "  leaves the CPU flat, so the evidence would say only 'it got slower'."
	@echo "culprit-App-High is 1-of-1 at Period 60, threshold 0.5s => ~60-90s to ALARM."
	@echo "REMEMBER: 'make app-recover' when done - this is a shared box."

app-recover:       ## turn the real app's chaos back off (do not skip this)
	@./scripts/chaos.sh off
	@echo "app back to baseline."

# Source-level regression: a real commit on Tehreem404/bad_app_demo's main,
# not a runtime knob. Idempotent — see scripts/regress_commit.sh's header.
regress-commit:     ## plant the culprit commit on bad_app_demo main (git push)
	@./scripts/regress_commit.sh apply

revert-commit:      ## git revert the culprit commit on bad_app_demo main (git push)
	@./scripts/regress_commit.sh revert

calibrate:         ## derive the alarm threshold from real observed data
	@./scripts/calibrate.sh

deploy:            ## deploy the detection stack (alarms + EventBridge + dispatch Lambda)
	@./scripts/deploy_detection.sh

verify:            ## prove the whole chain end to end (forces a real-app alarm)
	@./scripts/verify_chain.sh

payload:           ## show the last payload the Lambda built (DEMO FALLBACK - works on CLI v1)
	@./scripts/last_payload.sh $(or $(N),1)

logs:              ## tail the dispatch Lambda's logs (needs AWS CLI v2)
	@aws logs tail --help >/dev/null 2>&1 || { \
	  echo "'aws logs tail' needs AWS CLI v2; you are on:"; aws --version; \
	  echo "run 'make setup-awscli', or use 'make payload' which works on v1."; \
	  exit 1; }
	@aws logs tail /aws/lambda/culprit-dispatch --follow --region $(REGION)

alarms:            ## current state of every culprit- alarm
	@aws cloudwatch describe-alarms --alarm-name-prefix culprit- --region $(REGION) \
	  --query 'MetricAlarms[].[AlarmName,StateValue,StateUpdatedTimestamp]' --output table

dashboard:         ## print the CloudWatch dashboard URL
	@echo "https://console.aws.amazon.com/cloudwatch/home?region=$(REGION)#dashboards:name=Culprit"

destroy:           ## delete the detection stack (does NOT touch template.yaml's stack)
	@aws cloudformation delete-stack --stack-name $(STACK) --region $(REGION)
	@echo "delete requested for $(STACK)."
