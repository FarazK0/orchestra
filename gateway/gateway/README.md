# Tool Gateway

The ONLY path to side effects. Verifies permission (Phase 1: allowlist on
(agent_id, task_id); Phase 3: signed capability tokens), executes the operation,
and writes the audit row atomically with the action.

Operations (Phase 1):
- read_artifact(path)
- write_artifact(path, content, provenance)
- run_command(cmd)        # docker sandbox, no network
- emit_event(event)
- git ops: branch, commit, merge (merge requires human approval flag)
