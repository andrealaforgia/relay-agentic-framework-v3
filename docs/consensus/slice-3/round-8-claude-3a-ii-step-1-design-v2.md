# Slice 3a-ii, step 1, design v2: pause, resume and cancel

All ten findings accepted. S3-4.1 to S3-4.5 are answered here. S3-4.6 and S3-4.7 are conditions of the step 2 design round, S3-4.8 and S3-4.9 of step 3, S3-4.10 of step 4; none is designed here. Sections 1 to 3 of v1 are replaced by this document.

## 1. One lock to close a project's gates (S3-4.1)

Closure is recorded on the **feature** (`closed_by`: null, `paused` or `cancelled`), so it exists before the first story and covers every later roadmap revision. v1's copies on story rows are dropped.

A new row per project, the **gate lock**, is taken first by every workflow transaction, before any behaviour, story, project or feature row:

- **shared** by every transaction that authorises or records work: all of `Behaviours`, `Reviews`, `Integration` and `Stories`, including roadmap publication, release, gate decisions, claims and intent creation;
- **exclusive** by pause, resume and cancel.

These transactions are short: none holds the lock across an oracle run or a push. So a closure either commits before an authorising transaction starts, and is seen, or waits for it to commit. The existing order (behaviour, story, project, feature) is unchanged underneath it. It is a separate row from the project's, so the ledger does not turn shared holders into exclusive ones.

Tests: pause against the first roadmap publication, and against the release of a planned story, each in both commit orders, with the barrier waiting on PostgreSQL's own report of the lock wait.

## 2. What each closure does to each transition (S3-4.2)

| | Paused | Cancelled |
|---|---|---|
| The project's story slot | **Kept.** Another feature of the project waits. | **Freed in the cancelling transaction.** Every story of the feature not yet approved or rejected becomes `cancelled`, and `release_next` runs before commit. |
| Releasing a story of this feature | Not done | Never |
| An open gate of this feature | Stays open | Superseded, with its inbox item, in the cancelling transaction |
| A first human action on such a gate | Refused; nothing recorded; the gate stays open for after resume | Stale |
| An exact replay of an action already recorded | Its original receipt | Its original receipt |
| An outstanding integration intent | Reconciled to its end | Reconciled to its end; nothing opens or releases as a result |

Pause and cancel are human actions with receipts, like gate decisions; their target is the feature. `cancelled` is a new story state that neither holds the slot nor blocks the order of another feature.

Test: cancel a feature whose story is at an open gate while another feature waits: the gate is superseded, the waiting feature's story is released in that transaction, and a later approval of the old gate is stale.

## 3. Starting, recording, and the boundary between them (S3-4.3)

Every operation is one of three kinds.

**Starts work.** Refused when the feature is closed, whoever calls and by whatever route: propose, begin test design, register a contract, run red, run green, integrate, seal, assess, publish a roadmap, publish candidates, announce review tasks, `prepare_review`, `retry_integration`, `reopen_red`, dispute, and `claim`.

**Records work already started.** Never refused; the evidence is always kept:

- the result of a red or green run. Under **pause** it is judged as now, and any command it issues is stored held. Under **cancel** it is recorded as `superseded`: the execution is kept, the behaviour does not move, and no command or review cycle is created;
- `submit_review` for a task that already has a checkout receipt. Under cancel the submission is stored as stale;
- the settlement of a check batch: no intent if the feature is closed (the batch is `held` under pause, `abandoned` under cancel);
- the reconciliation of an intent.

**The boundary.** Work on a command starts at `claim`, and for a review at `prepare_review` (the checkout receipt). Closure first: neither can happen, through the worker route or by calling the operation directly. Claim first: the run or review it began may finish and be recorded as above.

`claim(principal, message_id)` commits a **grant** bound to the task and to the inputs the command was issued on, and returns it. Claiming again returns the same grant. 3b's worker will have to hold a grant before it invokes anything.

Tests, through the real review API: pause, then `prepare_review` with a valid capability is refused; `prepare_review`, then cancel, then `submit_review` is stored stale and changes nothing; a green held at a barrier across a cancel is recorded as superseded and opens no cycle; the same across a pause opens the cycle with its commands held.

## 4. Delivery that survives a refusal (S3-4.4)

A command row records its **purpose**: its kind, its behaviour, and what it was issued on (the accepted red for a build request; the review task; the closed cycle for a remediation; the batch for an integration request). Each kind has a predicate, "is this still wanted?":

| Command | Still wanted while |
|---|---|
| Build after an accepted red | the behaviour is `implementing` and that red is still the one on record |
| Review | the task has no effective submission and its cycle is open |
| Remediation | the behaviour is `implementing` and that cycle is its latest |
| Integration request | the batch it names has no successor and the cycle it names is the behaviour's live, satisfied one |

- **Claim** checks the predicate, so a duplicate build command delivered after the green is refused: its purpose is done.
- A command has a delivery state of its own, apart from "published": `held`, `discarded` (with why), or deliverable.
- **Re-announcement** runs on resume and on start: every deliverable command that is still wanted and has no grant is published again under the same message id. A refused claim therefore loses nothing, even with Redis emptied.
- **Reassessment is per behaviour.** Each behaviour's held commands are judged on their own; one behaviour's state never releases or strands another's. (Step 2's per-behaviour blockers will use this.)
- Cancel discards every undelivered or ungranted command of the feature.

Tests: publish a build command, pause, refuse its claim, empty Redis, resume, restart: the same message id is delivered again and granted once. A late duplicate build command after the green is refused.

## 5. A held or interrupted batch becomes runnable again (S3-4.5)

- A batch belongs to a **logical request** (the caller's request id) and has a generation within it. A repeated request is answered from the latest generation.
- When a batch is `held` (pause) and the feature is resumed, or when a batch is found still `running` on start (the process that owned it is gone; with one control plane this is certain, and slice 5's leases will make it so with several), it is closed as `interrupted` and one `integration.requested` command is issued, naming that batch and its review cycle.
- Its handler starts the next generation of the same logical request **only if** the behaviour's live, satisfied cycle is the one named and the named batch has no successor. Otherwise it records a stale outcome and starts nothing. So a delayed command for cycle A cannot start checks on cycle B.
- In this slice the handler is a scripted one-command consumer; 3b gives it a home.

Tests: pause while the last check is held; resume; the handler runs; trunk moves once. The same with the red reopened in between: stale, nothing runs. Kill the handler after the new batch is created: on restart the batch is found, a new request is issued, and the outcome is one settled intent. A repeat of the original request id during all of this never starts a second set of checks.

## Step 1 exit test

A feature paused with a red run, a review and the last regression check each in flight; everything observed is recorded; nothing new starts, no command is delivered and trunk does not move. Restart with Redis empty. Resume: the held work is delivered again exactly once and the story reaches its gate. Then the same starting point with cancel in place of resume: the slot is freed for a waiting feature, late results are kept as audit only, and an intent authorised before the cancel is still reconciled.

End with: DISPOSITION: ACCEPT | ACCEPT_WITH_RESERVATIONS | CHALLENGE | REQUEST_INFORMATION (for slice 3a-ii step 1 design v2).
