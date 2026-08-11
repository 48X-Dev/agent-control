.PHONY: help sync openapi-spec openapi-spec-check test test-extras test-all contrib-verify scripts-test models-test dispatcher-test knowledge-sync-test fleet-test test-models test-sdk lint lint-fix typecheck check build build-models build-server build-sdk publish publish-models publish-server publish-sdk hooks-install hooks-uninstall prepush evaluators-test evaluators-lint evaluators-lint-fix evaluators-typecheck evaluators-build contrib-test contrib-lint contrib-lint-fix contrib-typecheck contrib-build sdk-ts-generate sdk-ts-overlay-test sdk-ts-name-check sdk-ts-generate-check sdk-ts-build sdk-ts-test sdk-ts-lint sdk-ts-typecheck sdk-ts-release-check sdk-ts-publish-dry-run sdk-ts-publish telemetry-test telemetry-lint telemetry-lint-fix telemetry-typecheck telemetry-build telemetry-publish

# Workspace package names
PACK_MODELS := agent-control-models
PACK_SERVER := agent-control-server
PACK_SDK    := agent-control
PACK_ENGINE := agent-control-engine
PACK_TELEMETRY := agent-control-telemetry
PACK_EVALUATORS := agent-control-evaluators
PACK_DISPATCHER := agent-control-dispatcher
PACK_KNOWLEDGE_SYNC := agent-control-knowledge-sync
PACK_FLEET := agent-control-fleet
PACK_EXAMPLE_ADK := agent-control-google-adk-plugin-example
OPENAPI_SPEC_PATH := server/.generated/openapi.json

# Directories
MODELS_DIR := models
SERVER_DIR := server
SDK_DIR    := sdks/python
TS_SDK_DIR := sdks/typescript
ENGINE_DIR := engine
TELEMETRY_DIR := telemetry
EVALUATORS_DIR := evaluators/builtin
CONTRIB_DIR := evaluators/contrib
DISPATCHER_DIR := dispatcher
KNOWLEDGE_SYNC_DIR := knowledge_sync
FLEET_DIR := fleet
UI_DIR := ui

define run-contrib-target
	@set -e; \
	packages=$$(uv run python scripts/contrib_packages.py names); \
	for package in $$packages; do \
		$(MAKE) -C $(CONTRIB_DIR)/$$package $(1); \
	done
endef

help:
	@echo "Agent Control - Makefile commands"
	@echo ""
	@echo "Setup:"
	@echo "  make sync            - uv sync all workspace packages at root (single .venv for all)"
	@echo ""
	@echo "Run:"
	@echo "  make server-<target> - forward to server targets (e.g., server-help, server-alembic-upgrade)"
	@echo "  make ui-<target>     - forward to UI targets (e.g., ui-help, ui-dev, ui-lint, ui-lint--fix)"
	@echo "  make openapi-spec    - generate runtime OpenAPI spec at $(OPENAPI_SPEC_PATH)"
	@echo "  make openapi-spec-check - verify OpenAPI generation succeeds"
	@echo ""
	@echo "Test:"
	@echo "  make test            - run tests for core packages and all discovered contrib evaluators"
	@echo "  make contrib-verify  - verify root contrib packaging contract wiring"
	@echo "  make scripts-test    - run root contrib packaging contract tests"
	@echo "  make models-test     - run shared model tests with coverage"
	@echo "  make dispatcher-test - run dispatcher tests with coverage"
	@echo "  make fleet-test      - run fleet tests with coverage"
	@echo "  make test-extras     - run tests for all discovered contrib evaluators"
	@echo "  make test-all        - alias for make test"
	@echo "  make sdk-ts-test     - run TypeScript SDK tests"
	@echo ""
	@echo "Quality:"
	@echo "  make lint            - ruff check for all members"
	@echo "  make lint-fix        - ruff check --fix (auto-fix) for all members"
	@echo "  make typecheck       - mypy for all members"
	@echo "  make check           - run test, lint, and typecheck"
	@echo "  make sdk-ts-lint | sdk-ts-typecheck | sdk-ts-build | sdk-ts-generate | sdk-ts-overlay-test | sdk-ts-name-check"
	@echo "  make sdk-ts-release-check - run TypeScript SDK publish gate checks"
	@echo "  make sdk-ts-publish-dry-run - run npm publish dry-run for TypeScript SDK"
	@echo ""
	@echo "Build / Publish:"
	@echo "  make build           - build wheels for all members"
	@echo "  make publish         - publish all members (ensure credentials configured)"
	@echo "  make build-models | build-server | build-sdk"
	@echo "  make publish-models | publish-server | publish-sdk"
	@echo ""
	@echo "Git hooks:"
	@echo "  make hooks-install   - install repo-local git hooks (pre-push)"
	@echo "  make hooks-uninstall - restore default .git/hooks"
	@echo "  make prepush         - run pre-push checks locally"

