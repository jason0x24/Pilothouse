"""Shared state for the directory-plugin test.

`tests/fixtures/sample_plugin.py` is loaded TWICE in the plugin test:
once by the test itself via the normal Python import system, once by
the directory discovery path which gives it a fresh module name. Each
import gets its own copy of class-level state, so we can't assert
through `SamplePlugin.seen_events` alone.

This module is imported by name (`tests.fixtures.sample_plugin_sink`)
from both sides, so its `EVENTS` list is the single observable they
share.
"""

EVENTS: list[str] = []
