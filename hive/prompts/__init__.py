"""System-prompt templates for hive worker roles.

Each template is appended to the worker's claude invocation via
`--append-system-prompt`. Templates are markdown files in this directory;
they're user-editable.
"""

from __future__ import annotations

from pathlib import Path

PROMPTS_DIR = Path(__file__).resolve().parent

KNOWN_ROLES: list[str] = ["researcher", "planner", "code_editor", "orchestrator", "queen"]


class UnknownRoleError(ValueError):
    pass


def load_prompt(role: str) -> str:
    """Read the system-prompt template for `role`. Raises UnknownRoleError
    if the role has no template and isn't passed verbatim."""
    p = PROMPTS_DIR / f"{role}.md"
    if not p.exists():
        # Allow callers to pass an explicit free-form template instead of a role.
        raise UnknownRoleError(
            f"no prompt template for role {role!r}. Known roles: {', '.join(KNOWN_ROLES)}"
        )
    return p.read_text()


def render_prompt(role: str, **kwargs) -> str:
    """Load a template and substitute {placeholders}. Missing kwargs render
    as the literal '{placeholder}' so partial renders are still readable."""
    tmpl = load_prompt(role)
    try:
        return tmpl.format_map(_SafeDict(kwargs))
    except (KeyError, IndexError):
        return tmpl


class _SafeDict(dict):
    def __missing__(self, key: str) -> str:
        return "{" + key + "}"
