# claude-auto

**Never get stopped by a Claude Code usage limit again.**

`claude-auto` is a thin wrapper around the `claude` CLI that rotates across
several Claude accounts automatically. When the account you're on hits its
usage limit, it transparently switches to the next account **and resumes the
exact same conversation** — because session history is shared across accounts
and keyed by your working directory.

You type once. It keeps going.

```
→ Account: personal (1/2)
… you work normally …
⚡ personal hit usage limit (resets 2am). Switching to work and resuming...
→ Account: work (2/2)
… same conversation, picks up where you left off …
```

> **This does not bypass or raise any limit.** Each account is used strictly
> within its own normal usage window. `claude-auto` only saves you from manually
> switching accounts and re-pasting context when one window is spent. If you
> only have one account, there's nothing to rotate to.

---

## Why this exists

Each Claude subscription has its own rolling usage window. If you have access to
more than one account (e.g. a personal plan and a work plan), they reset on
independent clocks. `claude-auto` treats them as one pool: when one is cold, it
falls through to the next warm one without losing your place.

Two ideas make it work:

1. **An account is just a config directory.** `claude` reads all of its
   auth/session state from `$CLAUDE_CONFIG_DIR`. Point it at `~/.claude-personal`
   and you're the personal account; point it at `~/.claude-work` and you're the
   work account. `claude-auto` launches `claude` with the right
   `CLAUDE_CONFIG_DIR` for whichever account is currently warm.

2. **Sessions are shared and addressed by directory.** Claude stores each
   conversation as `projects/<encoded-cwd>/<session-uuid>.jsonl`, where the cwd
   is path-encoded (`/Users/you/code/app` → `-Users-you-code-app`). `claude-auto`
   symlinks every account's `projects/` (and friends) into one
   `~/.claude-shared` folder, so a session started under account A is visible to
   account B. On rotation it finds the newest session file for your current
   directory and relaunches the next account with `--resume <that-id>`.

That's the whole trick: **swap the config dir, resume by directory.**

---

## What it does on a rate limit

```
  ┌──────────────────────────────────────────────────────────┐
  │  launch `claude` with CLAUDE_CONFIG_DIR = current account  │
  └───────────────┬──────────────────────────────────────────┘
                  │  watches the terminal output (PTY) for a
                  │  "you've hit your limit · resets …" banner
                  ▼
        ┌───────────────────┐   no banner, you quit normally
        │  rate-limit hit?  │ ───────────────────────────────► exit
        └─────────┬─────────┘
                  │ yes
                  ▼
   mark account "cold until <reset time>"   (parsed from the banner;
                  │                          falls back to +5h)
                  ▼
   advance to the next account in the pool
                  │
                  ▼
   find newest session .jsonl for $PWD  ──►  relaunch with --resume <id>
                  │                          (or --continue if none found)
                  ▼
            (loop back to top)
```

If **every** account is cold (it cycled through all of them and each was already
rate-limited on launch), it gives up with a summary of when each one resets,
rather than spinning forever.

---

## Install

### Requirements

- Python 3.11+
- The `claude` CLI on your `PATH`
- `git` and `rsync`
- Two or more Claude accounts you can log into

### 1. Get the code

```bash
git clone https://github.com/<you>/claude-auto ~/claude-auto
ln -s ~/claude-auto/bin/claude-auto ~/.local/bin/claude-auto
# make sure ~/.local/bin is on your PATH
```

(Clone it wherever you like — state is stored next to the code by default, so
nothing is hardcoded to a specific path. Override with `$HIVE_STATE_DIR` if you
want state elsewhere.)

### 2. Create one config dir per account and log in

Each account is just a directory. Create one per account and authenticate into
each by running plain `claude` with that dir as its config home:

```bash
CLAUDE_CONFIG_DIR=~/.claude-personal claude   # log in as your personal account
CLAUDE_CONFIG_DIR=~/.claude-work     claude   # log in as your work account
# …repeat for each account, then /exit
```

Optional convenience aliases for your `~/.zshrc` / `~/.bashrc`:

```bash
alias claude-personal='CLAUDE_CONFIG_DIR=~/.claude-personal claude'
alias claude-work='CLAUDE_CONFIG_DIR=~/.claude-work claude'
```

### 3. Tell claude-auto about your accounts

Create `~/.claude-auto.json`:

```json
{
  "accounts": [
    { "name": "personal", "dir": "/Users/you/.claude-personal" },
    { "name": "work",     "dir": "/Users/you/.claude-work" }
  ]
}
```

The order is the rotation order. There is **no** built-in default list — if this
file is missing, `claude-auto` prints a first-run hint and exits.

### 4. Share sessions across accounts (one-time)

```bash
claude-auto --migrate          # add --dry-run first to preview
```

This rsyncs each account's `projects/`, `todos/`, `history.jsonl`, etc. into
`~/.claude-shared` and replaces the originals with symlinks. After this, any
account can resume any account's conversations. Originals are backed up as
`*.premigration-bak`, so it's reversible.

