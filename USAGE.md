# Using hive mode (advanced)

> This is the **optional** multi-agent layer. If you just want auto-rotation on
> rate limits, see [`README.md`](README.md) — you can ignore this entire file.

Hive mode turns your account pool into a small agent fleet: one interactive
**queen** account delegates headless worker jobs (`claude -p`) onto the *other*
accounts via an MCP server, so heavy work spreads across the pool instead of
burning down a single account.

## TL;DR

```bash
claude-auto queen              # launch interactive on the queen account,
                               # with the hive MCP server auto-loaded
```

Once inside the queen's Claude Code session, you have a `hive` MCP server with 7
tools. Ask the queen to delegate work, e.g.:

> "Use the hive to find me the top 5 LangGraph alternatives. Delegate a
> researcher worker, await the result, summarize for me."

The queen calls `delegate_worker(role="researcher", task="...")` then
`await_workers([job_id])`. The worker runs on a non-queen account, draws from
that account's SDK credit, and returns its result.

## The architecture

```
   you  ─────►  queen (interactive)
                 │
                 │ MCP
                 ▼
            hive-mcp-server  ──────►  worker A (claude -p, account=work)
            (bin/hive-mcp-server)  ─►  worker B (claude -p, account=personal)
                                   ─►  worker C (claude -p, account=…)
```

The **queen** is whichever account you set as `"queen"` in `~/.claude-auto.json`.
Workers are checked out from the *other* configured accounts.

State lives in the repo's `state/` directory (override with `$HIVE_STATE_DIR`):
- `pool.json` — per-account warm/cold/in-use status (flock-protected)
- `budget.json` — per-account monthly SDK credit usage
- `jobs/<job_id>.json` — completed worker results (post-mortem)

## Setup

Hive mode needs a few extra things beyond the everyday rotation:

1. **A queen.** Add `"queen": "<account-name>"` to `~/.claude-auto.json`.
2. **The MCP SDK.** Create a venv in the repo and install it:
   ```bash
   cd ~/claude-auto
   python3 -m venv .venv
   .venv/bin/pip install "mcp>=0.9"
   ```
   `bin/hive-mcp-server` invokes `.venv/bin/python -m hive.hive_controller`.
3. **Wire the MCP server into the queen.** Add a `hive` entry to the queen's
   `mcpServers` in `~/.claude-<queen>/settings.json` pointing at
   `~/claude-auto/bin/hive-mcp-server`.

Run `claude-auto hive doctor` to verify all of the above.

## Launching the queen

```bash
claude-auto queen              # convenience subcommand
claude-auto --account <queen>  # equivalent
```

Verify the MCP server loads by running `/mcp` once inside the queen. You should
see `hive` listed as a connected server with 7 tools.

## What the queen can do

Inside the queen's session the hive tools are available under the `hive` MCP
namespace (Claude Code surfaces them as `mcp__hive__<tool>`):

### `delegate_worker(role, task, …)`
Spawn a single worker. Returns a `job_id`.

| arg | default | notes |
|---|---|---|
| `role` | — | `researcher`, `planner`, `code_editor` |
| `task` | — | the prompt |
| `isolation` | `tempdir` | `tempdir`, `worktree`, or `cwd` |
| `allowed_tools` | `null` | e.g. `["Read","Bash(npm:*)"]` |
| `max_turns` | 40 | cap on the worker's agent loop |
| `max_cost_usd` | 2.00 | hard cost cap; worker returns `errored` if exceeded |
| `timeout_seconds` | 600 | wallclock cap |
| `system_prompt` | `null` | override the role template |

### `spawn_orchestrator(workstream_spec, max_depth=2)`
Spawn a meta-worker that can itself call `delegate_worker`. The orchestrator
gets a fresh hive-mcp connection so its decisions don't race the queen's. Depth
is hard-capped (default 2).

### `await_workers(job_ids, timeout=900)`
Block until all jobs complete (or timeout). Returns a list of `WorkerResult`
dicts.

### `worker_status(job_id)` / `abort_worker(job_id)`
Non-blocking status check / best-effort cancel.

