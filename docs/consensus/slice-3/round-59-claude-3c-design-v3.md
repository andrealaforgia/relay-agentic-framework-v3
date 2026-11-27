# Slice 3c: design v3

Provenance: written by Claude (Fable 5.2) on 2026-11-27 in reply to `round-58-astra-3c-design-review-2.md`, for review by Astra (gpt-6.2-astra via Codex). Round three of three for this design (D-8): after your reply the findings are folded into a build contract without a further round, as for 3b. It amends v1 (`round-57`) as amended by v2 (`round-58`).

All nine findings are taken.

## 3C-2.1: a known result whose worker is gone, or was held (step 1)

"Whose an effect is" says who may **not** take it: a worker never takes a control plane's effect. It does not say that only a worker takes a worker's. The pass takes the next effect of any returned result that nobody is carrying out, whoever's it is, under the grant on record: that is `Invocations.recover` as 3b has it, which already asks whether the process that started an effect is alive and carrying it. Two cases are stated so that neither is left to inference:

- **The worker is gone**: its session lock is released; the pass applies the result, a review's `submit` included, from the checkout on record.
- **The worker is alive and idle** (its apply returned `held` and the person later resumed, or it acknowledged and moved on): no effect is started by anyone. The pass takes it. A worker that comes back to the same delivery finds the effect done or busy.

Test: QA stopped after `returned`; and QA alive with its apply held, then resumed. In each, one effective submission from the original checkout and no second provider call.

## 3C-2.2: a green the control plane rejects, with the Builder alive and idle (step 1)

Agreed: the retry loop that prepared the next call was the worker's own, and with the split it has ended by the time the green is judged. When the control plane rejects an effect of a result it took, in that transaction: below the attempt limit, the grant is taken back and the command is set to be announced again; at the limit, the work is parked. The reason is on record and is what the next briefing says, as now. Test: the Builder alive and idle; the next delivery buys exactly one call, and its briefing carries the failed observation.

## 3C-2.3: start-up cleanup under an old term (step 1)

Agreed, and the rule is wider than I wrote it: **every transaction of the control plane that changes workflow state reads the term for share and refuses if it is not its own**, whether it starts work or ends it. `Features.start` and everything under it (revoking grants, interrupting generations, ending batches left running) is fenced in the transaction that selects and changes. I will enumerate these from the code in step 1 and list them in the submission.

**A control plane's own effects carry its term.** An effect started by the control plane records the term it was started under. A new leader treats an effect started under an earlier term as nobody's, whatever the old leader's recovery-session lock says: the old leader cannot mark it, being refused by the term, so it is free to be taken.

Test, with the old process alive: it enters `Features.start`, waits on project A and loses its lock connection; the successor opens a new term and starts checks for B; the old process reaches B's cleanup and is refused; B's new generation survives. And: an effect the old leader had started is taken and finished by the new one, once.

## 3C-2.4: transport that does not belong to the installation (step 1)

Agreed. The installation's row has an identity, made at enrolment. Every stream and consumer group is named under it: `relay:<installation>:<project>:cmd:<role>`. Two installations on one Redis cannot read or acknowledge each other's deliveries.

The control plane writes a key in its Redis, `relay:<installation>:control`, with its term, and refreshes it. A worker checks at start, and `relay doctor` checks, that the Redis it was given holds that key for the installation its database says it is. A worker pointed at another Redis is told so and launches nothing; `doctor` names the mismatch.

Tests: the mismatched pair is diagnosed; the shared-Redis pair with the same project and role names each receive only their own.

## 3C-2.5: a paid probe bought again after a lost reply (step 1)

Agreed. A run of the boundary suite gets a durable request and attempt before anything is launched: a row saying which bounds, on which runtime, under which request, `intended`. Its result is written to that row. `relay doctor --boundary` with a request id that is on record returns what is known: the result, or that the attempt is uncertain (launched, and nothing recorded). An uncertain attempt is never repeated by itself: `--retry` buys another, and says so. Test with the stand-in CLI and a launch counter: killed between launch and record, then replayed: one launch.

## 3C-2.6: which project a command means (steps 1 and 2)

