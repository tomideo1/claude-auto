"""Spawn the `claude` CLI under a chosen account.

Two modes:
- "interactive": PTY-wrapped, mirrors stdin/stdout, scans for rate-limit
  banners. This is what the queen uses for the human-facing REPL.
- "headless": subprocess.run, captures stdout/stderr, parses
  `--output-format json` output. This is what workers use.
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import select
import signal
import struct
import subprocess
import sys
import termios
import time
import tty
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Literal

from .rate_limit import (
    RESET_HINT_RE,
    parse_for_rate_limit,
    parse_reset_hint_to_iso,
    strip_ansi,
)


RunStatus = Literal["exit", "rate_limited", "error"]


@dataclass
class RunResult:
    status: RunStatus
    exit_code: int
    elapsed_seconds: float
    reset_hint: str | None
    reset_time_iso: str | None = None
    cost_usd: float | None = None
    session_id: str | None = None
    result_text: str | None = None
    stderr_tail: str = ""


def _set_winsize(fd: int) -> None:
    try:
        s = struct.pack("HHHH", 0, 0, 0, 0)
        size = fcntl.ioctl(sys.stdout.fileno(), termios.TIOCGWINSZ, s)
        fcntl.ioctl(fd, termios.TIOCSWINSZ, size)
    except Exception:
        pass


def _has_flag(args: Iterable[str], flag: str) -> bool:
    return any(a == flag or a.startswith(flag + "=") for a in args)


def _build_env(account_dir: str, env_extra: dict[str, str] | None) -> dict[str, str]:
    env = os.environ.copy()
    env["CLAUDE_CONFIG_DIR"] = account_dir
    if env_extra:
        env.update(env_extra)
    return env


def _run_interactive(
    account_dir: str,
    args: list[str],
    rate_limit_re: re.Pattern[str],
    cwd: Path | None,
    env_extra: dict[str, str] | None,
) -> RunResult:
    """PTY-wrapped run. User sees and types into the TUI."""
    import pty  # Unix-only

    started = time.time()
    pid, master_fd = pty.fork()
    if pid == 0:
        # Child
        env = _build_env(account_dir, env_extra)
        if cwd is not None:
            try:
                os.chdir(cwd)
            except OSError:
                pass
        try:
            os.execvpe("claude", ["claude"] + args, env)
        except FileNotFoundError:
            print("claude binary not found in PATH", file=sys.stderr)
            os._exit(127)

    # Parent
    _set_winsize(master_fd)
    old_tty = None
    if sys.stdin.isatty():
        old_tty = termios.tcgetattr(sys.stdin.fileno())
        tty.setraw(sys.stdin.fileno())

    def on_winch(_signum, _frame):
        _set_winsize(master_fd)

    old_winch = signal.signal(signal.SIGWINCH, on_winch)

    buffer = bytearray()
    BUFFER_MAX = 32768
    rate_limited = False
    reset_hint: str | None = None
    exit_code = 0

    try:
        while True:
            try:
                rlist, _, _ = select.select([sys.stdin, master_fd], [], [], 0.2)
            except (InterruptedError, OSError):
                continue

            if sys.stdin in rlist:
                try:
                    data = os.read(sys.stdin.fileno(), 4096)
                    if data:
                        os.write(master_fd, data)
                except OSError:
                    pass

            if master_fd in rlist:
                try:
                    data = os.read(master_fd, 4096)
                except OSError:
                    data = b""
                if not data:
                    break
                os.write(sys.stdout.fileno(), data)
                buffer.extend(strip_ansi(data))
                if len(buffer) > BUFFER_MAX:
                    del buffer[: len(buffer) - BUFFER_MAX]
                txt = buffer.decode("utf-8", errors="replace")
                if rate_limit_re.search(txt):
                    rate_limited = True
                    m = RESET_HINT_RE.search(txt)
                    if m:
                        reset_hint = m.group(0).strip()
                    # Give claude a beat to finish its output, then terminate.
                    time.sleep(0.4)
                    try:
                        os.kill(pid, signal.SIGTERM)
                        time.sleep(0.3)
                        os.kill(pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    break

            try:
                wpid, status = os.waitpid(pid, os.WNOHANG)
                if wpid != 0:
                    if os.WIFEXITED(status):
                        exit_code = os.WEXITSTATUS(status)
                    elif os.WIFSIGNALED(status):
                        exit_code = 128 + os.WTERMSIG(status)
                    break
            except ChildProcessError:
                break
    finally:
        signal.signal(signal.SIGWINCH, old_winch)
        if old_tty is not None:
            try:
                termios.tcsetattr(sys.stdin.fileno(), termios.TCSANOW, old_tty)
            except Exception:
                pass
        try:
            os.close(master_fd)
        except OSError:
            pass
        try:
            os.waitpid(pid, 0)
        except ChildProcessError:
            pass

    elapsed = time.time() - started
    tail = bytes(buffer[-2048:]).decode("utf-8", errors="replace")
    iso = parse_reset_hint_to_iso(reset_hint) if reset_hint else None
    return RunResult(
        status="rate_limited" if rate_limited else "exit",
        exit_code=exit_code,
        elapsed_seconds=elapsed,
        reset_hint=reset_hint,
        reset_time_iso=iso,
        cost_usd=None,
        session_id=None,
        result_text=None,
        stderr_tail=tail,
    )


def _run_headless(
    account_dir: str,
    args: list[str],
    rate_limit_re: re.Pattern[str],
    cwd: Path | None,
    env_extra: dict[str, str] | None,
    timeout_seconds: float | None,
) -> RunResult:
    """subprocess.run-based run for `claude -p`. Parses JSON output."""
    cmd = ["claude"] + list(args)

    # Auto-inject flags for headless workers. We intentionally do NOT inject
    # --bare: that flag disables OAuth and keychain reads, which breaks
    # subscription-based accounts (the only kind the hive supports). The
    # spec called for --bare but that pre-dates discovery of this constraint.
    if not _has_flag(cmd, "--output-format"):
        cmd.extend(["--output-format", "json"])
    if not _has_flag(cmd, "--dangerously-skip-permissions"):
        cmd.append("--dangerously-skip-permissions")

    env = _build_env(account_dir, env_extra)

    started = time.time()
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(cwd) if cwd else None,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout_seconds,
        )
    except FileNotFoundError:
        return RunResult(
            status="error", exit_code=127, elapsed_seconds=time.time() - started,
            reset_hint=None, stderr_tail="claude binary not found in PATH",
        )
    except subprocess.TimeoutExpired as e:
        return RunResult(
            status="error", exit_code=124, elapsed_seconds=time.time() - started,
            reset_hint=None,
            stderr_tail=(e.stderr or b"")[-2048:].decode("utf-8", errors="replace") or "timeout",
        )

    elapsed = time.time() - started
    stdout = proc.stdout.decode("utf-8", errors="replace")
    stderr = proc.stderr.decode("utf-8", errors="replace")
    stderr_tail = stderr[-2048:]

    # Rate-limit checks. Stderr is the most likely surface for headless mode.
    hit = (
        parse_for_rate_limit(stderr, rate_limit_re)
        or parse_for_rate_limit(stdout, rate_limit_re)
    )
    if hit:
        return RunResult(
            status="rate_limited",
            exit_code=proc.returncode,
            elapsed_seconds=elapsed,
            reset_hint=hit.reset_hint,
            reset_time_iso=hit.reset_time_iso,
            stderr_tail=stderr_tail,
        )

    # Try to parse the JSON envelope. `claude -p --output-format json` emits a
    # single JSON object on stdout. Fields seen in practice: result, session_id,
    # total_cost_usd, is_error, error.
    cost: float | None = None
    session_id: str | None = None
    result_text: str | None = None
    is_error = False
    try:
        parsed = json.loads(stdout) if stdout.strip() else None
        if isinstance(parsed, dict):
            result_text = parsed.get("result")
            session_id = parsed.get("session_id")
            v = parsed.get("total_cost_usd")
            if isinstance(v, (int, float)):
                cost = float(v)
            is_error = bool(parsed.get("is_error") or parsed.get("error"))
    except json.JSONDecodeError:
        pass

    status: RunStatus = "error" if (is_error or proc.returncode != 0) else "exit"
    return RunResult(
        status=status,
        exit_code=proc.returncode,
        elapsed_seconds=elapsed,
        reset_hint=None,
        cost_usd=cost,
        session_id=session_id,
        result_text=result_text,
        stderr_tail=stderr_tail,
    )


def run_claude(
    account_dir: str,
    args: list[str],
    rate_limit_re: re.Pattern[str],
    mode: Literal["interactive", "headless"] = "interactive",
    cwd: Path | None = None,
    env_extra: dict[str, str] | None = None,
    timeout_seconds: float | None = None,
) -> RunResult:
    """Run `claude` under the given account directory.

    Returns a RunResult with status='rate_limited' if a rate-limit banner was
    detected, 'exit' on a clean exit, or 'error' if the process couldn't
    start or returned an error envelope in headless mode.
    """
    if mode == "interactive":
        return _run_interactive(account_dir, args, rate_limit_re, cwd, env_extra)
    return _run_headless(account_dir, args, rate_limit_re, cwd, env_extra, timeout_seconds)
