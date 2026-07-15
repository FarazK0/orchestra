.PHONY: up down migrate clean-db test lint demo

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
