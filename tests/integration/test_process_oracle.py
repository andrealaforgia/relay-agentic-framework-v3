"""The oracle launches a real subject from a pristine checkout and records what it saw.
The subject's own claims, and any file it writes, carry no weight."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from relay.domain.evidence import Ineligibility, Launch
from relay.git.checkout import CheckoutFailed, pristine_checkout, trunk_head
from relay.testing.oracle import NativeIsolation, ProcessOracle
from tests.integration.fixtures import VAT_RETURNS_NET, commit


async def observe(tmp_path: Path, files: dict[str, str], launch: Launch):  # type: ignore[no-untyped-def]
    revision = commit(tmp_path / "subject", files)
    workdir = await pristine_checkout(str(tmp_path / "subject"), revision, tmp_path / "checkout")
    return await ProcessOracle(NativeIsolation()).observe(workdir, launch)


async def test_it_records_exit_code_and_complete_streams(tmp_path: Path) -> None:
    observation = await observe(
        tmp_path, {"vat.py": VAT_RETURNS_NET}, Launch(argv=("python3", "vat.py", "100"))
    )

    assert observation.is_eligible
    assert observation.exit_code == 0
    assert observation.stdout == b"100\n"
    assert observation.stderr == b""


async def test_standard_input_reaches_the_subject(tmp_path: Path) -> None:
    echo = "import sys\nprint(sys.stdin.read().upper(), end='')\n"

    observation = await observe(
        tmp_path, {"echo.py": echo}, Launch(argv=("python3", "echo.py"), stdin="net 100\n")
    )

    assert observation.stdout == b"NET 100\n"


async def test_a_program_that_does_not_exist_was_not_executed(tmp_path: Path) -> None:
    observation = await observe(
        tmp_path, {"vat.py": VAT_RETURNS_NET}, Launch(argv=("no-such-interpreter", "vat.py"))
    )

    assert observation.ineligibility is Ineligibility.NOT_EXECUTED


@pytest.mark.parametrize("ambiguous_status", [126, 127])
async def test_a_launcher_status_is_treated_as_not_executed(
    tmp_path: Path, ambiguous_status: int
) -> None:
    exits = f"import sys\nsys.exit({ambiguous_status})\n"

    observation = await observe(tmp_path, {"x.py": exits}, Launch(argv=("python3", "x.py")))

    assert observation.ineligibility is Ineligibility.NOT_EXECUTED


async def test_a_subject_that_hangs_is_stopped_and_marked(tmp_path: Path) -> None:
    hangs = "import time\nprint('100', flush=True)\ntime.sleep(60)\n"

    observation = await observe(
        tmp_path, {"vat.py": hangs}, Launch(argv=("python3", "vat.py"), timeout_seconds=0.5)
    )

    assert observation.ineligibility is Ineligibility.TIMED_OUT


async def test_a_subject_killed_by_a_signal_is_marked(tmp_path: Path) -> None:
    dies = "import os, signal\nprint('100', flush=True)\nos.kill(os.getpid(), signal.SIGKILL)\n"

    observation = await observe(tmp_path, {"vat.py": dies}, Launch(argv=("python3", "vat.py")))

    assert observation.ineligibility is Ineligibility.KILLED_BY_SIGNAL


async def test_output_beyond_the_capture_limit_is_not_silently_truncated(tmp_path: Path) -> None:
    floods = "print('120')\nprint('x' * 5_000_000)\n"
    revision = commit(tmp_path / "subject", {"vat.py": floods})
    workdir = await pristine_checkout(str(tmp_path / "subject"), revision, tmp_path / "checkout")

    observation = await ProcessOracle(NativeIsolation(), capture_limit_bytes=1_000_000).observe(
        workdir, Launch(argv=("python3", "vat.py"))
    )

    assert observation.ineligibility is Ineligibility.OUTPUT_OVERFLOW


async def test_the_subject_does_not_inherit_the_supervisor_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("RELAY_WORKER_TOKEN", "canary-credential")
    monkeypatch.setenv("VAT_RATE", "20")
    leaks = (
        "import os\n"
        "print(os.environ.get('RELAY_WORKER_TOKEN', 'absent'))\n"
        "print(os.environ.get('VAT_RATE', 'absent'))\n"
    )

    observation = await observe(tmp_path, {"leak.py": leaks}, Launch(argv=("python3", "leak.py")))

    assert observation.stdout == b"absent\nabsent\n"


async def test_the_checkout_is_exactly_the_pinned_revision(tmp_path: Path) -> None:
    repository = tmp_path / "subject"
    baseline = commit(repository, {"vat.py": VAT_RETURNS_NET})
    commit(repository, {"vat.py": "import sys\nprint(int(sys.argv[1]) * 120 // 100)\n"})

    workdir = await pristine_checkout(str(repository), baseline, tmp_path / "checkout")
    observation = await ProcessOracle(NativeIsolation()).observe(
        workdir, Launch(argv=("python3", "vat.py", "100"))
    )

    assert observation.stdout == b"100\n"


@pytest.mark.parametrize("revision", ["main", "HEAD", "--upload-pack=touch pwned", "abc123"])
async def test_only_a_full_commit_hash_can_be_checked_out(tmp_path: Path, revision: str) -> None:
    commit(tmp_path / "subject", {"vat.py": VAT_RETURNS_NET})

    with pytest.raises(CheckoutFailed, match="40-character"):
        await pristine_checkout(str(tmp_path / "subject"), revision, tmp_path / "checkout")


async def test_an_unknown_revision_fails_loudly(tmp_path: Path) -> None:
    commit(tmp_path / "subject", {"vat.py": VAT_RETURNS_NET})

    with pytest.raises(CheckoutFailed):
        await pristine_checkout(str(tmp_path / "subject"), "0" * 40, tmp_path / "checkout")


def branch(repository: Path, name: str, revision: str) -> None:
    subprocess.run(["git", "-C", str(repository), "branch", "-f", name, revision], check=True)


async def test_the_trunk_is_the_exactly_named_branch_not_one_that_ends_the_same_way(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "subject"
    impostor = commit(repository, {"vat.py": VAT_RETURNS_NET})
    trunk = commit(repository, {"vat.py": "print(120)\n"})
    branch(repository, "a/refs/heads/main", impostor)

    assert await trunk_head(str(repository), "main") == trunk


async def test_a_look_alike_branch_does_not_stand_in_for_a_missing_trunk(tmp_path: Path) -> None:
    repository = tmp_path / "subject"
    impostor = commit(repository, {"vat.py": VAT_RETURNS_NET})
    branch(repository, "a/refs/heads/release", impostor)

    with pytest.raises(CheckoutFailed, match="no branch 'release'"):
        await trunk_head(str(repository), "release")
