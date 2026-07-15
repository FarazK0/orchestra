.PHONY: up down migrate clean-db test lint demo

up:
	docker compose up -d

down:
	docker compose down

clean-db:
	docker compose down && docker volume rm orchestra_pgdata && docker compose up -d && sleep 8 && uv run alembic -c infra/alembic.ini upgrade head

migrate:
	uv run alembic -c infra/alembic.ini upgrade head

test:
	uv run pytest

lint:
	uv run ruff check . && uv run ruff format --check .

demo:
	bash scripts/demo.sh
