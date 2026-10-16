# ═══════════════════════════════════════════════════════════════
# openSME — Makefile
# ═══════════════════════════════════════════════════════════════
# Test pyramid + tier-based deployment for openSME Compose.
#
# Architecture: core services + optional profiles
# Tiers: soho (4c/8G) | small (8c/24G) | medium (16c/48G)
#
# Profile mapping:
#   soho:   core only (Portal, Traefik, PG, Redis, Memcached, Zitadel)
#   small:  core + office + paperless (+ optional tika)
#   medium: core + office + paperless + stalwart + sogo + collabora
#
# Quick start:
#   make bootstrap        # install deps
#   make test             # layers 0-3
#   make test-all         # layers 0-6
#   make lint             # static checks only
#   make up               # starts soho by default
#   make up PROFILE=small
#   make up PROFILE=medium
#   make down

.PHONY: test test-all test-static lint compose-check env-check secret-scan \
        specs contracts yaml-lint \
        container smoke integration e2e saga security \
        bootstrap bpm-deploy clean help \
        up down status logs pull upgrade \
        mailcow-up mailcow-down mailcow-purge mailcow-status mailcow-logs \
        up-soho up-small up-medium up-all \
        nix-build nix-load nix-images \
        agent-build agent-status
.PHONY: agentic-gate backup backup-db backup-dry-run bench compliance \
        down-volumes fmt lint-code logs-size perf-check \
        provision-demo-users provision-demo-users-clean provision-demo-users-dry \
        prune restore restore-from survey-deploy test-run

.DEFAULT_GOAL := help

