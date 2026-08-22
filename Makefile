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
REQS    := $(wildcard requirements.txt app/requirements.txt infra/demo-app/requirements.txt)

.DEFAULT_GOAL := help
.PHONY: help setup install check smoke clean setup-gh setup-awscli

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
	@echo "First time on a new machine:  make setup && make install && make check"

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
setup-awscli:
	@if aws --version 2>&1 | grep -q 'aws-cli/2'; then \
		echo "==> aws cli v2 already installed ($$(aws --version 2>&1))"; \
	else \
		echo "==> installing aws cli v2"; \
		curl -fsSL "https://awscli.amazonaws.com/awscli-exe-linux-$$(uname -m).zip" -o /tmp/awscliv2.zip; \
		unzip -oq /tmp/awscliv2.zip -d /tmp; \
		sudo /tmp/aws/install --update; \
		rm -rf /tmp/awscliv2.zip /tmp/aws; \
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
