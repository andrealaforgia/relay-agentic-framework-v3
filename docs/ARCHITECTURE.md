# Relay v3: architectural assessment, migration plan and agreed architecture

Status: Architecture Proposal v2, accepted with reservations by Claude (Fable 5.2) and Codex (`gpt-6.2-astra`) after three rounds. The exchange is in `docs/consensus/spec-v1.0/`. Four of the seven Product Owner decisions have since been made (`docs/DECISIONS.md`); two of them differ from what the models recommended and amend the proposal as marked below. The provider execution boundary in section 4 remains unproven.

## 1. What was inspected

| Subject | Finding |
|---|---|
| Old Relay, `~/dev/relay-agentic-framework` (working tree, with uncommitted changes) | Python 3.12, synchronous redis-py, typer. One Redis stream is bus, ledger and audit log; every role reads every message. Coordinator state lives in memory and is rebuilt by replay. No PostgreSQL, no leases, no HTTP API, no OpenTelemetry, no web dashboard (terminal view only). 591 tests pass; mypy strict is clean. |
| Agent repository, `~/dev/claude-code-agents` at `a5c8cb8` (working tree has uncommitted changes) | 26 `alf-*` agents. Installed as Markdown with front matter in `~/.claude/agents` and as TOML with `developer_instructions` in `~/.codex/agents`. Both renderings carry the same instructions. |
| Claude Code 2.1.329 | Headless: `-p`, `--output-format stream-json`, `--json-schema`, `--agent`, `--tools`, `--permission-mode`, `--permission-prompts none`, `--settings`, `--max-budget-usd`, `--session-id`, `--resume`. Read from `--help`; not yet exercised. |
| Codex CLI 0.167.1 | Headless: `codex exec --json`, `--output-schema`, `-s read-only|workspace-write`, `-m`, `-o`, `exec resume`. Exercised for this review. Observed: `exec resume` does not keep the session's model; it falls back to the configured default unless `-m` is passed. |
| Models | "Astra" is a Codex model. Available ids include `gpt-6.2-astra`, `gpt-6.2-sol`, `gpt-6.1-sol`, `gpt-6-luna`. Aliases resolve through configuration, never by assumption. |

## 2. Reuse verdict on the old Relay

Reused only where aligned with the new specification.

| Old component | Verdict | Reason |
|---|---|---|
| Evidence runner, receipts, evidence replay | Port the isolation and digest mechanisms only | Its digest-pinned, no-network container and baseline file digests are reused. Its pytest reporter runs inside the subject's process, so under decision PO-1 it carries no gate authority and is not ported as a gate. |
| Fault classifier | Port, as diagnostics only | Distinguishes "did not run" from "failed". It never establishes a red on its own. |
| Claude and Codex runners | Port the stream parsers | Rewrite as async adapters with stdin prompts, process-group kill, structured output and Codex usage capture. The old default of bypassing permission checks is dropped. |
| Usage fold, pricing table | Port with changes | Separate observed, estimated and unknown; version the table; never price an unknown model as a known one. |
| Briefing assembly | Port with changes | Smallest sufficient context, rewired to artefact references. |
| Fake-runner end-to-end test pattern, fixture Git repository | Carry over | The template for testing Relay without a model. |
| `docs/DECISIONS.md`, repair reports | Reference | Lessons that still hold: models propose and code disposes; the Builder never grades its own work; no timeout ever approves; never invent a zero cost; check for an existing result before executing; a late-rescued request is a duplicate, not a retry. |
| Publisher, sequence script, dedup map, ledger reader | Discard | Redis as ledger. Sequence and idempotency belong in PostgreSQL. |
| Dispatcher (1612 lines), projection (1249 lines), state model | Reference only | Mined for transition guards; not ported. State in memory, restart by replay, a different entity model. |
| Expectation protocol, corrections and repair layers | Discard | Not in the new specification. |
| Bespoke `roles/*.md` | Discard | Superseded by the canonical `alf-*` definitions. |
| `relay watch` (live board: liveness and behaviour states; `--events`; `--tokens`) | Port the views and their rendering; rewrite the data source | Required by PO-8: it is how the Product Owner observes the agents. The old one folds a Redis ledger; here the same facts are read from PostgreSQL (the ledger, assignments, the inbox). Arrives with slice 3c. |
| Interactive interpreter (pty proxy, hooks, inbox, wake) | Discard | Workflow recovery must not depend on a provider session. |
| Model verdict as the completion gate | Discard | Completion is decided by evidence and current reviews. |

