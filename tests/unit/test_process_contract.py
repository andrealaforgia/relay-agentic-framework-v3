"""Gate-bearing evidence is judged from what an outside oracle observed of a process,
against a contract the Verifier registered before the run (decision PO-1)."""

import pytest

from relay.domain.evidence import (
    Baseline,
    EvidencePolicy,
    Execution,
    Expected,
    Ineligibility,
    Launch,
    ProcessContract,
    ProcessObservation,
    RedRejection,
    RedVerdict,
    judge_green,
)
from relay.domain.evidence import judge_red as judge_red_execution

CONTRACT = ProcessContract(
    contract_id="B1-standard-rate",
    observes="the gross amount printed for a net amount at the standard rate",
    launch=Launch(argv=("python3", "vat.py", "100")),
    expect=Expected(exit_code=0, stdout="120\n"),
    baseline=Baseline(exit_code=0, stdout="100\n"),
)


def judge_red(contract: ProcessContract, observation: ProcessObservation) -> RedVerdict:
    execution = Execution(revision="a" * 40, isolation="container", observation=observation)
    return judge_red_execution(contract, execution, EvidencePolicy())


def ran(exit_code: int, stdout: str = "", stderr: str = "") -> ProcessObservation:
    return ProcessObservation(exit_code, stdout.encode(), stderr.encode())


def test_the_registered_baseline_observation_is_a_valid_red() -> None:
    verdict = judge_red(CONTRACT, ran(0, "100\n"))

    assert verdict.is_valid
    assert verdict.rejection is None


@pytest.mark.parametrize("ineligibility", list(Ineligibility))
def test_an_ineligible_run_is_never_red_nor_green(ineligibility: Ineligibility) -> None:
    looks_right = ProcessObservation(0, b"100\n", ineligibility=ineligibility)
    looks_green = ProcessObservation(0, b"120\n", ineligibility=ineligibility)

    assert judge_red(CONTRACT, looks_right).rejection is RedRejection(ineligibility.value)
    assert not judge_green(CONTRACT, looks_green).is_valid


def test_a_baseline_that_already_satisfies_the_expectation_is_not_red() -> None:
    assert judge_red(CONTRACT, ran(0, "120\n")).rejection is RedRejection.ALREADY_PASSES


@pytest.mark.parametrize(
    "observation",
    [
        ran(1, "", "Traceback (most recent call last):\nSyntaxError: invalid syntax"),
        ran(0, "99\n"),
        ran(2, "100\n"),
        ran(0, "100\nand more\n"),
        ran(0, "100"),
    ],
)
def test_failing_in_any_way_other_than_the_registered_one_is_not_red(
    observation: ProcessObservation,
) -> None:
    assert judge_red(CONTRACT, observation).rejection is RedRejection.UNEXPECTED_FAILURE


def test_a_missing_feature_is_registered_by_its_exact_diagnostic() -> None:
    contract = ProcessContract(
        contract_id="B2-reduced-rate",
        observes="the gross amount at the reduced rate",
        launch=Launch(argv=("vat", "reduced", "100")),
        expect=Expected(exit_code=0, stdout="105\n"),
        baseline=Baseline(exit_code=2, stderr="unknown command: reduced\n"),
    )

    documented_diagnostic = ran(2, "", "unknown command: reduced\n")
    broken_installation = ran(2, "", "cannot load catalogue for unknown command: reduced\n")

    assert judge_red(contract, documented_diagnostic).is_valid
    assert judge_red(contract, broken_installation).rejection is RedRejection.UNEXPECTED_FAILURE


def test_a_rejection_explains_itself() -> None:
    verdict = judge_red(CONTRACT, ran(0, "99\n"))

    assert "B1-standard-rate" in verdict.reason
    assert "registered baseline" in verdict.reason
    assert "pre-registered baseline observation" in judge_red(CONTRACT, ran(0, "100\n")).reason


def test_green_is_the_expectation_observed_and_nothing_else() -> None:
    assert judge_green(CONTRACT, ran(0, "120\n")).is_valid
    assert not judge_green(CONTRACT, ran(0, "100\n")).is_valid
    assert not judge_green(CONTRACT, ran(1, "120\n")).is_valid
    assert not judge_green(CONTRACT, ran(0, "120\nextra\n")).is_valid


def test_a_baseline_must_name_exactly_one_stream() -> None:
    with pytest.raises(ValueError, match="exactly one"):
        Baseline(exit_code=1)
    with pytest.raises(ValueError, match="exactly one"):
        Baseline(exit_code=1, stdout="x", stderr="y")


def test_a_contract_must_state_what_it_observes() -> None:
    with pytest.raises(ValueError, match="observes"):
        ProcessContract(
            contract_id="B1",
            observes=" ",
            launch=CONTRACT.launch,
            expect=CONTRACT.expect,
            baseline=CONTRACT.baseline,
        )


def test_a_contract_whose_baseline_already_meets_the_expectation_is_refused() -> None:
    with pytest.raises(ValueError, match="baseline already satisfies"):
        ProcessContract(
            contract_id="B1",
            observes="x",
            launch=Launch(argv=("true",)),
            expect=Expected(exit_code=0, stdout="120\n"),
            baseline=Baseline(exit_code=0, stdout="120\n"),
        )


def test_a_contract_is_identified_by_its_content() -> None:
    same = ProcessContract.from_dict(CONTRACT.to_dict())
    different = ProcessContract(
        contract_id=CONTRACT.contract_id,
        observes=CONTRACT.observes,
        launch=CONTRACT.launch,
        expect=Expected(exit_code=0, stdout="121\n"),
        baseline=CONTRACT.baseline,
    )

    assert same == CONTRACT
    assert same.digest == CONTRACT.digest
    assert CONTRACT.digest.startswith("sha256:")
    assert different.digest != CONTRACT.digest


def test_native_execution_is_not_evidence_unless_the_project_approves_it() -> None:
    native = Execution(revision="a" * 40, isolation="native", observation=ran(0, "100\n"))

    refused = judge_red_execution(CONTRACT, native, EvidencePolicy(native_approved=False))
    admitted = judge_red_execution(CONTRACT, native, EvidencePolicy(native_approved=True))

    assert refused.rejection is RedRejection.ISOLATION_NOT_APPROVED
    assert admitted.is_valid
