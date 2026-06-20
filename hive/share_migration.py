"""One-time migration: collapse per-account session state into ~/.claude-shared.

This is the `--migrate` path. It rsyncs each account's `projects/`, `todos/`,
etc. into a single shared directory, then replaces the original with a
symlink so `--resume` works across accounts.

Lifted from claude-auto.py with minimal changes.
"""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path


HOME = Path.home()
SHARED_DIR = HOME / ".claude-shared"

# Subdirectories of each .claude-<acct>/ that we share across accounts.
SHARED_SUBDIRS = [
    "projects",
    "todos",
    "file-history",
    "shell-snapshots",
    "plans",
    "tasks",
    "session-env",
]

# Single files to share (concatenated/deduped on migration).
SHARED_FILES = ["history.jsonl"]


def encode_cwd(p: Path | str) -> str:
    return str(p).replace("/", "-")


def _rsync_merge(src: Path, dst: Path) -> None:
    """Merge src/ into dst/. First-seen wins on filename collision."""
    dst.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["rsync", "-a", "--ignore-existing", f"{src}/", f"{dst}/"],
        check=True,
    )


def _is_symlink_to(path: Path, target: Path) -> bool:
    try:
        return path.is_symlink() and Path(os.readlink(path)).resolve() == target.resolve()
    except OSError:
        return False


def migrate(accounts: list[tuple[str, str]], dry_run: bool = False) -> None:
    print(f"[claude-auto] Migration target: {SHARED_DIR}")
    if dry_run:
        print("[claude-auto] DRY RUN — no changes will be made")
    SHARED_DIR.mkdir(exist_ok=True)
    for sub in SHARED_SUBDIRS:
        (SHARED_DIR / sub).mkdir(exist_ok=True)

    for name, acct_str in accounts:
        acct = Path(acct_str)
        if not acct.exists():
            print(f"  [{name}] skip (no dir)")
            continue
        print(f"  [{name}] {acct}")

        for sub in SHARED_SUBDIRS:
            src = acct / sub
            shared_target = SHARED_DIR / sub
            if not src.exists() and not src.is_symlink():
                continue
            if _is_symlink_to(src, shared_target):
                print(f"    {sub:<18} already symlinked → ok")
                continue
            if src.is_symlink():
                old = os.readlink(src)
                print(f"    {sub:<18} symlink to {old} → rewire")
                if not dry_run:
                    src.unlink()
                    src.symlink_to(shared_target)
                continue
            print(f"    {sub:<18} merge → {shared_target}")
            if not dry_run:
                _rsync_merge(src, shared_target)
                bak = acct / f"{sub}.premigration-bak"
                if bak.exists():
                    bak = acct / f"{sub}.premigration-bak.{int(time.time())}"
                src.rename(bak)
                src.symlink_to(shared_target)
                print(f"    {sub:<18} symlinked. Backup at {bak.name}")

        for fname in SHARED_FILES:
            src = acct / fname
            shared_target = SHARED_DIR / fname
            if not src.exists() and not src.is_symlink():
                continue
            if _is_symlink_to(src, shared_target):
                print(f"    {fname:<18} already symlinked → ok")
                continue
            if src.is_symlink():
                if not dry_run:
                    src.unlink()
                    src.symlink_to(shared_target)
                print(f"    {fname:<18} rewired symlink")
                continue
            print(f"    {fname:<18} merge → {shared_target}")
            if not dry_run:
                existing: set[str] = set()
                if shared_target.exists():
                    with shared_target.open("r", errors="replace") as f:
                        existing = set(f.read().splitlines())
                with src.open("r", errors="replace") as f:
                    new_lines = f.read().splitlines()
                added = [ln for ln in new_lines if ln not in existing]
                with shared_target.open("a") as f:
                    if (
                        added
                        and shared_target.stat().st_size > 0
                        and not shared_target.read_text(errors="replace").endswith("\n")
                    ):
                        f.write("\n")
                    for ln in added:
                        f.write(ln + "\n")
                bak = acct / f"{fname}.premigration-bak"
                if bak.exists():
                    bak = acct / f"{fname}.premigration-bak.{int(time.time())}"
                src.rename(bak)
                src.symlink_to(shared_target)
                print(f"    {fname:<18} symlinked. Backup at {bak.name}")

    print("[claude-auto] migration complete")


def find_latest_session(cwd: Path, accounts: list[tuple[str, str]]) -> str | None:
    """Find the most recently modified <uuid>.jsonl session for cwd.
    Looks first in shared, then falls back to each account's projects dir."""
    candidates: list[Path] = []
    enc = encode_cwd(cwd)
    shared_proj = SHARED_DIR / "projects" / enc
    if shared_proj.exists():
        candidates.extend(shared_proj.glob("*.jsonl"))
    for _, acct_str in accounts:
        p = Path(acct_str) / "projects" / enc
        if p.exists():
            candidates.extend(p.glob("*.jsonl"))
    if not candidates:
        return None
    latest = max(candidates, key=lambda p: p.stat().st_mtime)
    return latest.stem
