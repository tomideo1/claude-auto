"""The hive MCP server.

Run as: `python -m hive.hive_controller` or via `bin/hive-mcp-server`.

Exposes the queen-facing tools defined in the spec section 4.6:
    delegate_worker, spawn_orchestrator, await_workers,
    worker_status, abort_worker, pool_status, cost_status

Job state is held in-memory (job_id → concurrent.futures.Future). On
completion, the WorkerResult is persisted to `state/jobs/<job_id>.json`
for post-mortem.
"""

from __future__ import annotations

import concurrent.futures as cf
import json
import os
import signal
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from .account_pool import AccountPool
from .budget import Budget
from .rate_limit import compile_patterns
from .worker import (
    WorkerResult,
    WorkerSpec,
    run_worker,
    write_mcp_config_for_orchestrator,
)


def _load_runtime_config():
    """Read environment-driven config. Mirrors cli.load_config but tolerates
    being run as an MCP server subprocess where CWD may differ."""
    from .cli import load_config, _state_dir  # local import to avoid cycle at top
    accounts, patterns, queen = load_config()
    return accounts, patterns, queen, _state_dir()


class HiveController:
    """Stateful controller: holds the pool, budget, executor, job map."""

    def __init__(self):
        accounts, patterns, queen, state_dir = _load_runtime_config()
        self.accounts = accounts
        self.queen_name = queen
        self.state_dir = state_dir
        self.hive_root = Path(__file__).resolve().parent.parent
        self.rate_limit_re = compile_patterns(patterns)
        self.pool = AccountPool(accounts, state_dir / "pool.json", queen=queen)
        self.budget = Budget(state_dir / "budget.json", [n for n, _ in accounts])
        worker_pool_size = max(1, len(accounts) - 1)
        self.executor = cf.ThreadPoolExecutor(
            max_workers=worker_pool_size,
            thread_name_prefix="hive-worker",
        )
        self.jobs: dict[str, cf.Future] = {}
        self.jobs_meta: dict[str, dict] = {}
        self._lock = threading.Lock()
        (state_dir / "jobs").mkdir(parents=True, exist_ok=True)
        # Make sure orphaned worker subprocesses get cleaned up when the
        # controller dies (e.g., queen crash → MCP server shutdown).
        try:
            os.setpgrp()
        except OSError:
            pass

    # ---------------------------------------------------------------- helpers

    def _persist_meta(self, job_id: str) -> None:
        """Write the current meta to state/jobs/<job_id>.json. Called on every
        transition so the `hive watch` dashboard can show in-flight jobs."""
        with self._lock:
            payload = dict(self.jobs_meta.get(job_id, {}))
        if not payload:
            return
        payload.setdefault("job_id", job_id)
        path = self.state_dir / "jobs" / f"{job_id}.json"
        try:
            path.write_text(json.dumps(payload, indent=2, default=str))
        except OSError:
            pass

    def _persist_job(self, job_id: str, result: WorkerResult) -> None:
        """Final persist: merges meta + WorkerResult."""
        with self._lock:
            meta = dict(self.jobs_meta.get(job_id, {}))
        payload = {
            **meta,
            **result.to_dict(),
            "persisted_at": time.time(),
        }
        path = self.state_dir / "jobs" / f"{job_id}.json"
        try:
            path.write_text(json.dumps(payload, indent=2, default=str))
        except OSError:
            pass

    def _meta(self, job_id: str) -> dict:
        with self._lock:
            return dict(self.jobs_meta.get(job_id, {}))

    def _record_meta(self, job_id: str, **changes) -> None:
        with self._lock:
            m = self.jobs_meta.setdefault(job_id, {})
            m.update(changes)
            m["job_id"] = job_id
        # Persist every transition so external observers can see live state.
        self._persist_meta(job_id)

    # ---------------------------------------------------------------- tools

    def delegate_worker(
        self,
        role: str,
        task: str,
        isolation: str = "tempdir",
        allowed_tools: list[str] | None = None,
        max_turns: int = 40,
        max_cost_usd: float = 2.0,
        timeout_seconds: int = 600,
        system_prompt: str | None = None,
    ) -> str:
        """Spawn a single worker. Returns the job_id; non-blocking."""
        spec = WorkerSpec(
            role=role,
            task=task,
            isolation=isolation,  # type: ignore[arg-type]
            allowed_tools=allowed_tools,
            max_turns=max_turns,
            max_cost_usd=max_cost_usd,
            timeout_seconds=timeout_seconds,
            system_prompt=system_prompt,
            depth=0,
            max_depth=2,
        )
        return self._submit(spec)

    def spawn_orchestrator(
        self,
        workstream_spec: str,
        max_depth: int = 2,
        max_cost_usd: float = 5.0,
        timeout_seconds: int = 1800,
    ) -> str:
        """Spawn an orchestrator worker that can itself call delegate_worker
        via a fresh hive-mcp-server subprocess."""
        job_id = f"orch_{uuid.uuid4().hex[:12]}"
        mcp_cfg_path = write_mcp_config_for_orchestrator(
            job_id=job_id,
            hive_root=self.hive_root,
            state_dir=self.state_dir,
        )
        spec = WorkerSpec(
            role="orchestrator",
            task=workstream_spec,
            isolation="tempdir",
            max_turns=80,
            max_cost_usd=max_cost_usd,
            timeout_seconds=timeout_seconds,
            mcp_config_path=str(mcp_cfg_path),
            depth=1,
            max_depth=max_depth,
        )
        return self._submit(spec, job_id=job_id)

    def _submit(self, spec: WorkerSpec, job_id: str | None = None) -> str:
        job_id = job_id or f"job_{uuid.uuid4().hex[:12]}"
        self._record_meta(
            job_id,
            status="queued",
            spec=spec.to_dict(),
            queued_at=time.time(),
        )
        future = self.executor.submit(self._run_and_log, spec, job_id)
        with self._lock:
            self.jobs[job_id] = future
        return job_id

    def _run_and_log(self, spec: WorkerSpec, job_id: str) -> WorkerResult:
        self._record_meta(job_id, status="running", started_at=time.time())
        try:
            result = run_worker(
                spec=spec,
                pool=self.pool,
                budget=self.budget,
                job_id=job_id,
                rate_limit_re=self.rate_limit_re,
            )
        except Exception as e:
            result = WorkerResult(
                job_id=job_id,
                account_name=None,
                role=spec.role,
                status="errored",
                result_text=None,
                cost_usd=0.0,
                elapsed_seconds=0.0,
                error=f"controller exception: {type(e).__name__}: {e}",
                spec=spec.to_dict(),
            )
        self._record_meta(
            job_id,
            status=result.status,
            finished_at=time.time(),
            account=result.account_name,
            cost_usd=result.cost_usd,
        )
        self._persist_job(job_id, result)
        return result

    def await_workers(self, job_ids: list[str], timeout: int = 900) -> list[dict]:
        end = time.time() + timeout
        results: list[dict] = []
        for jid in job_ids:
            with self._lock:
                fut = self.jobs.get(jid)
            if fut is None:
                # Maybe the job ran in a prior controller instance — check disk.
                persisted = self.state_dir / "jobs" / f"{jid}.json"
                if persisted.exists():
                    results.append(json.loads(persisted.read_text()))
                else:
                    results.append({"job_id": jid, "status": "unknown",
                                    "error": "no such job"})
                continue
            remaining = max(0.1, end - time.time())
            try:
                r = fut.result(timeout=remaining)
                results.append(r.to_dict())
            except cf.TimeoutError:
                results.append({
                    "job_id": jid, "status": "timeout",
                    "error": f"await timeout after {timeout}s",
                })
        return results

    def worker_status(self, job_id: str) -> dict:
        meta = self._meta(job_id)
        with self._lock:
            fut = self.jobs.get(job_id)
        if fut is None and not meta:
            persisted = self.state_dir / "jobs" / f"{job_id}.json"
            if persisted.exists():
                return json.loads(persisted.read_text())
            return {"job_id": job_id, "status": "unknown"}
        snapshot = {"job_id": job_id, **meta}
        if fut is not None:
            snapshot["done"] = fut.done()
            snapshot["cancelled"] = fut.cancelled()
        return snapshot

    def abort_worker(self, job_id: str) -> bool:
        """Best-effort cancel. cf.Future can only cancel pre-start; once the
        subprocess is running we'd need to track its pid. Phase 2 returns
        a cancel attempt; full PID tracking is Phase 4."""
        with self._lock:
            fut = self.jobs.get(job_id)
        if fut is None:
            return False
        return fut.cancel()

    def pool_status(self) -> dict:
        return self.pool.status()

    def cost_status(self) -> dict:
        return self.budget.status()