# ---------------------------
# Setup
# ---------------------------

sync:
	uv sync --all-packages

# ---------------------------
# OpenAPI spec
# ---------------------------

openapi-spec:
	uv run --package $(PACK_SERVER) python server/openapi.py --output $(OPENAPI_SPEC_PATH)

openapi-spec-check: openapi-spec
	test -s $(OPENAPI_SPEC_PATH)

# ---------------------------
# Run
# ---------------------------

# ---------------------------
# Test
# ---------------------------

test: contrib-verify scripts-test models-test telemetry-test server-test engine-test sdk-test evaluators-test dispatcher-test knowledge-sync-test fleet-test contrib-test

contrib-verify:
	uv run python scripts/contrib_packages.py verify

scripts-test:
	uv run --with pytest pytest scripts/tests -q

models-test:
	cd $(MODELS_DIR) && uv run pytest --cov=src --cov-report=xml:../coverage-models.xml -q

test-models: models-test

telemetry-test:
	$(MAKE) -C $(TELEMETRY_DIR) test

# A workspace member whose suite nothing ran, and several of its tests are proofs by absence.
dispatcher-test:
	cd $(DISPATCHER_DIR) && uv run pytest --cov=src --cov-report=xml:../coverage-dispatcher.xml -q

# Same gap as dispatcher-test above, one package over. --extra text-extraction because the
# Dockerfile installs it: without MarkItDown these tests exercise a different program than
# the one that ships. The database-backed tests skip when no Postgres answers.
knowledge-sync-test:
	uv run --package $(PACK_KNOWLEDGE_SYNC) --extra text-extraction \
		pytest $(KNOWLEDGE_SYNC_DIR)/tests \
		--cov=$(KNOWLEDGE_SYNC_DIR)/src \
		--cov-report=xml:coverage-knowledge-sync.xml -q

# Same gap again, one package over. No Postgres, no container runtime and no
# server: every test here is a file fact or a pure function.
fleet-test:
	uv run --package $(PACK_FLEET) pytest $(FLEET_DIR)/tests \
		--cov=$(FLEET_DIR)/src \
		--cov-report=xml:coverage-fleet.xml -q

# Run tests for discovered contrib evaluators
test-extras: contrib-test

# Run all tests (alias for test)
test-all: test

# Run tests, lint, and typecheck
check: test lint typecheck

# ---------------------------
# Quality
# ---------------------------

lint: engine-lint telemetry-lint evaluators-lint contrib-lint
	uv run --package $(PACK_MODELS) ruff check --config pyproject.toml models/src
	uv run --package $(PACK_SERVER) ruff check --config pyproject.toml server/src
	uv run --package $(PACK_SDK) ruff check --config pyproject.toml sdks/python/src
	uv run --package $(PACK_DISPATCHER) ruff check --config pyproject.toml dispatcher/src dispatcher/tests
	uv run --package $(PACK_KNOWLEDGE_SYNC) ruff check --config pyproject.toml knowledge_sync/src knowledge_sync/tests
	uv run --package $(PACK_FLEET) ruff check --config pyproject.toml fleet/src fleet/tests
	uv run --package $(PACK_EXAMPLE_ADK) ruff check --config pyproject.toml examples/google_adk_plugin/my_agent

lint-fix: engine-lint-fix telemetry-lint-fix evaluators-lint-fix contrib-lint-fix
	uv run --package $(PACK_MODELS) ruff check --config pyproject.toml --fix models/src
	uv run --package $(PACK_SERVER) ruff check --config pyproject.toml --fix server/src
	uv run --package $(PACK_SDK) ruff check --config pyproject.toml --fix sdks/python/src
	uv run --package $(PACK_DISPATCHER) ruff check --config pyproject.toml --fix dispatcher/src dispatcher/tests
	uv run --package $(PACK_KNOWLEDGE_SYNC) ruff check --config pyproject.toml --fix knowledge_sync/src knowledge_sync/tests
	uv run --package $(PACK_FLEET) ruff check --config pyproject.toml --fix fleet/src fleet/tests
	uv run --package $(PACK_EXAMPLE_ADK) ruff check --config pyproject.toml --fix examples/google_adk_plugin/my_agent

