.PHONY: sync lock-check fmt fmt-check lint typecheck import-lint docs-check \
        hygiene-check unit contract property integration acceptance \
        test test-all coverage services-up services-down migrate quality ci \
        scientific phase2-scientific phase2-offline \
        phase2-acceptance smoke-live

sync:
	uv sync --all-groups

lock-check:
	uv lock --check

fmt:
	uv run ruff format .

fmt-check:
	uv run ruff format --check .

lint:
	uv run ruff check .

typecheck:
	uv run mypy src scripts

import-lint:
	uv run lint-imports

docs-check:
	uv run python scripts/validate_docs.py

hygiene-check:
	uv run python scripts/check_repository_hygiene.py

quality: lock-check fmt-check lint typecheck import-lint docs-check hygiene-check

unit:
	uv run pytest tests/unit -q

contract:
	uv run pytest tests/contracts -q

property:
	uv run pytest tests/property -q

integration:
	uv run pytest -m integration -q

acceptance:
	uv run pytest tests/acceptance -q

scientific:
	uv run pytest -m scientific -q

phase2-scientific:
	uv run pytest -m scientific tests/unit tests/property -q

phase2-offline: quality test phase2-scientific
	uv run pytest tests/live -q

phase2-acceptance:
	uv run pytest -m integration tests/acceptance/test_phase2_multimodel_baseline.py -q

smoke-live:
	@if [ "$$MESOFORGE_LIVE_TESTS" != "1" ]; then \
		echo "smoke-live requires MESOFORGE_LIVE_TESTS=1 (opt-in only; never run by default CI)"; \
		exit 1; \
	fi
	uv run pytest -m live tests/live -q

test:
	uv run pytest tests/unit tests/contracts tests/property -q

test-all:
	uv run pytest -q

coverage:
	uv run pytest --cov=mesoforge --cov-report=term-missing --cov-fail-under=90 -q

services-up:
	docker compose -f deploy/local/compose.yaml up -d --wait

services-down:
	docker compose -f deploy/local/compose.yaml down -v

migrate:
	uv run alembic upgrade head

# Hosted stack (deploy/hosted). Migration is always an explicit operator step.
HOSTED = docker compose -f deploy/hosted/compose.yaml

hosted-image:
	sh deploy/hosted/build-image.sh

# Usage: make hosted-migrate DB=<database name from deploy/hosted/.env>
hosted-migrate:
	@test -n "$(DB)" || { echo "usage: make hosted-migrate DB=<database name>"; exit 1; }
	$(HOSTED) up -d --wait postgres minio
	$(HOSTED) run --rm admin mesoforge.application.operations migrate --expect-database "$(DB)"
	$(HOSTED) run --rm admin mesoforge.application.operations init-storage

hosted-up:
	$(HOSTED) run --rm admin mesoforge.application.operations migration-status
	$(HOSTED) up -d guidance-worker

hosted-status:
	$(HOSTED) run --rm admin mesoforge.application.operations status

hosted-forecast:
	$(HOSTED) run --rm forecast-worker

ci: sync quality test
