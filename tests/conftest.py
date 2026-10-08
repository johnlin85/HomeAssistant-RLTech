"""Pytest configuration for RLTech FTTR tests.

Two environments run the same suite (see tests/README.md):
- a plain venv with requirements.test.txt: the pure tests in tests/*.py run,
  tests/ha/ is skipped because Home Assistant is not installed;
- a Home Assistant 2026.9 environment with
  pytest-homeassistant-custom-component: everything runs, including tests/ha/.

The pure tests drive coroutines with asyncio.run(), so no event loop
fixture is overridden here: pytest-homeassistant-custom-component installs
session-scoped asyncio runners, and a function-scoped ``event_loop_policy``
override causes a ScopeMismatch there.
"""

from __future__ import annotations

import importlib.util

# tests/ha/ needs a Home Assistant runtime; skip it (without import errors)
# in the plain venv.
collect_ignore: list[str] = []
if importlib.util.find_spec("pytest_homeassistant_custom_component") is None:
    collect_ignore.append("ha")
