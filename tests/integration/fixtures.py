"""A VAT calculator whose baseline returns the net amount unchanged."""

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
SYNTAX_ERROR = ProcessObservation(exit_code=1, stderr=b"SyntaxError: invalid syntax")