# ----------------------------------------------------------------- MCP setup


def _build_mcp(controller: HiveController):
    """Construct the FastMCP server wired to a controller. Importing FastMCP
    lazily keeps `python -c 'import hive.hive_controller'` cheap when the
    user doesn't actually need the server."""
    from mcp.server.fastmcp import FastMCP

    mcp = FastMCP("hive")

    @mcp.tool()
    def delegate_worker(
        role: str,
        task: str,
        isolation: str = "tempdir",
        allowed_tools: list[str] | None = None,
        max_turns: int = 40,
        max_cost_usd: float = 2.0,
        timeout_seconds: int = 600,
        system_prompt: str | None = None,
    ) -> str:
        """Spawn a single worker. Returns a job_id (use await_workers to
        block for the result). Role is one of: researcher, planner,
        code_editor. Isolation is one of: tempdir, worktree, cwd."""
        return controller.delegate_worker(
            role=role, task=task, isolation=isolation,
            allowed_tools=allowed_tools, max_turns=max_turns,
            max_cost_usd=max_cost_usd, timeout_seconds=timeout_seconds,
            system_prompt=system_prompt,
        )

    @mcp.tool()
    def spawn_orchestrator(
        workstream_spec: str,
        max_depth: int = 2,
        max_cost_usd: float = 5.0,
        timeout_seconds: int = 1800,
    ) -> str:
        """Spawn an orchestrator worker that can itself delegate sub-workers."""
        return controller.spawn_orchestrator(
            workstream_spec=workstream_spec, max_depth=max_depth,
            max_cost_usd=max_cost_usd, timeout_seconds=timeout_seconds,
        )

    @mcp.tool()
    def await_workers(job_ids: list[str], timeout: int = 900) -> list[dict]:
        """Block until all jobs complete or timeout. Returns list of WorkerResult dicts."""
        return controller.await_workers(job_ids, timeout=timeout)

    @mcp.tool()
    def worker_status(job_id: str) -> dict:
        """Non-blocking status check for a single job."""
        return controller.worker_status(job_id)

    @mcp.tool()
    def abort_worker(job_id: str) -> bool:
        """Best-effort cancel."""
        return controller.abort_worker(job_id)

    @mcp.tool()
    def pool_status() -> dict:
        """Snapshot the account pool: warm/cold accounts, in-use, last-used times."""
        return controller.pool_status()

    @mcp.tool()
    def cost_status() -> dict:
        """Per-account SDK credit usage against the monthly cap."""
        return controller.cost_status()

    return mcp


def main() -> int:
    controller = HiveController()
    # Print a one-line startup banner to stderr so the queen/log shows we're up.
    sys.stderr.write(
        f"[hive] controller up: state={controller.state_dir}, "
        f"queen={controller.queen_name}, workers={controller.executor._max_workers}\n"
    )
    sys.stderr.flush()
    mcp = _build_mcp(controller)
    mcp.run()  # blocks; reads stdio
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
