"""The boundary suite against the real Claude CLI. It makes paid calls (about a cent for
both roles), so it runs only when asked: `RELAY_REAL_CLAUDE=1`. It is how a new version
of the CLI, or a change to the settings Relay launches a run with, is shown to hold."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

from relay.providers.boundary_suite import run_suite

pytestmark = pytest.mark.skipif(
    os.environ.get("RELAY_REAL_CLAUDE") != "1", reason="makes paid calls: set RELAY_REAL_CLAUDE=1"
)


@pytest.mark.timeout(600)
@pytest.mark.parametrize("role", ["qa", "builder"])
async def test_the_bounds_of_a_role_hold_against_the_real_cli(role: str) -> None:
    with tempfile.TemporaryDirectory(prefix="relay-ws-") as root:
        result = await run_suite(role, Path(root).resolve())

    assert [each.probe for each in result.observations if not each.passed] == []
    assert result.passed
