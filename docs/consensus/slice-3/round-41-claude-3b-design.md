# Slice 3b: workers and the Claude adapter (design v1)

Provenance: written by Claude (Fable 5.2) on 2026-11-21 for review by Astra (gpt-6.2-astra via Codex). Nothing in it is built. Code is at `fa83d49` on `main`, 789 tests.

## How this review is to converge (decision D-8)

At most three rounds on this design. Please rank findings by whether a real entry point can reach them, and say which step each belongs to: 3b is built in four steps and a finding against step 3 need not hold up step 1.

## What 3b has to prove

Specification scenario 1 in part: with Claude alone, a feature whose problem and roadmap a person has approved is carried from a released story to a completed feature by workers running the canonical `alf-*` agents, with no person doing a worker's part. The CLI and the live session are 3c. A second provider, remote hosts and leases are slices 4 and 5.

Conditions already accepted for 3b (rounds 1 and 2 of slice 3, and the 3a-ii records):

- **S3-1.7**: an invocation intent is durable before a provider is called; the raw result is durable before its effect is applied; a known result is recovered once; an uncertain paid call is parked for a person (D-6); a malformed reply is a known invocation and a retry is a separately recorded call.
- **S3-1.10**: single host. Git publication and credentials stay with the supervisor; each model is scoped to its role; gate evidence stays in the local Docker oracle (PO-1, PO-5).
- **D-5**: falls due at the first exposure outside the control plane. See section 7 for why I argue it is not yet due.
- A principal on every worker-callable operation. A worker receives the story and list revisions with what it reviews and hands them back unchanged.
- **PO-9**: token use captured from the first call, each figure marked observed, estimated or unknown.

## 1. What is ported from the old Relay, and what is not

From a survey of `relay-agentic-framework`:

- **Ported**: the stream-json line parser and the reading of the result event (`runners/claude.py`, about 100 lines); the price table and estimate (`pricing.py`); the process-group kill and stderr-to-file of the old Codex runner; the stub-binary test pattern.
- **Ported with changes**: the price table becomes versioned and dated, an unknown model has no price (the old one priced it as Sonnet), and the two cache-write multipliers the old code disagreed on (2.0 and 1.25) become one stated per cache lifetime.
- **Not ported**: the worker loop (a Redis ledger), `relay-send` (the model publishing its own messages), the `roles/*.md` playbooks, and bypassing permission checks by default. The old Relay never located agent definitions; that is written new.

## 2. The provider adapter

```python
class ProviderAdapter(Protocol):
    name: str
    async def capabilities(self) -> Capabilities          # version, models offered, structured output, sessions
    async def invoke(self, request: Invocation, *, on_event) -> ProviderResult
```

`Invocation` carries: the agent definition (name, path, content), the model identifier, reasoning effort if any, the briefing text, the JSON schema the reply must satisfy, the working directory, the tools allowed, a budget in dollars, a timeout, and the session identifier Relay chose.

`ProviderResult` is one of: `returned` (raw final text, the parsed structured reply if it parsed, usage, the provider's own cost if it gave one, the session id, the provider's exit status), `failed_before_start` (the process could not be launched: nothing was paid for), or `uncertain` (launched, and no result event was read: killed, timed out, crashed, or this process was told to stop).

The Claude adapter runs `claude -p --output-format stream-json --verbose --agent <name> --model <id> --json-schema <schema> --session-id <uuid> --permission-mode <mode> --tools <list> --max-budget-usd <n> --setting-sources ""` with:

- the briefing on standard input, never in the argument list (it would be visible to every process on the host);
- an explicit environment: the supervisor's minus anything Relay holds (database and Redis addresses, Git credentials);
- its own process group, killed as a group at the deadline or on cancellation;
- standard error to a file, read afterwards;
- the agent definition passed by `--agents` from the content Relay hashed, so the definition invoked is the definition recorded, whatever is installed on the host at that moment.

Flags are confirmed against `claude --help` of the installed version (2.1.330) when the adapter starts, as `capabilities()`: a flag that is not there is a refusal to start, not a silent omission.

Deterministic tests use a stub `claude` on `PATH` that replays a recorded stream; the recorded streams are fixtures taken from real runs. One opt-in smoke test (`RELAY_REAL_CLAUDE=1`) calls the real CLI; it is not part of `make check`.

## 3. Agent definitions

A role resolves to a definition by configuration, with the specification's table as the default: analyst `alf-user-story-writer`, verifier and builder `alf-atdd-developer`, QA `alf-test-design-reviewer`, sentinel `alf-security-assessor`. Search order: configured paths, then the project's `.claude/agents`, then `~/.claude/agents`. For each invocation Relay records the path, the SHA-256 of the content, and the Git revision of the repository the file is in, if it is in one (and `dirty` if the file differs from that revision).

