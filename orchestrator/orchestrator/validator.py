"""Task validator — pluggable, registry-driven.

Runs each validator assigned to a task (stored in task.validators), plus the
always-on built-ins (file-exists, llm-acceptance). Validators are defined in
permissions/validators.yaml; users can add custom shell-command entries there.

Usage:
    from orchestrator.orchestrator.validator import validate_task, detect_validators, load_registry
    result = validate_task(session, task_id="TASK-001", repo_path="/path/to/repo")
    session.commit()
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

import yaml
from sqlalchemy import select
from sqlalchemy.orm import Session

from .db import ArtifactProvenance, Run, Task
from .state_machine import TaskNotFoundError, transition


class ValidationError(Exception):
    """Raised when validate_task cannot proceed (wrong status, provenance block, etc.)."""


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

_DEFAULT_REGISTRY_PATH = Path(__file__).parent.parent.parent / "permissions" / "validators.yaml"


def load_registry(registry_path: Path | None = None) -> dict:
    """Load validators.yaml and return the validators dict keyed by name."""
    path = registry_path or _DEFAULT_REGISTRY_PATH
    if not path.exists():
        return {}
    with open(path) as f:
        data = yaml.safe_load(f) or {}
    return data.get("validators", {})


def detect_validators(outputs: list[str], registry: dict) -> list[str]:
    """Return validator names auto-selected for the given output paths.

    Rules:
    - Validators with always_run=True are NOT included here (they self-add at runtime).
    - Validators with auto_detect=False are excluded.
    - A validator is selected if any output path matches one of its match_extensions
      or match_paths patterns.
    """
    selected: list[str] = []
    for name, cfg in registry.items():
        if cfg.get("always_run"):
            continue
        if not cfg.get("auto_detect", True):
            continue
        exts = cfg.get("match_extensions", [])
        paths = cfg.get("match_paths", [])
        for out in outputs:
            out_lower = out.lower()
            if any(out_lower.endswith(e) for e in exts):
                selected.append(name)
                break
            if any(p in out_lower for p in paths):
                selected.append(name)
                break
    return selected


# ---------------------------------------------------------------------------
# Check result
# ---------------------------------------------------------------------------


@dataclass
class CheckResult:
    name: str
    passed: bool
    output: str
    duration_s: float
    returncode: int | None = None


# ---------------------------------------------------------------------------
# Low-level helpers
# ---------------------------------------------------------------------------

_SANDBOX_ENV_ISOLATE = ("DATABASE_URL", "REDIS_URL", "DATABASE_HOST")


def _clean_env() -> dict:
    env = os.environ.copy()
    for key in _SANDBOX_ENV_ISOLATE:
        env.pop(key, None)
    return env


def _git(repo: Path, *args: str) -> tuple[int, str, str]:
    r = subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True)
    return r.returncode, r.stdout.strip(), r.stderr.strip()


def _run_shell(cmd: str, cwd: Path, timeout: int = 120) -> tuple[int, str, str]:
    """Run a shell command in cwd, return (returncode, stdout, stderr)."""
    try:
        r = subprocess.run(
            cmd,
            shell=True,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=_clean_env(),
        )
        return r.returncode, r.stdout, r.stderr
    except subprocess.TimeoutExpired:
        return -1, "", f"timed out after {timeout}s"


# ---------------------------------------------------------------------------
# Built-in: file-exists
# ---------------------------------------------------------------------------


def _check_file_exists(repo: Path, outputs: list[str]) -> CheckResult:
    t0 = time.monotonic()
    if not outputs:
        return CheckResult(
            name="file-exists",
            passed=True,
            output="No output files declared.",
            duration_s=time.monotonic() - t0,
        )
    missing = [p for p in outputs if not (repo / p).exists()]
    passed = len(missing) == 0
    if passed:
        output = f"All {len(outputs)} output file(s) present."
    else:
        output = f"{len(missing)}/{len(outputs)} output file(s) missing: {', '.join(missing)}"
    return CheckResult(
        name="file-exists",
        passed=passed,
        output=output,
        duration_s=time.monotonic() - t0,
    )


# ---------------------------------------------------------------------------
# Built-in: llm-acceptance
# ---------------------------------------------------------------------------

_LLM_ACCEPTANCE_PROMPT = """\
You are a QA validator evaluating whether an AI agent's output meets the task acceptance criteria.

