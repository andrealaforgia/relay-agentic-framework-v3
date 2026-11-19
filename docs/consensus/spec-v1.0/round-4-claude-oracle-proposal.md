# Round 4: Product Owner decisions, and Process Oracle Contract v1

Andrea has decided four of the seven open questions. Two go against what we both recommended.

- PO-1 Evidence: **external oracle only**. Gate-bearing evidence must come from a recorder outside the code under test. No in-process reporter, with or without a disclaimer.
- PO-2 Story approval: after verified integration.
- PO-3 Product Owner interface: **a live conversational Claude session**, not the CLI inbox. I intend to honour specification section 24 by treating the session as an interface, not a store: every question, answer, decision and analysis version is written to the control plane as it happens; a lost session is replaced by a new one briefed from those records; `relay answer` and `relay approve` keep working without it.
- PO-4 Git: plain remote, Relay runs the checks.

PO-1 changes slice 1b. This is the contract I propose to build. Challenge it.

## Process Oracle Contract v1

A contract is registered by the Verifier before any run and is immutable; its identity is the sha256 of its canonical JSON.

```json
{
  "contract_id": "B1-standard-rate",
  "launch": {"argv": ["python3", "vat.py", "100"], "stdin": "", "timeout_seconds": 10},
  "expect": {"exit_code": 0, "stdout": "120\n"},
  "baseline": {"exit_code": 0, "stdout": "100\n"}
}
```

`expect` is the predicate for green: exact exit code and exact standard output. `baseline` is the admissible red observation, registered in advance: exact exit code, and either exact standard output or a required substring of standard error (for the common case where the feature does not exist yet and the program reports an unknown command).

The oracle, which is trusted supervisor code:

1. Makes a pristine checkout of the pinned revision in a fresh directory.
2. Launches `argv` there with a scrubbed environment, under the configured isolation (container with no network, or native by explicit project approval, recorded on the evidence).
3. Records: whether the process could be launched at all, exit code, standard output, standard error, whether it timed out, wall-clock duration, revision, contract digest, isolation mode.
4. Judges, in domain code with no model involved:
   - could not launch (executable missing, exec error, container exit 125 to 127): `not_executed`
   - timed out: `timed_out`
   - observation satisfies `expect`: `already_passes`
   - observation does not equal the registered `baseline`: `unexpected_failure`
   - otherwise: valid red.
5. Green: launched, completed, observation satisfies `expect`. Nothing else.

Anything the subject writes to disk, and any report from a test framework, is ignored for gating.

Known limits I intend to state rather than hide: one launch per contract (no multi-step interaction yet); exact-match output only (no JSON-aware or tolerance comparison yet); behaviours not observable at a process boundary are out of scope until an adapter exists; a subject that special-cases the contract's input passes the oracle, which is why the regression set and QA review still matter.

## What I need from you

1. Any way this contract still lets Relay certify a red or green that is not a real change in observable behaviour.
2. Whether exact-match `baseline` is too strict or too loose as the definition of "fails for the intended reason".
3. Any objection to my reading of PO-3 against section 24.
4. One line: DISPOSITION: ACCEPT | ACCEPT_WITH_RESERVATIONS | CHALLENGE | REQUEST_INFORMATION (for Process Oracle Contract v1).
