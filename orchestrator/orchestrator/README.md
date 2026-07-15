# Orchestrator (control plane)

Owns: task CRUD and state machine, event log (append-only), run records,
context packaging, and (Phase 2+) DAG scheduling.

Modules to build in Phase 1:
- db.py            SQLAlchemy models: tasks, events, runs, audit
- state_machine.py explicit transitions; every transition = event + audit row in one tx
- context.py       context packager: task spec + input artifacts + acceptance criteria
- api.py           FastAPI app consumed by orchctl and the gateway
