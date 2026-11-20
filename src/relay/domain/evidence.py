"""Deterministic judgement of executable evidence.

Evidence that opens or closes a gate comes only from an oracle outside the code under
test: Relay launches the subject and compares what it observed with a contract the
Verifier registered beforehand. No model and no test framework report takes part.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import asdict, dataclass
from enum import StrEnum
from typing import Any

MAX_TIMEOUT_SECONDS = 300
# Only an isolation that keeps the subject away from the recorder can open a gate.
GATE_BEARING_ISOLATIONS = frozenset({"container"})


@dataclass(frozen=True)
class Launch:
    argv: tuple[str, ...]
    stdin: str = ""
    timeout_seconds: float = 10.0

    def __post_init__(self) -> None:
        if not self.argv:
            raise ValueError("a launch needs at least the program to run")
        timeout = self.timeout_seconds
        is_a_number = isinstance(timeout, int | float) and not isinstance(timeout, bool)
        if not (is_a_number and math.isfinite(timeout) and 0 < timeout <= MAX_TIMEOUT_SECONDS):
            raise ValueError(
                f"timeout_seconds must be a positive number of at most {MAX_TIMEOUT_SECONDS}"
            )


@dataclass(frozen=True)
class Expected:
    """What the subject must produce once the behaviour exists."""

    exit_code: int
    stdout: str


@dataclass(frozen=True)
class Baseline:
    """What the subject is expected to produce before the behaviour exists: an exact exit
    code and the exact content of one stream. A substring would also match unrelated failures."""

    exit_code: int
    stdout: str | None = None
    stderr: str | None = None

    def __post_init__(self) -> None:
        if (self.stdout is None) == (self.stderr is None):
            raise ValueError("a baseline names exactly one of stdout or stderr")


class Ineligibility(StrEnum):
    """Why a run cannot count towards any gate, red or green."""

    NOT_EXECUTED = "not_executed"
    TIMED_OUT = "timed_out"
    KILLED_BY_SIGNAL = "killed_by_signal"
    OUTPUT_OVERFLOW = "output_overflow"


@dataclass(frozen=True)
class ProcessObservation:
    """What the oracle saw. Recorded by trusted code, never reported by the subject.
    Streams are the subject's complete output as bytes."""

    exit_code: int | None = None
    stdout: bytes = b""
    stderr: bytes = b""
    ineligibility: Ineligibility | None = None

    @property
    def is_eligible(self) -> bool:
        return self.ineligibility is None


@dataclass(frozen=True)
class Execution:
    """One run by the oracle: which revision, in which runtime, and what was seen."""

    revision: str
    isolation: str
    runtime_identity: str
    observation: ProcessObservation


def is_gate_bearing(isolation: str) -> bool:
    return isolation in GATE_BEARING_ISOLATIONS


@dataclass(frozen=True)
class CheckContract:
    """Something that must hold on every candidate before it reaches trunk: a launch and
    what it must produce. Registered on the project, outside any candidate's reach."""

    contract_id: str
    observes: str
    launch: Launch
    expect: Expected

    def __post_init__(self) -> None:
        if not self.observes.strip():
            raise ValueError("a check states the public behaviour it observes")

    def is_satisfied_by(self, execution: Execution) -> bool:
        observation = execution.observation
        return (
            is_gate_bearing(execution.isolation)
            and observation.is_eligible
            and observation.exit_code == self.expect.exit_code
            and observation.stdout == self.expect.stdout.encode()
        )

    @property
    def digest(self) -> str:
        canonical = json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"))
        return "sha256:" + hashlib.sha256(canonical.encode()).hexdigest()

    def to_dict(self) -> dict[str, Any]:
        document = asdict(self)
        document["launch"]["argv"] = list(self.launch.argv)
        return document

    @classmethod
    def from_dict(cls, document: dict[str, Any]) -> CheckContract:
        launch = dict(document["launch"])
        launch["argv"] = tuple(launch["argv"])
        return cls(
            contract_id=document["contract_id"],
            observes=document["observes"],
            launch=Launch(**launch),
            expect=Expected(**document["expect"]),
        )


