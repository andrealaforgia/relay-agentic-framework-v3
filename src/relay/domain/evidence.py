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


@dataclass(frozen=True)
class Execution:
    """One run by the oracle: which revision, in which runtime, and what was seen."""

    revision: str
    isolation: str
    runtime_identity: str
    observation: ProcessObservation


def is_gate_bearing(isolation: str) -> bool:
    return isolation in GATE_BEARING_ISOLATIONS


class RedRejection(StrEnum):
    NOT_EXECUTED = "not_executed"
    TIMED_OUT = "timed_out"
    KILLED_BY_SIGNAL = "killed_by_signal"
    OUTPUT_OVERFLOW = "output_overflow"
    ALREADY_PASSES = "already_passes"
    UNEXPECTED_FAILURE = "unexpected_failure"
    ISOLATION_NOT_ADMISSIBLE = "isolation_not_admissible"


@dataclass(frozen=True)
class RedVerdict:
    rejection: RedRejection | None
    reason: str

    @property
    def is_valid(self) -> bool:
        return self.rejection is None


@dataclass(frozen=True)
class GreenVerdict:
    is_valid: bool
    reason: str


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


def judge_green(contract: ProcessContract, observation: ProcessObservation) -> GreenVerdict:
    if contract.is_satisfied_by(observation):
        return GreenVerdict(True, f"{contract.contract_id}: the expectation was observed")
    return GreenVerdict(False, f"{contract.contract_id}: the expectation was not observed")