> The merge is non-destructive (`rsync --ignore-existing`, first-seen wins on a
> filename collision), so it's safe to re-run as you add accounts.

### 5. Use it

```bash
claude-auto                    # rotates automatically; just use Claude normally
```

Pass-through args go straight to `claude` (use `--` to be explicit):

```bash
claude-auto -- --model opus
claude-auto -- -p "summarize this repo"
```

Pin a specific account for one run:

```bash
claude-auto --account work
```

---

## Commands

| Command | What it does |
|---|---|
| `claude-auto` | Launch with auto-rotation (the main use) |
| `claude-auto --account NAME` | Start pinned to one account |
| `claude-auto --list` | List configured accounts (✓ = dir exists) |
| `claude-auto --status` | Show current account + shared dir |
| `claude-auto --migrate [--dry-run]` | One-time session-sharing migration |
| `claude-auto -h` | Help + the resolved account list |

Pass `--resume`, `-r`, `--continue`, or `-c` yourself and it won't second-guess
you — it only auto-injects a resume target when *it* is the one rotating.

---

## How session resume picks the right conversation

When `claude-auto` switches accounts mid-flight, it does **not** start a fresh
chat. It:

1. Encodes your current directory: `/Users/you/code/app` → `-Users-you-code-app`.
2. Looks for `*.jsonl` session files under that key — first in
   `~/.claude-shared/projects/<key>/`, then in each account's own
   `projects/<key>/` (for anything not yet migrated).
3. Picks the **most recently modified** one and relaunches the next account with
   `--resume <its-uuid>`.
4. If it finds nothing for this directory, it falls back to `--continue`.

Because the lookup is per-directory, two different projects keep two independent
rolling conversations — switching accounts in `~/code/app` never drags in the
chat you were having in `~/code/other`.

---

## Configuration reference (`~/.claude-auto.json`)

```json
{
  "accounts": [
    { "name": "personal", "dir": "/Users/you/.claude-personal" },
    { "name": "work",     "dir": "/Users/you/.claude-work" }
  ],
  "rate_limit_patterns": [
    "you'?ve\\s+hit\\s+your\\s+limit.{0,8}resets"
  ],
  "queen": "personal"
}
```

- **`accounts`** *(required)* — ordered list of `{name, dir}`. Rotation follows
  this order.
- **`rate_limit_patterns`** *(optional)* — extra regexes for detecting the limit
  banner, in case Claude's wording changes. Sensible defaults are built in.
- **`queen`** *(optional)* — only relevant to the advanced "hive" mode below.

---

## Advanced: hive mode (optional)

The same package ships a multi-agent layer: one interactive **queen** account
delegates headless worker jobs (`claude -p`) onto the *other* accounts via an
MCP server, spreading load across the pool. This is separate from the everyday
rotation above and entirely optional.

```bash
claude-auto hive status        # per-account warm/cold/in-use snapshot
claude-auto hive watch         # live dashboard
claude-auto hive cost-report   # per-account credit usage
claude-auto hive doctor        # environment health checks
```

See [`USAGE.md`](USAGE.md) for the hive architecture and setup. If you only want
"don't stop at the usage limit," ignore this section entirely.

---

## How it's built

| File | Responsibility |
|---|---|
| `bin/claude-auto` | Tiny launcher → `hive.cli:main` |
| `hive/cli.py` | Arg parsing + the rotation loop |
| `hive/claude_runner.py` | Spawns `claude` under a PTY, watches for the limit banner |
| `hive/account_pool.py` | `flock`-protected `state/pool.json` — which accounts are warm/cold |
| `hive/share_migration.py` | `--migrate` + the per-directory session lookup |
| `hive/rate_limit.py` | Banner regexes + reset-time parsing |
| `hive/{budget,worker,hive_controller}.py` | Optional hive (multi-agent) layer |

Rotation state lives in `state/pool.json` (per-account `last_used_at`,
`cold_until`, `cold_reason`), next to the code by default. Cold timers are
derived from the reset time in the banner when parseable, otherwise a 5-hour
fallback.

Run the tests:

```bash
python3 tests/test_rotation.py
```

---

## FAQ

**Does this bypass usage limits?**
No. Each account is used within its own normal limits. This only saves you from
manually switching accounts and re-pasting context when one window is spent.

**Is my conversation data safe across accounts?**
Sessions live on your machine under `~/.claude-shared`. Migration is
non-destructive and backs up originals. The wrapper uploads nothing.

**Should I be doing this?**
Check the terms of the Claude plans you hold. `claude-auto` is a convenience
wrapper for someone who legitimately has access to multiple accounts; it doesn't
circumvent any technical or contractual limit.

**macOS / Linux only?**
Yes — interactive mode uses a Unix PTY (`pty.fork`). No native Windows support
(WSL works).

---

## License

MIT — see [`LICENSE`](LICENSE).
