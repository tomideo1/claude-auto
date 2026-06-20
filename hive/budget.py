"""Per-account SDK credit accounting.

Post 2026-06-15, `claude -p` and Agent SDK calls draw from a fixed monthly
credit pool (Pro=$20, Max5x=$100, Max20x=$200). Once exhausted, calls fail
unless 'extra usage' is enabled per account, in which case they fall through
to pay-as-you-go API rates.

This module tracks per-account spend in `state/budget.json` and exposes
`can_afford()` for pre-flight checks before checking out an account.

Persisted shape:
    {
      "version": 1,
      "accounts": {
        "<name>": {
          "billing_cycle_start": "2026-05-17",
          "sdk_credit_cap_usd": 200.0,
          "sdk_credit_used_usd": 12.34,
          "extra_usage_enabled": false,
          "extra_usage_cap_usd": null,
          "extra_usage_used_usd": 0.0,
          "call_count": 42
        }
      }
    }
"""

from __future__ import annotations

import fcntl
import json
import os
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator


SCHEMA_VERSION = 1
CYCLE_LENGTH_DAYS = 30  # heuristic — see module docstring

# Per-tier defaults. Override per-account in ~/.claude-auto.json:
#   "budget": { "personal": { "tier": "max20x" }, "work": { "cap_usd": 100 } }
TIER_CAPS_USD: dict[str, float] = {
    "pro": 20.0,
    "max5x": 100.0,
    "max20x": 200.0,
    "team_standard": 20.0,
    "team_premium": 100.0,
    "enterprise_premium": 200.0,
}
DEFAULT_TIER = "max20x"


@dataclass
class BudgetEntry:
    account_name: str
    billing_cycle_start: str   # ISO date
    sdk_credit_cap_usd: float
    sdk_credit_used_usd: float
    extra_usage_enabled: bool
    extra_usage_cap_usd: float | None
    extra_usage_used_usd: float
    call_count: int

    @property
    def remaining_usd(self) -> float:
        base = max(0.0, self.sdk_credit_cap_usd - self.sdk_credit_used_usd)
        extra = 0.0
        if self.extra_usage_enabled:
            if self.extra_usage_cap_usd is None:
                extra = float("inf")
            else:
                extra = max(0.0, self.extra_usage_cap_usd - self.extra_usage_used_usd)
        return base + extra