Nothing has been ported yet. Each port arrives with the slice that needs it, with tests that expose the old weakness it had.

## 3. Migration plan

There is no data migration: old ledgers are Redis streams with a different envelope and entity model, and the old system has no dashboard whose behaviour could be preserved. What carries over is content: the roadmap's story and behaviour board, the ledger's chronological two-lane view, the token table's columns, and worker log tailing become dashboard and CLI views over PostgreSQL. Old projects are not upgraded in place; a project is re-opened under v3.

## 4. Agreed architecture

**Threat model.** One Product Owner. Enrolled, operator-controlled worker hosts. Repository contents, model output, test output and tool output are untrusted. Whoever controls an enrolled runner host can forge evidence; that is the accepted host-trust assumption. Hostile multi-tenancy is out of scope.

**Processes.** A control plane (authenticated API, state machines, scheduler, outbox publisher, lease reaper and reconciler, integration controller). One worker daemon per host, which is the trusted supervisor: it alone holds the worker's credentials and runs provider adapters and the model-free evidence runner. PostgreSQL and Redis as services. The CLI and dashboard talk only to the API.

**Transport.** Commands go out on Redis streams, one stream per project and capability pool, one consumer group per stream; domain events fan out on a project event stream. Claim, heartbeat and result submission are synchronous authenticated calls to the control-plane API, because lease acceptance needs a transactional answer. This is our reading of specification section 3.2.

**Durability.** A transition, its ledger event and its outgoing message commit together. The publisher appends to Redis and then marks the row sent. Unfinished unowned tasks are re-announced from PostgreSQL even if Redis is empty. A message is acknowledged only after a durable result or disposition.

**Leases.** PostgreSQL issues monotonically increasing generations on database time. Claim, renewal and result acceptance check project, task, worker, generation, unexpired lease and current input version under the task row lock. Redis claiming grants delivery, never ownership.

**Paid calls.** Invocation intent is persisted before the call. A reply lost after intent is an `unknown` effect: the reservation is kept, the work is parked and the Product Owner decides. Relay never makes a second paid call on its own.

**Evidence (amended by PO-1).** Gate-bearing evidence comes only from an oracle outside the code under test. The Verifier registers, before any run, a contract: how to launch the subject, the expected observation, and the observation the baseline is expected to produce. Relay launches the subject in an isolated checkout of the pinned revision and evaluates the predicate itself. Red requires that the subject launched, ran to completion, did not satisfy the expectation, and produced the registered baseline observation. Failure to launch, a timeout or any other observation is rejected with a reason. Green requires the same contract satisfied on the candidate, plus a frozen regression selection. Every behaviour gets its own red. The execution manifest pins revision, contract digest, isolation mode, command and environment. The first oracle adapter observes a process (arguments, standard input, exit code, standard output); further interfaces need their own adapters. A test framework's report, and any file the subject writes, has no gate authority.

**Ownership (revised after PO-1).** The acceptance contract is a record in PostgreSQL, so no changeset can alter, skip or weaken it, and no path-ownership importer is needed for it. What remains a Verifier responsibility, checked in counterpart review: the contract's launch must be the product's public entry point. If it launches a wrapper or relies on a fixture kept in the repository, a Builder could change that file instead of the product; such files must travel with the contract or be protected by digest. That protection is not built.

**Git.** One configured remote per project. Builders publish candidates under `refs/relay/attempts/<project>/<behaviour>/<attempt>` (to become task and generation once tasks exist), from a trusted Git context. The control plane creates immutable `refs/relay/candidates/<changeset>` and is the only component that advances trunk, by conditional update from the expected head. Provider invocations never hold authority to update trunk. Relay integrates only into projects whose required checks it can run or verify; anything else is visibly blocked.

