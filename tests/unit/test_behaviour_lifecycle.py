"""The behaviour lifecycle: explicit preconditions, explicit reasons for refusal."""

import pytest

from relay.domain.behaviour import Behaviour, BehaviourState, TransitionRefused
from relay.domain.evidence import (
    Baseline,
    Execution,
    Expected,
    GreenRejection,
    Launch,
    PairedRed,
    ProcessContract,
    ProcessObservation,
    RedRejection,
)

CONTRACT = ProcessContract(
    contract_id="B1-standard-rate",
    observes="the gross amount printed for a net amount at the standard rate",
    launch=Launch(argv=("python3", "vat.py", "100")),
    expect=Expected(exit_code=0, stdout="120\n"),
    baseline=Baseline(exit_code=0, stdout="100\n"),
)
FAILS_AS_REGISTERED = Execution("a" * 40, "container", "image", ProcessObservation(0, b"100\n"))
ERRORS = Execution("a" * 40, "container", "image", ProcessObservation(1, b"", b"SyntaxError"))


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

    verdict = behaviour.record_red_run(FAILS_AS_REGISTERED, started_in_cycle=0)

    assert verdict.is_valid
    assert behaviour.state is BehaviourState.IMPLEMENTING


def test_an_invalid_red_keeps_the_builder_blocked() -> None:
    behaviour = awaiting_red()

    verdict = behaviour.record_red_run(ERRORS, started_in_cycle=0)

    assert not verdict.is_valid
    assert behaviour.state is BehaviourState.VALIDATING_RED


def test_a_red_run_cannot_start_before_the_acceptance_test_is_registered() -> None:
    behaviour = Behaviour.propose("B1", "x")
    behaviour.begin_test_design()

    with pytest.raises(TransitionRefused) as refusal:
        behaviour.require_awaiting_red()

    assert "test_design" in str(refusal.value)
    assert behaviour.state is BehaviourState.TEST_DESIGN


def test_a_refusal_names_what_was_attempted_and_what_is_required() -> None:
    with pytest.raises(TransitionRefused) as refusal:
        Behaviour.propose("B1", "x").register_acceptance_test(CONTRACT)

    assert refusal.value.attempted == "register_acceptance_test"
    assert refusal.value.current is BehaviourState.PROPOSED
    assert refusal.value.required == (BehaviourState.TEST_DESIGN,)


RED = PairedRed("red-run", "a" * 40, CONTRACT.digest, "image")
PASSES = Execution("c" * 40, "container", "image", ProcessObservation(0, b"120\n"))
STILL_FAILS = Execution("c" * 40, "container", "image", ProcessObservation(0, b"100\n"))


def implementing() -> Behaviour:
    behaviour = awaiting_red()
    behaviour.record_red_run(FAILS_AS_REGISTERED, started_in_cycle=0)
    return behaviour


def test_a_paired_green_sends_the_behaviour_to_review() -> None:
    behaviour = implementing()

    verdict = behaviour.record_green_run(PASSES, RED, RED, candidate_descends_from_red=True)

    assert verdict.is_valid
    assert behaviour.state is BehaviourState.REVIEW


def test_a_candidate_that_still_fails_stays_with_the_builder() -> None:
    behaviour = implementing()

    verdict = behaviour.record_green_run(STILL_FAILS, RED, RED, candidate_descends_from_red=True)

    assert verdict.rejection is GreenRejection.EXPECTATION_NOT_MET
    assert behaviour.state is BehaviourState.IMPLEMENTING


def test_a_green_run_cannot_start_before_red() -> None:
    with pytest.raises(TransitionRefused) as refusal:
        awaiting_red().require_awaiting_green()

    assert refusal.value.current is BehaviourState.VALIDATING_RED


def test_a_red_can_be_reopened_while_implementing_and_only_then() -> None:
    behaviour = implementing()

    behaviour.reopen_red()

    assert behaviour.state is BehaviourState.VALIDATING_RED
    with pytest.raises(TransitionRefused):
        behaviour.reopen_red()


@pytest.mark.parametrize("behaviour_id", ["checkout/VAT", "", "-B1", "B 1"])
def test_a_behaviour_id_must_be_usable_in_a_ref(behaviour_id: str) -> None:
    with pytest.raises(ValueError, match="behaviour id"):
        Behaviour.propose(behaviour_id, "x")


def test_a_red_run_that_began_before_a_reopening_cannot_fill_the_new_cycle() -> None:
    behaviour = implementing()
    behaviour.reopen_red()

    late = behaviour.record_red_run(FAILS_AS_REGISTERED, started_in_cycle=0)

    assert late.rejection is RedRejection.SUPERSEDED
    assert behaviour.state is BehaviourState.VALIDATING_RED
    assert behaviour.record_red_run(FAILS_AS_REGISTERED, started_in_cycle=1).is_valid


def test_a_green_that_answered_a_superseded_red_does_not_reach_review() -> None:
    behaviour = implementing()
    newer_red = PairedRed("a-later-red-run", "b" * 40, CONTRACT.digest, "image")

    replaced = behaviour.record_green_run(PASSES, RED, newer_red, candidate_descends_from_red=True)
    removed = behaviour.record_green_run(PASSES, RED, None, candidate_descends_from_red=True)

    assert replaced.rejection is GreenRejection.RED_SUPERSEDED
    assert removed.rejection is GreenRejection.RED_SUPERSEDED
    assert behaviour.state is BehaviourState.IMPLEMENTING


def test_a_green_that_finishes_after_its_red_was_reopened_is_recorded_as_superseded() -> None:
    behaviour = implementing()
    behaviour.reopen_red()

    verdict = behaviour.record_green_run(PASSES, RED, None, candidate_descends_from_red=True)

    assert verdict.rejection is GreenRejection.RED_SUPERSEDED
    assert behaviour.state is BehaviourState.VALIDATING_RED


def test_a_red_that_finishes_after_another_was_accepted_is_recorded_as_superseded() -> None:
    behaviour = implementing()

    same_cycle = behaviour.record_red_run(FAILS_AS_REGISTERED, started_in_cycle=0)

    assert same_cycle.rejection is RedRejection.SUPERSEDED
    assert behaviour.state is BehaviourState.IMPLEMENTING


def test_a_second_green_after_the_first_was_accepted_changes_nothing() -> None:
    behaviour = implementing()
    behaviour.record_green_run(PASSES, RED, RED, candidate_descends_from_red=True)

    again = behaviour.record_green_run(PASSES, RED, RED, candidate_descends_from_red=True)

    assert again.rejection is GreenRejection.RED_SUPERSEDED
    assert behaviour.state is BehaviourState.REVIEW
