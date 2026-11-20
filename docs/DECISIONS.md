# Product Owner decisions

Decisions made by Andrea. Model recommendations are noted where he chose differently.

| Id | Date | Decision | Consequence |
|---|---|---|---|
| PO-1 | 2026-11-19 | **Gate-bearing evidence comes only from an oracle outside the code under test.** Both models had recommended accepting an in-process pytest reporter with a stated limitation. | Relay launches the subject and evaluates the registered predicate itself. A test framework's own report has no gate authority. The first release supports behaviours observable at a process boundary; each further interface (HTTP and so on) needs its own oracle adapter. The old Relay's pytest evidence runner is not ported as a gate; only its isolation and digest mechanisms are reused. |
| PO-2 | 2026-11-19 | **Story approval follows verified integration.** | Each reviewed behaviour integrates to trunk when green. Approval accepts the story and releases the next. Rejection opens a remediation or revert story; trunk is never rewritten. |
| PO-3 | 2026-11-19 | **The Product Owner talks to Relay through a live conversational Claude session.** Both models had recommended a CLI inbox with a thin skill. | The Interpreter is an interactive Claude Code session. To keep specification section 24 ("do not require provider-native session resumability for workflow recovery"), the session is an interface, not a store: every question, answer, decision and analysis version is written to the control plane as it happens, and a lost session is replaced by a new one briefed from those durable records. `relay answer` and `relay approve` remain available without the session (section 25). |
| PO-4 | 2026-11-19 | **First release integrates into a plain Git remote; Relay runs the checks.** | No hosted CI or branch-protection integration in v1. A project that requires checks Relay cannot run or verify is visibly blocked at integration. |

| PO-5 | 2026-11-20 | **Gate-bearing runs use the local Docker engine.** Confirms M-1 below. | Native execution never opens a gate; every runner host needs a local Docker engine. |
| PO-6 | 2026-11-20 | **Commit and push frequently to the GitHub remote** (`andrealaforgia/relay-agentic-framework-v3`, public). | Trunk is pushed after each green, committed step. |

Still on recommended defaults, not yet explicitly confirmed: review and security policy presets; pause on unknown paid calls; trust in enrolled worker hosts.

## Consequences drawn by the models, for the Product Owner to confirm or overturn

| Id | Date | Position | Why |
|---|---|---|---|
| M-1 | 2026-11-19 | **Native (unsandboxed) execution never opens a gate.** It is refused before anything is launched. The earlier recommended default allowed it with explicit project approval. | Follows from PO-1: a subject running under the supervisor's own account can reach the recorder, so its evidence is not from outside the code under test. Raised by Astra in code review (R1), accepted by Claude. Consequence: gate-bearing runs need Docker on the runner host. |

## Open questions for the Product Owner

- Mutation testing policy. Recommended by Claude, awaiting a decision: no percentage threshold; instead no surviving mutant on changed lines, each survivor a QA finding that is killed or dispositioned as equivalent by QA (never by the Builder); a ratchet on Relay's own domain code; a time box whose expiry is reported, not passed. (Arises from the plan to add mutation testing as a deterministic QA check.)