typecheck: engine-typecheck telemetry-typecheck evaluators-typecheck contrib-typecheck
	uv run --package $(PACK_MODELS) mypy --config-file pyproject.toml models/src
	uv run --package $(PACK_SERVER) mypy --config-file pyproject.toml server/src
	uv run --package $(PACK_SDK) mypy --config-file pyproject.toml sdks/python/src
	uv run --package $(PACK_DISPATCHER) mypy --config-file pyproject.toml dispatcher/src
	uv run --package $(PACK_KNOWLEDGE_SYNC) mypy --config-file pyproject.toml knowledge_sync/src
	uv run --package $(PACK_FLEET) mypy --config-file pyproject.toml fleet/src
	uv run --package $(PACK_EXAMPLE_ADK) mypy --config-file pyproject.toml examples/google_adk_plugin/my_agent

telemetry-lint:
	$(MAKE) -C $(TELEMETRY_DIR) lint

telemetry-lint-fix:
	$(MAKE) -C $(TELEMETRY_DIR) lint-fix

telemetry-typecheck:
	$(MAKE) -C $(TELEMETRY_DIR) typecheck

# ---------------------------
# Build / Publish
# ---------------------------

build: build-models build-server build-sdk engine-build telemetry-build evaluators-build contrib-build

build-models:
	cd $(MODELS_DIR) && uv build

build-server:
	cd $(SERVER_DIR) && uv build

build-sdk:
	cd $(SDK_DIR) && uv build

telemetry-build:
	cd $(TELEMETRY_DIR) && uv build

publish: publish-models publish-server publish-sdk engine-publish telemetry-publish

publish-models:
	cd $(MODELS_DIR) && uv publish

publish-server:
	cd $(SERVER_DIR) && uv publish

publish-sdk:
	cd $(SDK_DIR) && uv publish

telemetry-publish:
	cd $(TELEMETRY_DIR) && uv publish

# ---------------------------
# Git hooks
# ---------------------------

HOOKS_DIR := .githooks

hooks-install:
	git config core.hooksPath $(HOOKS_DIR)
	chmod +x $(HOOKS_DIR)/pre-push
	@echo "Installed git hooks from $(HOOKS_DIR)"

hooks-uninstall:
	git config --unset core.hooksPath || true
	@echo "Restored default git hooks path (.git/hooks)"

prepush:
	bash $(HOOKS_DIR)/pre-push

sdk-ts-generate: openapi-spec
	$(MAKE) -C $(TS_SDK_DIR) generate

sdk-ts-generate-check: openapi-spec
	$(MAKE) -C $(TS_SDK_DIR) generate-check

sdk-ts-name-check:
	$(MAKE) -C $(TS_SDK_DIR) name-check

sdk-ts-overlay-test:
	$(MAKE) -C $(TS_SDK_DIR) overlay-test

sdk-ts-build:
	$(MAKE) -C $(TS_SDK_DIR) build

sdk-ts-test:
	$(MAKE) -C $(TS_SDK_DIR) test

sdk-ts-lint:
	$(MAKE) -C $(TS_SDK_DIR) lint

sdk-ts-typecheck:
	$(MAKE) -C $(TS_SDK_DIR) typecheck

sdk-ts-release-check:
	$(MAKE) -C $(TS_SDK_DIR) release-check

sdk-ts-publish-dry-run:
	$(MAKE) -C $(TS_SDK_DIR) publish-dry-run

sdk-ts-publish:
	$(MAKE) -C $(TS_SDK_DIR) publish

sdk-ts-%:
	$(MAKE) -C $(TS_SDK_DIR) $(patsubst sdk-ts-%,%,$@)

engine-%:
	$(MAKE) -C $(ENGINE_DIR) $(patsubst engine-%,%,$@)

sdk-%:
	$(MAKE) -C $(SDK_DIR) $(patsubst sdk-%,%,$@)

evaluators-test:
	$(MAKE) -C $(EVALUATORS_DIR) test

evaluators-lint:
	$(MAKE) -C $(EVALUATORS_DIR) lint

evaluators-lint-fix:
	$(MAKE) -C $(EVALUATORS_DIR) lint-fix

evaluators-typecheck:
	$(MAKE) -C $(EVALUATORS_DIR) typecheck

evaluators-build:
	$(MAKE) -C $(EVALUATORS_DIR) build

.PHONY: server-%
server-%:
	$(MAKE) -C $(SERVER_DIR) $(patsubst server-%,%,$@)

.PHONY: ui-%
ui-%:
	$(MAKE) -C $(UI_DIR) $(patsubst ui-%,%,$@)

contrib-test:
	$(call run-contrib-target,test)

contrib-lint:
	$(call run-contrib-target,lint)

contrib-lint-fix:
	$(call run-contrib-target,lint-fix)

contrib-typecheck:
	$(call run-contrib-target,typecheck)

contrib-build:
	$(call run-contrib-target,build)
