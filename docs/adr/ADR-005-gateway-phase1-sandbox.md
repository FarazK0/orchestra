# ADR-005: Gateway run_command Uses subprocess in Phase 1

**Status:** Accepted
**Date:** 2026-07-14

## Context

The tool gateway's `run_command` endpoint is supposed to execute agent-requested
commands in an isolated Docker container with no network access (design doc,
section 3.6 and step 8). The purpose is containment: a misbehaving agent cannot
reach the internet, modify files outside the repo, or consume unbounded resources.

Docker-in-WSL2 requires Docker Desktop or a manual `dockerd` daemon. Adding
Docker as a runtime dependency of the gateway test suite on WSL2 would block
the walking skeleton phase on an operational dependency that is not yet
necessary for correctness.

## Decision

Phase 1 implements `run_command` using `subprocess.run()` with a timeout
and the working directory set to `repo_path`. No Docker isolation is applied.

This is explicitly a Phase 1 simplification. The gateway README and code
are annotated to mark this as a stub.

## Consequences

- The walking skeleton (Phase 1) ships without container isolation for commands.
- Tests can run without Docker by using safe built-in commands (`echo`, `false`).
- Phase 3 must replace `subprocess.run()` with
  `docker run --rm --network none --mount type=bind,src={repo_path},dst=/repo <image> <cmd>`.
- Until Phase 3, the gateway should only be trusted in a controlled dev environment.
  Production use of `run_command` before Phase 3 is a known risk accepted by the
  team.

## Alternatives considered

- **Docker in Phase 1:** Correct but blocks the walking skeleton on infra setup.
- **nsjail / bubblewrap:** Better isolation without Docker, but adds a build
  dependency and complicates WSL2 support.
- **No run_command in Phase 1:** Would prevent the validator (step 11) from
  running `pytest` through the gateway, breaking the demo.
