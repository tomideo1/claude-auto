"""End-to-end controller test.

Spawns a worker through HiveController.delegate_worker (the exact path the
queen uses via MCP), and verifies that:
  1. state/jobs/<job_id>.json appears immediately with status='queued'
     or 'running' — the watch dashboard can see in-flight jobs.
  2. After awaiting, status becomes 'completed' (or 'rate_limited'/'errored').

Run with: ~/projects/hive/.venv/bin/python tests/test_controller_e2e.py
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hive.hive_controller import HiveController


def _read_job(state_dir: Path, job_id: str) -> dict | None:
    p = state_dir / "jobs" / f"{job_id}.json"
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text())
    except (OSError, json.JSONDecodeError):
        return None


def main() -> int:
    controller = HiveController()
    print(f"queen        : {controller.queen_name}")
    print(f"state_dir    : {controller.state_dir}")
    print(f"worker pool  : {[n for n, _ in controller.accounts if n != controller.queen_name]}")
    print()

    job_id = controller.delegate_worker(
        role="researcher",
        task="Reply with exactly: ok",
        max_turns=1,
        max_cost_usd=0.50,
        timeout_seconds=120,
        allowed_tools=[],
    )
    print(f"submitted: {job_id}")

    # Read disk — verify the watch dashboard would see this job. We allow a
    # brief retry window because the executor thread may also be writing.
    early = None
    for _ in range(20):
        early = _read_job(controller.state_dir, job_id)
        if early is not None:
            break
        time.sleep(0.05)
    assert early is not None, "job file should exist within 1s of submit"
    early_status = early.get("status")
    print(f"  early state on disk: status={early_status}, "
          f"role={early.get('spec', {}).get('role')}, "
          f"task={early.get('spec', {}).get('task')!r}")
    assert early_status in ("queued", "running"), f"unexpected early status: {early_status}"

    # Block until done.
    results = controller.await_workers([job_id], timeout=180)
    final = results[0]
    print()
    print(f"final state  : status={final['status']}, account={final.get('account_name')}, "
          f"cost=${final.get('cost_usd', 0):.4f}")
    print(f"result_text  : {final.get('result_text')!r}")

    # Re-read disk: persisted view should match.
    disk = _read_job(controller.state_dir, job_id)
    assert disk is not None
    assert disk.get("status") == final["status"], (disk.get("status"), final["status"])

    if final["status"] == "completed":
        print()
        print("controller E2E: PASSED")
        return 0
    print()
    print(f"controller E2E: FAILED — final status={final['status']}, error={final.get('error')!r}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
