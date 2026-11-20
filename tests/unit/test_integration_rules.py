"""What may reach trunk, and what a push that did not plainly succeed leaves behind."""

import pytest

from relay.domain.behaviour import Behaviour, BehaviourState, TransitionRefused
from relay.domain.evidence import (
    Baseline,
    CheckContract,
    Execution,
    Expected,
    Ineligibility,
    Launch,
    ProcessContract,
    ProcessObservation,
    RedRejection,
)
from relay.domain.integration import (
    TrunkReading,
    a_refusal_settles_the_intent,
    read_trunk,
    regression_digest,
)

CONTRACT = ProcessContract(
    contract_id="B1-standard-rate",
    observes="the gross amount printed for a net amount at the standard rate",
    launch=Launch(argv=("python3", "vat.py", "100")),
    expect=Expected(exit_code=0, stdout="120\n"),
    baseline=Baseline(exit_code=0, stdout="100\n"),
)
CHECK = CheckContract(
    contract_id="zero-stays-zero",
    observes="the gross amount printed for a net amount of zero",
    launch=Launch(argv=("python3", "vat.py", "0")),
    expect=Expected(exit_code=0, stdout="0\n"),
)
BASE, CANDIDATE, ELSEWHERE = "a" * 40, "b" * 40, "c" * 40


def run(observation: ProcessObservation, isolation: str = "container") -> Execution:
    return Execution(CANDIDATE, isolation, "image", observation)


def in_review() -> Behaviour:
    return Behaviour("B1", "standard VAT rate", BehaviourState.REVIEW, CONTRACT)


def test_a_check_is_satisfied_by_its_expectation_observed_in_a_container() -> None:
    assert CHECK.is_satisfied_by(run(ProcessObservation(0, b"0\n")))


@pytest.mark.parametrize(
    "execution",
    [
        run(ProcessObservation(0, b"1\n")),
        run(ProcessObservation(1, b"0\n")),
        run(ProcessObservation(0, b"0\n", ineligibility=Ineligibility.TIMED_OUT)),
        run(ProcessObservation(0, b"0\n"), isolation="native"),
    ],
    ids=["other output", "other exit code", "ineligible run", "not in a container"],
)
def test_a_check_is_not_satisfied_by_anything_else(execution: Execution) -> None:
    assert not CHECK.is_satisfied_by(execution)


def test_a_check_must_say_what_it_observes() -> None:
    with pytest.raises(ValueError, match="observes"):
        CheckContract("c", " ", CHECK.launch, CHECK.expect)


def test_a_check_survives_being_stored() -> None:
    assert CheckContract.from_dict(CHECK.to_dict()) == CHECK
    assert CheckContract.from_dict(CHECK.to_dict()).digest == CHECK.digest


def test_an_integrated_behaviours_contract_becomes_a_check_of_its_expectation() -> None:
    check = CONTRACT.as_check()

    assert (check.contract_id, check.launch, check.expect) == (
        CONTRACT.contract_id,
        CONTRACT.launch,
        CONTRACT.expect,
    )
    assert check.is_satisfied_by(run(ProcessObservation(0, b"120\n")))


def test_the_regression_set_is_identified_by_its_members_and_the_policy_version() -> None:
    other = CONTRACT.as_check()

    assert regression_digest(3, [CHECK, other]) == regression_digest(3, [other, CHECK])
    assert regression_digest(3, [CHECK, other]) != regression_digest(4, [CHECK, other])
    assert regression_digest(3, [CHECK, other]) != regression_digest(3, [CHECK])


def test_a_moved_trunk_sends_a_reviewed_behaviour_back_for_a_fresh_red() -> None:
    behaviour = in_review()

    behaviour.reopen_from_review()

    assert behaviour.state is BehaviourState.VALIDATING_RED
    assert behaviour.red_cycle == 1
    assert behaviour.reopened_from_review


