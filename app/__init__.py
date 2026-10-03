"""AI Hive — multi-agent control center."""

# Single source of truth for the app version. Everything that shows or reports a
# version reads THIS (the title-bar badge in main_window, the MCP serverInfo) —
# never hard-code it elsewhere. Pre-1.0 (0.x.y) while the app is not yet ready
# to go live: bump y for fixes/small changes, x for notable features; 1.0.0 is
# reserved for the first public-ready release.
#
# BUMP THIS ON EVERY PR MERGED TO main, and add the matching `## x.y.z`
# section to CHANGELOG.md in the same change. The in-app updater
# (app/self_update.py) offers an update only when main's version is HIGHER than
# this one, and shows the CHANGELOG sections in between. The smoke suite checks
# that CHANGELOG.md's newest section equals this value.
__version__ = "0.23.2"
