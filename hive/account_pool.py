"""Account pool state machine.

Persists to `state/pool.json`. Every state-mutating operation takes
`fcntl.flock` on the file so multiple processes (the queen + worker spawns)
can share it safely.

Schema:
    {
      "version": 1,
      "queen": "personal",
      "current_cli_idx": 0,
      "accounts": {
        "<name>": {
          "dir": "/path/...",
          "last_used_at": null|float,
          "cold_until": null|float,
          "cold_reason": null|string,
          "in_use_by_job": null|string,
          "sdk_credit_used_usd": 0.0,
          "sdk_credit_cap_usd": 0.0,
          "interactive_extra_usage_usd": 0.0
        }, ...
      }
    }
"""

from __future__ import annotations

import fcntl
import json
import os
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator, Literal

SCHEMA_VERSION = 1

LEGACY_IDX_FILE = Path("/tmp/.claude_account_idx")
# Only relevant to the optional "hive" multi-agent mode. Everyday rotation
# ignores it. Set `"queen": "<name>"` in ~/.claude-auto.json to use hive mode.
DEFAULT_QUEEN = ""


@dataclass
class AccountState:
    name: str
    dir: str
    last_used_at: float | None = None
    cold_until: float | None = None
    cold_reason: str | None = None
    in_use_by_job: str | None = None
    sdk_credit_used_usd: float = 0.0
    sdk_credit_cap_usd: float = 0.0
    interactive_extra_usage_usd: float = 0.0

    @property
    def is_cold(self) -> bool:
        return self.cold_until is not None and self.cold_until > time.time()

    @property
    def is_in_use(self) -> bool:
        return self.in_use_by_job is not None

    def to_dict(self) -> dict:
        return {
            "dir": self.dir,
            "last_used_at": self.last_used_at,
            "cold_until": self.cold_until,
            "cold_reason": self.cold_reason,
            "in_use_by_job": self.in_use_by_job,
            "sdk_credit_used_usd": self.sdk_credit_used_usd,
            "sdk_credit_cap_usd": self.sdk_credit_cap_usd,
            "interactive_extra_usage_usd": self.interactive_extra_usage_usd,
        }

    @classmethod
    def from_dict(cls, name: str, d: dict, fallback_dir: str) -> "AccountState":
        return cls(
            name=name,
            dir=d.get("dir") or fallback_dir,
            last_used_at=d.get("last_used_at"),
            cold_until=d.get("cold_until"),
            cold_reason=d.get("cold_reason"),
            in_use_by_job=d.get("in_use_by_job"),
            sdk_credit_used_usd=float(d.get("sdk_credit_used_usd") or 0.0),
            sdk_credit_cap_usd=float(d.get("sdk_credit_cap_usd") or 0.0),
            interactive_extra_usage_usd=float(d.get("interactive_extra_usage_usd") or 0.0),
        )


class AccountPoolError(Exception):
    pass


class NoAccountAvailable(AccountPoolError):
    pass


