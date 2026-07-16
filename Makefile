.PHONY: up down migrate clean-db test lint demo demo-v2 dispatcher setup stop logs

up:
	docker compose up -d

down:
	docker compose down

clean-db:
	docker compose down && rm -rf $(HOME)/.orchestra/pgdata && docker compose up -d && until docker exec orchestra-postgres-1 pg_isready -U orchestra -q; do sleep 1; done && uv run alembic -c infra/alembic.ini upgrade head

migrate:
	uv run alembic -c infra/alembic.ini upgrade head

test:
	uv run pytest

lint:
	uv run ruff check . && uv run ruff format --check .

demo:
	bash scripts/demo.sh

demo-v2:
	bash scripts/demo_v2.sh

dispatcher:
	SANDBOX_REPO_PATH=$(SANDBOX_REPO_PATH) RUN_STORE_DIR=$(RUN_STORE_DIR) \
	uv run python -m orchestrator.orchestrator.dispatcher

setup:
	bash scripts/setup.sh

stop:
	@for p in /tmp/orchestra/pids/*.pid; do \
		[ -f "$$p" ] || continue; \
		kill "$$(cat $$p)" 2>/dev/null && echo "stopped $$(basename $$p .pid)" || true; \
		rm -f "$$p"; \
	done

logs:
	tail -f /tmp/orchestra/logs/*.log