@dataclass(frozen=True)
class ProcessContract:
    contract_id: str
    observes: str
    launch: Launch
    expect: Expected
    baseline: Baseline

    def __post_init__(self) -> None:
        if not self.observes.strip():
            raise ValueError("a contract states the public behaviour it observes")
        if self.baseline.stdout is not None and self.is_satisfied_by(
            ProcessObservation(self.baseline.exit_code, self.baseline.stdout.encode())
        ):
            raise ValueError("the registered baseline already satisfies the expectation")

    def is_satisfied_by(self, observation: ProcessObservation) -> bool:
        return (
            observation.is_eligible
            and observation.exit_code == self.expect.exit_code
            and observation.stdout == self.expect.stdout.encode()
        )

    def is_the_registered_baseline(self, observation: ProcessObservation) -> bool:
        if not observation.is_eligible or observation.exit_code != self.baseline.exit_code:
            return False
        if self.baseline.stdout is not None:
            return observation.stdout == self.baseline.stdout.encode()
        assert self.baseline.stderr is not None
        return observation.stderr == self.baseline.stderr.encode()

    def as_check(self) -> CheckContract:
        """Once a behaviour is integrated its expectation must go on holding: the same
        launch and expectation, as a check on every later candidate."""
        return CheckContract(self.contract_id, self.observes, self.launch, self.expect)

    @property
    def digest(self) -> str:
        canonical = json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"))
        return "sha256:" + hashlib.sha256(canonical.encode()).hexdigest()

    def to_dict(self) -> dict[str, Any]:
        document = asdict(self)
        document["launch"]["argv"] = list(self.launch.argv)
        return document

    @classmethod
    def from_dict(cls, document: dict[str, Any]) -> ProcessContract:
        launch = dict(document["launch"])
        launch["argv"] = tuple(launch["argv"])
        return cls(
            contract_id=document["contract_id"],
            observes=document["observes"],
            launch=Launch(**launch),
            expect=Expected(**document["expect"]),
            baseline=Baseline(**document["baseline"]),
        )


class RedRejection(StrEnum):
    NOT_EXECUTED = "not_executed"
    TIMED_OUT = "timed_out"
    KILLED_BY_SIGNAL = "killed_by_signal"
    OUTPUT_OVERFLOW = "output_overflow"
    ALREADY_PASSES = "already_passes"
    UNEXPECTED_FAILURE = "unexpected_failure"
    ISOLATION_NOT_ADMISSIBLE = "isolation_not_admissible"
    SUPERSEDED = "superseded"


@dataclass(frozen=True)
class RedVerdict:
    rejection: RedRejection | None
    reason: str

    @property
    def is_valid(self) -> bool:
        return self.rejection is None


class GreenRejection(StrEnum):
    ISOLATION_NOT_ADMISSIBLE = "isolation_not_admissible"
    NO_PAIRED_RED = "no_paired_red"
    RED_SUPERSEDED = "red_superseded"
    ASSIGNMENT_SUPERSEDED = "assignment_superseded"
    BASIS_SUPERSEDED = "basis_superseded"
    NOT_BUILT_ON_THE_RED_REVISION = "not_built_on_the_red_revision"
    RUNTIME_CHANGED = "runtime_changed"
    NOT_EXECUTED = "not_executed"
    TIMED_OUT = "timed_out"
    KILLED_BY_SIGNAL = "killed_by_signal"
    OUTPUT_OVERFLOW = "output_overflow"
    EXPECTATION_NOT_MET = "expectation_not_met"


@dataclass(frozen=True)
class GreenVerdict:
    rejection: GreenRejection | None
    reason: str

    @property
    def is_valid(self) -> bool:
        return self.rejection is None