### `pool_status()` / `cost_status()`
Snapshots of the account pool and per-account credit usage.

## Example queen prompts

**Single researcher:**
> "Delegate a researcher to find the top 5 alternatives to LangGraph, with
> one-line rationales. Await and summarize."

**Parallel fan-out:**
> "Delegate 3 researchers in parallel: (1) latest CRDT libraries, (2) latest OT
> libraries, (3) latest yjs alternatives. Await all and rank by
> production-readiness."

**Code edit with worktree:**
> "Delegate a code_editor with worktree isolation on this repo to add retry
> logic to `api/client.py`. Wait for the commit summary and tell me the worktree
> path so I can review."

**Orchestrator:**
> "Spawn an orchestrator to research and implement an OAuth provider integration
> for our app. Let it decompose the workstream itself."

## Inspecting / managing the hive (CLI)

```bash
claude-auto hive status              # pool snapshot: warm/cold/busy + last-used
claude-auto hive watch               # live dashboard
claude-auto hive cost-report         # per-account credit usage
claude-auto hive doctor              # full environment health check
claude-auto hive reset-budget NAME   # manual budget cycle reset
claude-auto hive prune-worktrees     # list leftover git worktrees
                                     # (HIVE_PRUNE_CONFIRM=yes to actually remove)
```

## Cost notes

Each worker draws from the account's **per-account monthly SDK credit pool**
(separate from the interactive subscription window):

| Tier | Monthly cap |
|---|---|
| Pro | $20 |
| Max 5x / Team Premium | $100 |
| Max 20x / Enterprise Premium | $200 |
| Team Standard | $20 |

Default cap is **$200** (Max 20x). Configure per-account in
`~/.claude-auto.json`:

```json
{
  "queen": "personal",
  "accounts": [ ... ],
  "budget": {
    "personal": { "cap_usd": 200, "extra_usage": { "enabled": false } },
    "work":     { "cap_usd": 100 }
  }
}
```

The queen's interactive REPL is **not** affected by SDK credit caps — it draws
from the subscription's interactive usage window as before.

## Troubleshooting

### "Not logged in" from a worker
The hive does **not** pass `--bare` — that flag disables OAuth/keychain auth and
only works with raw API keys. Each account must be logged in via
`CLAUDE_CONFIG_DIR=~/.claude-<acct> claude /login`.

Verify (replace with your account dir names):
```bash
for d in personal work; do CLAUDE_CONFIG_DIR=~/.claude-$d claude -p "say ok"; done
```

### Worker hit a rate limit
`pool.cold_until` is set on that account; subsequent checkouts skip it until
thaw. Re-delegate explicitly — the hive does **not** auto-retry workers (that
would mask cost issues).

### Roll back the symlink
```bash
rm ~/.local/bin/claude-auto
# re-link to wherever you cloned the repo, or restore your previous binary
```

## Layout

```
claude-auto/
├── README.md                  everyday rotation (start here)
├── USAGE.md                   ← hive mode (you are here)
├── LICENSE
├── pyproject.toml
├── .venv/                     mcp SDK lives here (you create this)
├── bin/
│   ├── claude-auto            → symlink into ~/.local/bin/claude-auto
│   └── hive-mcp-server        → wired into the queen's settings.json
├── hive/
│   ├── rate_limit.py
│   ├── claude_runner.py       PTY + headless modes
│   ├── account_pool.py        flock-protected pool.json
│   ├── budget.py              flock-protected budget.json
│   ├── worker.py              WorkerSpec, run_worker, isolation
│   ├── hive_controller.py     FastMCP server, ThreadPoolExecutor jobs
│   ├── share_migration.py     --migrate logic
│   ├── cli.py                 entry point + hive subcommands
│   └── prompts/               role system-prompt templates
├── state/                     gitignored (pool.json, budget.json, jobs/)
└── tests/
    ├── test_rotation.py       CLI rotation under a mocked rate-limit
    ├── test_worker_e2e.py     real claude -p worker
    └── test_mcp_stdio.py      MCP server JSON-RPC handshake
```
