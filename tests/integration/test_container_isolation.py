"""The execution boundary, shown with canaries. A canary counts only if the probe really
attempted the operation: the same probe, run natively, succeeds at what the container denies."""

from __future__ import annotations

import asyncio
import json
import os
import socket
import subprocess
from collections.abc import Iterator
from functools import cache
from pathlib import Path
from typing import Any

import pytest

from relay.domain.evidence import (
    Baseline,
    Execution,
    Expected,
    Ineligibility,
    Launch,
    PairedRed,
    ProcessContract,
    judge_green,
    judge_red,
)
from relay.git.checkout import pristine_checkout
from relay.testing import oracle
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


def containers(name: str) -> list[str]:
    """Containers with this name in any state; fails loudly if the engine cannot be asked."""
    return subprocess.run(
        ["docker", "ps", "--all", "--quiet", "--filter", f"name={name}"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.split()


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


async def test_a_subject_killed_inside_the_container_cannot_pass_either_gate(
    tmp_path: Path, runner_image: str
) -> None:
    dies = "import os, signal\nprint('100', flush=True)\nos.kill(os.getpid(), signal.SIGKILL)\n"
    revision = commit(tmp_path / "subject", {"vat.py": dies})
    workdir = await pristine_checkout(str(tmp_path / "subject"), revision, tmp_path / "checkout")
    launch = Launch(argv=("python3", "vat.py"), timeout_seconds=30)

    observation = await ProcessOracle(ContainerIsolation(runner_image)).observe(workdir, launch)

    assert observation.ineligibility is Ineligibility.KILLED_BY_SIGNAL
    # Contracts written to match exactly what a signal death looks like from outside.
    as_red = ProcessContract("B", "x", launch, Expected(0, "ok\n"), Baseline(137, stdout="100\n"))
    as_green = ProcessContract("B", "x", launch, Expected(137, "100\n"), Baseline(0, stdout="no\n"))
    execution = Execution(revision, "container", runner_image, observation)
    paired = PairedRed("red-run", "a" * 40, as_green.digest, runner_image)
    assert not judge_red(as_red, execution).is_valid
    assert not judge_green(as_green, execution, paired, candidate_descends_from_red=True).is_valid


async def test_the_subject_sees_only_the_files_of_the_revision_and_no_git_metadata(
    tmp_path: Path, runner_image: str
) -> None:
    lists = "import os\nprint(sorted(os.listdir('.')))\n"
    revision = commit(tmp_path / "subject", {"ls.py": lists})
    workdir = await pristine_checkout(str(tmp_path / "subject"), revision, tmp_path / "checkout")

    observation = await ProcessOracle(ContainerIsolation(runner_image)).observe(
        workdir, Launch(argv=("python3", "ls.py"), timeout_seconds=30)
    )

    assert observation.stdout == b"['ls.py']\n"
    assert not (workdir / ".git").exists()


async def test_the_operators_docker_proxy_settings_do_not_reach_the_subject(
    tmp_path: Path, runner_image: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    operator_config = tmp_path / "docker-config"
    operator_config.mkdir()
    proxy = "http://user:canary-proxy-credential@proxy.example:3128"
    (operator_config / "config.json").write_text(
        json.dumps({"proxies": {"default": {"httpProxy": proxy, "httpsProxy": proxy}}})
    )
    monkeypatch.setenv("DOCKER_CONFIG", str(operator_config))
    monkeypatch.setenv("HTTP_PROXY", proxy)
    # The client environment is resolved once per process; resolve it afresh here so this
    # test sees what a supervisor started with these settings would use.
    monkeypatch.setattr(oracle, "_docker_client", cache(oracle._docker_client.__wrapped__))
    shows = "import os\nprint(sorted(k for k in os.environ if 'proxy' in k.lower()))\n"
    revision = commit(tmp_path / "subject", {"env.py": shows})
    workdir = await pristine_checkout(str(tmp_path / "subject"), revision, tmp_path / "checkout")

    observation = await ProcessOracle(ContainerIsolation(runner_image)).observe(
        workdir, Launch(argv=("python3", "env.py"), timeout_seconds=30)
    )

    assert observation.stdout == b"[]\n"


async def test_cancelling_an_observation_does_not_leave_the_subject_running(
    tmp_path: Path, runner_image: str
) -> None:
    revision = commit(tmp_path / "subject", {"hang.py": "import time\ntime.sleep(600)\n"})
    workdir = await pristine_checkout(str(tmp_path / "subject"), revision, tmp_path / "checkout")
    before = set(running_oracle_containers())
    observing = asyncio.create_task(
        ProcessOracle(ContainerIsolation(runner_image)).observe(
            workdir, Launch(argv=("python3", "hang.py"), timeout_seconds=120)
        )
    )
    async with asyncio.timeout(30):
        while not (started := set(await asyncio.to_thread(running_oracle_containers)) - before):
            await asyncio.sleep(0.1)

    observing.cancel()
    with pytest.raises(asyncio.CancelledError):
        await observing

    assert started.isdisjoint(await asyncio.to_thread(running_oracle_containers))


async def test_the_docker_client_is_never_looked_up_in_the_subjects_checkout(
    tmp_path: Path, runner_image: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    planted = tmp_path / "planted-docker-ran"
    impostor = f"#!/bin/sh\ntouch {planted}\nexit 0\n"
    revision = commit(tmp_path / "subject", {"docker": impostor, "vat.py": VAT_RETURNS_NET})
    workdir = await pristine_checkout(str(tmp_path / "subject"), revision, tmp_path / "checkout")
    (workdir / "docker").chmod(0o755)
    monkeypatch.setenv("PATH", f".{os.pathsep}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setattr(oracle, "_docker_client", cache(oracle._docker_client.__wrapped__))
    monkeypatch.chdir(workdir)

    observation = await ProcessOracle(ContainerIsolation(runner_image)).observe(
        workdir, Launch(argv=("python3", "vat.py", "100"), timeout_seconds=30)
    )

    assert not planted.exists()
    assert observation.stdout == b"100\n"
    assert os.path.isabs(oracle._docker_client()[0])


async def test_a_checkout_the_engine_cannot_see_is_not_executed_and_not_invented(
    tmp_path: Path, runner_image: str
) -> None:
    invisible = tmp_path / "not-on-the-engine-host"

    observation = await ProcessOracle(ContainerIsolation(runner_image)).observe(
        invisible, Launch(argv=("python3", "vat.py", "100"), timeout_seconds=30)
    )

    assert observation.ineligibility is Ineligibility.NOT_EXECUTED
    assert not invisible.exists()


async def test_a_remote_engine_is_refused(
    tmp_path: Path, runner_image: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DOCKER_HOST", "tcp://engine.example:2375")
    monkeypatch.setattr(oracle, "_docker_client", cache(oracle._docker_client.__wrapped__))

    with pytest.raises(RuntimeError, match="local Docker engine"):
        await ProcessOracle(ContainerIsolation(runner_image)).observe(
            tmp_path, Launch(argv=("true",), timeout_seconds=30)
        )


async def test_a_container_created_just_before_cancellation_is_still_removed(
    tmp_path: Path, runner_image: str
) -> None:
    class CancelledAfterTheEngineCreates(ContainerIsolation):
        created: str = ""
        existed_when_cancelled: bool = False

        async def _docker(self, *arguments: str, **options: Any) -> oracle.Captured:
            captured = await super()._docker(*arguments, **options)
            if arguments[0] == "create":
                assert captured.exit_code == 0, captured.stderr
                self.created = arguments[arguments.index("--name") + 1]
                self.existed_when_cancelled = bool(
                    await asyncio.to_thread(containers, self.created)
                )
                raise asyncio.CancelledError
            return captured

    revision = commit(tmp_path / "subject", {"vat.py": VAT_RETURNS_NET})
    workdir = await pristine_checkout(str(tmp_path / "subject"), revision, tmp_path / "checkout")
    isolation = CancelledAfterTheEngineCreates(runner_image)

    with pytest.raises(asyncio.CancelledError):
        await ProcessOracle(isolation).observe(workdir, Launch(argv=("python3", "vat.py", "1")))

    assert isolation.existed_when_cancelled
    assert await asyncio.to_thread(containers, isolation.created) == []


async def test_output_beyond_the_capture_limit_is_an_overflow_in_a_container_too(
    tmp_path: Path, runner_image: str
) -> None:
    floods = "print('120')\nprint('x' * 5_000_000)\n"
    revision = commit(tmp_path / "subject", {"vat.py": floods})
    workdir = await pristine_checkout(str(tmp_path / "subject"), revision, tmp_path / "checkout")

    observation = await ProcessOracle(
        ContainerIsolation(runner_image), capture_limit_bytes=1_000_000
    ).observe(workdir, Launch(argv=("python3", "vat.py"), timeout_seconds=30))

    assert observation.ineligibility is Ineligibility.OUTPUT_OVERFLOW


async def test_the_engine_is_told_to_bound_what_it_stores_of_the_subjects_output(
    tmp_path: Path, runner_image: str
) -> None:
    class RecordsLogConfiguration(ContainerIsolation):
        log_configuration: dict[str, Any] | None = None

        async def _docker(self, *arguments: str, **options: Any) -> oracle.Captured:
            captured = await super()._docker(*arguments, **options)
            if arguments[0] == "create":
                name = arguments[arguments.index("--name") + 1]
                inspected = await super()._docker(
                    "inspect", "--format", "{{json .HostConfig.LogConfig}}", name
                )
                self.log_configuration = json.loads(inspected.stdout)
            return captured

    revision = commit(tmp_path / "subject", {"vat.py": VAT_RETURNS_NET})
    workdir = await pristine_checkout(str(tmp_path / "subject"), revision, tmp_path / "checkout")
    isolation = RecordsLogConfiguration(runner_image)

    await ProcessOracle(isolation, capture_limit_bytes=1_000_000).observe(
        workdir, Launch(argv=("python3", "vat.py", "1"), timeout_seconds=30)
    )

    assert isolation.log_configuration == {
        "Type": "json-file",
        "Config": {"max-file": "1", "max-size": "2000000"},
    }


async def test_the_subject_runs_as_the_same_fixed_user_whoever_runs_the_supervisor(
    tmp_path: Path, runner_image: str
) -> None:
    whoami = "import os\nprint(os.getuid(), os.getgid())\n"
    revision = commit(tmp_path / "subject", {"id.py": whoami})
    workdir = await pristine_checkout(str(tmp_path / "subject"), revision, tmp_path / "checkout")

    observation = await ProcessOracle(ContainerIsolation(runner_image)).observe(
        workdir, Launch(argv=("python3", "id.py"), timeout_seconds=30)
    )

    assert observation.stdout == b"65534 65534\n"
    assert os.getuid() != 65534


async def test_different_container_settings_give_a_different_runtime_identity(
    tmp_path: Path, runner_image: str
) -> None:
    class MoreMemory(ContainerIsolation):
        settings = tuple(
            "1g" if setting == "512m" else setting for setting in ContainerIsolation.settings
        )

    revision = commit(tmp_path / "subject", {"vat.py": VAT_RETURNS_NET})
    workdir = await pristine_checkout(str(tmp_path / "subject"), revision, tmp_path / "checkout")
    launch = Launch(argv=("python3", "vat.py", "1"), timeout_seconds=30)

    usual = await ContainerIsolation(runner_image).run(workdir, launch, 1_000_000)
    again = await ContainerIsolation(runner_image).run(workdir, launch, 1_000_000)
    different = await MoreMemory(runner_image).run(workdir, launch, 1_000_000)

    assert usual.runtime_identity == again.runtime_identity
    assert usual.runtime_identity != different.runtime_identity
    assert usual.runtime_identity.split(";")[0] == different.runtime_identity.split(";")[0]


async def test_a_timed_out_run_still_reports_the_runtime_it_ran_in(
    tmp_path: Path, runner_image: str
) -> None:
    revision = commit(
        tmp_path / "subject",
        {"vat.py": VAT_RETURNS_NET, "hang.py": "import time\ntime.sleep(60)\n"},
    )
    workdir = await pristine_checkout(str(tmp_path / "subject"), revision, tmp_path / "checkout")
    isolation = ContainerIsolation(runner_image)

    completed = await isolation.run(workdir, Launch(argv=("python3", "vat.py", "1")), 1_000_000)
    timed_out = await isolation.run(
        workdir, Launch(argv=("python3", "hang.py"), timeout_seconds=2), 1_000_000
    )

    assert timed_out.observation.ineligibility is Ineligibility.TIMED_OUT
    assert timed_out.runtime_identity == completed.runtime_identity


async def test_the_identity_is_the_engines_even_when_the_first_inspection_fails(
    tmp_path: Path, runner_image: str
) -> None:
    class FirstInspectionFails(ContainerIsolation):
        inspections = 0

        async def _state(self, container: str) -> oracle.ContainerState | None:
            self.inspections += 1
            return None if self.inspections == 1 else await super()._state(container)

    class NoInspectionWorks(ContainerIsolation):
        async def _state(self, container: str) -> oracle.ContainerState | None:
            return None

    revision = commit(tmp_path / "subject", {"vat.py": VAT_RETURNS_NET})
    workdir = await pristine_checkout(str(tmp_path / "subject"), revision, tmp_path / "checkout")
    launch = Launch(argv=("python3", "vat.py", "1"), timeout_seconds=30)

    usual = await ContainerIsolation(runner_image).run(workdir, launch, 1_000_000)
    recovered = await FirstInspectionFails(runner_image).run(workdir, launch, 1_000_000)
    unknown = await NoInspectionWorks(runner_image).run(workdir, launch, 1_000_000)

    assert recovered.observation.is_eligible
    assert recovered.runtime_identity == usual.runtime_identity
    assert runner_image not in recovered.runtime_identity
    assert unknown.observation.ineligibility is Ineligibility.NOT_EXECUTED


def test_the_runtime_identity_changes_with_the_mount_point_alone(runner_image: str) -> None:
    class MountedElsewhere(ContainerIsolation):
        subject_mount = "/work"

    usual = ContainerIsolation(runner_image)._identity("sha256:image", 1_000_000)
    elsewhere = MountedElsewhere(runner_image)._identity("sha256:image", 1_000_000)

    assert MountedElsewhere.settings == ContainerIsolation.settings
    assert usual != elsewhere


def test_the_runtime_identity_changes_with_the_capture_limit_alone(runner_image: str) -> None:
    """Both limits are below the floor at which the engine's log limit starts to grow, so
    the container is created with identical arguments; only the capture limit differs."""
    isolation = ContainerIsolation(runner_image)

    assert isolation._identity("sha256:image", 100_000) != isolation._identity(
        "sha256:image", 200_000
    )


async def test_the_subject_sees_the_mount_point_it_is_given(
    tmp_path: Path, runner_image: str
) -> None:
    revision = commit(tmp_path / "subject", {"cwd.py": "import os\nprint(os.getcwd())\n"})
    workdir = await pristine_checkout(str(tmp_path / "subject"), revision, tmp_path / "checkout")

    observation = await ProcessOracle(ContainerIsolation(runner_image)).observe(
        workdir, Launch(argv=("python3", "cwd.py"), timeout_seconds=30)
    )

    assert observation.stdout == b"/subject\n"


def test_an_unpinned_image_is_refused() -> None:
    with pytest.raises(ValueError, match="pinned by digest"):
        ContainerIsolation("python:3.14-alpine")
