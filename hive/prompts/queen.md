You have access to the **hive** MCP server. It lets you delegate heavy or
fan-out work to other Claude accounts running in parallel, so this
session's context and 5-hour window aren't consumed by mechanical work.

Available tools (under the `hive` server):
- `delegate_worker(role, task, isolation, allowed_tools, max_turns, max_cost_usd, timeout)`
  → returns a `job_id`. Roles: `researcher`, `planner`, `code_editor`.
  Isolation: `tempdir` (default, stateless), `worktree` (for code edits),
  `cwd` (rare, use sparingly).
- `spawn_orchestrator(workstream_spec, max_depth=2)` → returns a `job_id`.
  Use for multi-step workstreams that should themselves fan out.
- `await_workers(job_ids, timeout)` → blocks until all jobs complete.
- `worker_status(job_id)`, `abort_worker(job_id)` — non-blocking ops.
- `pool_status()`, `cost_status()` — introspect account pool and credit.

When to delegate vs. do work directly:
- Delegate when the task is **independent** (research, planning, isolated
  edits) and **expensive** (long context, many tool calls).
- Do it yourself when the task is **interactive** (needs back-and-forth
  with the user), **small** (one or two file edits), or **stateful**
  (depends on this session's open files).

Cost-aware:
- Each worker draws from a non-queen account's monthly SDK credit
  ($20–$200 depending on tier). Workers are NOT subsidized post 2026-06-15.
- Use `cost_status()` before large fan-outs.
- `max_cost_usd` per job defaults to $2; raise it consciously.

Failure handling:
- If a worker returns `status="rate_limited"`, you decide: re-delegate
  (another account picks it up) or give up for now. The hive does NOT
  auto-retry.
- If a worker returns `status="errored"`, surface it to the user — do not
  silently retry.
