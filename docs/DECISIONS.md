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
| PO-7 | 2026-11-20 | **Where a decision is the Product Owner's, Relay's build goes with Claude's recommendation.** Andrea's words: "For any decision you depend on me, go with anything you recommend". | Each such decision is recorded below as delegated, with the recommendation that was applied, so he can overturn it. Claude's reading, not his words: this does not cover destructive or irreversible actions, which are still confirmed first. |
| PO-8 | 2026-11-20 | **`relay watch` is a requirement of slice 3c.** Andrea's words: "I rely on watch as way to observing how relay's agents are behaving, add it to 3c". | The CLI ships with a live board in the shape of the old one: liveness and behaviour states, the event log, and token spend per role. Its presentation is reused from the old Relay where it fits; its data comes from PostgreSQL, not from a Redis ledger. |
| PO-9 | 2026-11-20 | **The estimated token cost stays visible, as it is in the old Relay.** Andrea's words: "There is also a consideration around token cost. The old system allows to see the estimated token cost". | Token use is captured by the provider adapters from their first call (slice 3b), and the estimate is shown from the first CLI (slice 3c): `relay costs` and the token view of `relay watch`. It is not left to the dashboard of slice 7. A figure is always marked as observed, estimated or unknown; an unknown model is never priced as a known one, and unknown is never shown as zero. |

Decided under PO-7 on 2026-11-20, each on Claude's recommendation:

| Id | Decision |
|---|---|
| D-1 | **Order of work:** core slices 2 and 3 first, then ingest slices 1 and 2, then the two interleaved. |
| D-2 | **Ingest evidence:** a project's own test-suite results may count as "observed" for ingest, always labelled as the project's own report. Gates stay oracle-only under PO-1. |
| D-3 | **PostgreSQL only.** The SQLite option in section 6.3 of the ingest specification is dropped. |
| D-4 | **Mutation testing has no percentage threshold.** No surviving mutant on changed lines; each survivor is a QA finding that QA kills or dispositions as equivalent, never the Builder; a ratchet on Relay's own domain code; a time box whose expiry is reported, not passed. |
| D-5 | **Review capabilities stay in plaintext for now**, readable only by trusted database accounts. They are stored as hashes, and redacted from every view, no later than the slice that first exposes them outside the control plane (the API and dashboard). |
| D-6 | **The recommended defaults stand:** the review and security policy presets, pausing on unknown paid calls, and trusting enrolled worker hosts. |
| D-7 | **Trunk moved during the checks** sends the behaviour straight back for a fresh red, with no person in between. Claude had first recommended blocking it for a person; Astra argued for reopening once a fresh reading shows trunk strictly beyond the expected head, and that is what is built. A hook refusal with trunk unmoved still waits for a person. |
| D-8 | **Review rounds with Astra converge.** Andrea, 2026-11-20: "I don't mind the many rounds between you and Astra for design. I think, in general, it would be good to converge at some point." The rule is Claude's: at most three rounds per design or build step; anything reachable through Relay's real entry points is fixed; after the third round, remaining reachable findings are fixed without a further round and the rest are recorded as named reservations in the slice record. |
| D-9 | **One project per managed trunk.** A repository and branch can be managed by one Relay project only; projects may share a repository on different branches. Claude's recommendation after Astra's finding S3-16.1, taken on 2026-11-21 under the delegation in D-1's terms: what a candidate must still satisfy is recorded per project, so two projects on one trunk would not check each other's work, and a feature's final check could not say which commit it certified. Andrea can overturn it; sharing a trunk would then need obligations and locks that belong to the trunk, not the project. |

## Consequences drawn by the models, for the Product Owner to confirm or overturn

| Id | Date | Position | Why |
|---|---|---|---|
| M-1 | 2026-11-19 | **Native (unsandboxed) execution never opens a gate.** It is refused before anything is launched. The earlier recommended default allowed it with explicit project approval. | Follows from PO-1: a subject running under the supervisor's own account can reach the recorder, so its evidence is not from outside the code under test. Raised by Astra in code review (R1), accepted by Claude. Consequence: gate-bearing runs need Docker on the runner host. |

## Open questions for the Product Owner

None. See PO-7 and D-1 to D-9.