# ---------------------------------------------------------------------------
# Colors
# ---------------------------------------------------------------------------
GREEN  := \033[0;32m
RED    := \033[0;31m
YELLOW := \033[1;33m
BLUE   := \033[0;34m
CYAN   := \033[0;36m
NC     := \033[0m

# ---------------------------------------------------------------------------
# Environment
# ---------------------------------------------------------------------------
PROFILE    ?= soho
# DOMAIN: .env wins over the ambient environment (an exported $DOMAIN from
# an unrelated shell would otherwise silently point tests at a foreign
# host); a make-command-line override (make e2e DOMAIN=x) still wins.
DOMAIN     := $(shell grep -m1 '^OPENSME_DOMAIN=' .env 2>/dev/null | cut -d= -f2 || echo 'opensme.local')
COMPOSE    ?= docker compose
# Use the generated .env when present (manual installs via init-env.sh /
# demo.sh); fall back to the template so CI/static runs stay hermetic (they
# have no .env). An explicit TEST_ENV=… always overrides.
TEST_ENV   := $(if $(wildcard .env),.env,.env.example)
PYTHON     ?= python3
# Go toolchain for lint-code. Override when an old system Go shadows a newer
# one: `GO=/usr/local/go/bin/go make lint-code`.
GO         ?= go
TEST_RUNNER := $(PYTHON) tests/run.py

# ---------------------------------------------------------------------------
# Compose file selection by tier
# ---------------------------------------------------------------------------
# Core: Traefik, PostgreSQL, PgBouncer, Redis, Memcached, Portal
# IDM: Zitadel (default) or Casdoor (lightweight)
# Overlays: opencloud, stalwart, sogo, paperless, monitoring

CORE_FILES := -f docker-compose.yml
IDM_FILES  := -f idm/zitadel.yml

ifeq ($(PROFILE),soho)
COMPOSE_FILES := $(CORE_FILES) $(IDM_FILES) -f profiles/soho.yml
COMPOSE_PROFILES :=
TIER_SERVICES := core (6 containers: Traefik, PG, PgBouncer, Redis, Memcached, Portal, Zitadel)
else ifeq ($(PROFILE),small)
COMPOSE_FILES := $(CORE_FILES) $(IDM_FILES) -f opencloud/opencloud.yml -f services/paperless.yml -f profiles/small.yml
COMPOSE_PROFILES := --profile paperless
TIER_SERVICES := core + office + paperless (10 containers)
else ifeq ($(PROFILE),medium)
COMPOSE_FILES := $(CORE_FILES) $(IDM_FILES) -f opencloud/opencloud.yml -f mail/stalwart.yml -f mail/sogo.yml -f services/paperless.yml -f profiles/medium.yml
COMPOSE_PROFILES := --profile paperless
TIER_SERVICES := core + office + mail + paperless (14 containers)
else ifeq ($(PROFILE),custom)
COMPOSE_FILES := $(COMPOSE_FILE)
COMPOSE_PROFILES :=
TIER_SERVICES := custom (COMPOSE_FILE from env)
else
$(error Unknown PROFILE "$(PROFILE)". Use: soho, small, medium, or custom)
endif

FULL_COMPOSE := $(COMPOSE) $(COMPOSE_FILES) $(COMPOSE_PROFILES)

# ---------------------------------------------------------------------------
# Docker Compose Operations
# ---------------------------------------------------------------------------
up:
	@if [ "$${MAIL_BACKEND:-$$(grep -E '^MAIL_BACKEND=' .env 2>/dev/null | head -1 | cut -d= -f2 || true)}" = "mailcow" ] \
	   && case "$${COMPOSE_FILE:-}" in *"mail/stalwart.yml"*) true;; *) false;; esac; then \
	  echo -e "$(RED)✗ MAIL_BACKEND=mailcow but mail/stalwart.yml is in COMPOSE_FILE — mail backends are strictly exclusive (XOR); remove the stalwart overlay or switch back to MAIL_BACKEND=stalwart$(NC)"; \
	  exit 1; \
	fi
	@echo -e "$(BLUE)── starting openSME (profile: $(PROFILE), $(TIER_SERVICES)) ──$(NC)"
	@$(FULL_COMPOSE) --env-file $(TEST_ENV) up -d
	@echo -e "$(GREEN)✓ Stack started ($(TIER_SERVICES))$(NC)"

up-soho:
	@$(MAKE) up PROFILE=soho

up-small:
	@$(MAKE) up PROFILE=small

up-medium:
	@$(MAKE) up PROFILE=medium

up-all:
	@echo -e "$(BLUE)── starting openSME (ALL services) ──$(NC)"
	@$(FULL_COMPOSE) --env-file $(TEST_ENV) \
		--profile paperless --profile tika --profile invoice --profile chat --profile element --profile collab --profile notes --profile ticketing --profile cms --profile store --profile ai --profile camunda \
		up -d
	@echo -e "$(GREEN)✓ Stack started with all optional services$(NC)"

down:
	@$(FULL_COMPOSE) down --remove-orphans
	@echo -e "$(GREEN)✓ Stack stopped$(NC)"

down-volumes:
	@$(FULL_COMPOSE) down -v --remove-orphans
	@echo -e "$(RED)⚠ Volumes removed — data is gone!$(NC)"

status:
	@$(FULL_COMPOSE) ps

logs:
	@$(FULL_COMPOSE) logs -f --tail=100

pull:
	@$(FULL_COMPOSE) pull
	@echo -e "$(GREEN)✓ Images pulled$(NC)"

upgrade:
	@echo -e "$(BLUE)── safe upgrade: pre-flight + pull + rolling up ──$(NC)"
	@./scripts/upgrade.sh --all

# ---------------------------------------------------------------------------
# Mail backend — switch MAIL_BACKEND=stalwart|mailcow (strict XOR)
# ---------------------------------------------------------------------------
mailcow-up:      ## Boot the mailcow mail stack (requires MAIL_BACKEND=mailcow in .env)
	@bash scripts/mailcow.sh up

mailcow-down:    ## Stop + unwire mailcow (keeps volumes)
	@bash scripts/mailcow.sh down

mailcow-purge:   ## Stop + unwire mailcow AND destroy its data volumes
	@bash scripts/mailcow.sh down --purge

mailcow-status:  ## mailcow health overview
	@bash scripts/mailcow.sh status

mailcow-logs:    ## Follow mailcow logs
	@bash scripts/mailcow.sh logs

# ---------------------------------------------------------------------------
# Disk hygiene
# ---------------------------------------------------------------------------
prune:
	@echo -e "$(BLUE)── pruning stale images/volumes/networks (age filter, keep 72h) ──$(NC)"
	docker system prune --filter "until=72h" -f
	@echo -e "$(GREEN)✓ Pruned images/build cache/networks older than 72h (volumes kept)$(NC)"

logs-size:
	@echo -e "$(BLUE)── per-container JSON log footprint ──$(NC)"
	@docker ps -aq | while read c; do \
		name=$$(docker inspect --format '{{.Name}}' "$$c" | tr -d /); \
		size=$$(du -sh "/var/lib/docker/containers/$$c/$$c-json.log" 2>/dev/null | cut -f1); \
		printf "%-32s %s\n" "$$name" "$${size:-0B}"; \
	done
	@echo -e "$(GREEN)✓ See docs/PERFORMANCE.md for log retention settings$(NC)"

# ---------------------------------------------------------------------------
# Benchmark (tests/07-bench) — optional live-host measurements
# ---------------------------------------------------------------------------
bench:
	@echo -e "$(BLUE)── running benchmarks (docker stats sampling + HTTP latency) ──$(NC)"
	@$(PYTHON) tests/07-bench/run_bench.py --out docs/perf/benchmark-run.md 2>&1
	@echo -e "$(GREEN)✓ Benchmarks written to docs/perf/benchmark-run.md$(NC)"

# ---------------------------------------------------------------------------
# Layer 0 — Static Validation (no containers needed)
# ---------------------------------------------------------------------------
lint: compose-check yaml-lint env-check secret-scan perf-check
	@echo -e "$(GREEN)✅ All linting passed$(NC)"

# ────────────────────────────────────────────────────────────────
# Code quality (mirrors the CI "Code quality" job)
# ────────────────────────────────────────────────────────────────
fmt: ## Apply all formatters (rustfmt, gofmt)
	cd portal && cargo fmt
	gofmt -w internal/ .
	@echo -e "$(GREEN)✅ formats applied$(NC)"

lint-code: ## Format checks + linters + unit tests (no fixes) — what CI enforces
	@$(GO) version | awk '{ v=substr($$3,3)+0; if (v < 1.22) { \
		printf "❌ Go >= 1.22 required, found %s\n", $$3; \
		print "   hint: GO=/usr/local/go/bin/go make lint-code"; exit 1 } }'
	cd portal && cargo fmt --check
	cd portal && cargo clippy --all-targets -- -D warnings
	cd portal && cargo test
	@if [ -n "$$(gofmt -l . | grep -v node_modules)" ]; then \
		echo "unformatted Go files: $$(gofmt -l . | grep -v node_modules)"; exit 1; fi
	$(GO) vet ./...
	$(GO) test ./...
	shellcheck scripts/*.sh
	@echo -e "$(GREEN)✅ code quality gates passed$(NC)"

compose-check:
	@echo -e "$(BLUE)── docker compose config (profile: $(PROFILE)) ──$(NC)"
	@$(FULL_COMPOSE) --env-file $(TEST_ENV) config --quiet 2>&1
	@echo -e "$(GREEN)✓ compose config valid ($(PROFILE) profile)$(NC)"

yaml-lint:
	@echo -e "$(BLUE)── YAML syntax validation ──$(NC)"
	@$(PYTHON) tests/00-static/yaml_lint.py 2>&1

env-check:
	@echo -e "$(BLUE)── env completeness ──$(NC)"
	@$(PYTHON) tests/00-static/check_env.py 2>&1

secret-scan:
	@echo -e "$(BLUE)── secret scanning ──$(NC)"
	@$(PYTHON) tests/00-static/scan_secrets.py 2>&1

perf-check:
	@echo -e "$(BLUE)── performance & budget invariants ──$(NC)"
	@$(PYTHON) tests/00-static/check_perf.py 2>&1

# ---------------------------------------------------------------------------
# Layer 1 — Spec compliance (no containers needed)
# ---------------------------------------------------------------------------
specs:
	@echo -e "$(BLUE)── spec compliance ──$(NC)"
	@$(PYTHON) tests/01-specs/validate_specs.py 2>&1

# ---------------------------------------------------------------------------
# Layer 2 — Contract validation (no containers needed)
# ---------------------------------------------------------------------------
contracts:
	@echo -e "$(BLUE)── contract validation ──$(NC)"
	@$(PYTHON) tests/02-contracts/validate_contracts.py 2>&1

# AS6 agent self-maintenance gate — OPT-IN, NOT part of test-static/lint.
# Default is informational (never fails); enforce with AGENTIC_STRICT=1.
agentic-gate:
	@echo -e "$(BLUE)── AS6 agent self-maintenance gate (opt-in) ──$(NC)"
	@$(PYTHON) tests/00-static/check_agentic.py 2>&1

test-static:
	@echo "$(BLUE)── static layers 0-2 (single entry point) ──$(NC)"
	@$(PYTHON) tests/run.py --static 2>&1
	@echo "$(GREEN)✅ Static layers (0-2) complete$(NC)"

# ---------------------------------------------------------------------------
# Layer 3+ — Require running stack
# ---------------------------------------------------------------------------
container:
	@echo -e "$(BLUE)── layer 3: container health ──$(NC)"
	@$(FULL_COMPOSE) ps --format 'table {{.Name}}\t{{.Status}}' 2>&1

smoke:
	@echo -e "$(BLUE)── layer 3: smoke tests ──$(NC)"
	@$(PYTHON) tests/03-smoke/run.py $(DOMAIN) 2>&1

integration:
	@echo -e "$(BLUE)── layer 4: cross-service integration ──$(NC)"
	@$(PYTHON) tests/04-integration/run.py 2>&1

e2e:
	@echo -e "$(BLUE)── layer 5: e2e — SSO flows (requires running stack) ──$(NC)"
	@$(PYTHON) tests/05-e2e/run.py $(DOMAIN) 2>&1
	@$(MAKE) saga

saga:
	@echo -e "$(BLUE)── layer 5: saga journey blueprints (requires running stack) ──$(NC)"
	@for script in saga_onboarding saga_quote_to_cash saga_leave_request \
	            saga_support_ticket saga_publishing saga_team_knowledge \
	            saga_smoke; do \
		$(PYTHON) tests/05-e2e/$$script.py $(DOMAIN) 2>&1 || exit 1; \
	done

security:
	@echo -e "$(BLUE)── layer 6: security audit ──$(NC)"
	@$(PYTHON) tests/06-security/audit.py 2>&1

# ---------------------------------------------------------------------------
# Combined test targets
# ---------------------------------------------------------------------------
test: test-static container smoke
	@echo -e "$(GREEN)═══════════════════════════════════$(NC)"
	@echo -e "$(GREEN) Layers 0-3 complete.$(NC)"
	@echo -e "$(GREEN)═══════════════════════════════════$(NC)"

test-all: test integration e2e security
	@echo -e "$(GREEN)═══════════════════════════════════$(NC)"
	@echo -e "$(GREEN) All test layers complete.$(NC)"
	@echo -e "$(GREEN)═══════════════════════════════════$(NC)"

# Full test runner (all layers via tests/run.py)
test-run:
	@$(TEST_RUNNER) --static --domain $(DOMAIN)

# ---------------------------------------------------------------------------
# Nix image builds (monitoring/nix/)
# ---------------------------------------------------------------------------
nix-build: nix-images

nix-images:
	@echo -e "$(BLUE)── building Nix monitoring images ──$(NC)"
	cd monitoring/nix && nix build .#dev-agent .#predictive-agent .#taskfleet
	@echo -e "$(GREEN)✅ Images built in monitoring/nix/result*$(NC)"

nix-load: nix-images
	@echo -e "$(BLUE)── loading Nix images into Docker ──$(NC)"
	docker load < monitoring/nix/result-dev-agent
	docker load < monitoring/nix/result-predictive-agent
	docker load < monitoring/nix/result-taskfleet
	docker tag dev-agent:latest-nix ghcr.io/tobias-weiss-ai-xr/dev-agent:latest
	docker tag predictive-agent:latest-nix ghcr.io/tobias-weiss-ai-xr/predictive-agent:latest
	docker tag taskfleet:latest-nix ghcr.io/tobias-weiss-ai-xr/taskfleet:latest
	@echo -e "$(GREEN)✅ Images loaded and tagged$(NC)"

# ---------------------------------------------------------------------------
# Dev maintenance bot (opensme-dev-agent)
# ---------------------------------------------------------------------------
agent-build:
	@echo -e "$(BLUE)── building dev-maintenance-bot image ──$(NC)"
	docker build -f opensme-dev-agent/Dockerfile -t ${DEV_MAINTENANCE_BOT_IMAGE:-opensme-dev-maintenance-bot:latest} .
	@echo -e "$(GREEN)✅ dev-maintenance-bot image built$(NC)"

agent-status:
	@docker run --rm -v opensme_dev-maintenance-bot-state:/var/lib/opensme:ro \
		--entrypoint /usr/local/bin/agent \
		${DEV_MAINTENANCE_BOT_IMAGE:-opensme-dev-maintenance-bot:latest} -status

# ---------------------------------------------------------------------------
# Backup / Restore
# ---------------------------------------------------------------------------
backup:
	@bash scripts/backup.sh --volumes

backup-db:
	@bash scripts/backup.sh

backup-dry-run:
	@bash scripts/backup.sh --volumes --dry-run

restore:
	@bash scripts/restore.sh --list
	@echo ""
	@echo "Run: make restore-from BACKUP=<timestamp> [PASSPHRASE=<p>]"
	@echo "Or:  ./scripts/restore.sh <timestamp>"

restore-from:
	@bash scripts/restore.sh $(BACKUP) $$([ -n "$$PASSPHRASE" ] && echo "--passphrase $(PASSPHRASE)")

# ---------------------------------------------------------------------------
# Demo User Provisioning
# ---------------------------------------------------------------------------
provision-demo-users:
	@bash scripts/provision-demo-users.sh --wait

provision-demo-users-clean:
	@bash scripts/provision-demo-users.sh --wait --clean

provision-demo-users-dry:
	@bash scripts/provision-demo-users.sh --dry-run

compliance:
	@python3 scripts/compliance.py

# ---------------------------------------------------------------------------
# Bootstrap / Cleanup
# ---------------------------------------------------------------------------
bootstrap:
	@echo -e "$(BLUE)── bootstrapping ──$(NC)"
	@./scripts/init-env.sh
	@pip install -r tests/requirements.txt 2>/dev/null || \
		echo -e "$(YELLOW)⚠ pip install skipped (install pyyaml manually: pip install pyyaml)$(NC)"
	@echo -e "$(GREEN)✅ Bootstrap complete — configure .env if needed, then: make up PROFILE=soho"

bpm-deploy:
	@echo -e "$(BLUE)── deploying BPMN seed processes ──$(NC)"
	@./bootstrap/bpmn-deploy.sh

survey-deploy:
	@echo -e "$(BLUE)── provisioning example LimeSurvey survey ──$(NC)"
	@./bootstrap/survey-deploy.sh

clean:
	@rm -rf tests/05-e2e/test-results/ tests/05-e2e/playwright-report/
	@rm -f /tmp/opensme-test-*.json
	@echo -e "$(GREEN)✓ Test artifacts cleaned$(NC)"

# ---------------------------------------------------------------------------
# Help
# ---------------------------------------------------------------------------
help:
	@echo ""
	@echo -e "$(BLUE)openSME — Makefile (PROFILE=$(PROFILE))$(NC)"
	@echo ""
	@echo -e "  $(GREEN)Architecture: core + optional overlays$(NC)"
	@echo "    Core: Traefik, PostgreSQL, PgBouncer, Redis, Memcached, Portal"
	@echo "    IDM:  Zitadel (default) or Casdoor (idm/casdoor.yml)"
	@echo "    Overlays: opencloud, stalwart, sogo, paperless, monitoring"
	@echo "    Mail backend (XOR): MAIL_BACKEND=stalwart (default) | mailcow → make mailcow-up/-down"
	@echo "    Service profiles:"
	@echo "      --profile invoice   Invoice Ninja (invoicing)"
	@echo "      --profile paperless Paperless-ngx + Gotenberg (document management)"
	@echo "      --profile tika      Paperless-Tika (enhanced OCR)"
	@echo "      --profile chat      Synapse (Matrix chat)"
	@echo "      --profile element   Element-Web (Matrix client)"
	@echo "      --profile collab    CryptPad (collaborative docs)"
	@echo "      --profile ticketing Nosdesk helpdesk (tickets + KB)"
	@echo "      --profile cms       crap-cms (lightweight website CMS)"
	@echo "      --profile store     RaisFast (e-commerce storefront)"
	@echo "      --profile ai        llama.cpp (local OpenAI-compatible backend)"
	@echo "      --profile notes     Notes/Impress (collaborative editing)"
	@echo ""
	@echo -e "  $(GREEN)VPS Tiers$(NC)"
	@echo "    make up PROFILE=soho     4c /  8 GB  (core only)"
	@echo "    make up PROFILE=small    8c / 24 GB  (core + office + paperless)"
	@echo "    make up PROFILE=medium  16c / 48 GB  (core + office + mail + paperless)"
	@echo "    make up PROFILE=custom   Use COMPOSE_FILE from env"
	@echo ""
	@echo -e "  $(GREEN)Operations$(NC)"
	@echo "    make up                  Start stack (PROFILE=soho|small|medium)"
	@echo "    make up-all              Start with ALL optional services"
	@echo "    make down                Stop stack"
	@echo "    make down-volumes        Stop + remove volumes (data loss!)"
	@echo "    make status              Show container status"
	@echo "    make logs                Tail logs"
	@echo "    make pull                Pull images"
	@echo ""
	@echo -e "  $(GREEN)Demo Third-Party Setup$(NC)"
	@echo "    make provision-demo-users    Provision 12 demo users in Zitadel (12 person SME)"
	@echo "    make provision-demo-users-clean  Delete + re-provision demo users"
	@echo "    make provision-demo-users-dry    Show demo user config (JSON) without provisioning"
	@echo "    make compliance              Generate compliance reports (JSON + MD)"
	@echo ""
	@echo -e "  $(GREEN)Demo Quickstart$(NC)"
	@echo "    ./scripts/demo.sh              Full demo stack (core + Zitadel + OpenCloud)"
	@echo "    ./scripts/demo.sh --with-demo-users      + 12 demo users auto-provisioned"
	@echo "    ./scripts/demo.sh --with-bpmn            + sample workflows (if engine active)"
	@echo "    ./scripts/demo.sh --with-seed-content     + seed content hint"
	@echo "    ./scripts/demo.sh --all-demo              All of the above"
	@echo ""
	@echo -e "  $(GREEN)Testing (Spec / Contract / Test scaffold)$(NC)"
	@echo "    make lint                Layer 0: compose-check + yaml-lint + env-check + secret-scan"
	@echo "    make specs               Layer 1: spec compliance (compose files match specs/)"
	@echo "    make contracts           Layer 2: contract validation (env, ports, health, networks, security)"
	@echo "    make test-static         Layers 0-2 (all static checks, no running stack)"
	@echo "    make container           Layer 3: container health (requires stack)"
	@echo "    make smoke               Layer 3: HTTP smoke tests (requires stack)"
	@echo "    make integration         Layer 4: integration tests (not yet implemented)"
	@echo "    make e2e                 Layer 5: e2e SSO/OIDC flows (running stack; e.g. make e2e DOMAIN=opensme.local)"
	@echo "    make saga                Layer 5: saga journey blueprints (running stack; skips cleanly on partial deployments)"
	@echo "    make security            Layer 6: security audit (exposed ports, secrets, TLS)"
	@echo "    make test                Layers 0-3 (static + container + smoke)"
	@echo "    make test-all            Layers 0-6 (full suite)"
	@echo "    make test-run            Full test runner (python3 tests/run.py --static)"
	@echo "    make fmt                 Apply all formatters (rustfmt, gofmt)"
	@echo "    make lint-code           CI code-quality gates (fmt/clippy/vet/test/shellcheck)"
	@echo ""
	@echo -e "  $(GREEN)Backup / Restore$(NC)"
	@echo "    make backup              Full backup (PG + Traefik + volumes)"
	@echo "    make backup-db           PostgreSQL + Traefik only"
	@echo "    make backup-dry-run      Preview backup"
	@echo "    make restore             List available backups"
	@echo "    make restore-from BACKUP=<ts> [PASSPHRASE=<p>]  Restore from backup (PASSPHRASE for .enc backups)"
	@echo ""
	@echo -e "  $(GREEN)Monitoring images (Nix)$(NC)"
	@echo "    make nix-build            Build dev-agent, predictive-agent, taskfleet images"
	@echo "    make nix-load             Build + load into Docker + tag for registry"
	@echo ""
	@echo -e "  $(GREEN)Dev maintenance bot$(NC)"
	@echo "    make agent-build          Build the dev-maintenance-bot image"
	@echo "    make agent-status         Print the bot's persisted status"
	@echo ""
	@echo -e "  $(GREEN)Other$(NC)"
	@echo "    make bootstrap           Generate a working .env (random secrets) + test deps"
	@echo "    make bpm-deploy          Deploy seed BPMN processes to the Operaton engine"
	@echo "    make clean               Remove test artifacts"
	@echo ""
