"""End-to-end test: spawn a single real worker via `claude -p` on a
non-queen account and verify the result, cost tracking, and pool release.

Run with:  ~/projects/hive/.venv/bin/python tests/test_worker_e2e.py
(or system python — the test doesn't need mcp installed.)
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hive.account_pool import AccountPool
from hive.budget import Budget
from hive.cli import load_config, _state_dir
from hive.worker import WorkerSpec, run_worker


def main() -> int:
    accounts, _, queen = load_config()
    pool = AccountPool(accounts, _state_dir() / "pool.json", queen=queen)
    budget = Budget(_state_dir() / "budget.json", [n for n, _ in accounts])

    print(f"queen account: {queen}")
    print(f"worker pool   : {[n for n, _ in accounts if n != queen]}")

    spec = WorkerSpec(
        role="researcher",
        task="Reply with exactly: ok",
        isolation="tempdir",
        allowed_tools=[],   # disallow all tools — pure text reply
        max_turns=1,
        max_cost_usd=0.50,
        timeout_seconds=120,
    )

    print(f"\nspec: role={spec.role}, task={spec.task!r}")
    print(f"max_cost_usd={spec.max_cost_usd}, max_turns={spec.max_turns}")
    print()

    started = time.time()
    result = run_worker(spec, pool, budget=budget)
    elapsed = time.time() - started

    print(f"=== RESULT ({elapsed:.1f}s) ===")
    print(f"status       : {result.status}")
    print(f"account      : {result.account_name}")
    print(f"cost_usd     : ${result.cost_usd:.6f}")
    print(f"elapsed      : {result.elapsed_seconds:.1f}s")
    print(f"working_dir  : {result.working_dir}")
    print(f"session_id   : {result.session_id}")
    print(f"result_text  : {result.result_text!r}")
    if result.error:
        print(f"error        : {result.error}")

    print("\n=== POOL after release ===")
    snap = pool.status()
    for a in snap["accounts"]:
        if a["name"] == result.account_name:
            print(f"  {a['name']}: in_use_by_job={a['in_use_by_job']}, "
                  f"last_used={time.strftime('%H:%M:%S', time.localtime(a['last_used_at'])) if a['last_used_at'] else '—'}, "
                  f"cold_until={a['cold_until']}")

    print("\n=== BUDGET after record ===")
    bsnap = budget.status()
    for a in bsnap["accounts"]:
        if a["name"] == result.account_name:
            print(f"  {a['name']}: used=${a['used_usd']:.6f}, "
                  f"remaining=${a['remaining_usd']:.4f}, calls={a['call_count']}")

    # Assertions: don't fail hard on the actual content (Claude may say "ok" or "Ok"),
    # but do verify the structural invariants.
    assert result.account_name != queen, f"worker ran on queen {queen}!"
    assert result.status in ("completed", "rate_limited", "errored"), result.status
    if result.status == "completed":
        assert result.cost_usd >= 0, "cost should be reported"
        assert result.result_text, "result_text should be populated on completed"
    return 0 if result.status == "completed" else 1


if __name__ == "__main__":
    sys.exit(main())