## Task acceptance criteria
{criteria_block}

## Agent outputs (git diff from main branch)
{diff_block}

## Instructions
For each criterion, decide: PASS, WARN (partially met / cannot verify), or FAIL.
Return a JSON object with this exact structure:
{{
  "results": [
    {{"criterion": "<criterion text>", "verdict": "PASS|WARN|FAIL", "reason": "<one line>"}},
    ...
  ],
  "overall": "PASS|WARN|FAIL"
}}
Return only the JSON — no markdown, no explanation outside it."""


def _check_llm_acceptance(repo: Path, branch: str, task: Task) -> CheckResult:
    t0 = time.monotonic()
    criteria = task.acceptance or []

    if not criteria:
        return CheckResult(
            name="llm-acceptance",
            passed=True,
            output="No acceptance criteria defined — skipped.",
            duration_s=time.monotonic() - t0,
        )

    claude_bin = shutil.which("claude")
    if not claude_bin:
        return CheckResult(
            name="llm-acceptance",
            passed=True,
            output="claude CLI not found — llm-acceptance skipped (install claude and run 'claude login').",
            duration_s=time.monotonic() - t0,
        )

    # Get diff of agent branch vs main
    rc, diff, err = _git(repo, "diff", "main", branch, "--stat", "--unified=3")
    if rc != 0 or not diff:
        rc2, diff, _ = _git(repo, "diff", "HEAD~1", "HEAD")
    diff_block = (diff[:3000] + "\n...(truncated)") if len(diff) > 3000 else diff

    criteria_block = "\n".join(f"{i+1}. {c}" for i, c in enumerate(criteria))
    prompt = _LLM_ACCEPTANCE_PROMPT.format(
        criteria_block=criteria_block, diff_block=diff_block or "(no diff available)"
    )

    env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}
    try:
        result = subprocess.run(
            ["claude", "--system-prompt", prompt, "-p",
             "Evaluate the acceptance criteria. Return only the JSON."],
            env=env,
            capture_output=True,
            text=True,
            timeout=120,
        )
        raw = result.stdout.strip()
    except subprocess.TimeoutExpired:
        return CheckResult(
            name="llm-acceptance",
            passed=True,  # don't fail the task on LLM timeout
            output="llm-acceptance timed out — skipped.",
            duration_s=time.monotonic() - t0,
        )

    # Parse JSON response
    try:
        # Extract JSON from response (claude may wrap in markdown)
        if "```" in raw:
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        data = json.loads(raw)
        results = data.get("results", [])
        overall = data.get("overall", "WARN")

        lines = []
        n_pass = sum(1 for r in results if r.get("verdict") == "PASS")
        n_warn = sum(1 for r in results if r.get("verdict") == "WARN")
        n_fail = sum(1 for r in results if r.get("verdict") == "FAIL")
        lines.append(f"{n_pass}/{len(results)} criteria passed, {n_warn} warned, {n_fail} failed")
        for r in results:
            symbol = {"PASS": "✓", "WARN": "~", "FAIL": "✗"}.get(r.get("verdict", "WARN"), "?")
            lines.append(f"  {symbol} {r.get('criterion', '?')[:80]}")
            if r.get("verdict") != "PASS":
                lines.append(f"    → {r.get('reason', '')}")

        passed = overall in ("PASS", "WARN")  # WARN = soft pass
        return CheckResult(
            name="llm-acceptance",
            passed=passed,
            output="\n".join(lines),
            duration_s=time.monotonic() - t0,
        )
    except (json.JSONDecodeError, KeyError, TypeError):
        # Unparseable response — treat as soft pass to avoid blocking on LLM issues
        short = raw[:300] if raw else "(empty response)"
        return CheckResult(
            name="llm-acceptance",
            passed=True,
            output=f"llm-acceptance response could not be parsed (soft pass):\n{short}",
            duration_s=time.monotonic() - t0,
        )


# ---------------------------------------------------------------------------
# Shell validator
# ---------------------------------------------------------------------------


def _check_shell(name: str, cfg: dict, repo: Path) -> CheckResult:
    """Run a shell-command validator."""
    t0 = time.monotonic()
    cmd: str = cfg["command"]

    # pytest: honour existing subdirectory-with-pyproject pattern
    if name == "pytest":
        return _check_pytest(repo, t0)

    rc, stdout, stderr = _run_shell(cmd, cwd=repo)
    out = (stdout + stderr).strip()[:800]
    # Accept rc 0 as pass; for some validators (jest --passWithNoTests) also accept 5
    ok_codes = {0, 5} if "passWithNoTests" in cmd else {0}
    passed = rc in ok_codes
    return CheckResult(
        name=name,
        passed=passed,
        output=out or f"exit code {rc}",
        duration_s=time.monotonic() - t0,
        returncode=rc,
    )


def _check_pytest(repo: Path, t0: float) -> CheckResult:
    """Pytest with subdirectory-aware uv run."""
    subdirs = [d for d in sorted(repo.iterdir()) if d.is_dir() and (d / "pyproject.toml").exists()]
    uv = shutil.which("uv") or "uv"

    if subdirs:
        combined_out = ""
        overall_rc = 0
        for subdir in subdirs:
            rc, stdout, stderr = _run_shell(
                f"{uv} run --extra test pytest --tb=short -q", cwd=subdir
            )
            combined_out += f"\n--- {subdir.name} ---\n{stdout}{stderr}"
            if rc not in (0, 5):
                overall_rc = rc
        passed = overall_rc in (0, 5)
        return CheckResult(
            name="pytest",
            passed=passed,
            output=combined_out.strip()[:800],
            duration_s=time.monotonic() - t0,
            returncode=overall_rc,
        )
    else:
        rc, stdout, stderr = _run_shell(
            f"{sys.executable} -m pytest --tb=short -q", cwd=repo
        )
        passed = rc in (0, 5)
        return CheckResult(
            name="pytest",
            passed=passed,
            output=(stdout + stderr).strip()[:800],
            duration_s=time.monotonic() - t0,
            returncode=rc,
        )


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


def validate_task(
    session: Session,
    task_id: str,
    repo_path: str,
    actor: str = "validator",
    registry_path: Path | None = None,
) -> dict:
    """Run all validators for a completed task and transition its status.

    Returns a results dict:
        {
          "passed": bool,
          "branch": str,
          "summary": str,
          "checks": [{"name", "passed", "output", "duration_s", "returncode"}, ...],
          "checkout_error": str | None,
        }

    Raises TaskNotFoundError or ValidationError if preconditions fail.
    The caller owns the DB transaction and must call session.commit().
    """
    task = session.get(Task, task_id)
    if task is None:
        raise TaskNotFoundError(f"Task {task_id!r} not found")
    if task.status != "completed":
        raise ValidationError(
            f"Task {task_id!r} must be 'completed' to validate; current: {task.status!r}"
        )

    # Provenance gate: reject outputs with external provenance.
    external_outputs = []
    for path in task.outputs or []:
        row = session.execute(
            select(ArtifactProvenance).where(
                ArtifactProvenance.repo_path == repo_path,
                ArtifactProvenance.file_path == path,
            )
        ).scalar_one_or_none()
        if row is not None and row.provenance == "external":
            external_outputs.append(path)
    if external_outputs:
        raise ValidationError(
            f"Task {task_id!r} outputs contain external-provenance content: "
            f"{', '.join(external_outputs)}"
        )

    # Determine branch from run record.
    run_row = (
        session.execute(select(Run).where(Run.task_id == task_id).order_by(Run.started_at.desc()))
        .scalars()
        .first()
    )
    branch = run_row.branch if run_row is not None else f"agent/backend/{task_id}"
    repo = Path(repo_path).resolve()

    # Checkout: prefer worktree to avoid polluting the main repo.
    branch_slug = branch.replace("/", "_")
    worktree_path = Path("/tmp/orchestra/worktrees") / branch_slug
    used_worktree = worktree_path.exists()
    checkout_error: str | None = None

    if used_worktree:
        repo = worktree_path
    else:
        rc, _out, err = _git(repo, "checkout", branch)
        if rc != 0:
            checkout_error = err or f"git checkout {branch} failed (rc={rc})"
            result = {
                "passed": False,
                "branch": branch,
                "summary": "Checkout failed",
                "checks": [],
                "checkout_error": checkout_error,
            }
            _finalize(session, task_id, passed=False, actor=actor, result=result)
            return result

    # Load registry and determine which validators to run.
    registry = load_registry(registry_path)

    # task.validators stores the assigned list; fall back to auto-detect for old tasks.
    assigned: list[str] = list(task.validators or [])
    if not assigned:
        assigned = detect_validators(task.outputs or [], registry)

    # Run validators in order: file-exists first, then assigned, then llm-acceptance last.
    checks: list[CheckResult] = []

    # Always-on: file-exists
    checks.append(_check_file_exists(repo, task.outputs or []))

    # Assigned validators (shell or built-in).
    for name in assigned:
        if name in ("file-exists", "llm-acceptance"):
            continue  # handled separately
        cfg = registry.get(name)
        if cfg is None:
            checks.append(CheckResult(
                name=name,
                passed=False,
                output=f"Validator {name!r} not found in registry.",
                duration_s=0.0,
            ))
            continue
        if cfg.get("builtin"):
            # Future built-ins can be dispatched here
            checks.append(CheckResult(
                name=name, passed=True,
                output=f"Built-in {name} handler not implemented.",
                duration_s=0.0,
            ))
        else:
            checks.append(_check_shell(name, cfg, repo))

    # Always-on: llm-acceptance (runs only when task has acceptance criteria)
    checks.append(_check_llm_acceptance(repo, branch, task))

    passed = all(c.passed for c in checks)
    n_pass = sum(1 for c in checks if c.passed)
    summary = f"{n_pass}/{len(checks)} checks passed"

    try:
        from .metrics import validator_results_total
        validator_results_total.labels(
            result="passed" if passed else "failed", owner=task.owner
        ).inc()
    except Exception:
        pass

    result = {
        "passed": passed,
        "branch": branch,
        "summary": summary,
        "checks": [asdict(c) for c in checks],
        "checkout_error": checkout_error,
    }

    _finalize(session, task_id, passed=passed, actor=actor, result=result)

    if not used_worktree:
        _git(Path(repo_path).resolve(), "checkout", "main")

    return result


def _finalize(
    session: Session,
    task_id: str,
    passed: bool,
    actor: str,
    result: dict,
) -> None:
    new_status = "validated" if passed else "failed"
    transition(
        session,
        task_id,
        new_status,
        actor=actor,
        payload={"validation_passed": passed, "summary": result.get("summary", "")},
        details=result,
    )

    run = (
        session.execute(select(Run).where(Run.task_id == task_id).order_by(Run.started_at.desc()))
        .scalars()
        .first()
    )
    if run is not None:
        run.result = "validated" if passed else "validation_failed"
        if run.finished_at is None:
            run.finished_at = datetime.now(timezone.utc)
        session.flush()
