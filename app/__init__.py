"""AI Hive — multi-agent control center."""

# Single source of truth for the app version. Everything that shows or reports a
# version reads THIS (the title-bar badge in main_window, the MCP serverInfo) —
# never hard-code it elsewhere. Pre-1.0 (0.x.y) while the app is not yet ready
# to go live: bump y for fixes/small changes, x for notable features; 1.0.0 is
# reserved for the first public-ready release.
#
# BUMP THIS ON EVERY PR MERGED TO main — the version must always increase, and
# there is no CI check enforcing that yet (see CLAUDE.md's Conventions
# section), so it is on you, not a bot, to remember it before merging.
__version__ = "0.21.1"
