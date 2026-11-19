# Relay v3: architectural assessment, migration plan and agreed architecture

Status: Architecture Proposal v2, accepted with reservations by Claude (Fable 5.2) and Codex (`gpt-6.2-astra`) after three rounds. The exchange is in `docs/consensus/spec-v1.0/`. The reservations are the seven Product Owner decisions in section 5 and the unproven provider execution boundary in section 4.

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
| Evidence runner, receipts, evidence replay | Port with changes | Deterministic test execution: baseline digests for every test file, collect before run, per-test outcomes, skips do not count, digest-pinned container with no network. Needs detaching from the old envelope and a positive match on the registered failure. |
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

**Evidence.** The Verifier registers the test id and the expected failure before the run. Red requires that test to be collected, executed and failed by assertion with the registered failure. Errors, skips, timeouts, non-execution and other failures are rejected with a reason. Green requires the same contract passing on the candidate plus a frozen regression selection. Every behaviour gets its own red. The execution manifest pins baseline SHA, contract bundle digest, runner image, command, runner configuration and environment. No generic JUnit XML: each test framework is admitted through an adapter that supplies collection, execution and attempt identity.

**Ownership.** A deterministic importer checks each changeset's paths. The Builder owns production and unit-test paths; the contract bundle (acceptance tests, fixtures, invocation, runner configuration) belongs to the Verifier. A Verifier amendment creates a successor contract and reopens red.

**Git.** One configured remote per project. Workers push only to `refs/relay/attempts/<task>/<generation>`, from a trusted Git context. The control plane creates immutable `refs/relay/candidates/<changeset>` and is the only component that advances trunk, by conditional update from the expected head. Provider invocations never hold authority to update trunk. Relay integrates only into projects whose required checks it can run or verify; anything else is visibly blocked.

**Provider execution boundary.** Not yet proven. Each adapter gets a written contract (mounts, environment, credentials, subprocess permissions, network) and must pass canary probes on the real CLI before it is eligible for a role. A canary passes only when the probe attempted the operation and the enforcement layer denied it.

**Consensus.** Immutable proposals identified by (project, logical id, revision, sha256). Responses bind to the exact revision. Escalation is a blocked state with a named owner, never agreement. Counterpart outcome is recorded as `disabled`, `unavailable`, `substituted` or `completed`.

## 5. Open Product Owner decisions

| Decision | Recommended default (both models) |
|---|---|
| Evidence assurance | Accept the in-process pytest reporter for now, state its forgery limitation in the evidence record and on the dashboard, keep an adversarial fixture as a permanent test. |
| Story approval | After verified integration. Rejection opens a remediation or revert decision; trunk is never rewritten. |
| Host trust and native execution | Trust enrolled operators. Unsandboxed native test execution needs explicit project approval and is marked on its evidence. |
| Review and security policy | Ship `claude-only` and `dual-provider` presets. Unresolved high and critical Sentinel findings block, with no override in v1. |
| Unknown paid calls and budgets | Pause for a human decision. Govern unpriced subscription calls by call and wall-clock limits. |
| First Git and CI target | Configured remote with enforced trunk ownership; no hosted CI in v1. |
| Product Owner interface | Central inbox through the `relay` CLI and a thin Claude Code skill that calls it. |

## 6. Slice order

Each slice is a vertical cut with executable acceptance tests against real PostgreSQL, Redis and Git. Provider replies are scripted in deterministic tests; real CLI smokes run separately.

| Slice | Proves | Specification scenarios | Status |
|---|---|---|---|
| 1a | Red gate in durable state; ledger; outbox; act once | 4 (state level), 11 (application effect) | Done |
| 1b | Evidence runner: wrong value, failed setup, nothing collected, forged claim, forged report | 4 | Next |
| 1c | Green on the same contract; contract bundle protected; frozen regression selection | 5 | |
| 2 | QA and Sentinel on one candidate; stale base rejected; one trunk advance across crashes | 6, 11 | |
| 3 | Claude-only story to the human gate; restart at the gate | 1, 3, 8, 14 | |
| 4 | Two providers disagree across machines; round cap escalates | 2, 7 | |
| 5 | Lost lease, late result, unknown paid call | 10, 11 | |
| 6 | Bounded autonomy, budgets, pause and cancel | 8, 9 | |
| 7 | Dashboard: roadmap, ledger, conversations, usage and cost | 12 | |
| 8 | Live redacted log tail | 13 | |