A role that resolves to no definition is a refusal to start that worker. Nothing falls back to "no agent".

The role restriction (verifier owns the acceptance contract; builder owns production code) is not left to the definition's prose. It is what each task may hand back (section 5): a verifier's reply has no field through which code could be changed, and a builder's has none through which a contract could.

## 4. Invocations (S3-1.7)

A table `invocations`, one row per call to a provider:

| Column | Meaning |
|---|---|
| `invocation_id` | Chosen by Relay before the call; also the provider session id |
| `assignment_id`, `grant_id`, `attempt` | The work, the grant it is done under, and which try this is |
| `role`, `provider`, `model`, `effort` | As resolved |
| `agent_path`, `agent_sha256`, `agent_revision` | Section 3 |
| `briefing_sha256`, `briefing` | What the model was given |
| `schema_sha256` | What the reply had to satisfy |
| `state` | `intended`, `returned`, `applied`, `rejected`, `not_started`, `uncertain`, `set_aside` |
| `raw_result`, `reply` | The final text as received; the structured reply if it parsed |
| `usage`, `provider_cost_usd`, `estimated_cost_usd`, `price_table` | Section 6 |
| `started_at`, `ended_at`, `reason` | |

The order of events for one task:

1. **Intend.** One transaction: the grant is checked current, the row is inserted `intended`, an event `invocation.intended` is recorded. At most one invocation of an assignment may be `intended` or `uncertain` at a time (a unique index).
2. **Call** the provider, outside any transaction.
3. **Record.** One transaction: the raw result is stored and the row becomes `returned`, or `not_started`, or `uncertain`. Usage is stored here.
4. **Apply.** The structured reply is validated and its workflow effect made through the ordinary application services, under the worker's principal and grant. The row becomes `applied` in the transaction that makes the effect, through the existing act-once receipt keyed on the invocation id. A reply that does not parse, does not satisfy the schema, or is refused by the service is `rejected` with the reason. That is a known invocation: nothing uncertain about it.

What follows from each ending:

- `applied`: the assignment is completed by the service that applied it, as now.
- `rejected`: a new invocation may be intended for the same grant, as a new row with `attempt + 1`, its briefing saying what was wrong with the last reply. After three attempts (configurable) the assignment is parked: an inbox item for the Product Owner, and nothing more is called.
- `not_started`: the grant is released; the assignment is wanted again.
- `uncertain`: parked at once. An inbox item says a paid call may have completed and its result was not received. The person chooses `retry` (a new invocation is allowed) or `set_aside` (the assignment stays parked). Nothing retries by itself. This is D-6.

On start, every `intended` row belongs to a process that is gone: it becomes `uncertain`. A `returned` row is applied (step 4) without calling anything: the result is known and is used once.

A question for you: with `--session-id` chosen by Relay, a call whose result was lost can in principle be read back from the provider's own transcript on the same host. I propose **not** to rely on that in 3b: the transcript's location and format are not an interface Claude Code promises. Uncertain stays uncertain.

## 5. Tasks: what a worker is asked and what it may hand back

Each task is: a command on the role's stream, an assignment to claim, a briefing assembled by Relay from durable records, a JSON schema for the reply, and one application-service call that applies it. The model is never given a way to call Relay; it replies, and trusted worker code applies the reply.

| Task | Role | Exists today | Reply | Applied through |
|---|---|---|---|---|
| `review` | qa, sentinel | yes | findings, verdicts on carried findings | `Reviews.submit_review` |
| `build`, `remediate` | builder | yes | a commit in its workspace, a note | supervisor publishes the attempt ref; `Behaviours.run_green` |
| `reconfirm` | analyst, verifier | yes | stands, or does not with a reason | `Decisions.reconfirm_*` |
| `correct`, `revise` | analyst | yes | a roadmap revision | `Stories.publish_roadmap` |
| `plan` | analyst | **new** | a roadmap | `Stories.publish_roadmap` |
| `specify` | verifier | **new** | the next behaviour of a released story and its contract, or "the story is demonstrated" | `Behaviours.propose`, `register_acceptance_test`; then `run_red` |
| `seal` | verifier | **new** | the behaviours that demonstrate the story | `Stories.seal` |
| `assess` | analyst | **new** | a statement for the Product Owner | `Stories.assess` |

Any task may instead reply with a **question** for the Product Owner or an **assumption**, applied through `Decisions.ask` and `Decisions.assume`.

The four new kinds are owed by the control plane in the transaction that makes them due: `plan` when a feature is opened; `specify` when a story is released and again when a behaviour of it is integrated; `seal` when `specify` replied that the story is demonstrated and every behaviour is on trunk; `assess` when a story is sealed. They are retired by the same rules as the existing kinds (closure, rejection, decisions).

Two points I expect you to press:

