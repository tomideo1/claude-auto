"""Worker subprocess invocation.

A "worker" is a single `claude -p` subprocess running under a non-queen
account directory. This module handles the full lifecycle: account
checkout, isolation setup (tempdir / git worktree / cwd), prompt
assembly, the subprocess call (via claude_runner.run_claude in headless
mode), budget accounting, and account release.

Workers are synchronous from this module's perspective. Parallelism is
the controller's job (ThreadPoolExecutor in hive_controller.py).
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import time
import uuid
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Literal

from .account_pool import AccountPool, NoAccountAvailable
from .budget import Budget
from .claude_runner import RunResult, run_claude
from .prompts import KNOWN_ROLES, UnknownRoleError, load_prompt, render_prompt
from .rate_limit import compile_patterns


Isolation = Literal["tempdir", "worktree", "cwd"]
WorkerStatus = Literal[
    "completed", "rate_limited", "timeout", "errored", "budget_exceeded", "no_account"
]


@dataclass
class WorkerSpec:
    role: str
    task: str
    isolation: Isolation = "tempdir"
    allowed_tools: list[str] | None = None
    max_turns: int = 40
    max_cost_usd: float = 2.00
    timeout_seconds: int = 600
    # If None, we render `role` from hive/prompts/. Pass a string to override.
    system_prompt: str | None = None
    # Path to an MCP config JSON for orchestrator workers; None for leaves.
    mcp_config_path: str | None = None
    # For worktree isolation: the repo to branch off (defaults to cwd).
    worktree_base: str | None = None
    # Orchestrator depth tracking (rendered into the orchestrator template).
    depth: int = 0
    max_depth: int = 2

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class WorkerResult:
    job_id: str
    account_name: str | None
    role: str
    status: WorkerStatus
    result_text: str | None
    cost_usd: float
    elapsed_seconds: float
    error: str | None = None
    session_id: str | None = None
    working_dir: str | None = None
    spec: dict | None = None

    def to_dict(self) -> dict:
        return asdict(self)


# ----------------------------------------------------------------- isolation


@dataclass
class _IsolationCtx:
    working_dir: Path
    cleanup: callable
    worktree_added: bool = False


def _setup_tempdir(job_id: str) -> _IsolationCtx:
    d = Path(tempfile.mkdtemp(prefix=f"hive-job-{job_id}-"))
    def _cleanup():
        shutil.rmtree(d, ignore_errors=True)
    return _IsolationCtx(working_dir=d, cleanup=_cleanup)


def _setup_worktree(job_id: str, base: Path) -> _IsolationCtx:
    if not (base / ".git").exists() and not _is_git_repo(base):
        raise RuntimeError(
            f"worktree isolation requested but {base} is not a git repo"
        )
    target = Path(tempfile.mkdtemp(prefix=f"hive-worktree-{job_id}-"))
    target.rmdir()  # git worktree wants a non-existent path
    branch = f"hive/{job_id}"
    subprocess.run(
        ["git", "-C", str(base), "worktree", "add", "-b", branch, str(target), "HEAD"],
        check=True,
        capture_output=True,
    )

    def _cleanup():
        # Don't auto-remove the worktree — the caller may want to merge it.
        # We DO offer a helper in cli.py: `claude-auto hive prune-worktrees`.
        pass

    return _IsolationCtx(working_dir=target, cleanup=_cleanup, worktree_added=True)


def _setup_cwd(_job_id: str, cwd: Path) -> _IsolationCtx:
    return _IsolationCtx(working_dir=cwd, cleanup=lambda: None)


def _is_git_repo(p: Path) -> bool:
    try:
        r = subprocess.run(
            ["git", "-C", str(p), "rev-parse", "--is-inside-work-tree"],
            capture_output=True,
            text=True,
        )
        return r.returncode == 0 and r.stdout.strip() == "true"
    except FileNotFoundError:
        return False


def _setup_isolation(spec: WorkerSpec, job_id: str, queen_cwd: Path) -> _IsolationCtx:
    if spec.isolation == "tempdir":
        return _setup_tempdir(job_id)
    if spec.isolation == "worktree":
        base = Path(spec.worktree_base) if spec.worktree_base else queen_cwd
        return _setup_worktree(job_id, base)
    if spec.isolation == "cwd":
        return _setup_cwd(job_id, queen_cwd)
    raise ValueError(f"unknown isolation: {spec.isolation}")


# ------------------------------------------------------------- prompt build


def _build_system_prompt(spec: WorkerSpec, working_dir: Path) -> str | None:
    if spec.system_prompt is not None:
        return spec.system_prompt
    if spec.role not in KNOWN_ROLES:
        return None
    try:
        return render_prompt(
            spec.role,
            worktree_path=str(working_dir),
            depth=spec.depth,
            max_depth=spec.max_depth,
        )
    except UnknownRoleError:
        return None


def _build_args(spec: WorkerSpec, system_prompt: str | None) -> list[str]:
    args: list[str] = ["-p", spec.task]
    if spec.max_turns:
        args.extend(["--max-turns", str(spec.max_turns)])
    if spec.allowed_tools:
        args.extend(["--allowedTools", ",".join(spec.allowed_tools)])
    if system_prompt:
        args.extend(["--append-system-prompt", system_prompt])
    if spec.mcp_config_path:
        args.extend(["--mcp-config", spec.mcp_config_path])
    return args


# ------------------------------------------------------------------ run


def run_worker(
    spec: WorkerSpec,
    pool: AccountPool,
    budget: Budget | None = None,
    *,
    job_id: str | None = None,
    queen_cwd: Path | None = None,
    rate_limit_re: re.Pattern[str] | None = None,
) -> WorkerResult:
    """Synchronous worker execution.

    1. Pre-flight: budget check.
    2. Checkout an account from the pool (exclude_queen=True).
    3. Set up isolation (tempdir / worktree / cwd).
    4. Build args + system prompt.
    5. Run `claude -p` headlessly.
    6. Record cost, release account.
    7. Return WorkerResult.
    """
    job_id = job_id or f"job_{uuid.uuid4().hex[:12]}"
    queen_cwd = queen_cwd or Path.cwd()
    rate_limit_re = rate_limit_re or compile_patterns()
    started = time.time()

    # --- budget pre-flight ---------------------------------------------------
    if budget is not None:
        afford_check = _find_affordable_account(spec, pool, budget)
        if afford_check is None:
            return WorkerResult(
                job_id=job_id, account_name=None, role=spec.role,
                status="budget_exceeded",
                result_text=None, cost_usd=0.0,
                elapsed_seconds=time.time() - started,
                error="no account can afford max_cost_usd; "
                      "lower the cap, enable extra usage, or reset cycles",
                spec=spec.to_dict(),
            )

    # --- checkout ------------------------------------------------------------
    try:
        acct = pool.checkout(job_id=job_id, kind="headless", exclude_queen=True)
    except NoAccountAvailable as e:
        return WorkerResult(
            job_id=job_id, account_name=None, role=spec.role,
            status="no_account",
            result_text=None, cost_usd=0.0,
            elapsed_seconds=time.time() - started,
            error=str(e),
            spec=spec.to_dict(),
        )

    # --- isolation -----------------------------------------------------------
    iso = None
    try:
        iso = _setup_isolation(spec, job_id, queen_cwd)
        system_prompt = _build_system_prompt(spec, iso.working_dir)
        args = _build_args(spec, system_prompt)

        # --- run ---------------------------------------------------------------
        result = run_claude(
            account_dir=acct.dir,
            args=args,
            rate_limit_re=rate_limit_re,
            mode="headless",
            cwd=iso.working_dir,
            timeout_seconds=spec.timeout_seconds,
        )

        cost = float(result.cost_usd or 0.0)
        status = _classify(result, cost, spec)

        error = None
        if status == "errored":
            error = result.stderr_tail or f"exit_code={result.exit_code}"
        elif status == "timeout":
            error = "worker exceeded timeout_seconds"
        elif status == "rate_limited":
            error = result.reset_hint or "rate limited (no hint)"

        if budget is not None and cost > 0:
            try:
                budget.record_call(acct.name, cost)
            except KeyError:
                pass

        pool.release(
            acct.name, job_id,
            cost_usd=cost,
            rate_limited=(status == "rate_limited"),
            reset_hint=result.reset_hint,
            reset_time_iso=result.reset_time_iso,
        )

        return WorkerResult(
            job_id=job_id,
            account_name=acct.name,
            role=spec.role,
            status=status,
            result_text=result.result_text,
            cost_usd=cost,
            elapsed_seconds=result.elapsed_seconds,
            error=error,
            session_id=result.session_id,
            working_dir=str(iso.working_dir),
            spec=spec.to_dict(),
        )
    except Exception as exc:
        # Defensive: release the account even if something blew up.
        try:
            pool.release(acct.name, job_id, cost_usd=0.0, rate_limited=False)
        except Exception:
            pass
        return WorkerResult(
            job_id=job_id, account_name=acct.name, role=spec.role,
            status="errored", result_text=None, cost_usd=0.0,
            elapsed_seconds=time.time() - started,
            error=f"{type(exc).__name__}: {exc}",
            working_dir=str(iso.working_dir) if iso else None,
            spec=spec.to_dict(),
        )
    finally:
        # Tempdir cleanup. Worktrees stay around for the caller to inspect.
        if iso and not iso.worktree_added:
            try:
                iso.cleanup()
            except Exception:
                pass


def _classify(result: RunResult, cost: float, spec: WorkerSpec) -> WorkerStatus:
    if result.status == "rate_limited":
        return "rate_limited"
    if result.status == "error":
        if result.exit_code == 124:  # subprocess.TimeoutExpired path
            return "timeout"
        return "errored"
    # result.status == "exit"
    if cost > spec.max_cost_usd:
        return "errored"  # over budget; surface as error rather than silently completing
    return "completed"


def _find_affordable_account(spec: WorkerSpec, pool: AccountPool, budget: Budget) -> str | None:
    """Returns the name of the first warm, non-queen account that can afford
    spec.max_cost_usd. None if none can."""
    for acct in pool.all():
        if acct.is_cold or acct.is_in_use:
            continue
        if acct.name == pool.queen_name():
            continue
        if budget.can_afford(acct.name, spec.max_cost_usd):
            return acct.name
    return None


# ---------------------------------------------------------------- helpers


def write_mcp_config_for_orchestrator(
    job_id: str,
    hive_root: Path,
    state_dir: Path,
    config_file: Path | None = None,
) -> Path:
    """Write a temporary --mcp-config JSON that points the orchestrator at a
    fresh hive-mcp-server subprocess. State is file-backed, so multiple MCP
    server instances coordinate via flock on pool.json / budget.json."""
    cfg_path = Path(tempfile.gettempdir()) / f"hive-mcp-{job_id}.json"
    env = {"HIVE_STATE_DIR": str(state_dir)}
    if config_file:
        env["HIVE_CONFIG"] = str(config_file)
    payload = {
        "mcpServers": {
            "hive": {
                "command": str(hive_root / "bin" / "hive-mcp-server"),
                "args": [],
                "env": env,
            }
        }
    }
    cfg_path.write_text(json.dumps(payload, indent=2))
    return cfg_path