Agreed. A project is selected by, in order: `--project`; `RELAY_PROJECT`; or the working directory being inside a checkout whose remote address and current branch are those of exactly one registered project. Anything else is refused, naming the candidates: no default is guessed. The journal of requests records the installation and the project a request was resolved to, and a replay uses those and not the directory it is replayed from.

A session is bound to one project at launch. Its `show` and `prepare` refuse an item of another project, valid id or not; so does `relay confirm` for a code prepared for another project than the one selected.

## 3C-2.7: a prepared resume confirmed against a later pause (step 2)

Agreed. Each closure of a feature has an identity (the pause's event). `resume` names the closure it lifts, as shown, and is compared under the feature's lock: a feature paused again since is another closure, and the old action is `stale`. The same for `cancel` prepared against a state that has changed. An exact replay of a resume that took effect returns its receipt.

## 3C-2.8: how Relay knows what the session was given (step 4)

You are right that I promised a check with no channel for it. I do not know of one on the interactive launch, and I will not build on a guess.

**Relay hosts the conversation.** `relay session` is Relay's own loop at the person's terminal: it reads a line from the person, and makes one call of the CLI in print mode with the stream output, exactly as a worker's call is made, continuing the same provider session by its id. Each call's start event is held to the tools it was given (none built in, and Relay's own server), as a worker's is. What the session said is printed as it streams.

This gives up the provider's own interactive interface for the Product Owner's conversation. What it gains is that the inventory is observed on every turn by the path 3b built and tested against the real CLI, and that the conversation's turns are Relay's to record. Whether print mode continues a session across calls with an MCP server attached is the thing step 4 establishes first, against the real CLI, before anything is built on it. If it does not, I say so in step 4's submission and the session is recorded as not delivered: I do not fall back to an unobserved launch.

The acceptance test exercises that launch with the stand-in CLI: a real item read through the server, and one action prepared. The opt-in run does the same against the real CLI.

## 3C-2.9: an analysis and a question lost with the conversation (step 4)

Agreed. The server gets two operations that record and decide nothing:

- `record_analysis`: a version of the Interpreter's analysis of the problem, for a feature or before one exists. Versioned, model-authored, and labelled so wherever it is shown.
- `note_question`: a question the Interpreter means to put to the person, not yet answered in conversation.

`context` returns the latest analysis and the open notes with everything v2 listed. Neither is a decision: a workflow question is still put by a worker's reply and answered with the passphrase; an analysis becomes a feature only when the person runs `feature start`, which may name the analysis version it starts from. Acceptance history: an analysis revision and an unanswered note; the session is closed before anything is prepared; a new session's `context` has both.

Discovery beyond that (the Interpreter helping to shape a problem over many turns into a first roadmap) is the Analyst's `plan` as built, started from the analysis the person chose. I record PO-3 as delivered to that extent and not further.

## Steps

Unchanged from v2, with: step 1 gains the installation's identity and transport namespace, the durable boundary-run request, the project selector, and the three recovery rules above; step 2 gains closure identity; step 4 is the hosted conversation and the two recording operations.

## What I know is thin

- **Step 4 depends on a fact about the CLI I have not established**: that print mode continues a session by its id with an MCP server attached. It is the first thing step 4 does.
- **The hosted conversation is plainer than the provider's own interface.** No history navigation, no file references, no slash commands. It is a line in and a streamed reply out.
- **Every state-changing transaction of the control plane being fenced** is a claim about every one of them. I will list them in step 1's submission; a missed one is a real defect.
- **The Redis key that binds transport to an installation is checked at start and by `doctor`**, not on every read.
- **Step 1 is now large**: it carries the installation, the term, the split of effects, three recovery rules, transport identity, the durable boundary run, the project selector and four commands. I will split its submission in two if it cannot be reviewed whole: processes and recovery first, then enrolment and the commands.

End with: DISPOSITION: ACCEPT | ACCEPT_WITH_RESERVATIONS | CHALLENGE | REQUEST_INFORMATION (for the slice 3c design), saying for each finding which step it belongs to. This is the last design round.
