# Product Owner decisions

Decisions made by Andrea. Model recommendations are noted where he chose differently.

| Id | Date | Decision | Consequence |
|---|---|---|---|
| PO-1 | 2026-11-19 | **Gate-bearing evidence comes only from an oracle outside the code under test.** Both models had recommended accepting an in-process pytest reporter with a stated limitation. | Relay launches the subject and evaluates the registered predicate itself. A test framework's own report has no gate authority. The first release supports behaviours observable at a process boundary; each further interface (HTTP and so on) needs its own oracle adapter. The old Relay's pytest evidence runner is not ported as a gate; only its isolation and digest mechanisms are reused. |
| PO-2 | 2026-11-19 | **Story approval follows verified integration.** | Each reviewed behaviour integrates to trunk when green. Approval accepts the story and releases the next. Rejection opens a remediation or revert story; trunk is never rewritten. |
| PO-3 | 2026-11-19 | **The Product Owner talks to Relay through a live conversational Claude session.** Both models had recommended a CLI inbox with a thin skill. | The Interpreter is an interactive Claude Code session. To keep specification section 24 ("do not require provider-native session resumability for workflow recovery"), the session is an interface, not a store: every question, answer, decision and analysis version is written to the control plane as it happens, and a lost session is replaced by a new one briefed from those durable records. `relay answer` and `relay approve` remain available without the session (section 25). |
| PO-4 | 2026-11-19 | **First release integrates into a plain Git remote; Relay runs the checks.** | No hosted CI or branch-protection integration in v1. A project that requires checks Relay cannot run or verify is visibly blocked at integration. |

Still on recommended defaults, not yet explicitly confirmed: host trust and native execution; review and security policy presets; pause on unknown paid calls.