class Budget:
    """File-backed monthly-cap tracker. flock-protected like AccountPool."""

    def __init__(
        self,
        state_path: Path,
        account_names: list[str],
        default_caps: dict[str, float] | None = None,
        extra_usage: dict[str, dict] | None = None,
    ):
        self.state_path = state_path
        self.account_names = list(account_names)
        # Per-account cap override; otherwise DEFAULT_TIER.
        self.default_caps = default_caps or {}
        # Per-account extra-usage config: {"enabled": bool, "cap_usd": float|None}
        self.extra_usage_config = extra_usage or {}

    # ---------------------------------------------------------------- locking

    @contextmanager
    def _locked(self) -> Iterator[dict]:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        if not self.state_path.exists():
            self.state_path.touch()
        with open(self.state_path, "r+") as f:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX)
            try:
                raw = f.read()
                state = json.loads(raw) if raw.strip() else {}
                state = self._migrate(state)
                self._auto_reset(state)
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

    def _default_entry(self, name: str) -> dict:
        cap = self.default_caps.get(name, TIER_CAPS_USD[DEFAULT_TIER])
        extra = self.extra_usage_config.get(name, {})
        return {
            "billing_cycle_start": date.today().isoformat(),
            "sdk_credit_cap_usd": float(cap),
            "sdk_credit_used_usd": 0.0,
            "extra_usage_enabled": bool(extra.get("enabled", False)),
            "extra_usage_cap_usd": extra.get("cap_usd"),
            "extra_usage_used_usd": 0.0,
            "call_count": 0,
        }

    def _migrate(self, state: dict) -> dict:
        state.setdefault("version", SCHEMA_VERSION)
        accts = state.setdefault("accounts", {})
        for name in self.account_names:
            if name not in accts:
                accts[name] = self._default_entry(name)
            else:
                # Ensure all fields present after schema bumps.
                default = self._default_entry(name)
                for k, v in default.items():
                    if k == "billing_cycle_start" and accts[name].get(k):
                        continue
                    if k == "sdk_credit_cap_usd" and accts[name].get(k):
                        continue
                    accts[name].setdefault(k, v)
        # Drop accounts that disappeared from config.
        for name in list(accts.keys()):
            if name not in self.account_names:
                del accts[name]
        return state

    def _auto_reset(self, state: dict) -> None:
        today = date.today()
        for name, entry in state["accounts"].items():
            try:
                started = date.fromisoformat(entry["billing_cycle_start"])
            except (ValueError, TypeError):
                started = today
                entry["billing_cycle_start"] = today.isoformat()
            if (today - started).days >= CYCLE_LENGTH_DAYS:
                entry["sdk_credit_used_usd"] = 0.0
                entry["extra_usage_used_usd"] = 0.0
                entry["call_count"] = 0
                entry["billing_cycle_start"] = today.isoformat()

    # -------------------------------------------------------------- queries

    def _entry(self, accts: dict, account: str) -> BudgetEntry:
        e = accts[account]
        return BudgetEntry(
            account_name=account,
            billing_cycle_start=e["billing_cycle_start"],
            sdk_credit_cap_usd=float(e["sdk_credit_cap_usd"]),
            sdk_credit_used_usd=float(e["sdk_credit_used_usd"]),
            extra_usage_enabled=bool(e["extra_usage_enabled"]),
            extra_usage_cap_usd=e.get("extra_usage_cap_usd"),
            extra_usage_used_usd=float(e.get("extra_usage_used_usd") or 0.0),
            call_count=int(e.get("call_count") or 0),
        )

    def get(self, account: str) -> BudgetEntry:
        with self._locked() as state:
            if account not in state["accounts"]:
                raise KeyError(f"unknown account: {account}")
            return self._entry(state["accounts"], account)

    def remaining(self, account: str) -> float:
        return self.get(account).remaining_usd

    def can_afford(self, account: str, est_cost_usd: float) -> bool:
        return self.remaining(account) >= max(0.0, est_cost_usd)

    # -------------------------------------------------------------- mutators

    def record_call(self, account: str, cost_usd: float) -> BudgetEntry:
        with self._locked() as state:
            if account not in state["accounts"]:
                raise KeyError(f"unknown account: {account}")
            e = state["accounts"][account]
            cost = float(cost_usd or 0.0)
            # Spill into extra usage once SDK credit is exhausted.
            base_remaining = max(0.0, e["sdk_credit_cap_usd"] - e["sdk_credit_used_usd"])
            if cost <= base_remaining:
                e["sdk_credit_used_usd"] = float(e["sdk_credit_used_usd"]) + cost
            else:
                e["sdk_credit_used_usd"] = e["sdk_credit_cap_usd"]
                spill = cost - base_remaining
                e["extra_usage_used_usd"] = float(e.get("extra_usage_used_usd") or 0.0) + spill
            e["call_count"] = int(e.get("call_count") or 0) + 1
            return self._entry(state["accounts"], account)

    def reset_cycle(self, account: str) -> None:
        with self._locked() as state:
            if account not in state["accounts"]:
                raise KeyError(f"unknown account: {account}")
            e = state["accounts"][account]
            e["sdk_credit_used_usd"] = 0.0
            e["extra_usage_used_usd"] = 0.0
            e["call_count"] = 0
            e["billing_cycle_start"] = date.today().isoformat()

    def set_cap(self, account: str, cap_usd: float) -> None:
        with self._locked() as state:
            if account not in state["accounts"]:
                raise KeyError(f"unknown account: {account}")
            state["accounts"][account]["sdk_credit_cap_usd"] = float(cap_usd)

    def status(self) -> dict:
        with self._locked() as state:
            out = {"accounts": []}
            for name in self.account_names:
                e = self._entry(state["accounts"], name)
                out["accounts"].append({
                    "name": e.account_name,
                    "billing_cycle_start": e.billing_cycle_start,
                    "cap_usd": e.sdk_credit_cap_usd,
                    "used_usd": round(e.sdk_credit_used_usd, 4),
                    "remaining_usd": round(e.remaining_usd, 4) if e.remaining_usd != float("inf") else "inf",
                    "extra_usage_enabled": e.extra_usage_enabled,
                    "extra_used_usd": round(e.extra_usage_used_usd, 4),
                    "call_count": e.call_count,
                })
            return out
