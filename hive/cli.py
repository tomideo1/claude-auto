"""claude-auto CLI entry point.

Usage
-----
  claude-auto [--account NAME] [-- ...claude args...]
  claude-auto --migrate           # one-time migration into ~/.claude-shared
  claude-auto --status            # show current account + shared dir
  claude-auto --list              # list configured accounts

Hive subcommands (Phase 2+):
  claude-auto queen                       # launch interactive on the queen account
  claude-auto hive serve                  # start the MCP server (stdio)
  claude-auto hive status                 # show pool + budget snapshot
  claude-auto hive cost-report            # per-account credit usage table
  claude-auto hive reset-budget <name>    # reset an account's monthly cycle
  claude-auto hive doctor                 # environment health checks
  claude-auto hive prune-worktrees        # remove leftover hive-* worktrees
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

from .account_pool import AccountPool, DEFAULT_QUEEN
from .claude_runner import RunResult, run_claude
from .rate_limit import DEFAULT_RATE_LIMIT_PATTERNS, compile_patterns
from .share_migration import SHARED_DIR, find_latest_session, migrate


HOME = Path.home()
CONFIG_FILE = HOME / ".claude-auto.json"
STATE_DIR_ENV = "HIVE_STATE_DIR"
# State lives next to the code by default (the repo's own `state/` dir), so the
# tool is portable wherever it's cloned. Override with $HIVE_STATE_DIR.
DEFAULT_STATE_DIR = Path(__file__).resolve().parent.parent / "state"

# Accounts come from ~/.claude-auto.json. There is intentionally no hardcoded
# default list — see `_print_no_accounts()` for the first-run hint.
DEFAULT_ACCOUNTS: list[tuple[str, str]] = []

FAST_LIMIT_SECONDS = 10.0


def _state_dir() -> Path:
    raw = os.environ.get(STATE_DIR_ENV)
    return Path(raw).expanduser() if raw else DEFAULT_STATE_DIR


def load_config() -> tuple[list[tuple[str, str]], list[str], str]:
    accounts = DEFAULT_ACCOUNTS
    patterns = DEFAULT_RATE_LIMIT_PATTERNS
    queen = DEFAULT_QUEEN
    if CONFIG_FILE.exists():
        try:
            cfg = json.loads(CONFIG_FILE.read_text())
            if isinstance(cfg.get("accounts"), list) and cfg["accounts"]:
                accounts = [(a["name"], a["dir"]) for a in cfg["accounts"]]
            if isinstance(cfg.get("rate_limit_patterns"), list) and cfg["rate_limit_patterns"]:
                patterns = cfg["rate_limit_patterns"]
            if isinstance(cfg.get("queen"), str) and cfg["queen"]:
                queen = cfg["queen"]
        except Exception as e:
            print(f"[claude-auto] WARN: bad {CONFIG_FILE}: {e}", file=sys.stderr)
    return accounts, patterns, queen


HELP = __doc__


def parse_args(argv: list[str]) -> tuple[dict, list[str]]:
    """Pull our own flags out of argv; return the rest as claude args."""
    out = {
        "account": None,
        "migrate": False,
        "dry_run": False,
        "status": False,
        "list": False,
        "help": False,
    }
    rest: list[str] = []
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--":
            rest.extend(argv[i + 1 :])
            break
        elif a == "--account" and i + 1 < len(argv):
            out["account"] = argv[i + 1]
            i += 2
            continue
        elif a == "--migrate":
            out["migrate"] = True
        elif a == "--dry-run":
            out["dry_run"] = True
        elif a == "--status":
            out["status"] = True
        elif a == "--list":
            out["list"] = True
        elif a in ("-h", "--help-auto"):
            out["help"] = True
        else:
            rest.append(a)
        i += 1
    return out, rest


def _print_help(accounts: list[tuple[str, str]], queen: str) -> None:
    print(HELP)
    print("\nConfigured accounts:")
    for n, d in accounts:
        marker = " (queen)" if n == queen else ""
        print(f"  {n:<10} {d}{marker}")


def _print_no_accounts() -> None:
    print(
        "[claude-auto] No accounts configured.\n"
        f"\nCreate {CONFIG_FILE} with the accounts you want to rotate across:\n\n"
        '  {\n'
        '    "accounts": [\n'
        f'      {{ "name": "personal", "dir": "{HOME}/.claude-personal" }},\n'
        f'      {{ "name": "work",     "dir": "{HOME}/.claude-work" }}\n'
        '    ]\n'
        '  }\n\n'
        "Then log into each account once:\n"
        f"  CLAUDE_CONFIG_DIR={HOME}/.claude-personal claude\n"
        "and run `claude-auto --migrate` to share sessions across them.",
        file=sys.stderr,
    )


def _print_list(accounts: list[tuple[str, str]]) -> None:
    for n, d in accounts:
        exists = "✓" if Path(d).exists() else "✗"
        print(f"  {exists} {n:<10} {d}")


def _print_status(pool: AccountPool) -> None:
    cur = pool.cli_current()
    print(f"current account: {cur.name}  ({cur.dir})")
    print(f"queen account:   {pool.queen_name()}")
    print(f"shared dir:      {SHARED_DIR}  (exists={SHARED_DIR.exists()})")


def _has_resume_flag(args: list[str]) -> bool:
    return any(a in ("--resume", "-r", "--continue", "-c") for a in args)


def main() -> int:
    # Hive / queen subcommands intercept BEFORE the existing flag parsing,
    # since they don't fit the [--account NAME] [-- ...] grammar.
    argv = sys.argv[1:]
    if argv and argv[0] == "hive":
        return _hive_subcommand(argv[1:])
    if argv and argv[0] == "queen":
        return _queen_subcommand(argv[1:])

    accounts, patterns, queen = load_config()
    rate_limit_re = compile_patterns(patterns)
    flags, claude_args = parse_args(argv)

    if flags["help"]:
        _print_help(accounts, queen)
        return 0

    if not accounts:
        _print_no_accounts()
        return 2

    if flags["list"]:
        _print_list(accounts)
        return 0

    if flags["migrate"]:
        migrate(accounts, dry_run=flags["dry_run"])
        return 0

    pool = AccountPool(accounts, _state_dir() / "pool.json", queen=queen)

    if flags["status"]:
        _print_status(pool)
        return 0

    initial_name: str | None = None
    if flags["account"]:
        if flags["account"] not in [n for n, _ in accounts]:
            print(f"unknown account: {flags['account']}", file=sys.stderr)
            print("available:", ", ".join(n for n, _ in accounts), file=sys.stderr)
            return 2
        initial_name = flags["account"]

    return _run_loop(pool, accounts, claude_args, rate_limit_re, initial_name=initial_name)


# --------------------------------------------------------------- queen


def _queen_subcommand(argv: list[str]) -> int:
    """`claude-auto queen [-- ...]` → equivalent to --account <queen>."""
    accounts, _, queen = load_config()
    # Build a synthetic argv that uses --account <queen> + passthrough args.
    rest: list[str] = list(argv)
    if "--" in rest:
        # Already has explicit separator — keep as-is.
        return _main_with_argv(["--account", queen] + rest)
    return _main_with_argv(["--account", queen] + (["--"] + rest if rest else []))


def _main_with_argv(argv: list[str]) -> int:
    """Re-enter main() with a custom argv (used by `queen` shorthand)."""
    saved = sys.argv
    sys.argv = [saved[0]] + argv
    try:
        return main()
    finally:
        sys.argv = saved


# --------------------------------------------------------------- hive


def _hive_subcommand(argv: list[str]) -> int:
    if not argv:
        _hive_help()
        return 0
    cmd = argv[0]
    rest = argv[1:]
    if cmd in ("-h", "--help", "help"):
        _hive_help()
        return 0
    if cmd == "serve":
        return _hive_serve()
    if cmd == "status":
        return _hive_status()
    if cmd == "cost-report":
        return _hive_cost_report()
    if cmd == "reset-budget":
        if not rest:
            print("usage: claude-auto hive reset-budget <name>", file=sys.stderr)
            return 2
        return _hive_reset_budget(rest[0])
    if cmd == "doctor":
        return _hive_doctor()
    if cmd == "prune-worktrees":
        return _hive_prune_worktrees()
    if cmd == "watch":
        return _hive_watch(rest)
    if cmd == "jobs":
        return _hive_jobs(rest)
    print(f"unknown hive subcommand: {cmd}", file=sys.stderr)
    _hive_help()
    return 2


def _hive_help() -> None:
    print(
        "claude-auto hive <subcommand>\n"
        "  serve                Start MCP server (stdio)\n"
        "  status               Pool + budget snapshot\n"
        "  watch [--interval S] Live dashboard — pool + recent jobs (Ctrl+C to exit)\n"
        "  jobs [--limit N]     List recent jobs (default 20)\n"
        "  cost-report          Per-account credit usage table\n"
        "  reset-budget NAME    Reset an account's monthly cycle\n"
        "  doctor               Environment health checks\n"
        "  prune-worktrees      Remove leftover hive-* worktrees\n"
    )


def _hive_serve() -> int:
    # Direct import + run. Tells the user to wire it via --mcp-config or
    # mcpServers config rather than launch interactively, since stdio expects
    # JSON-RPC, not a TTY.
    if sys.stdin.isatty():
        print(
            "[hive] serve speaks MCP JSON-RPC on stdio — do not launch interactively.\n"
            "Wire it into your queen via ~/.claude-<queen>/settings.json mcpServers,\n"
            "or pass --mcp-config to a `claude` invocation.",
            file=sys.stderr,
        )
        return 2
    from .hive_controller import main as controller_main
    return controller_main()


def _hive_status() -> int:
    accounts, _, queen = load_config()
    pool = AccountPool(accounts, _state_dir() / "pool.json", queen=queen)
    snap = pool.status()
    print(f"queen:    {snap['queen']}")
    print(f"current:  {snap['current_cli_account']} (idx {snap['current_cli_idx']})")
    print()
    print(f"{'name':<10} {'state':<8} {'in_use':<24} {'last_used':<22} reason")
    for a in snap["accounts"]:
        if a["cold_seconds_left"] > 0:
            state_s = f"cold +{a['cold_seconds_left']}s"
        elif a["in_use_by_job"]:
            state_s = "busy"
        else:
            state_s = "warm"
        last = (
            time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(a["last_used_at"]))
            if a["last_used_at"] else "—"
        )
        in_use = a["in_use_by_job"] or "—"
        reason = a["cold_reason"] or ""
        print(f"{a['name']:<10} {state_s:<8} {in_use:<24} {last:<22} {reason}")
    return 0


def _hive_cost_report() -> int:
    from .budget import Budget
    accounts, _, _ = load_config()
    budget = Budget(_state_dir() / "budget.json", [n for n, _ in accounts])
    snap = budget.status()
    print(f"{'name':<10} {'cycle_start':<12} {'cap':>10} {'used':>10} {'remaining':>12} {'calls':>6}  extra")
    for a in snap["accounts"]:
        rem = a["remaining_usd"]
        rem_s = f"${rem:>10.4f}" if isinstance(rem, (int, float)) else f"{rem:>11}"
        extra = "yes" if a["extra_usage_enabled"] else "no"
        print(
            f"{a['name']:<10} {a['billing_cycle_start']:<12} "
            f"${a['cap_usd']:>9.2f} ${a['used_usd']:>9.4f} {rem_s} "
            f"{a['call_count']:>6}  {extra}"
        )
    return 0


def _hive_reset_budget(name: str) -> int:
    from .budget import Budget
    accounts, _, _ = load_config()
    if name not in [n for n, _ in accounts]:
        print(f"unknown account: {name}", file=sys.stderr)
        return 2
    budget = Budget(_state_dir() / "budget.json", [n for n, _ in accounts])
    budget.reset_cycle(name)
    print(f"[hive] {name}: budget cycle reset")
    return 0


def _hive_doctor() -> int:
    """Health checks: account dirs, shared symlinks, venv, hive-mcp wiring."""
    import shutil as _shutil
    import subprocess as _subprocess

    accounts, _, queen = load_config()
    hive_root = Path(__file__).resolve().parent.parent
    venv_py = hive_root / ".venv" / "bin" / "python"
    queen_settings = HOME / f".claude-{queen}" / "settings.json"

    ok = True

    def check(label: str, passing: bool, detail: str = "") -> None:
        nonlocal ok
        mark = "✓" if passing else "✗"
        print(f"  {mark} {label}" + (f"  ({detail})" if detail else ""))
        if not passing:
            ok = False

    print(f"[hive] doctor — queen={queen}, hive_root={hive_root}")
    print()
    print("Accounts:")
    for name, d in accounts:
        check(f"{name:<10} dir exists", Path(d).is_dir(), d)
    print()
    print("Shared dir:")
    check(
        f"~/.claude-shared exists",
        SHARED_DIR.is_dir(),
        str(SHARED_DIR),
    )
    print()
    print("Tooling:")
    check("claude binary in PATH", bool(_shutil.which("claude")), _shutil.which("claude") or "")
    check("git in PATH", bool(_shutil.which("git")))
    check("rsync in PATH", bool(_shutil.which("rsync")))
    check("venv python exists", venv_py.is_file(), str(venv_py))
    if venv_py.is_file():
        try:
            r = _subprocess.run(
                [str(venv_py), "-c", "from mcp.server.fastmcp import FastMCP; print('ok')"],
                capture_output=True, text=True, timeout=10,
            )
            check("mcp SDK importable", r.returncode == 0 and "ok" in r.stdout, r.stderr.strip()[:80])
        except Exception as e:
            check("mcp SDK importable", False, str(e))
    print()
    print("Queen MCP wiring:")
    if queen_settings.is_file():
        try:
            qs = json.loads(queen_settings.read_text())
            mcp_servers = qs.get("mcpServers", {})
            wired = "hive" in mcp_servers
            check(f"hive entry in {queen_settings}", wired,
                  ", ".join(mcp_servers.keys()) or "no MCP servers")
        except Exception as e:
            check(f"settings.json parseable at {queen_settings}", False, str(e))
    else:
        check(f"{queen_settings} exists", False)
    print()
    print("State:")
    state_dir = _state_dir()
    check("state dir exists", state_dir.is_dir(), str(state_dir))
    check("pool.json present", (state_dir / "pool.json").exists())
    check("budget.json present", (state_dir / "budget.json").exists())

    print()
    print("Overall:", "PASS ✓" if ok else "FAIL ✗")
    return 0 if ok else 1


# ---------------------------------------------------------- watch / jobs

# Minimal ANSI helpers — no curses, no extra deps.
_ANSI = {
    "reset": "\x1b[0m",
    "dim":   "\x1b[2m",
    "bold":  "\x1b[1m",
    "red":   "\x1b[31m",
    "green": "\x1b[32m",
    "yellow":"\x1b[33m",
    "blue":  "\x1b[34m",
    "cyan":  "\x1b[36m",
    "gray":  "\x1b[90m",
    "clear": "\x1b[2J\x1b[H",
}

_STATE_COLOR = {
    "warm": "green", "busy": "yellow", "cold": "blue",
    "queued": "cyan", "running": "yellow",
    "completed": "green", "rate_limited": "blue",
    "errored": "red", "timeout": "red", "budget_exceeded": "red",
    "no_account": "red", "unknown": "gray",
}


def _c(text: str, color: str, use_color: bool = True) -> str:
    if not use_color or color not in _ANSI:
        return text
    return f"{_ANSI[color]}{text}{_ANSI['reset']}"


def _load_recent_jobs(state_dir: Path, limit: int = 20) -> list[dict]:
    jobs_dir = state_dir / "jobs"
    if not jobs_dir.is_dir():
        return []
    files = sorted(jobs_dir.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    out: list[dict] = []
    for f in files[:limit]:
        try:
            out.append(json.loads(f.read_text()))
        except (OSError, json.JSONDecodeError):
            continue
    return out


def _job_age(job: dict) -> str:
    ts = job.get("finished_at") or job.get("started_at") or job.get("queued_at")
    if not ts:
        return "—"
    try:
        delta = time.time() - float(ts)
    except (ValueError, TypeError):
        return "—"
    if delta < 60:
        return f"{int(delta)}s ago"
    if delta < 3600:
        return f"{int(delta / 60)}m ago"
    if delta < 86400:
        return f"{int(delta / 3600)}h ago"
    return f"{int(delta / 86400)}d ago"


def _task_preview(job: dict, width: int) -> str:
    spec = job.get("spec") or {}
    task = spec.get("task") or "(no task)"
    task = task.replace("\n", " ").replace("\r", " ").strip()
    if len(task) > width:
        return task[: width - 1] + "…"
    return task


def _render_dashboard(state_dir: Path, use_color: bool = True) -> str:
    accounts, _, queen = load_config()
    pool = AccountPool(accounts, state_dir / "pool.json", queen=queen)
    from .budget import Budget
    budget = Budget(state_dir / "budget.json", [n for n, _ in accounts])
    psnap = pool.status()
    bsnap = budget.status()
    bmap = {a["name"]: a for a in bsnap["accounts"]}

    lines: list[str] = []
    title = (
        f"Hive · queen={psnap['queen']} · "
        f"{time.strftime('%Y-%m-%d %H:%M:%S')} · "
        f"Ctrl+C to exit"
    )
    lines.append(_c(title, "bold", use_color))
    lines.append("")
    lines.append(_c(
        f"{'account':<10} {'state':<10} {'in_use':<22} {'last_used':<10} "
        f"{'used':>10} {'remaining':>12}  notes",
        "bold", use_color,
    ))
    for a in psnap["accounts"]:
        is_queen = a["name"] == psnap["queen"]
        if a["cold_seconds_left"] > 0:
            state, color = f"cold +{a['cold_seconds_left']}s", "cold"
        elif a["in_use_by_job"]:
            state, color = "busy", "busy"
        else:
            state, color = "warm", "warm"
        last = (
            time.strftime("%H:%M:%S", time.localtime(a["last_used_at"]))
            if a["last_used_at"] else "—"
        )
        in_use = (a["in_use_by_job"] or "—")[:22]
        b = bmap.get(a["name"], {})
        used = b.get("used_usd", 0.0)
        rem = b.get("remaining_usd", 0.0)
        rem_s = f"${rem:>10.2f}" if isinstance(rem, (int, float)) else f"{rem:>11}"
        note = "QUEEN" if is_queen else (a["cold_reason"] or "")
        lines.append(
            f"{a['name']:<10} "
            f"{_c(f'{state:<10}', color, use_color)} "
            f"{in_use:<22} {last:<10} "
            f"${used:>9.4f} {rem_s}  {note}"
        )

    lines.append("")
    jobs = _load_recent_jobs(state_dir, limit=15)
    if jobs:
        lines.append(_c(
            f"{'job':<24} {'account':<10} {'role':<14} {'status':<14} "
            f"{'cost':>8} {'age':<10} task", "bold", use_color,
        ))
        try:
            term_w = os.get_terminal_size().columns
        except OSError:
            term_w = 120
        # Fixed-width columns above sum to ~94 chars; remaining is for `task`.
        task_w = max(20, term_w - 94)
        for j in jobs:
            jid = (j.get("job_id") or "")[:24]
            acct = (j.get("account_name") or j.get("account") or "—")[:10]
            spec = j.get("spec") or {}
            role = (spec.get("role") or j.get("role") or "—")[:14]
            status = (j.get("status") or "unknown")[:14]
            color = _STATE_COLOR.get(status, "gray")
            cost = j.get("cost_usd") or 0.0
            try:
                cost_s = f"${float(cost):>7.4f}"
            except (TypeError, ValueError):
                cost_s = "—"
            age = _job_age(j)
            task = _task_preview(j, task_w)
            lines.append(
                f"{jid:<24} {acct:<10} {role:<14} "
                f"{_c(f'{status:<14}', color, use_color)} "
                f"{cost_s:>8} {age:<10} {task}"
            )
    else:
        lines.append(_c("(no jobs yet — delegate something from the queen)", "dim", use_color))

    return "\n".join(lines) + "\n"


def _hive_watch(argv: list[str]) -> int:
    interval = 2.0
    i = 0
    while i < len(argv):
        if argv[i] in ("--interval", "-n") and i + 1 < len(argv):
            try:
                interval = max(0.5, float(argv[i + 1]))
            except ValueError:
                pass
            i += 2
            continue
        i += 1
    state_dir = _state_dir()
    use_color = sys.stdout.isatty()
    try:
        while True:
            if use_color:
                sys.stdout.write(_ANSI["clear"])
            sys.stdout.write(_render_dashboard(state_dir, use_color=use_color))
            sys.stdout.flush()
            time.sleep(interval)
    except KeyboardInterrupt:
        sys.stdout.write("\n")
        return 0


def _hive_jobs(argv: list[str]) -> int:
    limit = 20
    i = 0
    while i < len(argv):
        if argv[i] in ("--limit", "-n") and i + 1 < len(argv):
            try:
                limit = max(1, int(argv[i + 1]))
            except ValueError:
                pass
            i += 2
            continue
        i += 1
    state_dir = _state_dir()
    jobs = _load_recent_jobs(state_dir, limit=limit)
    if not jobs:
        print("(no jobs yet)")
        return 0
    use_color = sys.stdout.isatty()
    print(_c(
        f"{'job':<24} {'account':<10} {'role':<14} {'status':<14} "
        f"{'cost':>8} {'age':<10} task", "bold", use_color,
    ))
    try:
        term_w = os.get_terminal_size().columns
    except OSError:
        term_w = 120
    task_w = max(20, term_w - 94)
    for j in jobs:
        jid = (j.get("job_id") or "")[:24]
        acct = (j.get("account_name") or j.get("account") or "—")[:10]
        spec = j.get("spec") or {}
        role = (spec.get("role") or j.get("role") or "—")[:14]
        status = (j.get("status") or "unknown")[:14]
        color = _STATE_COLOR.get(status, "gray")
        cost = j.get("cost_usd") or 0.0
        try:
            cost_s = f"${float(cost):>7.4f}"
        except (TypeError, ValueError):
            cost_s = "—"
        age = _job_age(j)
        task = _task_preview(j, task_w)
        print(
            f"{jid:<24} {acct:<10} {role:<14} "
            f"{_c(f'{status:<14}', color, use_color)} "
            f"{cost_s:>8} {age:<10} {task}"
        )
    return 0


# ---------------------------------------------------------- prune


def _hive_prune_worktrees() -> int:
    """List and optionally remove leftover hive-* worktrees from git."""
    import subprocess as _subprocess

    try:
        r = _subprocess.run(
            ["git", "worktree", "list", "--porcelain"],
            capture_output=True, text=True, check=True,
        )
    except (FileNotFoundError, _subprocess.CalledProcessError) as e:
        print(f"[hive] git worktree list failed: {e}", file=sys.stderr)
        return 1

    targets: list[str] = []
    for line in r.stdout.splitlines():
        if line.startswith("worktree "):
            path = line.split(" ", 1)[1]
            if "/hive-worktree-" in path or path.endswith(".bak"):
                targets.append(path)

    if not targets:
        print("[hive] no leftover hive worktrees")
        return 0

    print("[hive] candidates to prune:")
    for p in targets:
        print(f"  {p}")
    print()
    confirm = os.environ.get("HIVE_PRUNE_CONFIRM") == "yes"
    if not confirm:
        print("Set HIVE_PRUNE_CONFIRM=yes and re-run to actually remove.")
        return 0
    for p in targets:
        _subprocess.run(["git", "worktree", "remove", "--force", p], check=False)
        print(f"  removed {p}")
    return 0


def _run_loop(pool, accounts, claude_args, rate_limit_re, initial_name: str | None = None) -> int:
    cwd = Path.cwd()
    first = True
    # Exhaustion guard: count consecutive accounts that hit a limit immediately
    # on launch (i.e. were already rate-limited when we tried them). If this
    # reaches len(accounts) we've cycled through every account without doing
    # any real work — bail out instead of looping forever.
    consecutive_fast_limits = 0
    exhausted_accounts: list[tuple[str, str | None]] = []
    n_accounts = len(accounts)
    account_names = [n for n, _ in accounts]

    # Track the current account in-memory per session.  Reading current_cli_idx
    # from pool.json on every iteration causes concurrent sessions to fight: if
    # Session A calls cli_advance() after a rate-limit, it overwrites the shared
    # index and Session B's next loop iteration lands on the wrong account,
    # ignoring whatever --account was passed.
    if initial_name and initial_name in account_names:
        local_idx = account_names.index(initial_name)
    else:
        # Consult shared state only once at startup (no --account given).
        local_idx = pool.cli_idx_of(pool.cli_current().name)

    while True:
        acct = pool.by_name(account_names[local_idx])
        idx = local_idx
        sys.stderr.write(f"\r\n→ Account: {acct.name} ({idx + 1}/{n_accounts})\r\n")
        sys.stderr.flush()
        pool.cli_touch(acct.name)

        run_args = list(claude_args)
        if not first and not _has_resume_flag(run_args):
            sid = find_latest_session(cwd, accounts)
            run_args = (["--resume", sid] if sid else ["--continue"]) + run_args

        result: RunResult = run_claude(
            account_dir=acct.dir,
            args=run_args,
            rate_limit_re=rate_limit_re,
            mode="interactive",
        )
        first = False

        if result.status == "exit":
            return result.exit_code

        if result.status == "error":
            sys.stderr.write(f"\r\n⛔ claude failed to start: {result.stderr_tail or '(no detail)'}\r\n")
            return result.exit_code or 1

        # Rate-limited path
        pool.cli_mark_cold(acct.name, result.reset_hint, result.reset_time_iso)

        if result.elapsed_seconds < FAST_LIMIT_SECONDS:
            consecutive_fast_limits += 1
            exhausted_accounts.append((acct.name, result.reset_hint))
        else:
            consecutive_fast_limits = 1
            exhausted_accounts = [(acct.name, result.reset_hint)]

        if consecutive_fast_limits >= n_accounts:
            sys.stderr.write("\r\n")
            sys.stderr.write(f"⛔ All {n_accounts} accounts are rate-limited. Giving up.\r\n")
            for n, hint in exhausted_accounts:
                sys.stderr.write(f"   {n:<10} {hint or '(no reset time captured)'}\r\n")
            sys.stderr.write(
                "Try again later, or run `claude-auto --account <name>` once a limit resets.\r\n"
            )
            sys.stderr.flush()
            return 1

        local_idx = (local_idx + 1) % n_accounts
        nxt_name = account_names[local_idx]
        hint_str = f" ({result.reset_hint})" if result.reset_hint else ""
        sys.stderr.write(
            f"\r\n⚡ {acct.name} hit usage limit{hint_str}. Switching to {nxt_name} and resuming...\r\n"
        )
        sys.stderr.flush()
        time.sleep(2)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
