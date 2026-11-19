"""The execution boundary, shown with canaries. A canary counts only if the probe really
attempted the operation: the same probe, run natively, succeeds at what the container denies."""

from __future__ import annotations

import socket
import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest

from relay.domain.evidence import Ineligibility, Launch
from relay.git.checkout import pristine_checkout
from relay.testing.oracle import ContainerIsolation, Isolation, NativeIsolation, ProcessOracle
from tests.integration.fixtures import VAT_RETURNS_NET, commit

PROBE = """
import os, socket, sys

def attempt(label, operation):
    try:
        operation()
        print(label, "allowed")
    except OSError:
        print(label, "denied")

attempt("write to checkout:", lambda: open("planted.txt", "w").write("x"))
attempt("read host file:", lambda: open(sys.argv[1]).read())
attempt("reach the host:", lambda: socket.create_connection((sys.argv[2], int(sys.argv[3])), 3))
print("supervisor credential:", os.environ.get("RELAY_WORKER_TOKEN", "absent"))
"""


@pytest.fixture(scope="session")
def runner_image() -> str:
    subprocess.run(["docker", "pull", "-q", "python:3.14-alpine"], check=True, capture_output=True)
    return subprocess.run(
        [
            "docker",
            "image",
            "inspect",
            "python:3.14-alpine",
            "--format",
            "{{index .RepoDigests 0}}",
        ],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def running_oracle_containers() -> list[str]:
    return subprocess.run(
        ["docker", "ps", "--quiet", "--filter", "name=relay-oracle-"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.split()


@pytest.fixture
def host_listener() -> Iterator[int]:
    """Something on the host that a subject with network access could reach."""
    with socket.create_server(("0.0.0.0", 0)) as server:
        yield server.getsockname()[1]


async def probe(tmp_path: Path, isolation: Isolation, host: str, port: int) -> list[str]:
    secret = tmp_path / "host-secret.txt"
    secret.write_text("canary")
    revision = commit(tmp_path / "subject", {"probe.py": PROBE})
    workdir = await pristine_checkout(str(tmp_path / "subject"), revision, tmp_path / "checkout")
    observation = await ProcessOracle(isolation).observe(
        workdir,
        Launch(argv=("python3", "probe.py", str(secret), host, str(port)), timeout_seconds=30),
    )
    assert observation.is_eligible, observation
    return observation.stdout.decode().splitlines()


async def test_the_container_denies_what_the_probe_attempts(
    tmp_path: Path, runner_image: str, host_listener: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("RELAY_WORKER_TOKEN", "canary-credential")
    isolation = ContainerIsolation(runner_image)

    assert await probe(tmp_path, isolation, "host.docker.internal", host_listener) == [
        "write to checkout: denied",
        "read host file: denied",
        "reach the host: denied",
        "supervisor credential: absent",
    ]


async def test_the_probe_is_live_native_execution_allows_what_the_container_denies(
    tmp_path: Path, host_listener: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("RELAY_WORKER_TOKEN", "canary-credential")

    assert await probe(tmp_path, NativeIsolation(), "127.0.0.1", host_listener) == [
        "write to checkout: allowed",
        "read host file: allowed",
        "reach the host: allowed",
        "supervisor credential: absent",
    ]


async def test_a_subject_is_observed_through_the_container(
    tmp_path: Path, runner_image: str
) -> None:
    revision = commit(tmp_path / "subject", {"vat.py": VAT_RETURNS_NET})
    workdir = await pristine_checkout(str(tmp_path / "subject"), revision, tmp_path / "checkout")

    observation = await ProcessOracle(ContainerIsolation(runner_image)).observe(
        workdir, Launch(argv=("python3", "vat.py", "100"), timeout_seconds=30)
    )

    assert (observation.exit_code, observation.stdout, observation.stderr) == (0, b"100\n", b"")


async def test_a_program_missing_from_the_image_was_not_executed(
    tmp_path: Path, runner_image: str
) -> None:
    revision = commit(tmp_path / "subject", {"vat.py": VAT_RETURNS_NET})
    workdir = await pristine_checkout(str(tmp_path / "subject"), revision, tmp_path / "checkout")

    observation = await ProcessOracle(ContainerIsolation(runner_image)).observe(
        workdir, Launch(argv=("no-such-interpreter",), timeout_seconds=30)
    )

    assert observation.ineligibility is Ineligibility.NOT_EXECUTED


async def test_a_hung_container_is_removed(tmp_path: Path, runner_image: str) -> None:
    hangs = "import signal, time\nsignal.signal(signal.SIGTERM, signal.SIG_IGN)\ntime.sleep(600)\n"
    revision = commit(tmp_path / "subject", {"hang.py": hangs})
    workdir = await pristine_checkout(str(tmp_path / "subject"), revision, tmp_path / "checkout")

    observation = await ProcessOracle(ContainerIsolation(runner_image)).observe(
        workdir, Launch(argv=("python3", "hang.py"), timeout_seconds=3)
    )

    assert observation.ineligibility is Ineligibility.TIMED_OUT
    assert running_oracle_containers() == []


def test_an_unpinned_image_is_refused() -> None:
    with pytest.raises(ValueError, match="pinned by digest"):
        ContainerIsolation("python:3.14-alpine")
