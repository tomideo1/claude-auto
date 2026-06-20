"""Hive: multi-account Claude Code orchestration.

Phase 1 ships the library refactor of claude-auto: the same CLI behavior,
but the internals (account pool, rate-limit parsing, PTY runner) are now
importable modules ready for the MCP layer in Phase 2.
"""

__version__ = "0.1.0"