- **The builder's workspace.** The builder runs in a clone made by the supervisor at the red's revision, with file tools confined to it and no network tool. The clone has no remote and no credentials. When the builder replies, the supervisor commits nothing itself: it takes the commit the builder made, checks it descends from the red's revision, and publishes it under the attempt ref with the supervisor's own credentials. A reply naming a commit that is not in the workspace, or not a descendant, is `rejected`.
- **The reviewer's tree.** `prepare_review` exports the pinned candidate without Git metadata, as now. The reviewer's tools are read-only on that tree. QA and Sentinel get separate exports of the same commit.

The verifier in 3b writes a `ProcessContract` (what to launch, what must be observed), not test files in the repository. That is PO-1 as built since slice 1: the contract lives in PostgreSQL and the oracle evaluates it. `alf-atdd-developer` is briefed for exactly that part of its method.

## 6. Usage and cost (PO-9)

Per invocation: the four token counters as the provider reported them (a counter that was not reported is null, never zero), the provider's own cost figure if it gave one (`observed`), and Relay's estimate from the price table (`estimated`) with the table's version. A model the table does not know has no estimate (`unknown`). An `uncertain` invocation has usage `unknown`: it may have cost something.

The feature summary's `usage` stops being the literal `unknown`: it becomes the totals by role over the feature's invocations, each total marked `observed`, `estimated`, or `unknown` if any contributing invocation is unknown, with the count of uncertain invocations stated beside it. Invocations are tied to a feature through their assignment.

The views (`relay costs`, `relay watch --tokens`) are 3c. 3b provides the query they will read.

## 7. Principals, and D-5

Every worker-callable operation that lacks one gets a `Principal` parameter and a role check: `Behaviours.propose`, `begin_test_design`, `register_acceptance_test`, `run_red`, `run_green`, `reopen_red`; `Reviews.prepare_review`, `submit_review`, `dispute`. `run_red` and `run_green` are control-plane operations (the oracle is the control plane's); a worker asks for them by replying, and the worker runtime calls them under the control plane's principal.

In 3b the worker runtime is trusted code in the same trust domain as the control plane, on the same host, with the same database account. The model is the untrusted party, and it holds no capability: it never sees a review capability, a grant or an assignment id as something it can use, because it cannot call Relay. So I argue D-5 (hash capability verifiers, redact command payloads) is still not due: nothing is exposed outside the control plane's trust domain until slice 4's remote workers or 3c's CLI output. If you disagree, say what a model or a local process can reach in 3b that it could not before.

## 8. The worker runtime

One process, `relay worker --role <role>` (the command itself is 3c; 3b provides the function it calls). It reads its role's stream through a consumer group, and for each command: claims the assignment (the grant), assembles the briefing, runs steps 1 to 4 of section 4, and acknowledges the message only after step 3 has committed. A message delivered again finds its assignment granted or done, and the invocation table says what happened; nothing is called twice for one grant unless a person allowed it.

Concurrency in 3b is one task at a time per worker process. Workers of different roles run side by side. The control plane remains the integrator and the completer, as now.

Stopping: on SIGTERM the worker stops reading, lets a provider call in flight finish up to a grace period, and if it must kill it records `uncertain`.

## 9. Steps

1. **Adapter, definitions, invocations, usage.** Sections 2, 3, 4 and 6, with a stub provider and no workflow: an assignment of an existing kind is given to a function that runs the four steps and applies through a callback. Crash tests: after the provider returned and before the record; after the record and before the apply; a kill during the call.
2. **Principals and the two existing tasks.** Section 7; `review` and `build`/`remediate` driven end to end by a worker with a stub provider, including the builder's workspace and supervised publication.
3. **The owed work that is missing.** `plan`, `specify`, `seal`, `assess`; `reconfirm`, `correct`, `revise`; questions and assumptions from workers. A whole feature carried to completion by workers with a stub provider and a person deciding the gates.
4. **The real provider.** Recorded streams refreshed from real runs; the opt-in smoke; usage in the feature summary.

## What I know is thin

- **The reply schemas** are named, not specified. Each is specified with its step.
- **Budget** is a per-invocation cap passed to the CLI. Project budgets and bounded autonomy are slice 6.
- **Session continuation** is not used: every invocation is a fresh session, briefed from records. That costs cache warmth and buys recovery that never depends on a provider session.
- **Deterministic scanners beside Sentinel** (specification 5.7) are not in 3b.
- **The Problem Interpreter and discovery** (specification 6.A) are not in 3b: a feature starts from a problem statement a person wrote.

End with: DISPOSITION: ACCEPT | ACCEPT_WITH_RESERVATIONS | CHALLENGE | REQUEST_INFORMATION (for the slice 3b design), saying for each finding which step it belongs to and whether a real entry point can reach it.