@dataclass(frozen=True)
class PairedRed:
    """The accepted red a green must answer: same contract, same runtime, and a
    candidate that was built on top of the revision the red was observed on."""

    execution_id: str
    revision: str
    contract_digest: str
    runtime_identity: str


def refuse_isolation(contract: ProcessContract, isolation: str) -> RedVerdict:
    return RedVerdict(
        RedRejection.ISOLATION_NOT_ADMISSIBLE,
        f"{contract.contract_id}: {isolation} execution does not keep the subject away from "
        "the recorder, so it cannot open a gate",
    )


def judge_red(contract: ProcessContract, execution: Execution) -> RedVerdict:
    def rejected(rejection: RedRejection, explanation: str) -> RedVerdict:
        return RedVerdict(rejection, f"{contract.contract_id}: {explanation}")

    observation = execution.observation
    if not is_gate_bearing(execution.isolation):
        return refuse_isolation(contract, execution.isolation)
    if observation.ineligibility is not None:
        return rejected(
            RedRejection(observation.ineligibility.value),
            f"the run is ineligible ({observation.ineligibility.value.replace('_', ' ')})",
        )
    if contract.is_satisfied_by(observation):
        return rejected(RedRejection.ALREADY_PASSES, "the baseline already meets the expectation")
    if not contract.is_the_registered_baseline(observation):
        return rejected(
            RedRejection.UNEXPECTED_FAILURE,
            f"exit code {observation.exit_code} and its output do not match "
            "the registered baseline",
        )
    return RedVerdict(
        None, f"{contract.contract_id}: the run matched the pre-registered baseline observation"
    )


def refuse_green_before_launch(
    contract: ProcessContract, isolation: str, red: PairedRed | None
) -> GreenVerdict | None:
    """Reasons a green run cannot count that are known before anything is launched."""
    if not is_gate_bearing(isolation):
        return GreenVerdict(
            GreenRejection.ISOLATION_NOT_ADMISSIBLE,
            f"{contract.contract_id}: {isolation} execution cannot open a gate",
        )
    if red is None or red.contract_digest != contract.digest:
        return GreenVerdict(
            GreenRejection.NO_PAIRED_RED,
            f"{contract.contract_id}: no accepted red exists for this contract",
        )
    return None


def judge_green(
    contract: ProcessContract,
    execution: Execution,
    red: PairedRed | None,
    *,
    candidate_descends_from_red: bool,
) -> GreenVerdict:
    """A passing run is only a run. It certifies a change in behaviour when the same
    contract was seen failing as registered, in the same runtime, on a revision the
    candidate was built on."""

    def rejected(rejection: GreenRejection, explanation: str) -> GreenVerdict:
        return GreenVerdict(rejection, f"{contract.contract_id}: {explanation}")

    observation = execution.observation
    if refusal := refuse_green_before_launch(contract, execution.isolation, red):
        return refusal
    assert red is not None
    if execution.revision == red.revision or not candidate_descends_from_red:
        return rejected(
            GreenRejection.NOT_BUILT_ON_THE_RED_REVISION,
            "the candidate is not a change built on the revision the red was observed on",
        )
    # What was observed comes first: a run that timed out is reported as a timeout, not
    # as a runtime difference it may never have got far enough to establish.
    if observation.ineligibility is not None:
        return rejected(
            GreenRejection(observation.ineligibility.value),
            f"the run is ineligible ({observation.ineligibility.value.replace('_', ' ')})",
        )
    if execution.runtime_identity != red.runtime_identity:
        return rejected(
            GreenRejection.RUNTIME_CHANGED,
            "the runtime differs from the one the red was observed in; reopen the red "
            "to observe it again in this runtime",
        )
    if not contract.is_satisfied_by(observation):
        return rejected(GreenRejection.EXPECTATION_NOT_MET, "the expectation was not observed")
    return GreenVerdict(None, f"{contract.contract_id}: the expectation was observed")
