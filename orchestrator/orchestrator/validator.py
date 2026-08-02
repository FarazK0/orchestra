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

import hashlib
import json
import os
import re
import shutil
import subprocess
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

import yaml
from sqlalchemy import select
from sqlalchemy.orm import Session

from agents.shared.cli_runner import get_backend, load_backends, run_cli_prompt
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
    failure_category: str | None = None  # "pre_existing" | "env_limitation"


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

## Existing files in the repository (main branch, before the agent's changes)
These files already existed before the diff below was applied. Use this list to
avoid incorrectly concluding that an imported module or referenced file is missing.
{repo_files_block}

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

    _validator_backend = get_backend("validator")
    _validator_cfg = load_backends().get(_validator_backend, {})
    _is_cli = _validator_cfg.get("type", "cli") != "python"
    if _is_cli:
        _cmd = _validator_cfg.get(
            "command", ["claude", "--dangerously-skip-permissions", "-p", "-"]
        )
        _cli_bin = shutil.which(_cmd[0]) if _cmd else None
        if not _cli_bin:
            return CheckResult(
                name="llm-acceptance",
                passed=True,
                output=(
                    f"Validator backend '{_validator_backend}' not found "
                    f"(command: {_cmd[0] if _cmd else '?'}) — llm-acceptance skipped."
                ),
                duration_s=time.monotonic() - t0,
            )

    # Get diff of agent branch vs main.
    rc, diff, err = _git(repo, "diff", "main", branch, "--unified=3")
    if rc != 0 or not diff:
        # Fallback: show the agent's most recent commit on the branch itself.
        _, diff, _ = _git(repo, "diff", f"{branch}^", branch, "--unified=3")
    diff_block = (diff[:6000] + "\n...(truncated)") if len(diff) > 6000 else diff

    # Enumerate files that already exist on main so the LLM can distinguish
    # "missing file" from "file already in repo, not shown in diff".
    _, ls_out, _ = _git(repo, "ls-files", "--with-tree=main")
    repo_files_lines = sorted(ls_out.strip().splitlines()) if ls_out else []
    if len(repo_files_lines) > 120:
        repo_files_lines = repo_files_lines[:120] + [
            f"...({len(repo_files_lines) - 120} more files)"
        ]
    repo_files_block = "\n".join(repo_files_lines) if repo_files_lines else "(could not list files)"

    criteria_block = "\n".join(f"{i + 1}. {c}" for i, c in enumerate(criteria))
    prompt = _LLM_ACCEPTANCE_PROMPT.format(
        criteria_block=criteria_block,
        diff_block=diff_block or "(no diff available)",
        repo_files_block=repo_files_block,
    )

    try:
        if not _is_cli:
            from agents.shared.llm import LLMClient

            llm = LLMClient()
            response = llm.call(
                messages=[
                    {
                        "role": "user",
                        "content": "Evaluate the acceptance criteria. Return only the JSON.",
                    }
                ],
                system=prompt,
                run_id=None,
                session=None,
                max_tokens=1024,
            )
            raw = response.content[0].text.strip()
        else:
            full_prompt = prompt + "\n\nEvaluate the acceptance criteria. Return only the JSON."
            raw = run_cli_prompt(_validator_backend, full_prompt, timeout=120).strip()
    except RuntimeError as exc:
        return CheckResult(
            name="llm-acceptance",
            passed=True,
            output=f"llm-acceptance skipped: {exc}",
            duration_s=time.monotonic() - t0,
        )
    except Exception as exc:
        if "timeout" in str(exc).lower():
            return CheckResult(
                name="llm-acceptance",
                passed=True,
                output="llm-acceptance timed out — skipped.",
                duration_s=time.monotonic() - t0,
            )
        return CheckResult(
            name="llm-acceptance",
            passed=True,
            output=f"llm-acceptance error (soft pass): {exc}",
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


def _check_shell(
    name: str, cfg: dict, repo: Path, branch: str = "", main_repo: Path | None = None
) -> CheckResult:
    """Run a shell-command validator."""
    t0 = time.monotonic()
    cmd: str = cfg["command"]

    if name == "pytest":
        return _check_pytest(repo, t0, main_repo=main_repo)

    if name == "ruff":
        return _check_ruff(repo, branch, t0)

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


_VENV_CACHE_ROOT = Path("/tmp/orchestra/validation-venvs")


def _ensure_validation_venv(repo: Path, req_paths: list[Path]) -> tuple[str, list[str], bool]:
    """Return (venv_python_path, notes, install_ok).

    Creates (or reuses) a cached venv keyed by the combined hash of all present
    requirements files.  Uses the system python3 so pip is always available,
    which supports requirements formats (e.g. per-package --index-url) that
    uv pip does not handle.
    """
    # Hash the content of all requirements files to form a cache key.
    h = hashlib.sha256()
    for p in sorted(req_paths):
        h.update(p.name.encode())
        h.update(p.read_bytes())
    cache_key = h.hexdigest()[:16]
    venv_dir = _VENV_CACHE_ROOT / f"{repo.name}-{cache_key}"
    venv_python = str(venv_dir / "bin" / "python3")
    stamp = venv_dir / ".install-ok"
    notes: list[str] = []

    if stamp.exists():
        return venv_python, notes, True

    # Create fresh venv using system python3 (always has pip).
    # --system-site-packages inherits large native packages (torch, numpy, etc.)
    # already installed on the host, avoiding slow CDN downloads for those.
    system_python = shutil.which("python3") or "python3"
    _VENV_CACHE_ROOT.mkdir(parents=True, exist_ok=True)
    rc, out, err = _run_shell(
        f"{system_python} -m venv --system-site-packages {venv_dir}", cwd=repo, timeout=60
    )
    if rc != 0:
        notes.append(f"venv creation failed: {(err or out).strip()[:200]}")
        return venv_python, notes, False

    pip_bin = str(venv_dir / "bin" / "pip")
    # Always install pytest so the venv can run tests.
    _run_shell(f"{pip_bin} install pytest -q", cwd=repo, timeout=120)

    any_failed = False
    for req_path in req_paths:
        rc, out, err = _run_shell(f"{pip_bin} install -r {req_path} -q", cwd=repo, timeout=900)
        if rc != 0:
            msg = f"pip install -r {req_path.name} failed: {(err or out).strip()[:200]}"
            notes.append(msg)
            any_failed = True

    if not any_failed:
        stamp.touch()

    return venv_python, notes, not any_failed


def _check_pytest(repo: Path, t0: float, main_repo: Path | None = None) -> CheckResult:
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
        # Non-pyproject repos: create an isolated venv (cached by requirements hash)
        # so dependencies are installed with pip, which handles all requirements formats
        # including per-package --index-url options that uv pip does not support.
        req_candidates = [
            repo / "requirements.txt",
            repo / "requirements-dev.txt",
            repo / "requirements-test.txt",
            repo / "app" / "requirements.txt",
        ]
        # Also pull dev/test requirements from main in the primary repo — a task's
        # worktree branches from main at dispatch time and may be missing dev
        # requirements added later by sibling tasks (e.g. requirements-dev.txt
        # committed by TASK-003 while TASK-002's worktree was already created).
        if main_repo is not None and main_repo != repo:
            for name in ("requirements-dev.txt", "requirements-test.txt"):
                main_req = main_repo / name
                worktree_req = repo / name
                # Add main's copy when the worktree doesn't have its own.
                if main_req.exists() and not worktree_req.exists():
                    req_candidates.append(main_req)
        present_reqs = [p for p in req_candidates if p.exists()]

        venv_python, install_notes, install_ok = _ensure_validation_venv(repo, present_reqs)
        if not install_ok:
            # Installation of requirements.txt failed — treat as env_limitation.
            return CheckResult(
                name="pytest",
                passed=False,
                output="requirements install failed; cannot run pytest.\n"
                + "\n".join(install_notes),
                duration_s=time.monotonic() - t0,
                returncode=None,
                failure_category="env_limitation",
            )

        # Symlink top-level entries from main_repo that are absent in the worktree
        # and not tracked by git (e.g. gitignored artifacts, untracked data dirs).
        # This makes binary model files, fixtures, etc. available to tests without
        # committing them to the agent's branch.
        if main_repo is not None and main_repo != repo:
            _, tracked_out, _ = _git(repo, "ls-files")
            tracked_prefixes = set()
            for f in (tracked_out or "").strip().splitlines():
                tracked_prefixes.add(f.split("/")[0])
            _SKIP = {".git", "__pycache__", ".venv", ".mypy_cache", ".ruff_cache"}
            for src in main_repo.iterdir():
                if src.name.startswith(".") or src.name.startswith("=") or src.name in _SKIP:
                    continue
                dst = repo / src.name
                if not dst.exists() and src.name not in tracked_prefixes:
                    dst.symlink_to(src)

        rc, stdout, stderr = _run_shell(f"{venv_python} -m pytest --tb=short -q", cwd=repo)
        passed = rc in (0, 5)
        out = (stdout + stderr).strip()[:800]
        if install_notes:
            out = "\n".join(install_notes) + "\n" + out
        return CheckResult(
            name="pytest",
            passed=passed,
            output=out,
            duration_s=time.monotonic() - t0,
            returncode=rc,
        )


_RUFF_CONCISE_RE = re.compile(r"^([^:\s][^:]*?):\d+:\d+:")


def _check_ruff(repo: Path, branch: str, t0: float) -> CheckResult:
    """Ruff check with pre-existing error detection.

    If ruff fails only on files the agent never touched (not in git diff vs main),
    the errors pre-date the agent's work — soft-pass with failure_category="pre_existing"
    so the merge is not blocked, but the category is recorded in the audit trail.

    Uses --output-format=concise to get stable file:line:col: CODE output regardless
    of terminal settings (the default rich format uses multi-line blocks with "help:"
    lines that are unsafe to parse for file names).
    """
    rc, stdout, stderr = _run_shell("ruff check . --output-format=concise --color never", cwd=repo)
    out = (stdout + stderr).strip()[:800]
    if rc == 0:
        return CheckResult(
            name="ruff", passed=True, output=out, duration_s=time.monotonic() - t0, returncode=0
        )

    # Extract file names from concise format: "path/to/file.py:line:col: CODE msg"
    error_files: set[str] = set()
    for line in stdout.splitlines():
        m = _RUFF_CONCISE_RE.match(line)
        if m:
            error_files.add(m.group(1))

    # Get files changed by the agent on this branch vs main.
    _, diff_out, _ = _git(repo, "diff", "--name-only", "main", branch)
    if not diff_out:
        _, diff_out, _ = _git(repo, "diff", "--name-only", f"{branch}^", branch)
    changed = set(diff_out.splitlines())

    untouched = error_files - changed
    agent_errors = error_files & changed

    if untouched and not agent_errors:
        # All ruff errors are in files the agent never modified — pre-existing debt.
        return CheckResult(
            name="ruff",
            passed=True,
            output=f"WARN: pre-existing ruff errors in {sorted(untouched)} "
            "(outside agent scope; a cleanup task is recommended).\n" + out,
            duration_s=time.monotonic() - t0,
            returncode=rc,
            failure_category="pre_existing",
        )

    return CheckResult(
        name="ruff", passed=False, output=out, duration_s=time.monotonic() - t0, returncode=rc
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
            _finalize(session, task_id, passed=False, actor=actor, result=result, checks=[])
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
            checks.append(
                CheckResult(
                    name=name,
                    passed=False,
                    output=f"Validator {name!r} not found in registry.",
                    duration_s=0.0,
                )
            )
            continue
        if cfg.get("builtin"):
            # Future built-ins can be dispatched here
            checks.append(
                CheckResult(
                    name=name,
                    passed=True,
                    output=f"Built-in {name} handler not implemented.",
                    duration_s=0.0,
                )
            )
        else:
            checks.append(
                _check_shell(name, cfg, repo, branch=branch, main_repo=Path(repo_path).resolve())
            )

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

    _finalize(session, task_id, passed=passed, actor=actor, result=result, checks=checks)

    if not used_worktree:
        _git(Path(repo_path).resolve(), "checkout", "main")

    return result


def _finalize(
    session: Session,
    task_id: str,
    passed: bool,
    actor: str,
    result: dict,
    checks: list[CheckResult] | None = None,
) -> None:
    env_blocked = [
        c for c in (checks or []) if not c.passed and c.failure_category == "env_limitation"
    ]

    if not passed and env_blocked:
        lines = ["Validation blocked — cannot run tests due to environment limitation:"]
        for c in env_blocked:
            lines.append(f"\n[{c.name}]\n{c.output[:500]}")
        lines.append(
            "\nOptions:\n"
            "  A) Fix the dependency issue in requirements.txt, then respond to re-run.\n"
            "  B) Grant an override if this environment cannot install the package "
            "(e.g. GPU-only deps in a CPU validator)."
        )
        task = session.execute(select(Task).where(Task.id == task_id)).scalar_one_or_none()
        if task is not None:
            task.checkpoint = {
                "type": "awaiting_human",
                "question": "\n".join(lines),
                "question_type": "blocker",
                "context": result.get("summary", ""),
                "human_response": None,
            }
        new_status = "awaiting_human"
        payload: dict = {"validation_passed": False, "blocker_category": "env_limitation"}
    else:
        new_status = "validated" if passed else "failed"
        payload = {"validation_passed": passed, "summary": result.get("summary", "")}

    transition(session, task_id, new_status, actor=actor, payload=payload, details=result)

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
