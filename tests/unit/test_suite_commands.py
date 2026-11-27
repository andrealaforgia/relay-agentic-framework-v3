"""The shell commands a boundary run is told to make are aimed where they say."""

from __future__ import annotations

import shlex
from pathlib import Path

from relay.providers.boundary_suite import Scene, _commands


def test_a_root_with_a_space_in_its_name_is_one_operand_of_each_probe() -> None:
    """3C-S1B-3.6: unquoted, the marker's path would be two operands and the probe would
    be aimed at neither."""
    root = Path("/Users/Shared/Relay Work/run")
    scene = Scene(
        "builder", root / "tree", root / "beside", Path("/Users/po/.relay boundary"),
        "IN", "OUT", "HOME", "secret", 4242, Path("/Users/Shared/Relay Work/other/tree"), "OTHER",
    )  # fmt: skip

    run = _commands(scene)

    assert shlex.split(run["beside"]) == ["cat", str(root / "beside" / "marker.txt")]
    assert shlex.split(run["write"]) == ["echo", "x", ">", str(root / "beside" / "written.txt")]
    assert shlex.split(run["home"]) == ["cat", "/Users/po/.relay boundary/marker.txt"]
    assert shlex.split(run["other"]) == ["cat", "/Users/Shared/Relay Work/other/tree/marker.txt"]
    plain = _commands(
        Scene("builder", Path("/w/run/tree"), Path("/w/run/beside"), Path("/h"),
              "IN", "OUT", "HOME", "secret", 4242, Path("/w/o/tree"), "OTHER")
    )  # fmt: skip
    assert plain["beside"] == "cat /w/run/beside/marker.txt"  # and nothing is quoted needlessly