class AccountPool:
    """File-backed account state with flock-protected mutation."""

    def __init__(
        self,
        accounts: list[tuple[str, str]],
        state_path: Path,
        queen: str | None = None,
    ):
        # Preserve config order — used as a deterministic tiebreaker in checkout().
        self._config_order: list[str] = [n for n, _ in accounts]
        self._config_dirs: dict[str, str] = {n: d for n, d in accounts}
        self.state_path = state_path
        self._configured_queen = queen or DEFAULT_QUEEN

    # ---------------------------------------------------------------- locking

    @contextmanager
    def _locked(self) -> Iterator[dict]:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        if not self.state_path.exists():
            self.state_path.touch()
        # r+ requires the file to exist; we just touched it.
        with open(self.state_path, "r+") as f:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX)
            try:
                raw = f.read()
                state = json.loads(raw) if raw.strip() else {}
                state = self._migrate_in_place(state)
                yield state
                f.seek(0)
                f.truncate()
                json.dump(state, f, indent=2, sort_keys=True)
                f.write("\n")
                f.flush()
                os.fsync(f.fileno())
            finally:
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)

    # ------------------------------------------------------------- migration

    def _migrate_in_place(self, state: dict) -> dict:
        """Fill in missing fields, drop unknown accounts, adopt /tmp idx.

        Config (`~/.claude-auto.json`'s "queen" field) is authoritative for
        the queen name — pool.json caches it but a config change always wins
        on the next load. Use `claude-auto hive set-queen NAME` to change.
        """
        state.setdefault("version", SCHEMA_VERSION)
        state["queen"] = self._configured_queen

        accts = state.setdefault("accounts", {})
        # Add any accounts in config that aren't in state yet.
        for name, dir_ in self._config_dirs.items():
            if name not in accts:
                accts[name] = AccountState(name=name, dir=dir_).to_dict()
            else:
                # Keep the on-disk dir if it matches config; otherwise update.
                accts[name]["dir"] = dir_
                # Defensive: ensure all fields are present.
                blank = AccountState(name=name, dir=dir_).to_dict()
                for k, v in blank.items():
                    accts[name].setdefault(k, v)
        # Drop accounts that are no longer in config.
        for name in list(accts.keys()):
            if name not in self._config_dirs:
                del accts[name]

        # First-time migration from /tmp/.claude_account_idx.
        if "current_cli_idx" not in state:
            idx = 0
            try:
                if LEGACY_IDX_FILE.exists():
                    raw = LEGACY_IDX_FILE.read_text().strip()
                    if raw:
                        idx = int(raw) % max(1, len(self._config_order))
            except (ValueError, OSError):
                idx = 0
            state["current_cli_idx"] = idx

        # Clamp idx if config shrank.
        n = len(self._config_order)
        if n == 0:
            state["current_cli_idx"] = 0
        else:
            state["current_cli_idx"] = state["current_cli_idx"] % n

        return state

    # ------------------------------------------------------------------ reads

    def _account_from_state(self, name: str, accts: dict) -> AccountState:
        return AccountState.from_dict(name, accts[name], self._config_dirs.get(name, ""))

    def all(self) -> list[AccountState]:
        with self._locked() as state:
            return [self._account_from_state(n, state["accounts"]) for n in self._config_order]

    def by_name(self, name: str) -> AccountState:
        with self._locked() as state:
            if name not in state["accounts"]:
                raise AccountPoolError(f"unknown account: {name}")
            return self._account_from_state(name, state["accounts"])

    def queen_name(self) -> str:
        with self._locked() as state:
            return state["queen"]

    def queen_account(self) -> AccountState:
        with self._locked() as state:
            q = state["queen"]
            if q not in state["accounts"]:
                raise AccountPoolError(f"queen account {q!r} not configured")
            return self._account_from_state(q, state["accounts"])

    def status(self) -> dict:
        """Snapshot of the full pool for display / introspection."""
        now = time.time()
        with self._locked() as state:
            out = {
                "queen": state["queen"],
                "current_cli_idx": state["current_cli_idx"],
                "current_cli_account": (
                    self._config_order[state["current_cli_idx"]]
                    if self._config_order else None
                ),
                "accounts": [],
            }
            for name in self._config_order:
                a = self._account_from_state(name, state["accounts"])
                out["accounts"].append({
                    "name": a.name,
                    "dir": a.dir,
                    "last_used_at": a.last_used_at,
                    "cold_until": a.cold_until,
                    "cold_seconds_left": max(0, int(a.cold_until - now)) if a.cold_until else 0,
                    "cold_reason": a.cold_reason,
                    "in_use_by_job": a.in_use_by_job,
                    "sdk_credit_used_usd": a.sdk_credit_used_usd,
                    "sdk_credit_cap_usd": a.sdk_credit_cap_usd,
                })
            return out

    # ------------------------------------------------------ rich (Phase 2+)

    def checkout(
        self,
        job_id: str,
        kind: Literal["interactive", "headless"] = "headless",
        exclude_queen: bool = True,
    ) -> AccountState:
        """Pick the warmest, freshest, cheapest non-cold account and lock it
        to job_id. Raises NoAccountAvailable if nothing usable is left."""
        now = time.time()
        with self._locked() as state:
            queen = state["queen"]
            candidates = []
            for name in self._config_order:
                a = self._account_from_state(name, state["accounts"])
                if a.cold_until and a.cold_until > now:
                    continue
                if a.in_use_by_job is not None:
                    continue
                if exclude_queen and name == queen:
                    continue
                if kind == "headless" and a.sdk_credit_cap_usd > 0 \
                        and a.sdk_credit_used_usd >= a.sdk_credit_cap_usd:
                    continue
                candidates.append(a)
            if not candidates:
                raise NoAccountAvailable(
                    f"no warm account for kind={kind} (queen={queen}, exclude_queen={exclude_queen})"
                )

            # Selection: lowest last_used_at first (None counts as -inf so unused
            # accounts go first), tiebreaker on lowest credit used, then config order.
            def sort_key(a: AccountState) -> tuple:
                lua = a.last_used_at if a.last_used_at is not None else float("-inf")
                return (lua, a.sdk_credit_used_usd, self._config_order.index(a.name))

            candidates.sort(key=sort_key)
            chosen = candidates[0]
            state["accounts"][chosen.name]["in_use_by_job"] = job_id
            state["accounts"][chosen.name]["last_used_at"] = now
            return self._account_from_state(chosen.name, state["accounts"])

    def release(
        self,
        account_name: str,
        job_id: str,
        cost_usd: float = 0.0,
        rate_limited: bool = False,
        reset_hint: str | None = None,
        reset_time_iso: str | None = None,
    ) -> None:
        with self._locked() as state:
            if account_name not in state["accounts"]:
                raise AccountPoolError(f"unknown account: {account_name}")
            a = state["accounts"][account_name]
            if a.get("in_use_by_job") not in (None, job_id):
                # Released by a different job; log it but don't fail — likely a
                # crash recovery scenario.
                pass
            a["in_use_by_job"] = None
            if cost_usd > 0:
                a["sdk_credit_used_usd"] = float(a.get("sdk_credit_used_usd", 0.0)) + float(cost_usd)
            if rate_limited:
                a["cold_until"] = _cold_until_from_reset(reset_time_iso, reset_hint)
                a["cold_reason"] = reset_hint

    def mark_cold(self, account_name: str, until: float, reason: str | None) -> None:
        with self._locked() as state:
            if account_name not in state["accounts"]:
                raise AccountPoolError(f"unknown account: {account_name}")
            state["accounts"][account_name]["cold_until"] = float(until)
            state["accounts"][account_name]["cold_reason"] = reason

    def set_queen(self, name: str) -> None:
        with self._locked() as state:
            if name not in state["accounts"]:
                raise AccountPoolError(f"unknown account: {name}")
            state["queen"] = name

    # ----------------------------------------------------- CLI back-compat

    def cli_current(self) -> AccountState:
        """The account the CLI is currently pointing at (round-robin idx)."""
        with self._locked() as state:
            idx = state["current_cli_idx"]
            name = self._config_order[idx]
            return self._account_from_state(name, state["accounts"])

    def cli_idx_of(self, name: str) -> int:
        """Index of `name` in config order; raises if unknown."""
        if name not in self._config_order:
            raise AccountPoolError(f"unknown account: {name}")
        return self._config_order.index(name)

    def cli_set_idx(self, idx: int) -> None:
        n = len(self._config_order)
        if n == 0:
            return
        with self._locked() as state:
            state["current_cli_idx"] = idx % n

    def cli_set_account(self, name: str) -> int:
        """Point the CLI at `name`. Returns the new idx; raises if unknown."""
        if name not in self._config_order:
            raise AccountPoolError(f"unknown account: {name}")
        idx = self._config_order.index(name)
        self.cli_set_idx(idx)
        return idx

    def cli_advance(self) -> int:
        """Move the CLI pointer to the next account (mod len). Returns new idx."""
        n = len(self._config_order)
        if n == 0:
            return 0
        with self._locked() as state:
            state["current_cli_idx"] = (state["current_cli_idx"] + 1) % n
            return state["current_cli_idx"]

    def cli_mark_cold(
        self,
        name: str,
        reset_hint: str | None,
        reset_time_iso: str | None,
    ) -> None:
        """Mark the named account cold based on a parsed rate-limit hit."""
        until = _cold_until_from_reset(reset_time_iso, reset_hint)
        self.mark_cold(name, until, reset_hint)

    def cli_touch(self, name: str) -> None:
        """Update last_used_at for an account — used right after a clean launch."""
        with self._locked() as state:
            if name in state["accounts"]:
                state["accounts"][name]["last_used_at"] = time.time()


def _cold_until_from_reset(reset_time_iso: str | None, reset_hint: str | None) -> float:
    """Turn a parsed reset hint into an epoch. Falls back to +5h if unknown
    (the typical Claude Code interactive window length)."""
    if reset_time_iso:
        try:
            from datetime import datetime
            dt = datetime.fromisoformat(reset_time_iso)
            return dt.timestamp()
        except ValueError:
            pass
    # Fallback heuristic. We don't want to keep an account "cold" forever just
    # because we couldn't parse the banner.
    return time.time() + 5 * 3600
