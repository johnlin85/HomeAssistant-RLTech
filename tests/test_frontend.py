"""Frontend checks: JS syntax (node --check) and a jsdom smoke test.

Skipped when Node is not installed (for example in the HA test container).
The smoke test also needs jsdom (``NODE_PATH`` or a local ``node_modules``).
"""

from __future__ import annotations

from pathlib import Path
import shutil
import subprocess

import pytest

WWW = Path(__file__).parents[1] / "custom_components" / "rltech_fttr" / "www"
NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="node is not installed")


@pytest.mark.parametrize("script", sorted(WWW.glob("*.js")), ids=lambda p: p.name)
def test_js_syntax(script: Path) -> None:
    result = subprocess.run(
        [NODE, "--check", str(script)], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr


def test_frontend_smoke() -> None:
    probe = subprocess.run(
        [NODE, "-e", "require.resolve('jsdom')"], capture_output=True, check=False
    )
    if probe.returncode != 0:
        pytest.skip("jsdom is not installed")
    result = subprocess.run(
        [NODE, str(Path(__file__).parent / "js" / "smoke.cjs")],
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "frontend smoke ok" in result.stdout