def test_only_a_behaviour_in_review_is_reopened_this_way() -> None:
    behaviour = Behaviour("B1", "x", BehaviourState.IMPLEMENTING, CONTRACT)

    with pytest.raises(TransitionRefused):
        behaviour.reopen_from_review()


def test_a_new_trunk_that_already_satisfies_the_contract_needs_a_decision() -> None:
    behaviour = in_review()
    behaviour.reopen_from_review()

    verdict = behaviour.record_red_run(
        Execution(ELSEWHERE, "container", "image", ProcessObservation(0, b"120\n")),
        started_in_cycle=1,
    )

    assert verdict.rejection is RedRejection.ALREADY_PASSES
    assert behaviour.state is BehaviourState.NEEDS_DECISION


def test_a_first_red_that_already_passes_is_still_only_a_rejected_red() -> None:
    behaviour = Behaviour("B1", "x", BehaviourState.VALIDATING_RED, CONTRACT)

    behaviour.record_red_run(
        Execution(BASE, "container", "image", ProcessObservation(0, b"120\n")), started_in_cycle=0
    )

    assert behaviour.state is BehaviourState.VALIDATING_RED


def test_a_valid_red_on_the_new_trunk_ends_the_restart() -> None:
    behaviour = in_review()
    behaviour.reopen_from_review()

    behaviour.record_red_run(
        Execution(ELSEWHERE, "container", "image", ProcessObservation(0, b"100\n")),
        started_in_cycle=1,
    )

    assert behaviour.state is BehaviourState.IMPLEMENTING
    assert not behaviour.reopened_from_review


def test_integration_takes_a_reviewed_behaviour_to_integrated() -> None:
    behaviour = in_review()

    behaviour.integrate()

    assert behaviour.state is BehaviourState.INTEGRATED


def test_nothing_but_a_reviewed_behaviour_is_integrated() -> None:
    behaviour = Behaviour("B1", "x", BehaviourState.IMPLEMENTING, CONTRACT)

    with pytest.raises(TransitionRefused):
        behaviour.integrate()


def test_a_refused_integration_can_be_tried_again_from_blocked() -> None:
    behaviour = in_review()
    behaviour.park()

    behaviour.retry_integration()

    assert behaviour.state is BehaviourState.REVIEW


def test_the_first_send_being_refused_settles_the_intent() -> None:
    assert a_refusal_settles_the_intent(sends_started=1, sends_answered=1)


def test_a_refusal_does_not_settle_while_an_earlier_send_may_still_land() -> None:
    assert not a_refusal_settles_the_intent(sends_started=2, sends_answered=1)


def test_a_refusal_settles_once_every_send_is_known_to_have_failed() -> None:
    assert a_refusal_settles_the_intent(sends_started=2, sends_answered=2)


def test_a_refusal_before_anything_was_sent_settles_only_if_nothing_is_in_flight() -> None:
    assert a_refusal_settles_the_intent(sends_started=0, sends_answered=0)
    assert not a_refusal_settles_the_intent(sends_started=1, sends_answered=0)


def test_only_trunk_at_the_candidate_itself_means_the_integration_converged() -> None:
    reading = read_trunk(CANDIDATE, expected=BASE, candidate=CANDIDATE, beyond_expected=True)

    assert reading is TrunkReading.CONVERGED


def test_trunk_still_at_the_expected_head_proves_nothing() -> None:
    reading = read_trunk(BASE, expected=BASE, candidate=CANDIDATE, beyond_expected=False)

    assert reading is TrunkReading.UNDECIDED


def test_trunk_moved_on_from_the_expected_head_to_something_else_is_a_conflict() -> None:
    reading = read_trunk(ELSEWHERE, expected=BASE, candidate=CANDIDATE, beyond_expected=True)

    assert reading is TrunkReading.CONFLICT


def test_a_head_that_is_older_or_unrelated_settles_nothing() -> None:
    reading = read_trunk(ELSEWHERE, expected=BASE, candidate=CANDIDATE, beyond_expected=False)

    assert reading is TrunkReading.UNVERIFIABLE
