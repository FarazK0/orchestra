# ADR-001: Git as the artifact plane

Status: Accepted

## Decision
All project artifacts (docs, code, ADRs, reports) live in a real Git repository.
Agents work on branches named agent/<agent-id>/<task-id>; nothing writes to main
except the gateway-mediated merge flow.

## Rationale
Git already provides content-addressed storage, branching, merge conflict
detection, attribution via committer identity, and diffs for review. Building a
custom repository format duplicates this at high cost.

## Consequences
Attribution and content audit come from git log; the control plane stores only
references (branch names, commit SHAs) rather than artifact content.
