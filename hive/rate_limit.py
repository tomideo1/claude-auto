"""Rate-limit detection.

Patterns and parsers lifted from claude-auto.py. The main entry point is
`parse_for_rate_limit(text)` which returns a structured `RateLimitHit` with
a best-effort ISO timestamp for when the account thaws.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Iterable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


DEFAULT_RATE_LIMIT_PATTERNS: list[str] = [
    # Observed: "You've hit your limit · resets 2am (Africa/Lagos)"
    # Anchored on " resets" to avoid false-matching prose about hitting a limit.
    r"you'?ve\s+hit\s+your\s+limit.{0,8}resets",
    # Observed: "/upgrade to increase your usage limit."
    r"/upgrade\s+to\s+increase\s+your\s+usage\s+limit",
    # Older/alternate phrasings, kept as fallbacks:
    r"claude\s+(ai\s+)?usage\s+limit\s+reached",
    r"5[-\s]?hour\s+limit\s+reached",
    r"limit\s+will\s+reset\s+at",
    r"you'?ve\s+hit\s+the\s+(usage|rate)\s+limit",
    r"rate\s*limit(ed)?\b.{0,40}(retry|reset|try\s+again)",
]

ANSI_RE = re.compile(rb"\x1b\[[0-9;?]*[a-zA-Z]|\x1b\][^\x07]*\x07|\x1b[()][AB012]")

RESET_HINT_RE = re.compile(r"resets?\s+[^\r\n]{1,40}", re.IGNORECASE)

_TIME_OF_DAY_RE = re.compile(
    r"\b(?P<h>\d{1,2})(?::(?P<m>\d{2}))?\s*(?P<ampm>am|pm)\b", re.IGNORECASE
)
_HHMM_RE = re.compile(r"\b(?P<h>\d{1,2}):(?P<m>\d{2})\b")
_TZ_PAREN_RE = re.compile(r"\(([^)]+)\)")
_TZ_SHORT_RE = re.compile(r"\b(UTC|GMT|PST|PDT|EST|EDT|CST|CDT|MST|MDT|BST|CET|CEST|WAT)\b")
_IN_DURATION_RE = re.compile(
    r"\bin\s+(?P<n>\d{1,3})\s+(?P<unit>second|seconds|minute|minutes|hour|hours|day|days)\b",
    re.IGNORECASE,
)

_TZ_ALIASES = {
    "UTC": "UTC", "GMT": "UTC",
    "PST": "America/Los_Angeles", "PDT": "America/Los_Angeles",
    "EST": "America/New_York",     "EDT": "America/New_York",
    "CST": "America/Chicago",      "CDT": "America/Chicago",
    "MST": "America/Denver",       "MDT": "America/Denver",
    "BST": "Europe/London",
    "CET": "Europe/Paris",         "CEST": "Europe/Paris",
    "WAT": "Africa/Lagos",
}


@dataclass(frozen=True)
class RateLimitHit:
    matched_pattern: str       # the regex source that matched (for debugging)
    matched_text: str          # the actual substring from the input
    reset_hint: str | None     # "resets 2am (Africa/Lagos)" snippet, if any
    reset_time_iso: str | None # best-effort ISO 8601, None if unparseable


def compile_patterns(patterns: Iterable[str] | None = None) -> re.Pattern[str]:
    """Compile a list of rate-limit patterns into a single case-insensitive regex."""
    pats = list(patterns) if patterns else DEFAULT_RATE_LIMIT_PATTERNS
    return re.compile("|".join(pats), re.IGNORECASE)


def strip_ansi(b: bytes) -> bytes:
    return ANSI_RE.sub(b"", b)


def _resolve_tz(name: str) -> ZoneInfo | None:
    name = name.strip()
    if not name:
        return None
    if name in _TZ_ALIASES:
        try:
            return ZoneInfo(_TZ_ALIASES[name])
        except ZoneInfoNotFoundError:
            return None
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError:
        return None


def _extract_tz(hint: str) -> ZoneInfo | None:
    m = _TZ_PAREN_RE.search(hint)
    if m:
        tz = _resolve_tz(m.group(1))
        if tz:
            return tz
    m = _TZ_SHORT_RE.search(hint)
    if m:
        tz = _resolve_tz(m.group(1))
        if tz:
            return tz
    return None


def _next_occurrence(now_in_tz: datetime, hour_24: int, minute: int) -> datetime:
    candidate = now_in_tz.replace(hour=hour_24, minute=minute, second=0, microsecond=0)
    if candidate <= now_in_tz:
        candidate += timedelta(days=1)
    return candidate


def parse_reset_hint_to_iso(hint: str, *, now: datetime | None = None) -> str | None:
    """Best-effort: turn 'resets 2am (Africa/Lagos)' or 'resets in 4 hours'
    into an ISO 8601 timestamp. Returns None if nothing parseable."""
    if not hint:
        return None
    now_utc = now or datetime.now(timezone.utc)

    # Relative: "resets in N <unit>"
    m = _IN_DURATION_RE.search(hint)
    if m:
        n = int(m.group("n"))
        unit = m.group("unit").lower().rstrip("s")
        delta = {
            "second": timedelta(seconds=n),
            "minute": timedelta(minutes=n),
            "hour":   timedelta(hours=n),
            "day":    timedelta(days=n),
        }[unit]
        return (now_utc + delta).isoformat()

    tz = _extract_tz(hint) or timezone.utc
    now_local = now_utc.astimezone(tz)

    # Absolute am/pm: "2am" / "2:30pm"
    m = _TIME_OF_DAY_RE.search(hint)
    if m:
        h = int(m.group("h"))
        minute = int(m.group("m") or 0)
        is_pm = m.group("ampm").lower() == "pm"
        if h == 12:
            h_24 = 12 if is_pm else 0
        else:
            h_24 = h + 12 if is_pm else h
        if 0 <= h_24 <= 23 and 0 <= minute <= 59:
            return _next_occurrence(now_local, h_24, minute).astimezone(timezone.utc).isoformat()

    # 24h: "14:00"
    m = _HHMM_RE.search(hint)
    if m:
        h_24 = int(m.group("h"))
        minute = int(m.group("m"))
        if 0 <= h_24 <= 23 and 0 <= minute <= 59:
            return _next_occurrence(now_local, h_24, minute).astimezone(timezone.utc).isoformat()

    return None


def parse_for_rate_limit(
    text: str,
    compiled_re: re.Pattern[str] | None = None,
) -> RateLimitHit | None:
    """Scan text for a rate-limit signal. Returns a structured hit or None."""
    pat = compiled_re or compile_patterns()
    m = pat.search(text)
    if not m:
        return None
    hint_m = RESET_HINT_RE.search(text)
    hint = hint_m.group(0).strip() if hint_m else None
    iso = parse_reset_hint_to_iso(hint) if hint else None
    return RateLimitHit(
        matched_pattern=m.re.pattern,
        matched_text=m.group(0),
        reset_hint=hint,
        reset_time_iso=iso,
    )