**Provider execution boundary.** Not yet proven. Each adapter gets a written contract (mounts, environment, credentials, subprocess permissions, network) and must pass canary probes on the real CLI before it is eligible for a role. A canary passes only when the probe attempted the operation and the enforcement layer denied it.

**No secret stands for a review (D-11).** A reviewer acts on a review by holding the grant on the review's assignment, which a claim gives to a caller of the review's role: an enrolled, live worker process, or a caller inside the control plane's own process. A review command names the review and carries nothing that grants it, so nothing in the review tables, the outbox or the command streams lets its reader act as a reviewer. Grants are not secrets and are in the ledger. When an API exposes review operations, it authenticates the caller as the worker a grant was given to.

**Consensus.** Immutable proposals identified by (project, logical id, revision, sha256). Responses bind to the exact revision. Escalation is a blocked state with a named owner, never agreement. Counterpart outcome is recorded as `disabled`, `unavailable`, `substituted` or `completed`.

## 5. Product Owner decisions

Made: PO-1 external oracle only; PO-2 approval after integration; PO-3 live conversational session as the interface, with every exchange persisted so recovery never depends on the session; PO-4 plain Git remote. See `docs/DECISIONS.md`.

Open, on recommended defaults: trust enrolled operators, with unsandboxed native execution needing explicit project approval and a mark on its evidence; `claude-only` and `dual-provider` presets, with unresolved high and critical Sentinel findings blocking and no override in v1; pause for a human decision after an unknown paid call.

## 6. Slice order

Each slice is a vertical cut with executable acceptance tests against real PostgreSQL, Redis and Git. Provider replies are scripted in deterministic tests; real CLI smokes run separately.

| Slice | Proves | Specification scenarios | Status |
|---|---|---|---|
| 1a | Red gate in durable state; ledger; outbox; act once | 4 (state level), 11 (application effect) | Done |
| 1b | Process oracle: wrong value is red; failure to launch, timeout, unregistered failure and a forged report are not; container boundary shown with live canaries | 4 | Done |
| 1c | Green paired with its red: same contract, same runtime, candidate built on the red's revision and read from the Builder's attempt ref. The contract lives in PostgreSQL, so a Builder's changeset cannot alter it. Regression checks against earlier behaviours wait for slice 2, where integration first makes earlier behaviours part of trunk. | 5 (without regression) | Done |
| 2a | QA and Sentinel review the same pinned, published candidate; findings, per-candidate verification, the gate, recovery | 6 | Accepted |
| 2b | Integration: required checks on the exact candidate, one authorised trunk update, stale base, crash reconciliation | 5 (regression), 11 | Accepted |
| 3a-i | Stories above behaviours; a roadmap gate and a story gate decided once, by a person, on the stored review; restart with an empty broker | 8, 14 | Accepted |
| 3a-ii | Questions and changed decisions; corrections; pause and cancel; feature completion | 3 | Steps 1 and 2 of 4 (pause, resume, cancel; questions and changed decisions) built, each reviewed in three rounds with the last fixes unreviewed. Step 3 (corrections): part (i) accepted with reservations; part (ii), replacing what is on trunk, built and reviewed in three rounds, not accepted, its last fix unreviewed. Step 4 (feature completion) built and reviewed in three rounds, not accepted, its last fixes unreviewed |
| 3b | Workers and the Claude adapter driving the canonical agents; token use captured on every call (PO-9) | 1 (in part) | Designed in three rounds, not accepted; the build contract is `round-44`. Step 1 of 4 (the adapter, the price table, agent definitions, invocation records, worker liveness, the runner) built and reviewed in three rounds, not accepted, its last fixes unreviewed. Steps 2 to 4 (confinement and the existing tasks; the missing owed work; the real provider) not built |
| 3c | CLI, including `relay watch` (PO-8) and the estimated token cost in `relay costs` and `relay watch --tokens` (PO-9); the live session; packaging; usage instructions | 1 | |
| 4 | Two providers disagree across machines; round cap escalates | 2, 7 | |
| 5 | Lost lease, late result, unknown paid call | 10, 11 | |
| 6 | Bounded autonomy, budgets, pause and cancel | 8, 9 | |
| 7 | Dashboard: roadmap, ledger, conversations, usage and cost | 12 | |
| 8 | Live redacted log tail | 13 | |
