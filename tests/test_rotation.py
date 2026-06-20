"""Rotation smoke test: feed the CLI loop a mocked runner that returns
rate-limit then clean-exit, and assert that the loop advances accounts and
returns the right exit code without touching the real `claude` binary.

Run with:  python3 tests/test_rotation.py   (from the repo root)
"""

from __future__ import annotations

import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hive import cli
from hive.account_pool import AccountPool
from hive.claude_runner import RunResult
from hive.rate_limit import compile_patterns


def main() -> int:
    accounts = [("a", "/tmp/.claude-a"), ("b", "/tmp/.claude-b"), ("c", "/tmp/.claude-c")]

    with tempfile.TemporaryDirectory() as td:
        state_path = Path(td) / "pool.json"
        pool = AccountPool(accounts, state_path, queen="a")
        # Start on idx 0
        pool.cli_set_idx(0)
        rate_limit_re = compile_patterns()

        # Reset times must be in the future for the "should be cold" assertions
        # to hold whenever the test runs — compute them relative to now.
        now = datetime.now(timezone.utc)
        reset_a = (now + timedelta(hours=5)).isoformat()
        reset_b = (now + timedelta(hours=6)).isoformat()

        # Mocked runner: rate-limit on a, rate-limit on b, exit cleanly on c with code 7.
        calls: list[str] = []
        scripted = [
            RunResult(status="rate_limited", exit_code=0, elapsed_seconds=1.0,
                      reset_hint="resets 2am (Africa/Lagos)",
                      reset_time_iso=reset_a),
            RunResult(status="rate_limited", exit_code=0, elapsed_seconds=1.0,
                      reset_hint="resets 3am (Africa/Lagos)",
                      reset_time_iso=reset_b),
            RunResult(status="exit", exit_code=7, elapsed_seconds=2.0, reset_hint=None),
        ]

        def fake_runner(account_dir, args, rate_limit_re, mode="interactive", **kwargs):
            calls.append(account_dir)
            return scripted.pop(0)

        with patch.object(cli, "run_claude", side_effect=fake_runner), \
             patch.object(cli, "find_latest_session", return_value=None), \
             patch.object(time, "sleep", return_value=None):  # don't sleep
            exit_code = cli._run_loop(pool, accounts, [], rate_limit_re)

        # Assertions inside the `with` so pool.json still exists.
        assert exit_code == 7, f"expected exit code 7, got {exit_code}"
        assert calls == [accounts[0][1], accounts[1][1], accounts[2][1]], \
            f"expected rotation a→b→c, got {calls}"
        a = pool.by_name("a")
        b = pool.by_name("b")
        c = pool.by_name("c")
        assert a.cold_until is not None and a.cold_until > time.time(), "a should be cold"
        assert b.cold_until is not None and b.cold_until > time.time(), "b should be cold"
        assert c.cold_until is None, f"c should NOT be cold, got {c.cold_until}"
        print("rotation test: PASSED")
        print(f"  calls: {[Path(d).name for d in calls]}")
        print(f"  a cold_until: {a.cold_until}  reason: {a.cold_reason!r}")
        print(f"  b cold_until: {b.cold_until}  reason: {b.cold_reason!r}")
        print(f"  c cold_until: {c.cold_until}")
    return 0


def test_exhaustion() -> int:
    """All accounts hit rate-limit fast → loop bails with exit 1."""
    accounts = [("a", "/tmp/.claude-a"), ("b", "/tmp/.claude-b")]
    with tempfile.TemporaryDirectory() as td:
        state_path = Path(td) / "pool.json"
        pool = AccountPool(accounts, state_path, queen="a")
        pool.cli_set_idx(0)
        rate_limit_re = compile_patterns()

        scripted = [
            RunResult(status="rate_limited", exit_code=0, elapsed_seconds=0.5,
                      reset_hint="resets 2am"),
            RunResult(status="rate_limited", exit_code=0, elapsed_seconds=0.5,
                      reset_hint="resets 3am"),
        ]

        def fake_runner(account_dir, args, rate_limit_re, mode="interactive", **kwargs):
            return scripted.pop(0)

        with patch.object(cli, "run_claude", side_effect=fake_runner), \
             patch.object(cli, "find_latest_session", return_value=None), \
             patch.object(time, "sleep", return_value=None):
            exit_code = cli._run_loop(pool, accounts, [], rate_limit_re)

    assert exit_code == 1, f"expected exit code 1 (exhaustion), got {exit_code}"
    print("exhaustion test: PASSED")
    return 0


if __name__ == "__main__":
    main()
    test_exhaustion()
    print("\nALL TESTS PASSED")
