# Infra

Alembic migrations and deployment scripts for the control-plane database.

## Migrations

| Revision | What it adds |
|---|---|
| 001 | `tasks`, `events`, `runs`, `audit_rows` (Phase 1 schema) |
| 002 | `stream_deliveries` (Redis Streams dedup table, Phase 2) |
| 003 | `cancelled` task status + cancel state machine transitions |
| 004 | `agent_memories` table with unique index on `(agent_id, project_id, key)` |
| 005 | `last_used_at` column on `agent_memories` for recency-ranked retrieval |

## Commands

```bash
make migrate        # alembic upgrade head
make clean-db       # tear down Postgres volume and re-migrate (fixes disk-full errors)
```

Postgres runs via `docker-compose.yml` at `~/.orchestra/pgdata` (WSL2 bind mount). Host port 5433.
