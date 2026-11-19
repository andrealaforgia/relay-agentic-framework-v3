# Round 5: Process Oracle Contract v2, and the code for slices 1a and 1b

## Dispositions on your round 4 challenge

| Item | Disposition | What I did |
|---|---|---|
| O1 | ACCEPT | Green stays a run outcome. Every execution row records revision, contract digest and isolation; the build request carries the base revision the red was observed on. The pairing rule (accepted red on the pinned base plus green on its candidate, same contract digest, same execution settings, fresh red if the manifest changes) is slice 1c and is not built yet. The runtime identity (image digest or native interpreter identity) is not yet recorded on the execution row; that is a known gap for 1c. |
| O2 | ACCEPT | Every contract must state what public behaviour it observes (`observes`, refused if blank). Whether the launch is the product's interface rather than a test framework is a Verifier judgement and a counterpart review item; I do not pretend code can decide it. |
| O3 | ACCEPT | Streams are compared whole, as bytes, against the UTF-8 encoding of the registered text. Stdout and stderr are captured separately. Timeout, death by signal, output over the capture limit and launcher statuses 125 to 127 make a run ineligible for both gates. |
| O4 | ACCEPT | Native evidence is refused unless the project has approved it, and the refusal is on the ledger. A container isolation exists: no network, read-only root and checkout, no capabilities, bounded memory, processes and CPU, no host environment, image pinned by digest. Its boundary is tested with canaries that are shown to be live by running the same probe natively. |
| Baseline | ACCEPT | Exact exit code and exact content of one selected stream. The substring matcher is gone. The evidence wording is "the run matched the pre-registered baseline observation". |
| PO-3 | ACCEPT | The trusted human interface commits Andrea's answers, bound to the exact question or evidence snapshot. Your disconnect-after-commit test is the acceptance test for that slice. |

## Code to review

The repository is your working directory. Read it; do not modify it. `make check` passes (63 tests against real PostgreSQL, Redis, Git and Docker; mypy strict; ruff).

- `src/relay/domain/evidence.py`, `src/relay/domain/behaviour.py`
- `src/relay/persistence/` (migrations, ledger, outbox, receipts)
- `src/relay/transport/` (envelope, streams)
- `src/relay/application/behaviours.py`, `src/relay/application/red_runs.py`
- `src/relay/git/checkout.py`, `src/relay/testing/oracle.py`
- `tests/`

Review as the Builder's counterpart and as QA: correctness, concurrency, security of the oracle and checkout, places where a test would pass while the guarantee is broken, unnecessary complexity. Give findings with file and line, a concrete failure, and BLOCKING or NON-BLOCKING. Do not restate what is fine.

End with: DISPOSITION: ACCEPT | ACCEPT_WITH_RESERVATIONS | CHALLENGE | REQUEST_INFORMATION (for Process Oracle Contract v2 and slices 1a and 1b as built).
