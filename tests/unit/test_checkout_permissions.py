"""What the subject may execute depends on what Git recorded, not on the host's umask or
the account that made the checkout."""

import os
from pathlib import Path

from relay.testing.oracle import normalise_permissions


def mode(path: Path) -> int:
    return path.stat().st_mode & 0o777


def test_the_same_tracked_files_get_the_same_permissions_however_they_were_checked_out(
    tmp_path: Path,
) -> None:
    for host, (helper, data, directory) in {
        "restrictive": (0o700, 0o600, 0o700),
        "ordinary": (0o755, 0o644, 0o755),
    }.items():
        checkout = tmp_path / host
        (checkout / "bin").mkdir(parents=True)
        (checkout / "bin" / "helper").write_text("#!/bin/sh\n")
        (checkout / "data.txt").write_text("x")
        (checkout / "bin" / "helper").chmod(helper)
        (checkout / "data.txt").chmod(data)
        (checkout / "bin").chmod(directory)

        normalise_permissions(checkout)

        assert mode(checkout / "bin" / "helper") == 0o755
        assert mode(checkout / "data.txt") == 0o644
        assert mode(checkout / "bin") == 0o755


def test_a_symbolic_link_is_left_alone(tmp_path: Path) -> None:
    outside = tmp_path / "outside.txt"
    outside.write_text("x")
    outside.chmod(0o600)
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    os.symlink(outside, checkout / "link")

    normalise_permissions(checkout)

    assert mode(outside) == 0o600
