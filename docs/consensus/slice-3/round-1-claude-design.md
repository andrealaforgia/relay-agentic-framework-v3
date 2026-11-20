# Slice 3 design v1: from a problem to the story gate

Slices 1 and 2 take one behaviour from a registered contract to trunk. Slice 3 puts the story above it and the Product Owner at its gate, then lets real agents drive it. Specification scenarios 1, 3, 8 and 14.

I propose three sub-slices, each reviewed by you before the next starts, as with 2a and 2b.

## 3a. Stories, questions and the gate, with no model anywhere

Everything here is deterministic control-plane state. Agents are scripted callers in the tests, as reviewers were in 2a.

**Records**

- **Feature.** One problem submitted by the Product Owner to a project. States: `discovery`, `planning`, `delivering`, `completed`, `cancelled`. A `paused` flag. Approval mode is `per_story`; autonomy is slice 6.
- **Problem analysis.** Versioned documents per feature. A version is `draft` or `accepted`. Accepting a version requires that no open question blocks the analysis.
- **Question.** Raised by a role through one inbox: text, why it matters, options with trade-offs, and the subjects it blocks (the analysis, stories, behaviours). `open` or `answered`.
- **Decision.** Durable, per feature. Its source is `product_owner` or `model`, and that distinction is kept wherever a decision is shown or handed to an agent. Answering a question creates a `product_owner` decision.
- **Story.** Title, outcome, acceptance criteria, position in the roadmap. States: `planned`, `released`, `awaiting_approval`, `approved`, `changes_requested`, `rejected`.
- **Roadmap.** The ordered stories of a feature, versioned. The Product Owner approves the first version before any story is released.
- **Behaviour.** Gains a story and a position within it.

**Rules**

1. A story is released only when: the roadmap is approved, every earlier story is `approved`, no open question blocks it, and the feature is neither paused nor cancelled. Release is a transition the control plane makes; no agent requests it.
2. The Verifier registers a story's behaviours in order. A behaviour may enter test design only when the one before it is `integrated`. The Verifier may add behaviours while the story is `released` or `changes_requested`, and must declare the list complete.
3. Every operation that starts or releases work on a subject (propose, begin test design, register the contract, run red, run green, integrate) is refused while an open question blocks that subject or its story, with the question named in the refusal. A run already in flight finishes and is recorded.
4. When a story's behaviour list is declared complete and its last behaviour is integrated, in that same transaction the story becomes `awaiting_approval`, a **story review** is recorded, and one approval request goes to the Product Owner's inbox stream. The review is assembled by code from records only: behaviours and their contracts, the integrated commits, the red, green and check executions, findings and how each was settled, decisions with their source, and usage (empty until slice 5).
5. At the gate the Product Owner may: `approve` (the next eligible story is released in the same transaction); `request_changes` with a reason (the story returns to work and the Verifier must add behaviours; nothing on trunk is undone); `reject` with a reason (the story is closed as rejected, trunk is not rewritten, and the feature waits for a revised roadmap, per PO-2). Each is recorded as a `product_owner` decision.
6. `pause` stops every release of work across the feature (rule 3's list, and story release). `resume` lifts it. `cancel` is terminal and stops the same things for good. In all three, runs in flight finish and are recorded, and an authorised integration intent is still reconciled from trunk: an intent is a fact about the repository, not work that can be un-started.
7. Nothing in this slice calls a model, and no recovery step depends on a provider session.

**Acceptance tests (3a)**

- Scenario 8: all behaviours of story 1 integrated; story 2 is not released, and proposing a behaviour under it is refused, until `approve`.
- Scenario 14: at the gate, discard every service object, build new ones on the same database, and find the same gate, the same review and no new execution.
- Scenario 3: a question blocking story 1 refuses its red run and names the question; the answer is a decision of source `product_owner`, visible in the context for that story, and the run then proceeds.
- `request_changes` reopens the story without touching trunk; `reject` leaves later stories unreleased.
- Pause while a green run is held at a barrier: the run is recorded, no review cycle command is announced until `resume`. Cancel with an outstanding intent: reconciliation still records the integration, and nothing further is released.
- Duplicate `approve`, and `approve` racing `request_changes`: one decision takes effect.

## 3b. Workers and the Claude adapter

- A **provider adapter** interface: capability discovery, invoke an agent definition with a context and a required output schema, cancel, collect usage and logs. One implementation, Claude Code through its CLI in non-interactive mode. Codex is slice 4.
- **Agent definitions** are the canonical `alf-*` files, located by a configurable search path. Every invocation records the definition's path, repository revision and content hash.
- A **worker** reads commands from its role's stream, acts once per message (the receipts table), invokes the adapter, validates the structured output against the schema for that command, and calls the same application API the scripted callers of 3a used. Malformed output is recorded and retried a bounded number of times, then raised as a blocked task; it is never coerced.
- Builders and Verifiers work in a checkout of their own and publish through Git as now (attempt refs). Reviewers get the tree from `prepare_review`.
- Deterministic tests use a scripted adapter. Real Claude runs are separate smoke tests, not part of `make check`.
- Leases and fencing are slice 5; here a worker that dies simply leaves its message to be claimed again, which the receipts already make safe.

## 3c. CLI, the live session and packaging

- `relay` commands that perform the real operations of 3a and 3b: `init`, `up`, `worker start`, `doctor`, `feature start`, `status`, `roadmap`, `ledger`, `answer`, `approve`, `pause`, `resume`, `cancel`, with JSON output.
- The Product Owner's live conversational session (PO-3) is a Claude Code session that reads the inbox and calls those same commands. Every question, answer and decision is already durable from 3a, so losing the session loses nothing.
- Docker Compose for the control plane, PostgreSQL and Redis; a worker image.
- Scenario 1 end to end with the scripted adapter in `make check`, and once for real as a recorded smoke run.
- The usage instructions the Product Owner asked for are written here, when there is something to use.

## What I would like challenged

- Whether `request_changes` and `reject` have the right effects, given that trunk already holds the story's work (PO-2).
- Whether rule 6 defines pause and cancel tightly enough for work in flight.
- The split itself: is 3a too much or too little to review in one go?
- Anything in 3b that would force a change to 3a's records.

There is still no authenticated identity for callers: in 3a "the Product Owner" is whoever calls the Product Owner operations. That is stated, not solved, until the API of 3c.

End with: DISPOSITION: ACCEPT | ACCEPT_WITH_RESERVATIONS | CHALLENGE | REQUEST_INFORMATION (for slice 3 design v1).
