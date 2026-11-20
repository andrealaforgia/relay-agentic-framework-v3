"""A container run is admitted only when the engine's account of it and the attached
client's agree. A matching exit code is not proof that the capture was complete."""

import pytest

from relay.domain.evidence import Ineligibility
from relay.testing.oracle import Captured, ContainerState, reconcile_container_run

IMAGE = "sha256:feed"


def exited(exit_code: int, *, error: str = "", out_of_memory: bool = False) -> ContainerState:
    return ContainerState("exited", exit_code, out_of_memory, error, IMAGE)


def test_agreeing_captures_are_admitted_with_the_engines_output() -> None:
    observation = reconcile_container_run(
        Captured(0, b"120\n", b""), Captured(0, b"120\n", b""), exited(0)
    )

    assert observation.is_eligible
    assert (observation.exit_code, observation.stdout) == (0, b"120\n")


def test_an_attach_that_failed_after_a_prefix_is_ineligible_even_when_exit_codes_coincide() -> None:
    attach_error_after_prefix = Captured(1, b"registered pre", b"")
    engine_record = Captured(0, b"registered prefix and the rest\n", b"")

    observation = reconcile_container_run(attach_error_after_prefix, engine_record, exited(1))

    assert observation.ineligibility is Ineligibility.NOT_EXECUTED


@pytest.mark.parametrize(
    ("attached", "logged", "state"),
    [
        pytest.param(Captured(0, b"x"), Captured(0, b"x"), None, id="engine-has-no-record"),
        pytest.param(
            Captured(127, b""),
            Captured(0, b""),
            ContainerState("created", 127, False, "exec: not found", IMAGE),
            id="never-started",
        ),
        pytest.param(Captured(0, b"x"), Captured(1, b""), exited(0), id="reading-back-failed"),
        pytest.param(
            Captured(0, b""),
            Captured(1, b""),
            exited(0),
            id="reading-back-failed-though-both-captures-are-empty",
        ),
        pytest.param(
            Captured(0, b"x"), Captured(timed_out=True), exited(0), id="reading-back-timed-out"
        ),
        pytest.param(Captured(0, b"x"), Captured(0, b"x"), exited(3), id="exit-codes-disagree"),
        pytest.param(
            Captured(0, b"x", b"client noise"),
            Captured(0, b"x", b""),
            exited(0),
            id="client-diagnostics-in-the-attached-stream",
        ),
    ],
)
def test_any_disagreement_or_missing_record_is_ineligible(
    attached: Captured, logged: Captured, state: ContainerState | None
) -> None:
    assert reconcile_container_run(attached, logged, state).ineligibility is (
        Ineligibility.NOT_EXECUTED
    )


def test_running_out_of_memory_is_a_death_by_signal() -> None:
    observation = reconcile_container_run(
        Captured(137, b""), Captured(0, b""), exited(137, out_of_memory=True)
    )

    assert observation.ineligibility is Ineligibility.KILLED_BY_SIGNAL


def test_output_beyond_the_limit_is_an_overflow_not_a_disagreement() -> None:
    observation = reconcile_container_run(
        Captured(0, b"12", overflowed=True), Captured(0, b"12", overflowed=True), exited(0)
    )

    assert observation.ineligibility is Ineligibility.OUTPUT_OVERFLOW
