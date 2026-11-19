"""The behaviour lifecycle: explicit preconditions, explicit reasons for refusal."""

import pytest

from relay.domain.behaviour import Behaviour, BehaviourState, TransitionRefused
from relay.domain.evidence import (
    Baseline,
    Expected,
    Launch,
    ProcessContract,
    ProcessObservation,
)

CONTRACT = ProcessContract(
    contract_id="B1-standard-rate",
    observes="the gross amount printed for a net amount at the standard rate",
    launch=Launch(argv=("python3", "vat.py", "100")),
    expect=Expected(exit_code=0, stdout="120\n"),
    baseline=Baseline(exit_code=0, stdout="100\n"),
)
FAILS_AS_REGISTERED = ProcessObservation(exit_code=0, stdout=b"100\n")
ERRORS = ProcessObservation(exit_code=1, stderr=b"SyntaxError")


def awaiting_red() -> Behaviour:
    behaviour = Behaviour.propose("B1", "standard VAT rate is applied")
    behaviour.begin_test_design()
    behaviour.register_acceptance_test(CONTRACT)
    return behaviour


def test_a_new_behaviour_is_proposed() -> None:
    assert Behaviour.propose("B1", "x").state is BehaviourState.PROPOSED


def test_registering_the_acceptance_test_starts_red_validation() -> None:
    assert awaiting_red().state is BehaviourState.VALIDATING_RED


def test_a_valid_red_releases_the_behaviour_to_the_builder() -> None:
    behaviour = awaiting_red()

    verdict = behaviour.record_red_run(FAILS_AS_REGISTERED)

    assert verdict.is_valid
    assert behaviour.state is BehaviourState.IMPLEMENTING


def test_an_invalid_red_keeps_the_builder_blocked() -> None:
    behaviour = awaiting_red()

    verdict = behaviour.record_red_run(ERRORS)

    assert not verdict.is_valid
    assert behaviour.state is BehaviourState.VALIDATING_RED


def test_the_builder_cannot_start_before_red_validation() -> None:
    behaviour = Behaviour.propose("B1", "x")
    behaviour.begin_test_design()

    with pytest.raises(TransitionRefused) as refusal:
        behaviour.record_red_run(FAILS_AS_REGISTERED)

    assert "test_design" in str(refusal.value)
    assert behaviour.state is BehaviourState.TEST_DESIGN


def test_a_refusal_names_what_was_attempted_and_what_is_required() -> None:
    with pytest.raises(TransitionRefused) as refusal:
        Behaviour.propose("B1", "x").register_acceptance_test(CONTRACT)

    assert refusal.value.attempted == "register_acceptance_test"
    assert refusal.value.current is BehaviourState.PROPOSED
    assert refusal.value.required == (BehaviourState.TEST_DESIGN,)
